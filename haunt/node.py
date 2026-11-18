"""The node agent for Linux machines (a Pi with speakers or a screen) and for
simulated nodes. It speaks the same protocol as the ESP32 firmware:

  - status: retained, heartbeat every 5 s, and the MQTT will says offline;
  - out/audio-<layer>: the same as out/audio with that layer (so each layer is
        remembered on its own, and comes back if this node restarts)
  - out/audio: {file, layer?, loop?, volume?, ramp?, stop?}
        with a layer ("loop", "bed", ...): replaces what that layer plays;
        without: a one-shot that overlaps everything else.
        {stop: true, layer: x} stops a layer; {stop: true} stops all audio.
        ramp {start, step, every}: a loop that starts quiet and grows.
  - out/video: {file, loop?, cut?, volume?, duck?}
        loop: the background, played whenever no clip is;
        a clip plays once over it, then the background resumes. A clip that
        arrives while another plays is dropped, unless cut: true.
        Reports in/video {event: started|ended|dropped, value: file}.
        duck: while the clip plays, the audio layers drop to this fraction.
  - out/text: {text, seconds}  text over the video ("" clears it)
  - out/overlay: {id, ass, z?}  ASS events drawn over the video on a 1920x1080 canvas
        (graphics: bands, bars, cards); "" clears that overlay
  - anything else: remembered and logged (a simulated node).

Media comes from the hub (/media/...), downloaded once and cached, or straight
from --media-dir when the hub's media folder is on this machine.

Playback is mpv over its JSON IPC (one process per audio layer, one for video).
Without mpv, one-shot audio falls back to afplay (macOS) and video is logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import __version__, topics
from .bus import Bus, MqttBus

log = logging.getLogger("haunt.node")

CACHE = Path.home() / ".cache" / "haunt" / "media"


class Mpv:
    """One mpv process, driven over its JSON IPC socket."""

    def __init__(self, name: str, args: list[str], exe: str = "mpv",
                 on_event: Callable[[dict], None] | None = None) -> None:
        self.name = name
        self.args = args
        self.exe = exe
        self.on_event = on_event
        self.sock = os.path.join(tempfile.gettempdir(), f"haunt-mpv-{os.getpid()}-{name}.sock")
        self.proc: asyncio.subprocess.Process | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def ensure(self) -> None:
        async with self._lock:
            if self.writer and not self.writer.is_closing() and (self.proc is None or self.proc.returncode is None):
                return
            if self.exe:
                if os.path.exists(self.sock):
                    os.unlink(self.sock)
                self.proc = await asyncio.create_subprocess_exec(
                    self.exe, "--idle=yes", "--no-terminal", f"--input-ipc-server={self.sock}", *self.args,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            for _ in range(50):
                try:
                    reader, self.writer = await asyncio.open_unix_connection(self.sock)
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    await asyncio.sleep(0.1)
            else:
                raise RuntimeError(f"mpv {self.name}: IPC socket never came up")
            self._reader_task = asyncio.create_task(self._read(reader))

    async def _read(self, reader: asyncio.StreamReader) -> None:
        while line := await reader.readline():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "event" in msg and self.on_event:
                self.on_event(msg)

    async def command(self, *args: Any) -> None:
        await self.ensure()
        self.writer.write(json.dumps({"command": list(args)}).encode() + b"\n")
        await self.writer.drain()

    async def close(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self.writer:
            self.writer.close()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()


class Video:
    """A background loop with clips over it, on one mpv window."""

    def __init__(self, mpv_exe: str | None, report: Callable[[str, str], None],
                 extra_args: list[str] | None = None) -> None:
        self.report = report
        self.loop_file: Path | None = None
        self.clip: Path | None = None
        args = ["--fs", "--osd-font-size=64", "--osd-border-size=3", "--keep-open=no",
                "--volume-max=200", *(extra_args or [])]
        self.mpv = Mpv("video", args, mpv_exe or "", self._on_event) if mpv_exe else None

    def _on_event(self, msg: dict) -> None:
        if msg.get("event") == "end-file" and msg.get("reason") in ("eof", "error") and self.clip:
            ended, self.clip = self.clip, None
            self.report("ended", ended.name)
            asyncio.get_running_loop().create_task(self._resume_loop())

    async def _resume_loop(self) -> None:
        if self.loop_file and self.mpv and not self.clip:
            await self.mpv.command("loadfile", str(self.loop_file), "replace")
            await self.mpv.command("set_property", "loop-file", "inf")

    async def set_loop(self, path: Path) -> None:
        self.loop_file = path
        if not self.clip:
            await self._resume_loop()
        if not self.mpv:
            log.info("(no mpv) video loop %s", path.name)

    async def play_clip(self, path: Path, cut: bool, volume: float | None) -> None:
        if self.clip and not cut:
            self.report("dropped", path.name)
            return
        self.clip = path
        self.report("started", path.name)
        if not self.mpv:
            log.info("(no mpv) video clip %s", path.name)
            return
        await self.mpv.command("set_property", "loop-file", "no")
        await self.mpv.command("set_property", "volume", 100 * (volume if volume is not None else 1))
        await self.mpv.command("loadfile", str(path), "replace")

    async def stop(self) -> None:
        self.clip = None
        self.loop_file = None
        if self.mpv:
            await self.mpv.command("stop")

    async def overlay(self, oid: int, ass: str, z: int = 0) -> None:
        if not self.mpv:
            return
        if ass:
            await self.mpv.command("osd-overlay", oid, "ass-events", ass, 1920, 1080, z)
        else:
            await self.mpv.command("osd-overlay", oid, "none", "")

    async def text(self, text: str, seconds: float) -> None:
        if self.mpv:
            await self.mpv.command("show-text", text.replace("\\N", "\n"), int(seconds * 1000))
        else:
            log.info("(no mpv) text %r for %ss", text, seconds)


class Audio:
    """Named looping layers plus overlapping one-shots."""

    def __init__(self, mpv_exe: str | None) -> None:
        self.mpv_exe = mpv_exe
        self.afplay = shutil.which("afplay")
        self.layers: dict[str, Mpv] = {}
        self.ramps: dict[str, asyncio.Task] = {}
        self.level: dict[str, float] = {}   # each layer's own volume, before ducking
        self.duck = 1.0
        self.shots: list[subprocess.Popen] = []

    @property
    def available(self) -> bool:
        return bool(self.mpv_exe or self.afplay)

    async def layer(self, name: str, path: Path, volume: float = 1.0, ramp: dict | None = None) -> None:
        if name in self.ramps:
            self.ramps.pop(name).cancel()
        if not self.mpv_exe:
            log.info("(no mpv) layer %s: %s", name, path.name)
            return
        mpv = self.layers.get(name)
        if not mpv:
            mpv = self.layers[name] = Mpv(f"audio-{name}", ["--no-video", "--volume-max=200"], self.mpv_exe)
        start = ramp.get("start", volume) if ramp else volume
        self.level[name] = start
        await mpv.command("set_property", "volume", 100 * start * self.duck)
        await mpv.command("loadfile", str(path), "replace")
        await mpv.command("set_property", "loop-file", "inf")
        if ramp:
            self.ramps[name] = asyncio.create_task(self._ramp(name, mpv, start, volume, ramp))

    async def _ramp(self, name: str, mpv: Mpv, level: float, full: float, ramp: dict) -> None:
        while level < full:
            await asyncio.sleep(float(ramp.get("every", 10)))
            level = min(full, level + float(ramp.get("step", 0.1)))
            self.level[name] = level
            await mpv.command("set_property", "volume", 100 * level * self.duck)

    async def set_duck(self, k: float) -> None:
        """Drop every layer to k of its own level (1: back up), e.g. while someone speaks."""
        if k == self.duck:
            return
        self.duck = k
        for name, mpv in self.layers.items():
            await mpv.command("set_property", "volume", 100 * self.level.get(name, 1.0) * k)

    async def stop_layer(self, name: str) -> None:
        if name in self.ramps:
            self.ramps.pop(name).cancel()
        if name in self.layers:
            await self.layers[name].command("stop")

    def once(self, path: Path, volume: float = 1.0) -> None:
        self.shots = [p for p in self.shots if p.poll() is None]
        if self.mpv_exe:
            cmd = [self.mpv_exe, "--no-video", "--no-terminal", "--volume-max=200",
                   f"--volume={int(volume * 100)}", str(path)]
        elif self.afplay:
            cmd = [self.afplay, "-v", str(min(volume, 1.0)), str(path)]
        else:
            log.info("(no player) would play %s", path.name)
            return
        self.shots.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    async def stop_all(self) -> None:
        for name in list(self.layers):
            await self.stop_layer(name)
        for p in self.shots:
            if p.poll() is None:
                p.terminate()
        self.shots.clear()


class NodeAgent:
    def __init__(self, name: str, bus: Bus, kind: str = "pi", media_url: str | None = None,
                 audio: bool = True, video: bool = False, media_dir: Path | None = None,
                 mpv: str | None = "auto") -> None:
        self.name = name
        self.bus = bus
        self.kind = kind
        self.media_url = media_url
        self.media_dir = media_dir
        exe = shutil.which("mpv") if mpv == "auto" else mpv
        self.audio = Audio(exe) if audio else None
        self.video = Video(exe, self._report_video) if video else None
        self.started = time.time()
        self.outputs: dict[str, Any] = {}
        self._clip_duck: dict[str, float] = {}

    def status(self, online: bool = True) -> dict:
        caps = []
        if self.audio and self.audio.available:
            caps.append("audio")
        if self.video and self.video.mpv:
            caps.append("video")
        return {"online": online, "id": self.name, "kind": self.kind, "fw": f"haunt-py {__version__}",
                "ip": _ip(), "uptime": int(time.time() - self.started), "caps": caps,
                "host": platform.node()}

    async def run(self) -> None:
        self.bus.subscribe(f"{topics.ROOT}/n/{self.name}/out/+", self._on_out)
        runner = asyncio.create_task(self.bus.run())
        try:
            while True:
                await self.bus.connected.wait()
                await self.bus.publish(topics.status(self.name), self.status(), retain=True)
                await asyncio.sleep(5)
        finally:
            runner.cancel()

    def _report_video(self, event: str, file: str) -> None:
        loop = asyncio.get_running_loop()
        loop.create_task(self.bus.publish(topics.node_in(self.name, "video"), {"event": event, "value": file}))
        if self.audio and event in ("started", "ended"):
            k = self._clip_duck.get(file, 1.0) if event == "started" else 1.0
            loop.create_task(self.audio.set_duck(k))

    async def _on_out(self, topic: str, payload: Any) -> None:
        output = topic.rsplit("/", 1)[1]
        self.outputs[output] = payload
        p = payload if isinstance(payload, dict) else {}
        try:
            if output == "audio" and self.audio:
                await self._audio(p)
            elif output.startswith("audio-") and self.audio:
                await self._audio({**p, "layer": output[len("audio-"):]})
            elif output == "video" and self.video:
                await self._video(p)
            elif output == "overlay" and self.video:
                await self.video.overlay(int(p.get("id", 1)), str(p.get("ass", "")), int(p.get("z", 0)))
            elif output == "text" and self.video:
                await self.video.text(str(p.get("text", "")), float(p.get("seconds", 4)))
            else:
                log.debug("%s %s %s", self.name, output, payload)
        except Exception:
            log.exception("%s: %s %s failed", self.name, output, payload)

    async def _audio(self, p: dict) -> None:
        layer = p.get("layer")
        if p.get("stop"):
            await (self.audio.stop_layer(layer) if layer else self.audio.stop_all())
            return
        path = await self.media(p["file"], p.get("v"))
        if not path:
            return
        volume = float(p.get("volume", 1.0))
        if layer:
            await self.audio.layer(layer, path, volume, p.get("ramp"))
        else:
            self.audio.once(path, volume)

    async def _video(self, p: dict) -> None:
        if p.get("stop"):
            await self.video.stop()
            return
        path = await self.media(p["file"], p.get("v"))
        if not path:
            return
        if p.get("loop"):
            await self.video.set_loop(path)
        else:
            self._clip_duck[path.name] = float(p.get("duck", 1.0))
            await self.video.play_clip(path, bool(p.get("cut")), p.get("volume"))

    async def media(self, file: str, version: Any) -> Path | None:
        if self.media_dir and (self.media_dir / file).is_file():
            return self.media_dir / file
        return await asyncio.to_thread(self._fetch, file, version)

    def _fetch(self, file: str, version: Any) -> Path | None:
        target = CACHE / f"{version or 0}-{file.replace('/', '__')}"
        if target.exists():
            return target
        if not self.media_url:
            log.warning("no media URL; cannot fetch %s", file)
            return None
        CACHE.mkdir(parents=True, exist_ok=True)
        url = f"{self.media_url.rstrip('/')}/{urllib.parse.quote(file)}"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                tmp = target.with_suffix(target.suffix + ".part")
                tmp.write_bytes(r.read())
                tmp.replace(target)
            return target
        except Exception as e:
            log.warning("fetch %s failed: %s", url, e)
            return None


def make_mqtt_agent(name: str, host: str, port: int, kind: str = "pi", **kw: Any) -> NodeAgent:
    offline = {"online": False, "id": name, "kind": kind}
    bus = MqttBus(host, port, client_id=f"haunt-node-{name}", will=(topics.status(name), offline))
    return NodeAgent(name, bus, kind, **kw)


def _ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"

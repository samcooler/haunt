"""The node agent for Linux machines (a Pi with speakers or a screen) and for
simulated nodes. It speaks the same protocol as the ESP32 firmware:

  - status: retained, heartbeat every 5 s, and the MQTT will says offline;
  - out/audio: play a file from the hub's media (downloaded once, cached);
  - out/leds, out/<anything>: a simulated node just remembers and logs it.

Inputs from a simulated node come from the console (it publishes in/<input> as if
the node had), so there is nothing to simulate here beyond presence.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__, topics
from .bus import Bus, MqttBus

log = logging.getLogger("haunt.node")

CACHE = Path.home() / ".cache" / "haunt" / "media"


class Player:
    """Plays audio with whatever this machine has: mpv (Pi), afplay (macOS)."""

    def __init__(self) -> None:
        self.mpv = shutil.which("mpv")
        self.afplay = shutil.which("afplay")
        self.procs: list[subprocess.Popen] = []

    @property
    def available(self) -> bool:
        return bool(self.mpv or self.afplay)

    def play(self, path: Path, volume: float | None = None, loop: bool = False) -> None:
        self.procs = [p for p in self.procs if p.poll() is None]
        if self.mpv:
            cmd = [self.mpv, "--no-video", "--really-quiet", str(path)]
            if volume is not None:
                cmd.insert(1, f"--volume={int(volume * 100)}")
            if loop:
                cmd.insert(1, "--loop=inf")
        elif self.afplay:
            cmd = [self.afplay, str(path)]
            if volume is not None:
                cmd[1:1] = ["-v", str(volume)]
        else:
            log.info("(no audio player) would play %s", path.name)
            return
        self.procs.append(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    def stop(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        self.procs.clear()


class NodeAgent:
    def __init__(self, name: str, bus: Bus, kind: str = "pi", media_url: str | None = None,
                 audio: bool = True) -> None:
        self.name = name
        self.bus = bus
        self.kind = kind
        self.media_url = media_url
        self.player = Player() if audio else None
        self.started = time.time()
        self.outputs: dict[str, Any] = {}

    def status(self, online: bool = True) -> dict:
        caps = ["audio"] if self.player and self.player.available else []
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

    async def _on_out(self, topic: str, payload: Any) -> None:
        output = topic.rsplit("/", 1)[1]
        self.outputs[output] = payload
        if output == "audio" and self.player and isinstance(payload, dict):
            if payload.get("stop"):
                self.player.stop()
                return
            path = await asyncio.to_thread(self._fetch, payload["file"], payload.get("v"))
            if path:
                self.player.play(path, payload.get("volume"), bool(payload.get("loop")))
        else:
            log.debug("%s %s %s", self.name, output, payload)

    def _fetch(self, file: str, version: Any) -> Path | None:
        target = CACHE / f"{version or 0}-{file}"
        if target.exists():
            return target
        if not self.media_url:
            log.warning("no media URL; cannot fetch %s", file)
            return None
        CACHE.mkdir(parents=True, exist_ok=True)
        url = f"{self.media_url.rstrip('/')}/{urllib.request.quote(file)}"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                tmp = target.with_suffix(target.suffix + ".part")
                tmp.write_bytes(r.read())
                tmp.replace(target)
            return target
        except Exception as e:
            log.warning("fetch %s failed: %s", url, e)
            return None


def make_mqtt_agent(name: str, host: str, port: int, kind: str = "pi", media_url: str | None = None,
                    audio: bool = True) -> NodeAgent:
    offline = {"online": False, "id": name, "kind": kind}
    bus = MqttBus(host, port, client_id=f"haunt-node-{name}", will=(topics.status(name), offline))
    return NodeAgent(name, bus, kind, media_url, audio)


def _ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"

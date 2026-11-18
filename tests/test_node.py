"""The Pi node's video and audio logic, against a fake mpv IPC server."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

from haunt import topics
from haunt.bus import MemoryBus
from haunt.node import Mpv, NodeAgent


class FakeMpv:
    """Listens where Mpv will connect, records commands, can send end-file."""

    def __init__(self, sock: str) -> None:
        self.sock = sock
        self.commands: list[list] = []
        self.writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        if os.path.exists(self.sock):
            os.unlink(self.sock)
        self.server = await asyncio.start_unix_server(self._client, path=self.sock)

    async def _client(self, reader, writer) -> None:
        self.writers.append(writer)
        while line := await reader.readline():
            self.commands.append(json.loads(line)["command"])

    async def eof(self) -> None:
        for w in self.writers:
            w.write(b'{"event":"end-file","reason":"eof"}\n')
            await w.drain()

    def loaded(self) -> list[str]:
        return [Path(c[1]).name for c in self.commands if c[0] == "loadfile"]


async def make_agent(tmp_path: Path):
    media = tmp_path / "media"
    (media / "video_loops").mkdir(parents=True)
    (media / "video_once").mkdir()
    for f in ("video_loops/idle.mp4", "video_once/a.mp4", "video_once/b.mp4"):
        (media / f).write_bytes(b"x")
    bus = MemoryBus()
    agent = NodeAgent("overlord", bus, audio=False, video=True, media_dir=media, mpv="mpv")
    # Point the agent's mpv at the fake: no process, just the socket.
    agent.video.mpv.exe = ""
    fake = FakeMpv(agent.video.mpv.sock)
    await fake.start()
    bus.subscribe(f"{topics.ROOT}/n/overlord/out/+", agent._on_out)
    return agent, bus, fake


def video_events(bus):
    return [p["event"] + ":" + p["value"] for t, p in bus.history if t == topics.node_in("overlord", "video")]


async def test_clip_over_loop_then_back(tmp_path):
    agent, bus, fake = await make_agent(tmp_path)
    await bus.publish(topics.node_out("overlord", "video"), {"file": "video_loops/idle.mp4", "loop": True})
    await bus.publish(topics.node_out("overlord", "video"), {"file": "video_once/a.mp4"})
    await asyncio.sleep(0.05)
    assert fake.loaded() == ["idle.mp4", "a.mp4"]
    # A second clip while the first plays is dropped; one that cuts plays.
    await bus.publish(topics.node_out("overlord", "video"), {"file": "video_once/b.mp4"})
    await asyncio.sleep(0.05)
    assert fake.loaded() == ["idle.mp4", "a.mp4"]
    await bus.publish(topics.node_out("overlord", "video"), {"file": "video_once/b.mp4", "cut": True})
    await asyncio.sleep(0.05)
    assert fake.loaded()[-1] == "b.mp4"
    # The clip ends: the loop comes back.
    await fake.eof()
    await asyncio.sleep(0.05)
    assert fake.loaded()[-1] == "idle.mp4"
    assert video_events(bus) == ["started:a.mp4", "dropped:b.mp4", "started:b.mp4", "ended:b.mp4"]
    await agent.video.mpv.close()
    fake.server.close()


async def test_text_and_missing_media(tmp_path):
    agent, bus, fake = await make_agent(tmp_path)
    await bus.publish(topics.node_out("overlord", "text"), {"text": "PUMP\\NTHE LEVERS", "seconds": 4})
    await bus.publish(topics.node_out("overlord", "video"), {"file": "video_once/missing.mp4"})
    await asyncio.sleep(0.05)
    assert ["show-text", "PUMP\nTHE LEVERS", 4000] in fake.commands
    assert fake.loaded() == []
    await agent.video.mpv.close()
    fake.server.close()


async def test_audio_ducks_under_a_clip(tmp_path):
    agent, bus, fake = await make_agent(tmp_path)
    from haunt.node import Audio
    agent.audio = Audio("mpv")
    calls = []

    async def set_duck(k):
        calls.append(k)
    agent.audio.set_duck = set_duck
    await bus.publish(topics.node_out("overlord", "video"), {"file": "video_once/a.mp4", "duck": 0.7})
    await asyncio.sleep(0.05)
    await fake.eof()
    await asyncio.sleep(0.05)
    assert calls == [0.7, 1.0]
    await agent.video.mpv.close()
    fake.server.close()

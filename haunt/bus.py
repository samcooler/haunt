"""The bus: publish JSON, subscribe with MQTT wildcards.

MqttBus talks to a real broker and reconnects forever. MemoryBus is the same
interface in-process, for tests and for running a piece with no broker at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from . import topics

log = logging.getLogger("haunt.bus")

Handler = Callable[[str, Any], Awaitable[None] | None]


def encode(payload: Any) -> bytes:
    if payload is None:
        return b""
    return json.dumps(payload, separators=(",", ":")).encode()


def decode(raw: bytes | str) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    if raw == "":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


class Bus:
    def __init__(self) -> None:
        self._subs: list[tuple[str, Handler]] = []
        self.connected = asyncio.Event()

    def subscribe(self, pattern: str, handler: Handler) -> None:
        self._subs.append((pattern, handler))

    async def _dispatch(self, topic: str, payload: Any) -> None:
        for pattern, handler in list(self._subs):
            if topics.matches(pattern, topic):
                try:
                    r = handler(topic, payload)
                    if asyncio.iscoroutine(r):
                        await r
                except Exception:
                    log.exception("handler for %s failed on %s", pattern, topic)

    async def publish(self, topic: str, payload: Any = None, retain: bool = False) -> None:
        raise NotImplementedError

    async def run(self) -> None:
        raise NotImplementedError


class MemoryBus(Bus):
    """In-process broker: keeps retained messages and delivers them to late subscribers."""

    def __init__(self) -> None:
        super().__init__()
        self.retained: dict[str, Any] = {}
        self.history: list[tuple[str, Any]] = []
        self.connected.set()

    def subscribe(self, pattern: str, handler: Handler) -> None:
        super().subscribe(pattern, handler)
        for topic, payload in list(self.retained.items()):
            if topics.matches(pattern, topic):
                r = handler(topic, payload)
                if asyncio.iscoroutine(r):
                    asyncio.get_running_loop().create_task(r)

    async def publish(self, topic: str, payload: Any = None, retain: bool = False) -> None:
        # Round-trip through JSON so tests see exactly what a broker would carry.
        payload = decode(encode(payload))
        if retain:
            if payload is None:
                self.retained.pop(topic, None)
            else:
                self.retained[topic] = payload
        self.history.append((topic, payload))
        await self._dispatch(topic, payload)

    async def run(self) -> None:
        await asyncio.Event().wait()


class MqttBus(Bus):
    def __init__(self, host: str = "localhost", port: int = 1883, client_id: str | None = None,
                 will: tuple[str, Any] | None = None) -> None:
        super().__init__()
        self.host, self.port, self.client_id, self.will = host, port, client_id, will
        self._client = None
        self._pending: list[tuple[str, bytes, bool]] = []

    async def publish(self, topic: str, payload: Any = None, retain: bool = False) -> None:
        data = encode(payload)
        if self._client is None:
            # Not connected yet: deliver once we are, so startup order does not matter.
            self._pending.append((topic, data, retain))
            return
        try:
            await self._client.publish(topic, data, qos=1 if retain else 0, retain=retain)
        except Exception as e:  # dropped connection; run() reconnects
            log.warning("publish %s failed: %s", topic, e)
            self._pending.append((topic, data, retain))

    async def run(self) -> None:
        import aiomqtt

        will = None
        if self.will:
            will = aiomqtt.Will(self.will[0], encode(self.will[1]), qos=1, retain=True)
        delay = 1.0
        while True:
            try:
                async with aiomqtt.Client(self.host, self.port, identifier=self.client_id,
                                          will=will, keepalive=10) as client:
                    self._client = client
                    delay = 1.0
                    await client.subscribe(f"{topics.ROOT}/#")
                    self.connected.set()
                    log.info("bus connected to %s:%s", self.host, self.port)
                    pending, self._pending = self._pending, []
                    for topic, data, retain in pending:
                        await client.publish(topic, data, qos=1 if retain else 0, retain=retain)
                    async for message in client.messages:
                        await self._dispatch(str(message.topic), decode(message.payload))
            except aiomqtt.MqttError as e:
                self._client = None
                self.connected.clear()
                log.warning("bus lost (%s); retrying in %.0fs", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10)

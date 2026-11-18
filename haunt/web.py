"""The hub's web server: the operator panel, the artist console, and an HTTP API.

The browser is just another bus client: /ws streams every bus message and accepts
commands. The HTTP API is for curl, the CLI and Claude:

  GET  /api/snapshot           everything: show state, params, vars, nodes, recent log
  POST /api/cmd/<verb>         JSON body, same as the haunt/cmd/<verb> topic
  POST /api/in/<node>/<input>  inject an input as if a node sent it (simulation)
  GET  /media/<file>           the piece's media, for nodes to download
  GET  /fw/<file>              firmware images, for ESP32 OTA
  GET  /display/<name>         a full-screen page that is the node <name> (out/display slides)
  POST /api/ota/<node>?name=f  body = firmware image; stores it and tells the node to fetch it
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from . import topics
from .bus import Bus

WEB = Path(__file__).parent / "web"


class Hub:
    """Keeps the latest message on every topic, plus a ring of recent log lines."""

    def __init__(self, bus: Bus, media: Path, firmware: Path) -> None:
        self.bus = bus
        self.media = media
        self.firmware = firmware
        self.latest: dict[str, Any] = {}
        self.log: deque = deque(maxlen=300)
        self.clients: set[web.WebSocketResponse] = set()
        self.port = 8080
        self.displays: dict[str, int] = {}   # browser display nodes: name -> open pages
        bus.subscribe(f"{topics.ROOT}/#", self._on)

    async def _on(self, topic: str, payload: Any) -> None:
        if topics.noisy(topic):
            return
        if topic == topics.SHOW_LOG:
            self.log.append(payload)
        elif payload is None:
            self.latest.pop(topic, None)
        else:
            self.latest[topic] = payload
        if self.clients:
            msg = json.dumps({"topic": topic, "p": payload, "t": time.time()})
            for ws in list(self.clients):
                try:
                    await ws.send_str(msg)
                except (ConnectionResetError, RuntimeError):
                    self.clients.discard(ws)

    def snapshot(self) -> dict:
        nodes: dict[str, dict] = {}
        for topic, p in self.latest.items():
            parts = topics.split_node_topic(topic)
            if not parts:
                continue
            name, kind, thing = parts
            n = nodes.setdefault(name, {"status": None, "in": {}, "out": {}})
            if kind == "status":
                n["status"] = p
            else:
                n[kind][thing] = p
        return {
            "show": self.latest.get(topics.SHOW_STATE),
            "params": self.latest.get(topics.SHOW_PARAMS),
            "vars": self.latest.get(topics.SHOW_VARS),
            "nodes": nodes,
            "log": list(self.log)[-50:],
        }

    # -- handlers -------------------------------------------------------------
    async def ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        node = request.query.get("node")   # a /display/<name> page: this browser is that node
        if node:
            self.displays[node] = self.displays.get(node, 0) + 1
            await self.bus.publish(topics.status(node), {
                "online": True, "id": node, "kind": "browser", "fw": "haunt display",
                "ip": request.remote, "caps": ["display"]}, retain=True)
        for topic, p in list(self.latest.items()):
            await ws.send_str(json.dumps({"topic": topic, "p": p}))
        for p in list(self.log):
            await ws.send_str(json.dumps({"topic": topics.SHOW_LOG, "p": p, "replay": True}))
        self.clients.add(ws)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    m = json.loads(msg.data)
                    await self.send(m["topic"], m.get("p"))
                except (ValueError, KeyError, PermissionError):
                    pass
        finally:
            self.clients.discard(ws)
            if node:
                self.displays[node] -= 1
                if not self.displays[node]:
                    await self.bus.publish(topics.status(node), {"online": False, "id": node,
                                                                 "kind": "browser"}, retain=True)
        return ws

    async def send(self, topic: str, payload: Any) -> None:
        # Browsers may command the show and simulate inputs; nothing else.
        if not (topic.startswith(f"{topics.ROOT}/cmd/") or
                topics.matches(f"{topics.ROOT}/n/+/in/+", topic)):
            raise PermissionError(topic)
        await self.bus.publish(topic, payload)

    async def api_snapshot(self, request: web.Request) -> web.Response:
        return web.json_response(self.snapshot())

    async def api_cmd(self, request: web.Request) -> web.Response:
        body = await _json(request)
        await self.bus.publish(topics.cmd(request.match_info["verb"]), body)
        await asyncio.sleep(0.15)  # let the engine act, so the reply shows the result
        return web.json_response({"ok": True, "show": self.latest.get(topics.SHOW_STATE, {}).get("state")})

    async def api_in(self, request: web.Request) -> web.Response:
        body = await _json(request)
        body.setdefault("event", "press")
        body.setdefault("source", "sim")
        await self.bus.publish(topics.node_in(request.match_info["node"], request.match_info["input"]), body)
        await asyncio.sleep(0.15)
        return web.json_response({"ok": True, "show": self.latest.get(topics.SHOW_STATE, {}).get("state")})

    async def api_ota(self, request: web.Request) -> web.Response:
        from .node import _ip

        node = request.match_info["node"]
        name = Path(request.query.get("name", "firmware.bin")).name
        data = await request.read()
        if len(data) < 1024:
            raise web.HTTPBadRequest(text="that is not a firmware image")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        file = f"{node}-{stamp}-{name}"
        (self.firmware / file).write_bytes(data)
        url = f"http://{_ip()}:{self.port}/fw/{file}"
        await self.bus.publish(topics.node_out(node, "ota"), {"url": url, "size": len(data)})
        return web.json_response({"ok": True, "url": url})

    async def page(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    async def display(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB / "display.html", headers={"Cache-Control": "no-cache"})

    def app(self) -> web.Application:
        app = web.Application(client_max_size=8 * 1024 * 1024)
        app.router.add_get("/", self.page)
        app.router.add_get("/console", self.page)
        app.router.add_get("/display/{name}", self.display)
        app.router.add_get("/ws", self.ws)
        app.router.add_get("/api/snapshot", self.api_snapshot)
        app.router.add_post("/api/cmd/{verb}", self.api_cmd)
        app.router.add_post("/api/in/{node}/{input}", self.api_in)
        app.router.add_post("/api/ota/{node}", self.api_ota)
        app.router.add_static("/static", WEB)
        self.media.mkdir(parents=True, exist_ok=True)
        self.firmware.mkdir(parents=True, exist_ok=True)
        app.router.add_static("/media", self.media)
        app.router.add_static("/fw", self.firmware)
        return app


async def _json(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="body must be JSON")
    return body if isinstance(body, dict) else {"value": body}


async def serve(hub: Hub, port: int) -> web.AppRunner:
    hub.port = port
    runner = web.AppRunner(hub.app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner

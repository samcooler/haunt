"""haunt: run a piece, run a node, and drive a running show from the terminal.

  haunt run pieces/hello --sim     the hub: engine + web UI + recorder (+ simulated nodes)
  haunt node --name speaker        a Pi node: presence, audio
  haunt status                     what the show is doing, every node, every param
  haunt tail                       live bus traffic
  haunt set brightness 0.4         change a param live
  haunt goto awake | reset | action Hint | blackout on
  haunt press lamp.button          simulate an input (press, or `on`/`off`/a value)
  haunt push pieces/hello --watch  send a piece to the hub on every save (it hot-reloads)
  haunt ota lamp firmware.bin      push firmware to an ESP32 node
  haunt new pieces/mything         start a new piece

Client commands talk to the hub over HTTP: --hub or $HAUNT_URL (default http://localhost:8080).
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA = Path(os.environ.get("HAUNT_DATA", Path.home() / ".haunt"))


# -- hub --------------------------------------------------------------------------
def _split_broker(s: str) -> tuple[str, int]:
    host, _, port = s.partition(":")
    return host or "localhost", int(port or 1883)


def _broker_up(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _start_mosquitto(port: int) -> subprocess.Popen | None:
    exe = shutil.which("mosquitto")
    for cand in ("/opt/homebrew/opt/mosquitto/sbin/mosquitto", "/usr/local/opt/mosquitto/sbin/mosquitto",
                 "/usr/sbin/mosquitto"):
        if not exe and Path(cand).exists():
            exe = cand
    if not exe:
        return None
    conf = Path(tempfile.gettempdir()) / "haunt-mosquitto.conf"
    # Listen on every interface so ESP32s on the LAN can reach a laptop hub.
    conf.write_text(f"listener {port}\nallow_anonymous true\n")
    proc = subprocess.Popen([exe, "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if _broker_up("localhost", port):
            return proc
        time.sleep(0.1)
    proc.terminate()
    return None


async def _run(args) -> None:
    from .bus import MqttBus
    from .engine import Engine
    from .node import make_mqtt_agent
    from .piece import Piece
    from .recorder import Recorder
    from .web import Hub, serve

    piece = Piece(args.piece)
    host, port = _split_broker(args.broker)
    broker = None
    if not _broker_up(host, port):
        if host in ("localhost", "127.0.0.1"):
            broker = _start_mosquitto(port)
        if broker is None:
            sys.exit(f"no MQTT broker at {host}:{port} (install mosquitto, or pass --broker)")
        print(f"started mosquitto on :{port}")
        atexit.register(broker.terminate)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so atexit runs under systemd/kill

    data = DATA / piece.name
    bus = MqttBus(host, port, client_id=f"haunt-hub-{socket.gethostname()}")
    engine = Engine(piece, bus, data_dir=data)
    hub = Hub(bus, piece.media, DATA / "firmware")
    Recorder(bus, data / "logs")
    tasks = [asyncio.create_task(bus.run())]
    await asyncio.wait_for(bus.connected.wait(), 10)
    await serve(hub, args.port)
    tasks.append(asyncio.create_task(engine.run()))

    if args.sim is not None:
        devices = piece.load_devices()
        names = [n for n, d in devices.items() if args.sim == "all" or not d.get("id")]
        for name in names:
            kind = "sim"
            agent = make_mqtt_agent(name, host, port, kind=kind,
                                    media_url=f"http://localhost:{args.port}/media",
                                    audio=bool(devices[name].get("audio")) and not args.mute)
            tasks.append(asyncio.create_task(agent.run()))
        print(f"simulating: {', '.join(names) or 'nothing (every node has an id; use --sim all)'}")

    ip = _lan_ip()
    print(f"haunt: {piece.name}\n  panel    http://{ip}:{args.port}/\n  console  http://{ip}:{args.port}/console"
          f"\n  broker   {host}:{port}\n  logs     {data / 'logs'}")
    try:
        await asyncio.gather(*tasks)
    finally:
        if broker:
            broker.terminate()


async def _node(args) -> None:
    from .node import make_mqtt_agent

    host, port = _split_broker(args.broker)
    name = args.name or socket.gethostname().split(".")[0]
    media = args.media or f"http://{host}:{args.http_port}/media"
    agent = make_mqtt_agent(name, host, port, kind="pi", media_url=media)
    print(f"haunt node {name} -> {host}:{port}")
    await agent.run()


def _lan_ip() -> str:
    from .node import _ip
    return _ip()


# -- clients ----------------------------------------------------------------------
def _hub(args) -> str:
    return (args.hub or os.environ.get("HAUNT_URL") or "http://localhost:8080").rstrip("/")


def _http(args, path: str, body: dict | None = None, data: bytes | None = None) -> dict:
    url = _hub(args) + path
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        hint = ""
        if "No route to host" in str(e) or "Errno 65" in str(e):
            hint = ("\n  macOS Local Network privacy may be blocking this Python; try /usr/bin/python3, "
                    "or allow your terminal under Privacy & Security > Local Network")
        sys.exit(f"cannot reach the hub at {url}: {e}{hint}")


def _value(s: str):
    try:
        return json.loads(s)
    except ValueError:
        return s


def _status(args) -> None:
    snap = _http(args, "/api/snapshot")
    if args.json:
        print(json.dumps(snap, indent=1))
        return
    show = snap.get("show") or {}
    since = time.time() - show.get("since", time.time())
    print(f"{show.get('piece')}: {show.get('state')} for {since:.0f}s"
          + ("  [BLACKOUT]" if show.get("blackout") else ""))
    if show.get("error"):
        print(f"  ERROR: {show['error']}")
    print("nodes:")
    devices = show.get("devices") or {}
    for name in sorted(set(devices) | set(snap["nodes"])):
        st = (snap["nodes"].get(name) or {}).get("status") or {}
        mark = "up  " if st.get("online") else "DOWN"
        extra = " ".join(f"{k}={st[k]}" for k in ("kind", "fw", "ip", "rssi") if st.get(k) is not None)
        note = "" if name in devices else "  (not in devices.yaml)"
        print(f"  {mark} {name:14} {extra}{note}")
    print("params:")
    for name, p in (snap.get("params") or {}).items():
        print(f"  {name:16} {p['value']!s:10}" + (f" (default {p['default']})" if p.get("modified") else ""))
    if snap.get("vars"):
        print("vars:", json.dumps(snap["vars"]))
    print("recent:")
    for line in snap["log"][-args.lines:]:
        print(f"  {time.strftime('%H:%M:%S', time.localtime(line['t']))} {line['level']:5} {line['msg']}")


def _cmd(args, verb: str, body: dict) -> None:
    r = _http(args, f"/api/cmd/{verb}", body)
    print(f"ok; show is in {r.get('show')}")


def _press(args) -> None:
    node, _, input_ = args.input.partition(".")
    body = {"event": args.event}
    if args.value is not None:
        body["value"] = _value(args.value)
    r = _http(args, f"/api/in/{node}/{input_}", body)
    print(f"ok; show is in {r.get('show')}")


async def _tail(args) -> None:
    import aiohttp

    url = _hub(args).replace("http", "ws", 1) + "/ws"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url) as ws:
            async for msg in ws:
                m = json.loads(msg.data)
                if m.get("replay") or (not args.all and "t" not in m):
                    continue  # skip the snapshot sent on connect unless --all
                t = time.strftime("%H:%M:%S", time.localtime(m.get("t", time.time())))
                topic = m["topic"].removeprefix("haunt/")
                if args.filter and args.filter not in topic:
                    continue
                p = m["p"]
                if topic == "show/log":
                    line = f"log   {p['level']:5} {p['msg']}"
                elif topic == "show/state":
                    line = f"state {p.get('state')}" + (f"  ERROR {p['error']}" if p.get("error") else "")
                elif topic == "show/params" and not args.all:
                    continue
                elif topic.endswith("/status") and p and not args.all:
                    line = f"{topic} {'online' if p.get('online') else 'OFFLINE'}"
                else:
                    line = f"{topic} {json.dumps(p)}"
                print(f"{t} {line}", flush=True)


def _push(args) -> None:
    """rsync a piece folder to the hub; the hub hot-reloads it. --watch repeats on every save."""
    src = Path(args.piece).resolve()
    if not (src / "show.py").exists():
        sys.exit(f"{src} is not a piece (no show.py)")
    dest = f"{args.to}:{args.path.rstrip('/')}/{src.name}/"
    cmd = ["rsync", "-az", "--delete", "--exclude", "__pycache__", "--exclude", ".*", f"{src}/", dest]

    def push() -> bool:
        r = subprocess.run(cmd)
        print(f"{time.strftime('%H:%M:%S')} pushed {src.name} -> {dest}" if r.returncode == 0
              else f"push failed (rsync exit {r.returncode})", flush=True)
        return r.returncode == 0

    if not push() or not args.watch:
        return

    def stamp() -> dict:
        return {str(f): f.stat().st_mtime for f in src.rglob("*")
                if f.is_file() and "__pycache__" not in f.parts and not f.name.startswith(".")}

    last = stamp()
    print("watching; every save is pushed (ctrl-c to stop)")
    while True:
        time.sleep(0.4)
        now = stamp()
        if now != last:
            last = now
            push()


def _ota(args) -> None:
    data = Path(args.file).read_bytes()
    r = _http(args, f"/api/ota/{args.node}?name={Path(args.file).name}", data=data)
    print(f"sent {len(data)} bytes; {args.node} will fetch {r.get('url')}")


NEW_SHOW = '''"""{name}: what this piece is, in a sentence."""

from haunt import Show

show = Show(initial="idle", idle_reset=90)


@show.on_reset
def fresh(s):
    s.vars.visits = 0


@show.state
async def idle(s):
    """Waiting for someone."""
    s.leds("thing", effect="breathe", color=lambda p: p.color)
    await s.wait("thing.button.press")
    s.vars.visits += 1
    return "active"


@show.state(timeout=20, then="idle")
async def active(s):
    """Someone is here."""
    s.leds("thing", effect="solid", color="#ffffff")
'''

NEW_DEVICES = """nodes:
  thing:
    kind: esp32
    # id: esp-xxxxxx     (the board prints its id on boot; the console lists unassigned boards)
    leds: {pin: 14, count: 30, order: GRB, max_brightness: 0.5}
    inputs:
      button: {pin: 4, kind: button}
"""

NEW_PARAMS = """color: "#3040ff"
"""


def _new(args) -> None:
    d = Path(args.path)
    if d.exists() and any(d.iterdir()):
        sys.exit(f"{d} already exists and is not empty")
    (d / "media").mkdir(parents=True, exist_ok=True)
    (d / "show.py").write_text(NEW_SHOW.format(name=d.name))
    (d / "devices.yaml").write_text(NEW_DEVICES)
    (d / "params.yaml").write_text(NEW_PARAMS)
    print(f"made {d}; run it with: haunt run {d} --sim")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="haunt", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hub", help="hub URL for client commands (default $HAUNT_URL or http://localhost:8080)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run a piece: engine, web UI, recorder")
    p.add_argument("piece")
    p.add_argument("--broker", default="localhost:1883", help="MQTT broker (started for you if local and absent)")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--sim", nargs="?", const="unbound", choices=["unbound", "all"],
                   help="simulate nodes: those with no id (default), or all")
    p.add_argument("--mute", action="store_true", help="simulated nodes do not play audio here")

    p = sub.add_parser("node", help="run this machine as a node (Pi: audio)")
    p.add_argument("--name")
    p.add_argument("--broker", default=os.environ.get("HAUNT_BROKER", "haunt.local:1883"))
    p.add_argument("--http-port", type=int, default=8080)
    p.add_argument("--media", help="media URL (default http://<broker host>:8080/media)")

    p = sub.add_parser("status", help="show state, nodes, params, recent log")
    p.add_argument("--json", action="store_true")
    p.add_argument("-n", "--lines", type=int, default=12)

    p = sub.add_parser("tail", help="live bus traffic")
    p.add_argument("filter", nargs="?", help="only topics containing this")
    p.add_argument("--all", action="store_true", help="also print the current state of every topic first")

    p = sub.add_parser("set", help="set a param live")
    p.add_argument("name")
    p.add_argument("value")
    p = sub.add_parser("unset", help="return a param (or all) to its params.yaml default")
    p.add_argument("name", nargs="?")
    p = sub.add_parser("goto", help="force a state")
    p.add_argument("state")
    sub.add_parser("reset", help="clear the show's variables and go to the initial state")
    p = sub.add_parser("action", help="press an operator action")
    p.add_argument("name")
    p = sub.add_parser("blackout", help="all LEDs off (on) or restore them (off)")
    p.add_argument("on", choices=["on", "off"])
    p = sub.add_parser("identify", help="blink a node so you can find it")
    p.add_argument("node")
    p = sub.add_parser("press", help="simulate an input, e.g. `haunt press lamp.button`")
    p.add_argument("input", help="node.input")
    p.add_argument("event", nargs="?", default="press", help="press, release, on, off, value")
    p.add_argument("value", nargs="?")
    p = sub.add_parser("push", help="send a piece to the hub over ssh (it hot-reloads)")
    p.add_argument("piece")
    p.add_argument("--to", default=os.environ.get("HAUNT_SSH", "haunt@haunt.local"),
                   help="ssh destination of the hub (default $HAUNT_SSH or haunt@haunt.local)")
    p.add_argument("--path", default="haunt/pieces", help="pieces folder on the hub, relative to home")
    p.add_argument("--watch", action="store_true", help="keep pushing on every save")
    p = sub.add_parser("ota", help="push a firmware image to an ESP32 node")
    p.add_argument("node")
    p.add_argument("file")
    p = sub.add_parser("new", help="start a new piece folder")
    p.add_argument("path")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    try:
        if args.cmd == "run":
            asyncio.run(_run(args))
        elif args.cmd == "node":
            asyncio.run(_node(args))
        elif args.cmd == "status":
            _status(args)
        elif args.cmd == "tail":
            asyncio.run(_tail(args))
        elif args.cmd == "set":
            _cmd(args, "param", {"name": args.name, "value": _value(args.value)})
        elif args.cmd == "unset":
            _cmd(args, "param_reset", {"name": args.name} if args.name else {})
        elif args.cmd == "goto":
            _cmd(args, "goto", {"state": args.state})
        elif args.cmd == "reset":
            _cmd(args, "reset", {})
        elif args.cmd == "action":
            _cmd(args, "action", {"name": args.name})
        elif args.cmd == "blackout":
            _cmd(args, "blackout", {"on": args.on == "on"})
        elif args.cmd == "identify":
            _cmd(args, "identify", {"node": args.node})
        elif args.cmd == "press":
            _press(args)
        elif args.cmd == "push":
            _push(args)
        elif args.cmd == "ota":
            _ota(args)
        elif args.cmd == "new":
            _new(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

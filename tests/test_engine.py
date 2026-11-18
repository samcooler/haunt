import asyncio
import os
import shutil
import time
from pathlib import Path

import pytest

from haunt import topics
from haunt.bus import MemoryBus
from haunt.engine import Engine
from haunt.piece import Piece

HELLO = Path(__file__).parent.parent / "pieces" / "hello"

DEVICES = """
nodes:
  lamp:
    id: esp-000001
    leds: {pin: 14, count: 8}
    inputs: {button: {pin: 4, kind: button}}
"""


def make_piece(tmp_path: Path, show: str, params: str = "", devices: str = DEVICES) -> Piece:
    d = tmp_path / "piece"
    d.mkdir(exist_ok=True)
    (d / "show.py").write_text(show)
    (d / "params.yaml").write_text(params)
    (d / "devices.yaml").write_text(devices)
    return Piece(d)


async def settle(t: float = 0.05) -> None:
    await asyncio.sleep(t)


async def start(piece: Piece, tmp_path: Path) -> tuple[Engine, MemoryBus]:
    bus = MemoryBus()
    eng = Engine(piece, bus, data_dir=tmp_path / "data")
    await eng.start()
    await settle()
    return eng, bus


def outs(bus: MemoryBus, node: str, output: str = "leds") -> list:
    return [p for t, p in bus.history if t == topics.node_out(node, output)]


def logs(bus: MemoryBus) -> list[str]:
    return [p["msg"] for t, p in bus.history if t == topics.SHOW_LOG]


async def press(bus: MemoryBus, node="lamp", input_="button", event="press", value=None):
    await bus.publish(topics.node_in(node, input_), {"event": event, "value": value})
    await settle()


def write_later(path: Path, text: str) -> None:
    """Rewrite a file so its mtime visibly changes."""
    path.write_text(text)
    t = time.time() + 2
    os.utime(path, (t, t))


SIMPLE = """
from haunt import Show
show = Show()

@show.state
async def idle(s):
    s.leds("lamp", effect="breathe", color=lambda p: p.color)
    await s.wait("lamp.button.press")
    return "play"

@show.state(timeout=0.2, then="idle")
async def play(s):
    s.leds("lamp", effect="solid", color="#ffffff")
"""


async def test_starts_in_initial_and_binds_params(tmp_path):
    eng, bus = await start(make_piece(tmp_path, SIMPLE, 'color: "#123456"'), tmp_path)
    assert eng.state == "idle"
    assert bus.retained[topics.SHOW_STATE]["state"] == "idle"
    assert outs(bus, "lamp")[-1] == {"effect": "breathe", "color": "#123456"}
    assert bus.retained[topics.assign("esp-000001")]["name"] == "lamp"

    # A param change re-sends the outputs bound to it.
    await bus.publish(topics.cmd("param"), {"name": "color", "value": "#abcdef"})
    await settle()
    assert outs(bus, "lamp")[-1]["color"] == "#abcdef"
    # ...and survives a restart of the engine.
    eng.stop()
    eng2, bus2 = await start(Piece(eng.piece.path), tmp_path)
    assert outs(bus2, "lamp")[-1]["color"] == "#abcdef"
    eng2.stop()


async def test_event_transition_and_timeout(tmp_path):
    eng, bus = await start(make_piece(tmp_path, SIMPLE, 'color: "#123456"'), tmp_path)
    await press(bus)
    assert eng.state == "play"
    await settle(0.3)
    assert eng.state == "idle"
    assert any("(timeout)" in m for m in logs(bus))
    eng.stop()


async def test_node_coming_online_gets_its_outputs_again(tmp_path):
    eng, bus = await start(make_piece(tmp_path, SIMPLE, 'color: "#123456"'), tmp_path)
    n = len(outs(bus, "lamp"))
    await bus.publish(topics.status("lamp"), {"online": True, "id": "esp-000001"}, retain=True)
    await settle()
    assert len(outs(bus, "lamp")) == n + 1
    eng.stop()


async def test_crashing_state_falls_back_to_initial(tmp_path):
    show = SIMPLE.replace('s.leds("lamp", effect="solid", color="#ffffff")', "1 / 0")
    eng, bus = await start(make_piece(tmp_path, show, 'color: "#123456"'), tmp_path)
    await press(bus)
    await settle(2.2)  # the engine waits 2 s before retrying, so a crash loop cannot spin
    assert eng.state == "idle"
    errors = [m for m in logs(bus) if "crashed" in m]
    assert errors and "ZeroDivisionError" in errors[0] and "show.py:" in errors[0]
    eng.stop()


async def test_hot_reload_and_broken_edit(tmp_path):
    piece = make_piece(tmp_path, SIMPLE, 'color: "#123456"')
    eng, bus = await start(piece, tmp_path)
    write_later(piece.path / "show.py", SIMPLE.replace('effect="breathe"', 'effect="chase"'))
    eng._housekeeping()
    await settle()
    assert outs(bus, "lamp")[-1]["effect"] == "chase"
    assert eng.state == "idle"

    write_later(piece.path / "show.py", "def broken(:\n")
    eng._housekeeping()
    await settle()
    assert "SyntaxError" in bus.retained[topics.SHOW_STATE]["error"]
    await press(bus)                    # the last good show still runs
    assert eng.state == "play"
    eng.stop()


async def test_param_edit_in_yaml_applies_without_reentering(tmp_path):
    piece = make_piece(tmp_path, SIMPLE, 'color: "#123456"')
    eng, bus = await start(piece, tmp_path)
    since = eng.since
    write_later(piece.path / "params.yaml", 'color: "#00ff00"')
    eng._housekeeping()
    await settle()
    assert outs(bus, "lamp")[-1]["color"] == "#00ff00"
    assert eng.since == since
    eng.stop()


async def test_idle_reset(tmp_path):
    show = SIMPLE.replace("Show()", "Show(idle_reset=0.1)").replace("timeout=0.2", "timeout=10")
    eng, bus = await start(make_piece(tmp_path, show, 'color: "#123456"'), tmp_path)
    await press(bus)
    assert eng.state == "play"
    await settle(0.15)
    eng._housekeeping()
    await settle()
    assert eng.state == "idle"
    eng.stop()


async def test_operator_commands(tmp_path):
    show = SIMPLE + """
@show.on_reset
def fresh(s):
    s.vars.score = 0

@show.action("Bump")
async def bump(s):
    s.vars.score += 1
"""
    eng, bus = await start(make_piece(tmp_path, show, 'color: "#123456"'), tmp_path)
    assert eng.vars.score == 0
    await bus.publish(topics.cmd("action"), {"name": "Bump"})
    await bus.publish(topics.cmd("goto"), {"state": "play"})
    await settle()
    assert eng.vars.score == 1 and eng.state == "play"

    await bus.publish(topics.cmd("blackout"), {"on": True})
    await settle()
    assert outs(bus, "lamp")[-1]["effect"] == "off"
    await bus.publish(topics.cmd("reset"), {})
    await settle()
    assert eng.vars.score == 0 and eng.state == "idle"
    assert outs(bus, "lamp")[-1]["effect"] == "off"      # still blacked out
    await bus.publish(topics.cmd("blackout"), {"on": False})
    await settle()
    assert outs(bus, "lamp")[-1]["effect"] == "breathe"
    eng.stop()


async def test_missing_param_reference_is_a_load_error(tmp_path):
    show = SIMPLE.replace("timeout=0.2", 'timeout="nope"')
    eng, bus = await start(make_piece(tmp_path, show, 'color: "#123456"'), tmp_path)
    assert eng.show is None
    assert "no param 'nope'" in bus.retained[topics.SHOW_STATE]["error"]
    eng.stop()


async def test_hello_piece_plays_through(tmp_path):
    shutil.copytree(HELLO, tmp_path / "hello")
    eng, bus = await start(Piece(tmp_path / "hello"), tmp_path)
    await bus.publish(topics.cmd("param"), {"name": "pulls_to_win", "value": 2})
    await press(bus)
    assert eng.state == "awake"
    assert outs(bus, "speaker", "audio")[-1]["file"] == "wake.wav"
    await press(bus, input_="pull", event="on")
    await press(bus, input_="pull", event="on")
    assert eng.state == "win"
    assert outs(bus, "lamp")[-1]["effect"] == "rainbow"
    eng.stop()


def test_topic_matching():
    assert topics.matches("haunt/n/+/in/+", "haunt/n/lamp/in/button")
    assert topics.matches("haunt/#", "haunt/show/state")
    assert not topics.matches("haunt/n/+/in/+", "haunt/n/lamp/out/leds")
    assert topics.split_node_topic("haunt/n/lamp/status") == ("lamp", "status", "")


async def test_new_piece_scaffold_runs(tmp_path):
    from haunt.cli import main
    main(["new", str(tmp_path / "fresh")])
    eng, bus = await start(Piece(tmp_path / "fresh"), tmp_path)
    assert eng.error is None and eng.state == "idle"
    await press(bus, node="thing")
    assert eng.state == "active" and eng.vars.visits == 1
    eng.stop()

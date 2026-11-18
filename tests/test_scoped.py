"""State-scoped handlers and timers: s.on / s.every / s.at / s.idle, s.leaving, s.pick."""

import asyncio
from pathlib import Path

from haunt import topics
from tests.test_engine import DEVICES, make_piece, press, settle, start

SCOPED = """
from haunt import Show
show = Show()

@show.on_reset
def fresh(s):
    s.vars.heard = []
    s.vars.ticks = 0
    s.vars.quiet = 0
    s.vars.at = 0

@show.state
async def one(s):
    @s.on("lamp.button.press")
    def pressed(ev):
        s.vars.heard.append("one")
        before = s.entries
        s.goto("two")
        s.vars.heard.append(f"now in {s.state}, moved {s.entries - before}")

    @s.on("lamp.button.press")
    def second_handler(ev):
        s.vars.heard.append("second handler ran")   # must not: the state is over

@show.state
def two(s):
    s.on("lamp.button.press", lambda ev: s.vars.heard.append("two"))
    s.every(0.05, lambda: setattr(s.vars, "ticks", s.vars.ticks + 1))
    s.at(0.12, lambda: setattr(s.vars, "at", s.vars.at + 1))
    s.idle(0.1, lambda: setattr(s.vars, "quiet", s.vars.quiet + 1), pattern="lamp.*")
"""


async def test_scoped_handlers_end_with_their_state(tmp_path):
    eng, bus = await start(make_piece(tmp_path, SCOPED), tmp_path)
    await press(bus)
    assert eng.state == "two"
    assert eng.vars.heard == ["one", "now in two, moved 1"]
    await press(bus)
    assert eng.vars.heard == ["one", "now in two, moved 1", "two"]   # one's handlers are gone
    eng.stop()


async def test_scoped_timers(tmp_path):
    eng, bus = await start(make_piece(tmp_path, SCOPED), tmp_path)
    await press(bus)
    await settle(0.3)
    assert eng.vars.ticks >= 4
    assert eng.vars.at == 1
    assert eng.vars.quiet >= 2
    quiet = eng.vars.quiet
    for _ in range(4):                      # activity keeps idle from firing
        await press(bus)
        await settle(0.04)
    assert eng.vars.quiet == quiet
    await bus.publish(topics.cmd("goto"), {"state": "one"})
    await settle()
    ticks = eng.vars.ticks
    await settle(0.2)
    assert eng.vars.ticks == ticks          # timers ended with the state
    eng.stop()


async def test_pick_never_repeats_and_media_from_piece_yaml(tmp_path):
    piece = make_piece(tmp_path, SCOPED)
    elsewhere = tmp_path / "bigmedia" / "video_once"
    elsewhere.mkdir(parents=True)
    for n in ("janis_nag_a.mp4", "janis_nag_b.mp4", "janis_yes.mp4"):
        (elsewhere / n).write_bytes(b"x")
    (piece.path / "piece.yaml").write_text("media: [/nonexistent, ../bigmedia]\n")
    eng, bus = await start(piece, tmp_path)
    assert piece.media == (tmp_path / "bigmedia").resolve()
    picks = [eng.ctx.pick("video_once/janis_nag_*") for _ in range(6)]
    assert set(picks) == {"video_once/janis_nag_a.mp4", "video_once/janis_nag_b.mp4"}
    assert all(a != b for a, b in zip(picks, picks[1:]))
    assert eng.ctx.pick("video_once/nothing_*") is None
    eng.stop()


BURST = """
from haunt import Show
show = Show()

@show.on_reset
def fresh(s):
    s.vars.log = []

@show.on("lamp.button.press")
def burst(s, ev):
    # One input, several events in a row, the way a game turns one pull into many.
    for name in ("go", "after_go", "win"):
        s.emit(f"game.{name}")

@show.state
def a(s):
    s.on("game.go", lambda ev: s.goto("b"))
    s.on("game.after_go", lambda ev: s.vars.log.append("a heard after_go"))   # must not

@show.state(timeout=0.1, then="a")
def b(s):
    s.vars.log.append("entered b")
    s.on("game.after_go", lambda ev: s.vars.log.append("b heard after_go"))
    s.on("game.win", lambda ev: s.goto("c"))

@show.state
def c(s):
    s.vars.log.append("entered c")
    return "a" if s.vars.get("bounce") else None
"""


async def test_a_burst_crosses_a_state_change_in_order(tmp_path):
    eng, bus = await start(make_piece(tmp_path, BURST), tmp_path)
    await press(bus)
    assert eng.vars.log == ["entered b", "b heard after_go", "entered c"]
    assert eng.state == "c"
    eng.vars.bounce = True
    await bus.publish(topics.cmd("goto"), {"state": "c"})
    await settle()
    assert eng.state == "a"                 # an entry that returns a state moves straight on
    await bus.publish(topics.cmd("goto"), {"state": "b"})
    await settle(0.2)
    assert eng.state == "a"                 # a plain state's timeout
    eng.stop()

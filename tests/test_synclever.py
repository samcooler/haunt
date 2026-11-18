"""synclever, played through on an in-memory bus: the whole arc, the purple pool,
the spent pull, Janis's patience, the diorama, media and gains."""

import asyncio
import shutil
from pathlib import Path

import pytest

from haunt import topics
from haunt.bus import MemoryBus
from haunt.engine import Engine
from haunt.piece import Piece

PIECE = Path(__file__).parent.parent / "pieces" / "synclever"

MEDIA = """
video_loops/idle.mp4 video_loops/game1.mp4 video_loops/network_idle.mp4
video_once/janis_encouragement_good_work_1.mp4 video_once/janis_encouragement_doing_great.mp4
video_once/janis_progress_confirmation_yes.mp4 video_once/janis_progress_confirmation_congrats_1.mp4
video_once/janis_progress_confirmation_congrats_2.mp4 video_once/janis_progress_confirmation_congrats_3.mp4
video_once/janis_progress_confirmation_thats_it.mp4 video_once/janis_progress_confirmation_essences_1.mp4
video_once/janis_progress_confirmation_essences_2.mp4
video_once/janis_suspicion_what_doing_1.mp4 video_once/janis_anger_no_1.mp4 video_once/janis_anger_no_2.mp4
video_once/janis_warn_dont_look.mp4 video_once/janis_warn_dont_go_in.mp4 video_once/janis_anger_stop_it.mp4
video_once/janis_beg_button.mp4 video_once/janis_threatening_disciplinary_1.mp4
video_once/janis_panic_imminent.mp4 video_once/puppet_escape_party.mp4
audio_loops/idle.mp3 audio_loops/clock_ambient.mp3 diorama_pi/audio_loops/spirit_distress.mp3
audio_once/lever_red.mp3 audio_once/lever_purple.mp3 audio_once/lever_red_error.mp3
audio_once/lever_purple_error.mp3 audio_once/spirits_denied.mp3 audio_once/victory_fanfare.mp3
audio_once/level_up.mp3 audio_once/component_powerup.mp3 audio_once/steam.mp3
""".split()


async def settle(t=0.05):
    await asyncio.sleep(t)


@pytest.fixture
async def room(tmp_path):
    shutil.copytree(PIECE, tmp_path / "synclever", ignore=shutil.ignore_patterns("__pycache__"))
    media = tmp_path / "media"
    for f in MEDIA:
        (media / f).parent.mkdir(parents=True, exist_ok=True)
        (media / f).write_bytes(b"x")
    (tmp_path / "synclever" / "piece.yaml").write_text(f"media: [{media}]\n")
    bus = MemoryBus()
    eng = Engine(Piece(tmp_path / "synclever"), bus, data_dir=tmp_path / "data")
    await eng.start()
    await settle()
    yield eng, bus
    eng.stop()
    errors = [p["msg"] for t, p in bus.history if t == topics.SHOW_LOG and p["level"] == "error"]
    assert not errors, errors


def game(eng):
    return eng.keep["game"]


def rope_of(eng, color):
    return next(n for n, d in eng.devices.items() if n.startswith("rope") and d.get("color") == color)


async def pull(bus, eng, color, times=1):
    for _ in range(times):
        await bus.publish(topics.node_in(rope_of(eng, color), "lever"), {"event": "pull"})
    await settle(0.02)


def sent(bus, node, output):
    return [p for t, p in bus.history if t == topics.node_out(node, output)]


def clips(bus):
    return [Path(p["file"]).stem for p in sent(bus, "overlord", "video") if not p.get("loop")]


def sounds(bus):
    return [Path(p["file"]).stem for p in sent(bus, "overlord", "audio")]


async def win_round(bus, eng):
    for color in game(eng).goals:
        await pull(bus, eng, color, 3)          # 40% a pull: the third lights it


async def test_the_whole_arc_to_freedom(room):
    eng, bus = room
    assert eng.error is None, eng.error
    assert eng.state == "passive"
    assert sent(bus, "queue", "display")[-1]["slides"][0]["text"].startswith("Avoid purple")
    assert sent(bus, "diorama", "leds")[-1]["effect"] == "flicker"
    assert sent(bus, "diorama", "switch")[-1] == {"latch": True}

    await pull(bus, eng, "red")
    assert eng.state == "easy_round"
    assert game(eng).level("red") == 0          # the first pull is spent starting the round
    assert len(game(eng).goals) == 1
    assert "lever_red" in sounds(bus)

    await win_round(bus, eng)
    assert eng.state == "medium_round" and len(game(eng).goals) == 2
    await win_round(bus, eng)
    assert eng.state == "hard_round" and len(game(eng).goals) == 3
    await win_round(bus, eng)
    assert eng.state == "party"
    await settle(0.7)                                 # the strings come on 200 ms apart
    assert {"lights4": True} in sent(bus, "party", "switch")
    assert game(eng).level("purple") == 0             # purple starts empty at the party

    await pull(bus, eng, "purple", 7)
    assert eng.state == "party"
    await pull(bus, eng, "purple", 2)                 # 8 x 12.5% fills it, less what drained between
    assert eng.state == "victory2"
    assert "puppet_escape_party" in clips(bus)
    assert sent(bus, "diorama", "leds")[-1]["effect"] == "rainbow"


async def test_first_purple_pull_starts_the_final_and_still_counts(room):
    eng, bus = room
    await bus.publish(topics.cmd("goto"), {"state": "final_wait"})
    await settle()
    assert sent(bus, "extractor_red", "leds")[-1]["effect"] == "sparkle"   # the machine breaking
    await pull(bus, eng, "purple")
    assert eng.state == "final"
    assert game(eng).level("purple") == pytest.approx(12.5, abs=0.5)      # not spent: no reset
    await pull(bus, eng, "purple", 8)
    assert eng.state == "victory2"


async def test_final_times_out_to_kickout_then_passive(room):
    eng, bus = room
    eng.params.overrides.update(final_time=0.1, kickout_time=0.1)
    await bus.publish(topics.cmd("goto"), {"state": "final"})
    await settle(0.15)
    assert eng.state == "kickout"
    await settle(0.15)
    assert eng.state == "passive"


async def test_janis_loses_patience_with_wrong_colours(room):
    eng, bus = room
    await bus.publish(topics.cmd("goto"), {"state": "easy_round"})
    await settle()
    g = game(eng)
    g.goals = ("blue",)
    await pull(bus, eng, "red", 2)
    assert eng.vars.stray == 2
    assert "lever_red_error" in sounds(bus)
    before = len(clips(bus))
    await pull(bus, eng, "red")                       # the third strike
    assert eng.vars.stray == 0
    assert any(c.startswith(("janis_suspicion", "janis_anger")) for c in clips(bus)[before:])
    # A wrong colour drains three times as fast while the goals show.
    assert g.rate("red") == pytest.approx(3 * g.rate("blue"))


async def test_purple_in_a_round_is_inactive(room):
    eng, bus = room
    await bus.publish(topics.cmd("goto"), {"state": "easy_round"})
    await settle()
    await pull(bus, eng, "purple")
    assert "lever_purple_error" in sounds(bus)
    assert sent(bus, "overlord", "text")[-1]["text"].startswith("PURPLE LEVERS ARE INACTIVE")
    await pull(bus, eng, "purple")
    assert len(sent(bus, "overlord", "text")) == 1     # at most once every 10 s


async def test_the_diorama(room):
    eng, bus = room
    await bus.publish(topics.cmd("goto"), {"state": "party"})
    await settle()
    await bus.publish(topics.node_in("diorama", "door"), {"event": "on"})
    await settle()
    assert clips(bus)[-1] in ("janis_warn_dont_go_in", "janis_warn_dont_look")
    assert sent(bus, "diorama", "leds")[-1]["color"] == "#5A0000"     # the red stutter
    await bus.publish(topics.node_in("diorama", "button"), {"event": "press"})
    await settle(0.1)
    assert "spirits_denied" in sounds(bus)
    assert any(p.get("color") == "#BF5AF2" for p in sent(bus, "diorama", "leds"))   # purple flashes


async def test_media_gain_and_layers(room):
    eng, bus = room
    janis = [p for p in sent(bus, "overlord", "video") if "janis" in p["file"]]
    await pull(bus, eng, "red")
    janis = [p for p in sent(bus, "overlord", "video") if "janis" in p["file"]]
    assert janis and janis[-1]["volume"] == 2.0       # every Janis clip +6 dB
    assert sent(bus, "overlord", "audio-bed")[-1]["file"] == "audio_loops/clock_ambient.mp3"
    eng.params.overrides["party_time"] = 10
    await bus.publish(topics.cmd("goto"), {"state": "party"})
    await settle(5.2)
    distress = sent(bus, "diorama_pi", "audio-loop")[-1]
    assert distress["file"] == "diorama_pi/audio_loops/spirit_distress.mp3"
    assert distress["ramp"] == {"start": 0.5, "step": 0.1, "every": 10}


async def test_ropes_show_their_charge(room):
    eng, bus = room
    await pull(bus, eng, "red")                       # starts round 1, spent
    g = game(eng)
    g.goals = ("red",)
    await pull(bus, eng, "red")
    leds = sent(bus, "rope01", "leds")[-1]
    assert leds["effect"] == "level" and leds["level"] == pytest.approx(0.4, abs=0.02)
    assert leds["color"] == "#FF3B30"


async def test_the_band_over_the_video(room):
    eng, bus = room

    def band():
        return [p["ass"] for p in sent(bus, "overlord", "overlay") if p["id"] == 8][-1]

    def banner():
        return [p["ass"] for p in sent(bus, "overlord", "overlay") if p["id"] == 11][-1]

    await settle(0.2)
    assert "MOVE ANY LEVER TO BEGIN" in band()
    await pull(bus, eng, "red")
    await settle(0.2)
    assert "GOAL" in band() and "1 of 3" in band()
    await win_round(bus, eng)
    await settle(0.2)
    assert "EXTRACTION SPEED" in banner()
    eng.params.overrides["party_time"] = 10
    await bus.publish(topics.cmd("goto"), {"state": "party"})
    await settle(1.3)
    assert "MAXIMUM" in banner()                       # the party card (no clip playing)
    await pull(bus, eng, "purple")
    await settle(0.2)
    assert "EXTRACTION INTEGRITY" in band() and "DISRUPTS EXTRACTION" in banner()


async def test_captions_follow_janis_clips(room):
    eng, bus = room
    await bus.publish(topics.node_in("overlord", "video"), {"event": "started", "value": "janis_beg_button.mp4"})
    await settle()
    cap = [p for p in sent(bus, "overlord", "overlay") if p["id"] == 12][-1]
    assert "That button has been disabled" in cap["ass"]
    await bus.publish(topics.node_in("overlord", "video"), {"event": "ended", "value": "janis_beg_button.mp4"})
    await settle()
    assert [p for p in sent(bus, "overlord", "overlay") if p["id"] == 12][-1]["ass"] == ""

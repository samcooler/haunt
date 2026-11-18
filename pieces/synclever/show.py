"""synclever: the lever room. Ported from Pulleys' game_state_machine/state_config.yaml.

The story. Janis, the boss on the big screen, is cheerful corporate productivity
and actually bad. The ropes are levers. Lighting the colours ("collecting the
essences") powers the extraction of the art spirits' souls for her; visitors are
her workers without knowing it. Three rounds teach compliance (light the colours
she wants, never purple). The party is the first victory. Then the diorama
glows, its door is open, and inside are the spirits and a hint: purple frees
them. Its "free the spirits" button is a red herring. The real way out is
defiance: fill the purple pool in the final, and the spirits escape.

Flow:
    passive -> easy_round -> medium_round -> hard_round -> party (40 s)
      -> final_wait (80 s to pull a purple lever; none: back to passive)
      -> final (3 m) -> victory2 -> passive;  final times out -> kickout -> passive.
    Filling the purple pool wins from party, final_wait or final.

Every state is a plain function: it runs the moment the show enters it, sets
the room, and registers what it reacts to. Game events are "game.<name>", as
the arbiter named them: pull, goal_pull, nongoal_pull, nongoal_<colour>_pull,
final_pull, goal_component_activated, first_goal_component_activated,
nongoal_component_activated, component_deactivated, all_components_activated,
all_components_deactivated, final_component_activated,
final_component_deactivated, all_final_components_activated.
"""

from __future__ import annotations

import asyncio
import random
import time

from haunt import Show

from band import Band
from game import COLORS, MAJORS, PURPLE, Game

show = Show(initial="passive")

ROUNDS = ("easy_round", "medium_round", "hard_round")
FOLDERS = {"audio_loop": "audio_loops", "audio_bed": "audio_loops", "audio_once": "audio_once",
           "video_loop": "video_loops", "video_once": "video_once"}
COLD_WHITE = "#C8FFF4"     # the diorama's failing hospital fluorescents


# ── the room ─────────────────────────────────────────────────────────────────
def game(s) -> Game:
    if "game" not in s.keep:
        p = s.param
        s.keep["game"] = Game(lambda: (p.charge_per_pull, p.final_charge_per_pull, p.decay_rate))
    return s.keep["game"]


def band(s) -> Band:
    if "band" not in s.keep:
        s.keep["band"] = Band(game(s), content(s)["colors"], s.param)
    return s.keep["band"]


def content(s) -> dict:
    return s.load("content.yaml") or {}


def ropes(s, color: str | None = None) -> list[str]:
    return [n for n, d in s.devices.items()
            if n.startswith("rope") and (color is None or d.get("color") == color)]


def media(s, kind: str, name: str, node: str) -> str | None:
    """A cue's file, Pulleys' way: <folder>/<kind>_<name>.* or <folder>/<name>.*, a node's
    own folder (<node>/<folder>/) first; a * picks one at random, never twice running."""
    folder = FOLDERS[kind]
    found = s.pick(f"{node}/{folder}/{kind}_{name}", f"{node}/{folder}/{name}") or \
        s.pick(f"{folder}/{kind}_{name}", f"{folder}/{name}")
    if not found:
        missing = s.keep.setdefault("missing", set())
        if (kind, name) not in missing:
            missing.add((kind, name))
            s.log(f"no media for {kind}_{name}")
    return found


def gain(s, cue: str) -> float:
    import fnmatch
    table = content(s).get("gain") or {}
    if cue in table:
        return float(table[cue])
    for pattern, g in table.items():
        if fnmatch.fnmatchcase(cue, pattern):
            return float(g)
    return 1.0


def audio_once(s, name: str, node: str = "overlord") -> None:
    if file := media(s, "audio_once", name, node):
        s.out(node, "audio", persist=False, file=file, volume=gain(s, f"audio_once_{name}"))


def audio_loop(s, name: str, node: str = "overlord", layer: str = "loop") -> None:
    """A background loop on a node's layer ("loop" or "bed"); name "off" stops it."""
    kind = "audio_loop" if layer == "loop" else "audio_bed"
    if name == "off":
        s.out(node, f"audio-{layer}", stop=True)
        return
    if file := media(s, kind, name, node):
        cue = f"{kind}_{name}"
        ramp = (content(s).get("ramp") or {}).get(cue)
        s.out(node, f"audio-{layer}", file=file, volume=gain(s, cue), **({"ramp": ramp} if ramp else {}))


def video_loop(s, name: str) -> None:
    if file := media(s, "video_loop", name, "overlord"):
        s.out("overlord", "video", file=file, loop=True)


def video_once(s, name: str, cut: bool = False) -> None:
    """A clip over the loop. One that arrives while another plays is dropped, unless it
    cuts: a state's entry clip cuts, an event's does not (as on the Overlord)."""
    if file := media(s, "video_once", name, "overlord"):
        s.out("overlord", "video", persist=False, file=file, cut=cut,
              volume=gain(s, f"video_once_{name}"), duck=0.7)   # the loop and bed drop ~9 dB under her


def janis(s, *lines: str, cut: bool = False) -> None:
    """One of these Janis clips (a * in a name picks among its files)."""
    video_once(s, "janis_" + random.choice(lines), cut)


def message(s, name: str) -> None:
    m = (content(s).get("messages") or {}).get(name)
    if not m:
        return
    last = s.keep.setdefault("message_at", {})
    if time.time() - last.get(name, -1e9) < float(m.get("cooldown", 0)):
        return
    last[name] = time.time()
    s.out("overlord", "text", persist=False, text=m["text"], seconds=float(m.get("seconds", 4)))


def setting(s, key: str, value: str) -> None:
    """A device setting, as Pulleys named them (diorama.lights: creepy ...)."""
    settings = s.vars.get("settings") or {}
    settings[key] = value
    s.vars.settings = settings
    if key == "diorama.lights":
        diorama_lights(s)
    elif key == "diorama.latch":
        s.out("diorama", "switch", latch=value == "open")
    elif key == "party.lights":
        party_lights(s, value == "lit")
    elif key == "extractors.effect":
        for color in MAJORS:
            show_extractor(s, color)
    elif key == "television.channel":
        s.out("television", "display", slides=(content(s).get("television") or {}).get(value, []))
    elif key == "queue.sign":
        s.out("queue", "display", slides=(content(s).get("sign") or {}).get(value, []))


def party_lights(s, on: bool) -> None:
    # The strings come on 200 ms apart, as the party board did.
    loop = asyncio.get_running_loop()
    for i in range(4):
        loop.call_later(0.2 * i, lambda i=i: s.out("party", "switch", **{f"lights{i + 1}": on}))


def rgb(s, color: str) -> str:
    return content(s)["colors"].get(color, "#FFFFFF")


def dim(hex_color: str, k: float) -> str:
    h = hex_color.lstrip("#")
    return "#" + "".join(f"{int(int(h[i:i + 2], 16) * max(0.0, min(1.0, k))):02X}" for i in (0, 2, 4))


def show_rope(s, name: str) -> None:
    """A rope shows its colour's charge as a level, its empty part glowing dimly.
    A goal's rope pulses while a round is on. Values are functions so that a
    re-send (a param change, the rope rebooting) shows the charge as it is then."""
    g, color = game(s), s.devices[name].get("color")
    if color not in COLORS:
        return
    goal = s.state in ROUNDS and color in g.goals
    s.leds(name, effect="level", color=rgb(s, color),
           color2=lambda p: dim(rgb(s, color), p.rope_idle),
           level=lambda p: g.level(color) / 100, drain=lambda p: g.rate(color) / 100,
           pulse=1.2 if goal else 0, fade=0.15)


def show_extractor(s, color: str) -> None:
    g = game(s)
    name = f"extractor_{color}"
    effect = (s.vars.get("settings") or {}).get("extractors.effect", "none")
    if effect == "party":
        s.leds(name, effect="rainbow", speed=1.5, brightness=lambda p: p.extractor_brightness)
    elif effect == "breaking":
        s.leds(name, effect="sparkle", color=rgb(s, PURPLE), color2="#000000",
               brightness=lambda p: p.extractor_brightness)
    else:
        wanted = s.state in ROUNDS and color in g.goals
        s.leds(name, effect="level", color=rgb(s, color), color2="#000000",
               level=lambda p: g.level(color) / 100, drain=lambda p: g.rate(color) / 100,
               pulse=0.8 if wanted else 0, brightness=lambda p: p.extractor_brightness, fade=0.3)


def show_color(s, color: str) -> None:
    for name in ropes(s, color):
        show_rope(s, name)
    if color in MAJORS:
        show_extractor(s, color)


def show_all(s) -> None:
    for color in COLORS:
        show_color(s, color)


def diorama_lights(s) -> None:
    lights = (s.vars.get("settings") or {}).get("diorama.lights", "creepy")
    door_open = bool(s.vars.get("door_open"))
    if lights == "dark":
        s.leds("diorama", effect="off", fade=1.0)
    elif lights == "paradise":
        s.leds("diorama", effect="rainbow", speed=0.15, fade=3.0)   # the spirits made it
    else:
        # Dim while the door is shut, easing up as it opens.
        s.leds("diorama", effect="flicker", color=COLD_WHITE, fade=1.5,
               brightness=lambda p: 1.0 if door_open else p.diorama_shut)


def enter(s) -> Game:
    """What every state does first."""
    g = game(s)
    g.goals_visible = s.state in ROUNDS
    band(s).on_state(s.state)
    return g


# ── the game's pulls ─────────────────────────────────────────────────────────
def pull(s, color: str) -> None:
    """One pull, as the arbiter took it: the pull's events first, so a state can react
    before its charge goes in; then the charge, and what it lights. A pull whose
    reaction reset the components (a round starting) is spent on that."""
    g = game(s)
    cls = g.classify(color)
    if cls is None:
        return
    if cls == "final":
        band(s).on_purple_pull()
    resets = g.resets
    names = ["pull", f"{cls}_pull"] + ([f"nongoal_{color}_pull"] if cls == "nongoal" else [])
    for name in names:
        s.emit(f"game.{name}", color)
        if g.resets != resets:
            return
    lit = g.pour(color)
    show_color(s, color)
    if not lit:
        return
    if color == PURPLE:
        s.emit("game.final_component_activated", color)
        if g.lit(PURPLE) and ropes(s, PURPLE):
            s.emit("game.all_final_components_activated", color)
        return
    if color in g.goals:
        if g.lit_majors() & set(g.goals) == {color}:
            s.emit("game.first_goal_component_activated", color)
        s.emit("game.goal_component_activated", color)
    else:
        s.emit("game.nongoal_component_activated", color)
    if g.simon_done():
        s.emit("game.all_components_activated", color)


@show.on("*.lever.pull")
def lever(s, ev):
    color = (s.devices.get(ev.node) or {}).get("color")
    if color not in COLORS:
        s.log(f"{ev.node} pulled, but it has no colour in devices.yaml")
        return
    audio_once(s, f"lever_{color}")   # every pull, every state: the lever's own sound
    pull(s, color)


@show.every(0.1)
def drain(s):
    g = game(s)
    events = g.tick()
    for name, color in events:
        s.emit(f"game.{name}", color)
    # A wrong colour draining out can leave exactly the goals lit: that completes a round.
    if events and g.simon_done():
        s.emit("game.all_components_activated", "")


# ── round-the-clock: the band, the diorama's board, the steam ─────────────────
@show.every(0.05)
def draw_band(s):
    b, now = band(s), time.time()
    sent = s.keep.setdefault("overlay", {})
    if not b.busy() and now - sent.get("frame_at", 0) < 0.125:
        return
    sent["frame_at"] = now
    for oid, ass, z in zip((8, 11), b.frame(), (20, 21)):
        last, at = sent.get(oid, (None, 0))
        if ass != last or now - at > 1.0:
            sent[oid] = (ass, now)
            s.out("overlord", "overlay", persist=False, id=oid, ass=ass, z=z)


@show.on("overlord.video.*")
async def clip(s, ev):
    if ev.kind not in ("started", "ended"):
        return
    band(s).set_clip(ev.kind == "started")
    # What Janis says, along the bottom while she says it (captions.yaml: clip -> text).
    text = (s.load("captions.yaml") or {}).get(str(ev.value).rsplit(".", 1)[0]) if ev.kind == "started" else None
    if not text:
        s.out("overlord", "overlay", persist=False, id=12, ass="")
        return
    from band import RES_X, RES_Y, ass_rect
    esc = str(text).replace("\\", "\\\\").replace("{", "(").replace("}", ")")
    s.out("overlord", "overlay", persist=False, id=12, z=15, ass="\n".join([
        ass_rect(0, RES_Y - 190, RES_X, 190, "000000", alpha="50"),
        f"{{\\an2\\pos({RES_X // 2},{RES_Y - 50})\\fs68\\bord3\\shad0\\b1\\q0"
        f"\\1c&HFFFFFF&\\3c&H000000&}}{esc}"]))
    s.keep["caption_for"] = ev.value
    await s.sleep(20)   # a backstop, if the clip's end is never heard
    if s.keep.get("caption_for") == ev.value:
        s.out("overlord", "overlay", persist=False, id=12, ass="")


@show.on("diorama.door.*")
async def door(s, ev):
    s.vars.door_open = ev.kind == "on"
    if ev.kind == "on" and (s.vars.get("settings") or {}).get("diorama.lights") != "dark":
        # Someone opens it: a dim red stutter, then the tubes, brighter now.
        s.out("diorama", "leds", persist=False, effect="sparkle", color="#5A0000", color2="#100000", fade=0)
        await s.sleep(2)
    diorama_lights(s)


@show.on("diorama.button.press")
async def spirits_button(s, ev):
    # "Free the spirits": the tubes flash purple a few times. It does nothing else.
    for _ in range(3):
        s.out("diorama", "leds", persist=False, effect="solid", color=rgb(s, PURPLE), fade=0)
        await s.sleep(0.25)
        s.out("diorama", "leds", persist=False, effect="off", fade=0)
        await s.sleep(0.2)
    diorama_lights(s)


@show.every("ambient_every")
async def steam(s):
    await s.sleep(random.uniform(0, 2))
    audio_once(s, "steam")


@show.on_reset
def fresh(s):
    s.vars.settings = {}
    s.vars.stray = 0
    setting(s, "diorama.lights", "creepy")
    setting(s, "diorama.latch", "open")       # never locked: the diorama is always open
    setting(s, "television.channel", "idle")


# ── the rounds ─────────────────────────────────────────────────────────────────
def stray(s, d: int) -> None:
    """Janis's patience: each wrong pull is a strike, each goal pull forgives one."""
    old = s.vars.get("stray", 0)
    new = max(0, old + d)
    s.vars.stray = new
    if old < s.param.stray_limit <= new:
        s.vars.stray = 0
        janis(s, "suspicion_what_doing_*", "anger_no_*")


def play_round(s, goals: str, purple_line: str, next_state: str, first: bool) -> None:
    g = enter(s)
    s.vars.stray = 0
    if first:
        setting(s, "queue.sign", "wait")      # a group is in; it stays wait until passive
    g.reset()
    g.deal(int(s.param[goals]))
    s.log(f"goals: {', '.join(g.goals)}")
    audio_loop(s, "off", node="diorama_pi")
    video_loop(s, "game1")
    if not first:
        audio_once(s, "level_up")
        janis(s, "progress_confirmation_yes", "progress_confirmation_thats_it",
              "progress_confirmation_essences_1", "progress_confirmation_essences_2", cut=True)
    show_all(s)

    @s.on("game.nongoal_pull")
    def wrong(ev):
        audio_once(s, f"lever_{ev.value}_error")
        stray(s, +1)

    @s.on("game.final_pull")
    def purple_now(ev):
        audio_once(s, "lever_purple_error")
        message(s, "purple_inactive")

    s.on("game.goal_pull", lambda ev: stray(s, -1))

    @s.on("game.goal_component_activated")
    def lit(ev):
        audio_once(s, "component_powerup")
        janis(s, "encouragement_*")

    s.on("game.component_deactivated", lambda ev: audio_once(s, "component_powerdown"))
    s.on("game.final_component_activated", lambda ev: janis(s, purple_line))
    s.on("game.all_components_activated", lambda ev: s.goto(next_state))
    s.idle("nag_delay", lambda: janis(s, "nag_*"), pattern="game.*")
    s.every("encourage_every", lambda: janis(s, "encouragement_*"), when=g.goal_active)
    s.every("productivity_every", lambda: janis(s, "nag_maintain_*"), when=g.no_goal_charge)
    s.every(30, lambda: message(s, "pump"))


@show.state
def passive(s):
    """Nobody playing. The first pull of any lever starts round 1 (and is spent on it)."""
    g = enter(s)
    setting(s, "diorama.lights", "creepy")   # back from victory2's paradise
    setting(s, "party.lights", "dark")
    setting(s, "extractors.effect", "none")
    setting(s, "queue.sign", "enter")          # let the next group in
    audio_loop(s, "off", node="diorama_pi")    # the spirits' distress stops, however we got here
    g.reset()
    g.goals = ()
    audio_loop(s, "idle")
    audio_loop(s, "clock_ambient", layer="bed")   # stays on until victory2
    video_loop(s, "idle")
    show_all(s)

    @s.on("game.pull")
    def begin(ev):
        janis(s, "encouragement_*")
        s.goto("easy_round")


@show.state(timeout="round_time", then="passive")
def easy_round(s):
    """Round 1: light the one colour Janis wants. Purple makes her warn."""
    play_round(s, "easy_goals", "warn_*", "medium_round", first=True)


@show.state(timeout="round_time", then="passive")
def medium_round(s):
    """Round 2: two colours. Purple makes her beg."""
    play_round(s, "medium_goals", "beg_*", "hard_round", first=False)


@show.state(timeout="round_time", then="passive")
def hard_round(s):
    """Round 3: three colours. Purple makes her angry."""
    play_round(s, "hard_goals", "anger_*", "party", first=False)


# ── the endgame ────────────────────────────────────────────────────────────────
def diorama_reactions(s, final: bool = False) -> None:
    @s.on("diorama.door.on")
    def looking(ev):
        lines = ["warn_dont_go_in", "warn_dont_look"] + (["anger_stop_it"] if final else [])
        janis(s, *lines)

    @s.on("diorama.button.press")
    def denied(ev):
        audio_once(s, "spirits_denied")
        janis(s, *(["beg_button", "anger_no_1", "anger_no_2"] if final else ["beg_button"]))

    s.on("game.all_final_components_activated", lambda ev: s.goto("victory2"))


@show.state(timeout="party_time", then="final_wait")
def party(s):
    """The first victory: fanfare, congratulations, the party lights. Purple starts
    empty here and its charge carries on to the end."""
    g = enter(s)
    setting(s, "party.lights", "lit")
    setting(s, "extractors.effect", "party")   # rainbow and sparkles for the first 10 s
    g.reset()
    g.goals = ()
    audio_loop(s, "off", node="diorama_pi")
    audio_once(s, "victory_fanfare")
    janis(s, "progress_confirmation_congrats_1", "progress_confirmation_congrats_2",
          "progress_confirmation_congrats_3", cut=True)
    show_all(s)
    diorama_reactions(s)
    s.on("game.final_component_activated", lambda ev: janis(s, "suspicion_what_doing_*"))
    s.at(5, lambda: audio_loop(s, "spirit_distress", node="diorama_pi"))   # the spirits start screaming
    s.at(10, lambda: setting(s, "extractors.effect", "none"))


@show.state(timeout="first_lever_time", then="passive")
def final_wait(s):
    """The party is over and Janis threatens. The first purple pull starts the final;
    none in time, and it is back to the start."""
    enter(s)
    setting(s, "extractors.effect", "breaking")   # purple flashes: the machine failing
    janis(s, "threatening_disciplinary_1", cut=True)
    video_loop(s, "network_idle")
    diorama_reactions(s)
    s.on("game.final_pull", lambda ev: s.goto("final"))
    s.every(25, lambda: janis(s, "warn_dont_look"))   # dropped while another clip plays
    s.every(30, lambda: message(s, "pump"))


@show.state(timeout="final_time", then="kickout")
def final(s):
    """Only purple matters. Janis panics at every purple light; fill the pool to win."""
    enter(s)
    setting(s, "party.lights", "dark")
    video_loop(s, "network_idle")
    audio_loop(s, "off", node="diorama_pi")
    janis(s, "threatening_*", cut=True)
    diorama_reactions(s, final=True)
    s.on("game.final_component_activated", lambda ev: janis(s, "panic_*"))
    s.every(30, lambda: message(s, "pump"))
    s.idle(20, lambda: janis(s, "threatening_*"), pattern="game.*")


@show.state(timeout="victory_time", then="passive")
def victory2(s):
    """The spirits are free: the jailbreak video, paradise in the diorama, the party lights."""
    enter(s)
    setting(s, "diorama.lights", "paradise")
    setting(s, "extractors.effect", "none")
    setting(s, "party.lights", "lit")
    audio_loop(s, "off", node="diorama_pi")
    audio_loop(s, "off", layer="bed")
    video_once(s, "puppet_escape_party", cut=True)
    video_loop(s, "idle")                       # the happy idle loop, once the video ends
    s.at(4, lambda: audio_once(s, "cage_unlocking"))

    @s.at(7)
    def liberation():
        audio_once(s, "victory_liberation")
        message(s, "afterword")


@show.state(timeout="kickout_time", then="passive")
def kickout(s):
    """Time ran out in the final: everything stops, then back to the start."""
    enter(s)
    setting(s, "party.lights", "dark")
    setting(s, "extractors.effect", "none")
    audio_loop(s, "off")
    audio_loop(s, "off", layer="bed")
    audio_loop(s, "off", node="diorama_pi")


# ── for the operator panel ─────────────────────────────────────────────────────
@show.action("Queue: closed")
def queue_closed(s):
    """The sign outside says Closed (until the next group starts)."""
    setting(s, "queue.sign", "closed")


@show.action("Queue: open")
def queue_open(s):
    setting(s, "queue.sign", "enter" if s.state == "passive" else "wait")

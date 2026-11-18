"""The show API: what a piece's show.py is written against.

    from haunt import Show

    show = Show(initial="idle", idle_reset=90)

    @show.state
    async def idle(s):
        s.leds("ring", effect="breathe", color=lambda p: p.idle_color)
        await s.wait("ring.button.press")
        return "play"

    @show.state(timeout=30, then="idle")
    async def play(s):
        ...

A state is a function. As an async function it can wait and sleep: it returns
the name of the next state, or nothing to stay put until something else moves
the show (a timeout, a handler, an operator). As a plain function it runs the
moment the show enters it, sets the room up and registers what it reacts to,
all before the event that moved the show has finished (so the rest of a burst
of events lands in the new state, in order):

    @show.state(timeout="round_time", then="passive")
    def round(s):
        s.leds("lamp", effect="solid", color="#ff0000")
        s.on("lamp.button.press", lambda ev: s.goto("won"))      # while in this state
        s.every(8, lambda: s.play("speaker", "keep_going.wav"))  # while in this state
        s.idle(12, lambda: s.play("speaker", "hello.wav"))       # after 12 s of quiet
        s.at(5, lambda: s.leds("lamp", effect="breathe"))        # 5 s in

Every state can have a timeout. `idle_reset` sends the show back to
the initial state when nobody has touched anything for that many seconds.
s.keep holds live objects (a game model) across reloads; s.load("x.yaml")
reads a piece's own file; s.pick("folder/name_*") picks media.

Output values may be functions of the params (`lambda p: p.idle_color`): they are
re-sent whenever a param changes, which is what makes live tuning work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Event:
    name: str                 # "<node>.<input>.<event>", e.g. "ring.button.press"
    value: Any = None
    t: float = field(default_factory=time.time)
    source: str = "node"      # "node", "operator", "sim", "show"

    @property
    def node(self) -> str:
        return self.name.split(".")[0]

    @property
    def input(self) -> str:
        parts = self.name.split(".")
        return parts[1] if len(parts) > 1 else ""

    @property
    def kind(self) -> str:
        return self.name.split(".")[-1]


@dataclass
class StateSpec:
    name: str
    fn: Callable
    timeout: float | None = None
    then: str | None = None
    doc: str = ""


@dataclass
class Handler:
    pattern: str
    fn: Callable


@dataclass
class Every:
    seconds: float
    fn: Callable
    states: tuple[str, ...] | None = None


@dataclass
class Action:
    label: str
    fn: Callable | None
    doc: str = ""


class Show:
    def __init__(self, initial: str | None = None, idle_reset: float | None = None) -> None:
        self.initial = initial
        self.idle_reset = idle_reset
        self.states: dict[str, StateSpec] = {}
        self.handlers: list[Handler] = []
        self.timers: list[Every] = []
        self.actions: dict[str, Action] = {}
        self.on_reset_fns: list[Callable] = []

    # -- states -------------------------------------------------------------
    def state(self, fn: Callable | None = None, *, timeout: float | str | None = None,
              then: str | None = None):
        """Declare a state. `timeout` may be a number or the name of a param."""

        def deco(f: Callable) -> Callable:
            spec = StateSpec(f.__name__, f, timeout, then, (f.__doc__ or "").strip())
            self.states[f.__name__] = spec
            if self.initial is None:
                self.initial = f.__name__
            return f

        return deco(fn) if fn is not None else deco

    # -- things that run in any state ---------------------------------------
    def on(self, pattern: str):
        """Run `async def handler(s, ev)` for every event matching pattern, in any state."""

        def deco(f: Callable) -> Callable:
            self.handlers.append(Handler(pattern, f))
            return f

        return deco

    def every(self, seconds: float | str, *, states: list[str] | None = None):
        """Run `async def tick(s)` every N seconds (or every <param> seconds)."""

        def deco(f: Callable) -> Callable:
            self.timers.append(Every(seconds, f, tuple(states) if states else None))
            return f

        return deco

    def action(self, label: str):
        """An operator button. `async def hint(s)` runs when it is pressed."""

        def deco(f: Callable) -> Callable:
            self.actions[label] = Action(label, f, (f.__doc__ or "").strip())
            return f

        return deco

    def on_reset(self, f: Callable) -> Callable:
        """Run `async def fresh(s)` on Reset and at startup: clear the show's variables here."""
        self.on_reset_fns.append(f)
        return f

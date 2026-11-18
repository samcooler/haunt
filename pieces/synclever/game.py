"""The levers' charge: the game under the story. Ported from Pulleys' arbiter
(src/arbiter_mesh/components.cpp), which was the version that ran the first install.

Five majors, one per game-1 colour, and every rope wearing a colour feeds it.
A pull adds charge_per_pull percent; charge drains at decay_rate percent per
second. Reaching 100 lights a component, and it stays lit until it has drained
all the way to 0, so one hovering near full does not flicker. While a round
shows its goals, a colour that is not a goal drains NONGOAL_DECAY times faster.

Purple is one shared pool: a pull on any purple rope fills it (by
final_charge_per_pull), and lighting it is the win (`all_final_components_activated`).

Pure logic with an injectable clock: show.py turns what it returns into events.
"""

from __future__ import annotations

import itertools
import random
import time
from typing import Callable

MAJORS = ("red", "blue", "green", "yellow", "orange")
PURPLE = "purple"
COLORS = MAJORS + (PURPLE,)
NONGOAL_DECAY = 3.0
EPS = 1e-9


class Component:
    __slots__ = ("charge", "stamp", "on")

    def __init__(self, now: float) -> None:
        self.charge, self.stamp, self.on = 0.0, now, False


class Game:
    def __init__(self, rates: Callable[[], tuple[float, float, float]],
                 clock: Callable[[], float] = time.time, rng: random.Random | None = None) -> None:
        """rates() -> (charge_per_pull, final_charge_per_pull, decay_rate), read live."""
        self.rates = rates
        self.clock = clock
        self.rng = rng or random.Random()
        now = clock()
        self.majors = {c: Component(now) for c in MAJORS}
        self.purple = Component(now)
        self.goals: tuple[str, ...] = ()
        self._visible = False         # a round is on: wrong colours drain faster
        self.resets = 0               # reset() calls so far: a pull that sees it change is spent
        self.last_rise: dict[str, float] = {}
        self.stray_at: dict[str, float] = {}
        self.purple_at = -1e9

    @property
    def goals_visible(self) -> bool:
        return self._visible

    @goals_visible.setter
    def goals_visible(self, on: bool) -> None:
        self._settle_all()   # charge so far drained at the old rate
        self._visible = on

    def _settle_all(self) -> None:
        now = self.clock()
        for c in [*self.majors.values(), self.purple]:
            self._settle(c, now)

    # -- the room's actions ------------------------------------------------------
    def reset(self) -> None:
        now = self.clock()
        for c in [*self.majors.values(), self.purple]:
            c.charge, c.stamp, c.on = 0.0, now, False
        self.last_rise.clear()
        self.stray_at.clear()
        self.resets += 1

    def deal(self, n: int) -> tuple[str, ...]:
        """n goal colours at random, never the same set twice running."""
        n = max(1, min(len(MAJORS), n))
        self._settle_all()
        choices = [tuple(c) for c in itertools.combinations(MAJORS, n)]
        fresh = [c for c in choices if set(c) != set(self.goals)] or choices
        self.goals = self.rng.choice(fresh)
        return self.goals

    def classify(self, color: str) -> str | None:
        if color == PURPLE:
            return "final"
        if color not in self.majors:
            return None
        return "goal" if color in self.goals else "nongoal"

    def pour(self, color: str) -> bool:
        """One pull's charge on a colour. True if that lit it."""
        cpp, fcpp, _ = self.rates()
        c = self.purple if color == PURPLE else self.majors[color]
        now = self.clock()
        self._settle(c, now)
        c.charge = min(100.0, c.charge + (fcpp if color == PURPLE else cpp))
        self.last_rise[color] = now
        if color == PURPLE:
            self.purple_at = now
        elif color not in self.goals:
            self.stray_at[color] = now
        if not c.on and c.charge >= 100.0:
            c.on = True
            return True
        return False

    # -- reading -----------------------------------------------------------------
    def rate(self, color: str) -> float:
        _, _, decay = self.rates()
        if color in self.majors and self.goals and self.goals_visible and color not in self.goals:
            return decay * NONGOAL_DECAY
        return decay

    def level(self, color: str) -> float:
        """0-100 now."""
        c = self.purple if color == PURPLE else self.majors[color]
        return max(0.0, c.charge - self.rate(color) * (self.clock() - c.stamp))

    def lit(self, color: str) -> bool:
        return (self.purple if color == PURPLE else self.majors[color]).on

    def lit_majors(self) -> set[str]:
        return {c for c, comp in self.majors.items() if comp.on}

    def simon_done(self) -> bool:
        return bool(self.goals) and self.lit_majors() == set(self.goals)

    def goal_active(self) -> bool:
        return any(self.majors[c].on for c in self.goals)

    def no_goal_charge(self) -> bool:
        return bool(self.goals) and all(self.level(c) <= EPS for c in self.goals)

    # -- time ----------------------------------------------------------------------
    def tick(self) -> list[tuple[str, str]]:
        """Components that have drained out since the last tick: [(event, colour)]."""
        now = self.clock()
        events = []
        for color, c in self.majors.items():
            if c.on:
                self._settle(c, now)
                if c.charge <= 0:
                    c.on = False
                    events.append(("component_deactivated", color))
        if events and not self.lit_majors():
            events.append(("all_components_deactivated", ""))
        if self.purple.on:
            self._settle(self.purple, now)
            if self.purple.charge <= 0:
                self.purple.on = False
                events.append(("final_component_deactivated", PURPLE))
        return events

    def _settle(self, c: Component, now: float) -> None:
        color = PURPLE if c is self.purple else next(k for k, v in self.majors.items() if v is c)
        if c.charge > 0:
            c.charge = max(0.0, c.charge - self.rate(color) * (now - c.stamp))
            if c.charge < EPS:
                c.charge = 0.0
        c.stamp = now

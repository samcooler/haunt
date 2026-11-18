"""What the Overlord draws over Janis: ported from Pulleys' overlord/goal_display.py.

Playtesting found most visitors never work out a round from Janis's reactions
alone, so a band runs across the top of the video:

    ROUND 1 of 3   GOAL [red] [blue]   CURRENT [blue|||] [green|]      1:42

  GOAL     the colours wanted, solid; outlined white once that one is lit.
  CURRENT  every colour with charge, most recently pulled first, filled to its
           charge; a colour that is not a goal glows red, brighter just after a pull.
  right    the round's time left.

And big words when the game moves: EXTRACTION SPEED INCREASED when a round is
won, TIME'S UP when one runs out. Through the party, a card with confetti
(MAXIMUM EXTRACTION ACHIEVED / PARTY! / CELEBRATE YOUR PROGRESS!) that waits
while a clip plays. From the first purple pull in the endgame, the machine's
alarm: EXTRACTION INTEGRITY ... FAILING over a purple bar that drains as the
purple pool fills, and PURPLE ACTIVITY DISRUPTS EXTRACTION on every purple pull.
In the countdown states the band holds the time left. In passive, MOVE ANY
LEVER TO BEGIN.

frame() returns the ASS for two overlays (band, banner) on a 1920x1080 canvas.
"""

from __future__ import annotations

import math
import random

from game import MAJORS, PURPLE, NONGOAL_DECAY  # noqa: F401  (beside this file)

RES_X, RES_Y = 1920, 1080
BAND_H = 170
BLOCK_W, BLOCK_H, GAP = 130, 104, 18
BLOCK_Y = (BAND_H - BLOCK_H) // 2
WRONG = "FF3B30"
WRONG_FLASH_S = 1.5
BANNER_S = 2.0
ROUND_WON = "EXTRACTION SPEED\\NINCREASED"
CARD_FADE_S = 0.6
CARD_WORDS = ("MAXIMUM\\NEXTRACTION\\NACHIEVED", "PARTY!", "CELEBRATE YOUR\\NPROGRESS!")
CARD_WORD_S = 4.0
CARD_WORD_FADE_S = 0.35
CLIP_MAX_S = 40.0
DISRUPT_FLASH_S = 1.8
PURPLE_RGB = "BF5AF2"
ALARM = "FF3B30"
CONFETTI_COLORS = ("FF3B30", "0A84FF", "34C759", "FFD60A", "FF9500", "BF5AF2", "FFFFFF", "FF2D95")

ROUNDS = ("easy_round", "medium_round", "hard_round")
START = "passive"
VICTORY_STATES = ("party",)
DISRUPT_STATES = ("party", "final_wait", "final")
COUNTDOWN_STATES = {"final_wait": "first_lever_time", "final": "final_time"}


def ass_color(rrggbb: str) -> str:
    rrggbb = rrggbb.lstrip("#")
    return f"&H{rrggbb[4:6]}{rrggbb[2:4]}{rrggbb[0:2]}&"


def ass_rect(x, y, w, h, color, alpha="00"):
    return (f"{{\\an7\\pos({x},{y})\\bord0\\shad0\\1c{ass_color(color)}\\1a&H{alpha}&\\p1}}"
            f"m 0 0 l {w} 0 {w} {h} 0 {h}{{\\p0}}")


def ass_text(x, y, text, size, color="FFFFFF", align=7):
    return (f"{{\\an{align}\\pos({x},{y})\\fs{size}\\bord2\\shad0\\b1"
            f"\\1c{ass_color(color)}\\3c&H000000&}}{text}")


def ass_box(x, y, w, h, color, bord, alpha="00", blur=0):
    return (f"{{\\an7\\pos({x},{y})\\bord{bord}\\shad0\\blur{blur}\\1a&HFF&"
            f"\\3c{ass_color(color)}\\3a&H{alpha}&\\p1}}"
            f"m 0 0 l {w} 0 {w} {h} 0 {h}{{\\p0}}")


def ass_banner(text, color, alpha="00", size=190):
    return (f"{{\\an5\\pos({RES_X // 2},{RES_Y // 2})\\fs{size}\\bord8\\shad4\\b1"
            f"\\1c{ass_color(color)}\\3c&H000000&\\1a&H{alpha}&\\3a&H{alpha}&}}{text}")


class Band:
    def __init__(self, game, colors: dict[str, str], param) -> None:
        self.game = game
        self.rgb = {k: v.lstrip("#") for k, v in colors.items()}
        self.param = param            # s.param: round_time and the countdowns, live
        self.state = None
        self.entered = self.prev_entered = game.clock()
        self.banner = None            # (text, colour, until, size)
        self.disrupted = False
        self.clip = False             # when a clip started, or False
        self.card_t0 = None
        rnd = random.Random(7)
        self.confetti = [(rnd.uniform(0, RES_X), rnd.uniform(-RES_Y, 0), rnd.uniform(160, 380),
                          rnd.uniform(20, 90), rnd.uniform(0.6, 2.2), rnd.uniform(0, 6.3),
                          rnd.choice(CONFETTI_COLORS), rnd.uniform(-400, 400), rnd.choice((0, 1)))
                         for _ in range(110)]

    # -- what happened ------------------------------------------------------------
    def on_state(self, new: str) -> None:
        now = self.game.clock()
        prev, self.state = self.state, new
        self.prev_entered, self.entered = self.entered, now
        if new not in DISRUPT_STATES or prev not in DISRUPT_STATES:
            self.disrupted = False
        self.banner = None
        self.card_t0 = None
        if prev not in ROUNDS or new == prev or new in VICTORY_STATES:
            return
        i = ROUNDS.index(prev)
        if i + 1 < len(ROUNDS) and new == ROUNDS[i + 1]:
            self.banner = (ROUND_WON, "34C759", now + BANNER_S, 150)
        elif new == START and now - self.prev_entered >= self.param.round_time - 2:
            self.banner = ("TIME'S UP", "FF9500", now + BANNER_S, 190)

    def on_purple_pull(self) -> None:
        if self.state in DISRUPT_STATES:
            self.disrupted = True

    def set_clip(self, playing: bool) -> None:
        self.clip = self.game.clock() if playing else False

    # -- drawing --------------------------------------------------------------------
    def frame(self) -> tuple[str, str]:
        now = self.game.clock()
        if self.disrupted and self.state in DISRUPT_STATES:
            band, banner = self._alarm_band(now), self._alarm_flash(now)
        else:
            banner = self._card(now) if self.state in VICTORY_STATES else self._banner(now)
            band = [] if banner else self._band(now)
        return "\n".join(band), "\n".join(banner)

    def busy(self) -> bool:
        """Whether frames are moving fast (confetti, the alarm): draw more often."""
        return self.state in VICTORY_STATES or self.disrupted

    def _left(self, now: float) -> float | None:
        param = COUNTDOWN_STATES.get(self.state)
        return max(0.0, self.param[param] - (now - self.entered)) if param else None

    def _clock(self, left: float, color: str = "FFFFFF") -> str:
        return ass_text(RES_X - 40, BAND_H // 2, f"{int(left) // 60}:{int(left) % 60:02d}", 70, color, align=6)

    def _band(self, now: float) -> list[str]:
        g, state = self.game, self.state
        left = self._left(now)
        if left is not None:
            return [ass_rect(0, 0, RES_X, BAND_H, "000000", alpha="50"), self._clock(left)]
        if state not in ROUNDS and state != START:
            return []
        lines = [ass_rect(0, 0, RES_X, BAND_H, "000000", alpha="50")]
        if state == START or not g.goals:
            lines.append(ass_text(RES_X // 2, BAND_H // 2, "MOVE ANY LEVER TO BEGIN", 78, align=5))
            return lines
        x = 40
        lines.append(ass_text(x, 22, "ROUND", 34, "8B949E"))
        lines.append(ass_text(x, 62, f"{ROUNDS.index(state) + 1} of {len(ROUNDS)}", 56))
        x += 210
        lines.append(ass_text(x, BAND_H // 2, "GOAL", 50, align=4))
        x += 160
        for c in g.goals:
            lines.append(ass_rect(x, BLOCK_Y, BLOCK_W, BLOCK_H, self.rgb.get(c, "FFFFFF")))
            if g.lit(c):
                lines.append(ass_box(x, BLOCK_Y, BLOCK_W, BLOCK_H, "FFFFFF", 7))
            x += BLOCK_W + GAP
        x += 50
        lines.append(ass_text(x, BAND_H // 2, "CURRENT", 50, align=4))
        x += 260
        active = [c for c in MAJORS if g.level(c) > 0.5]
        active.sort(key=lambda c: -g.last_rise.get(c, 0))
        room = (RES_X - 220) - x
        bw, gap = BLOCK_W, GAP
        if active and len(active) * BLOCK_W + (len(active) - 1) * GAP > room:
            gap = min(GAP, 10)
            bw = max(24, (room - (len(active) - 1) * gap) // len(active))
        for c in active:
            color = self.rgb.get(c, "FFFFFF")
            wrong = c not in g.goals
            if wrong:
                hot = now - g.stray_at.get(c, -1e9) < WRONG_FLASH_S
                pulse = (int(now * 4) % 2 == 0) if hot else False
                lines.append(ass_box(x, BLOCK_Y, bw, BLOCK_H, WRONG, 22 if hot else 14,
                                     "00" if hot and pulse else "20", blur=16))
            fill = int(BLOCK_H * min(g.level(c), 100) / 100)
            lines.append(ass_rect(x, BLOCK_Y, bw, BLOCK_H, "000000", alpha="30"))
            if fill:
                lines.append(ass_rect(x, BLOCK_Y + BLOCK_H - fill, bw, fill, color))
            lines.append(ass_box(x, BLOCK_Y, bw, BLOCK_H, WRONG if wrong else color, 6 if wrong else 4))
            x += bw + gap
        left = max(0.0, self.param.round_time - (now - self.entered))
        lines.append(self._clock(left, "FF3B30" if left <= 20 else "FFFFFF"))
        return lines

    def _banner(self, now: float) -> list[str]:
        if not self.banner:
            return []
        text, color, until, size = self.banner
        if now >= until:
            self.banner = None
            return []
        left = until - now
        alpha = "00" if left > 0.5 else f"{int((0.5 - left) / 0.5 * 255):02X}"
        h = 300 if "\\N" not in text else 440
        return [ass_rect(0, RES_Y // 2 - h // 2, RES_X, h, "000000", alpha="60"),
                ass_banner(text, color, alpha, size)]

    def _card_up(self, now: float) -> bool:
        clip = self.clip is not False and now - self.clip < CLIP_MAX_S
        return self.state in VICTORY_STATES and not clip and now - self.entered >= 1.0

    def _card(self, now: float) -> list[str]:
        if not self._card_up(now):
            self.card_t0 = None
            return []
        if self.card_t0 is None:
            self.card_t0 = now
        t = now - self.card_t0
        fade = min(1.0, t / CARD_FADE_S)
        a = f"{int((1 - fade) * 255):02X}"
        lines = [ass_rect(0, 0, RES_X, RES_Y, "1E0B3A", alpha=f"{max(0x18, int((1 - fade) * 255)):02X}")]
        turn, tt = divmod(t, CARD_WORD_S)
        word = CARD_WORDS[int(turn) % len(CARD_WORDS)]
        wfade = min(fade, tt / CARD_WORD_FADE_S, (CARD_WORD_S - tt) / CARD_WORD_FADE_S, 1.0)
        wa = f"{int((1 - max(0.0, wfade)) * 255):02X}"
        scale = 100 + 6 * math.sin(t * 4)
        lines.append(f"{{\\an5\\pos({RES_X // 2},{RES_Y // 2})\\fs190\\b1\\bord10\\shad8"
                     f"\\fscx{scale:.0f}\\fscy{scale:.0f}\\1c{ass_color('FFD60A')}\\3c{ass_color('7A3E00')}"
                     f"\\4c&H000000&\\1a&H{wa}&\\3a&H{wa}&\\4a&H80&}}{word}")
        for x0, y0, speed, sway, freq, phase, color, spin, shape in self.confetti:
            y = (y0 + speed * t) % (RES_Y + 120) - 60
            x = x0 + sway * math.sin(freq * t + phase)
            w, h = (14, 30) if shape else (22, 22)
            fx = abs(math.cos(freq * 1.7 * t + phase)) * 100
            lines.append(f"{{\\an5\\pos({x:.0f},{y:.0f})\\bord0\\shad0\\frz{(spin * t) % 360:.0f}"
                         f"\\fscx{fx:.0f}\\1c{ass_color(color)}\\1a&H{a}&\\p1}}"
                         f"m 0 0 l {w} 0 {w} {h} 0 {h}{{\\p0}}")
        return lines

    def _alarm_band(self, now: float) -> list[str]:
        g = self.game
        integrity = 0.0 if g.lit(PURPLE) else max(0.0, 1.0 - g.level(PURPLE) / 100)
        hot = now - g.purple_at < DISRUPT_FLASH_S
        blink = int(now * 3) % 2 == 0
        lines = [ass_rect(0, 0, RES_X, BAND_H, "1A0000", alpha="20"),
                 ass_box(0, 0, RES_X, BAND_H, ALARM, 10 if hot else 6,
                         "00" if (hot and int(now * 6) % 2 == 0) else "50"),
                 ass_text(40, 18, "EXTRACTION INTEGRITY", 58),
                 ass_text(RES_X - 40, 18, "FAILING", 58, ALARM if blink else "FFFFFF", align=9)]
        left = self._left(now)
        if left is not None:
            lines.append(ass_text(RES_X // 2, 14, f"{int(left) // 60}:{int(left) % 60:02d}", 64,
                                  ALARM if left <= 20 else "FFFFFF", align=8))
        x, y, w, h = 40, 92, RES_X - 80, 52
        lines.append(ass_rect(x, y, w, h, "000000", alpha="20"))
        if integrity > 0:
            lines.append(ass_rect(x, y, max(2, int(w * integrity)), h, PURPLE_RGB))
        lines.append(ass_box(x, y, w, h, "FFFFFF", 3))
        return lines

    def _alarm_flash(self, now: float) -> list[str]:
        t = now - self.game.purple_at
        if t >= DISRUPT_FLASH_S:
            return []
        a = "00" if t < DISRUPT_FLASH_S - 0.4 else f"{int((t - (DISRUPT_FLASH_S - 0.4)) / 0.4 * 255):02X}"
        jx, jy = random.randint(-8, 8), random.randint(-5, 5)
        cx, cy = RES_X // 2 + jx, RES_Y // 2 + jy
        text = "PURPLE ACTIVITY\\NDISRUPTS EXTRACTION"
        return [ass_rect(0, RES_Y // 2 - 170, RES_X, 340, "000000", alpha="50"),
                f"{{\\an5\\pos({cx + 9},{cy + 5})\\fs120\\b1\\bord0\\shad0"
                f"\\1c{ass_color(ALARM)}\\1a&H{'60' if a == '00' else a}&}}{text}",
                f"{{\\an5\\pos({cx},{cy})\\fs120\\b1\\bord6\\shad0"
                f"\\1c{ass_color(PURPLE_RGB)}\\3c&H000000&\\1a&H{a}&\\3a&H{a}&}}{text}"]

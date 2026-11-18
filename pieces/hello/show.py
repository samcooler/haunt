"""hello: press the lamp to wake it, pull the rope enough times to make it sing.

The smallest piece that uses every part of haunt: states with timeouts, params
bound to outputs (live tuning), a counter, an operator action, and a Pi with audio.
"""

from haunt import Show

show = Show(initial="idle", idle_reset=60)


@show.on_reset
def fresh(s):
    s.vars.pulls = 0


@show.state
async def idle(s):
    """Breathing slowly, waiting for a visitor to press the button."""
    s.vars.pulls = 0
    s.leds("lamp", effect="breathe", color=lambda p: p.idle_color,
           speed=lambda p: p.breathe_speed, brightness=lambda p: p.brightness * 0.5)
    await s.wait(["lamp.button.press", "lamp.pull.on"])
    return "awake"


@show.state(timeout="awake_timeout", then="idle")
async def awake(s):
    """Counting pulls. Each one makes the lamp brighter."""
    s.play("speaker", "wake.wav")
    while s.vars.pulls < s.param.pulls_to_win:
        level = s.vars.pulls / s.param.pulls_to_win
        s.leds("lamp", effect="solid", color=lambda p: p.awake_color,
               brightness=lambda p, level=level: p.brightness * (0.2 + 0.8 * level), fade=0.3)
        await s.wait("lamp.pull.on")
        s.vars.pulls += 1
        s.log(f"pull {s.vars.pulls}/{s.param.pulls_to_win}")
    return "win"


@show.state(timeout="celebrate_for", then="idle")
async def win(s):
    """The lamp sings."""
    s.play("speaker", "win.wav")
    s.leds("lamp", effect="rainbow", speed=1.0, brightness=lambda p: p.brightness)


@show.action("Hint")
async def hint(s):
    """Flash the lamp so a visitor notices it."""
    s.out("lamp", "leds", persist=False, effect="sparkle", color="#ffffff", fade=0)
    await s.sleep(1.5)
    s.restore("lamp")

# haunt: notes for Claude

Read DESIGN.md first. The principles there are the point of this repo, and Pulleys
(`../pulleys`) is the system they were learned from.

## Where changes go

- **Behaviour, rules, timing, colours, thresholds:** in the piece (`pieces/<name>/`), never in
  firmware or the runtime. A value someone might tune on site belongs in `params.yaml`; bind it as
  `lambda p: p.name` so it applies live.
- **Wiring** (pins, LED counts, which board is which): `devices.yaml`.
- **The runtime** (`haunt/`) stays piece-agnostic. If a piece needs a workaround, the runtime is
  probably missing a feature; add it generally and test it.
- **Firmware** changes only for a new kind of capability. Its behaviour must stay generic and come
  from config. Keep the protocol in `haunt/topics.py`, `firmware/src/main.cpp` and `haunt/node.py`
  in step.

## Seeing what a running show is doing

```sh
.venv/bin/haunt status            # state, nodes, params, recent log  ($HAUNT_URL or --hub)
.venv/bin/haunt tail [filter]     # live bus
curl -s $HAUNT_URL/api/snapshot   # everything, as JSON
ls ~/.haunt/<piece>/logs/         # one JSONL file per run: every bus message
```

Use these before guessing. To test a show change without hardware:
`.venv/bin/haunt run pieces/<name> --sim --mute`, then drive it with `haunt press` / `haunt goto`.

## Tests

`.venv/bin/pytest -q`. Engine behaviour is tested against `MemoryBus` (no broker). Every engine
guarantee in `haunt/engine.py`'s docstring has a test; keep it that way.

## Firmware

`cd firmware && ~/.platformio/penv/bin/pio run` builds all three chips. FastLED is deliberately not
used (it fixes the LED pin at compile time). Pin library versions in `platformio.ini`.

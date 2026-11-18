# haunt: design

A runtime for interactive, immersive art: Raspberry Pis, ESP32s and laptops on one
network, running a show that people walk into.

## What went wrong last time (Pulleys, first install)

- Updating nodes was hard: firmware over an ESP-NOW mesh, through a bridge, through the Overlord.
- Networks: venue and hotel Wi-Fi, the Overlord's own AP, a laptop that needed internet for Claude.
- Simple changes (a threshold, a colour, a rule) lived deep in C++, so each took a build and a flash.
- Tuning was slow for the same reason.
- The game flow broke when people did unexpected things, and there was no quick way to put it right.

## Principles

1. **Firmware is generic and boring.** An ESP32 reports what it senses and shows what it is told.
   Its pins, LED count, inputs and thresholds come from `devices.yaml` at runtime. Reflashing is
   for new *kinds* of capability, which should be rare, and it goes over plain HTTP OTA.
2. **All behaviour lives in one place: the piece folder on the hub.** It is Python and YAML,
   it hot-reloads on save, and a broken edit never takes the show down.
3. **Everything worth tuning is a param.** Params are sliders in the console, bound live to
   outputs, and tuned values survive restarts.
4. **The show cannot get stuck.** Every state can time out, an untouched show returns to the
   start (`idle_reset`), a crashed state falls back to the initial one, and operators can force
   any state, reset, or black out.
5. **We bring our own network.** The hub Pi is the access point; nothing depends on venue Wi-Fi.
6. **Everything is visible.** All traffic is on one MQTT bus, recorded per session, and readable
   by the console, the CLI and Claude alike.
7. **It works with no hardware.** Any node can be simulated, so a piece can be written and
   played on a laptop.

## Shape

```
 laptop (browser, Claude, editor)          optional uplink (phone on USB, 4G, Ethernet)
          │ Wi-Fi "haunt"                              │
          ▼                                            ▼
 ┌──────────────────────── hub Pi (hostname haunt) ─────────────────────┐
 │ Wi-Fi AP (NetworkManager shared mode: DHCP + NAT to any uplink)       │
 │ mosquitto :1883                                                       │
 │ haunt run <piece>: engine · web UI :8080 · recorder · media/firmware  │
 └───────────────────────────────────────────────────────────────────────┘
          │ MQTT over Wi-Fi
   ┌──────┴──────────┬──────────────────┐
 ESP32 nodes       Pi nodes             simulated nodes
 (firmware/)       (haunt node: audio)  (haunt run --sim)
```

## Pieces

A piece is a folder:

| File | What |
|---|---|
| `devices.yaml` | every node by name: its hardware id, LEDs, inputs, audio |
| `params.yaml` | every tunable value with its default and range |
| `show.py` | the states and rules, written against `haunt.Show` |
| `media/` | sounds (and later video) that nodes download from the hub |

`show.py` in brief (`pieces/hello/show.py` is the full example):

```python
show = Show(initial="idle", idle_reset=60)

@show.state
async def idle(s):
    s.leds("lamp", effect="breathe", color=lambda p: p.idle_color)   # re-sent when the param changes
    await s.wait("lamp.button.press")
    return "awake"

@show.state(timeout="awake_timeout", then="idle")                  # a number or a param name
async def awake(s): ...

@show.on("*.pull.on")        # runs in any state
@show.every(30)              # runs on a timer
@show.action("Hint")         # an operator button
@show.on_reset               # set up s.vars
```

Events are named `<node>.<input>.<event>`: `lamp.button.press`, `rope.pull.on`, `rope.pull.value`.

## Wire protocol

MQTT topics and payloads are listed in `haunt/topics.py`. In short:

- A node connects with a retained `haunt/n/<name>/status` and an MQTT will that marks it offline.
- An unassigned ESP32 uses its hardware id (`esp-a1b2c3`) as its name. The hub publishes
  `haunt/assign/<hwid>` (retained) with the node's name and config. The node stores both and reconnects
  under its name, so swapping a board is a one-line change in `devices.yaml`.
- Inputs: `haunt/n/<name>/in/<input>` `{"event": "press"}`. Outputs: `haunt/n/<name>/out/<output>`.
- Commands: `haunt/cmd/<verb>`, used by the panel, the CLI and Claude alike.
- When a node comes back online the hub re-sends its outputs, so a rebooted node recovers by itself.

## Decisions and why

| Decision | Why | Instead of |
|---|---|---|
| MQTT (mosquitto) | Mature; retained state and wills; good ESP32 libraries; the debugging tools already exist | Zenoh (younger), OSC (no state, no delivery), ESP-NOW (no IP, needs bridges) |
| Wi-Fi everywhere, hub is the AP | The AP is always available; IP gives us HTTP OTA and every standard tool | Mesh relaying |
| One generic firmware, config over MQTT | Tuning and wiring changes without a reflash | Per-role firmware builds |
| Adafruit NeoPixel | The LED pin is set at runtime (FastLED fixes it at compile time) | FastLED |
| Show logic in Python | Expressive and testable, edits apply live, and Claude reads and writes it well | YAML rule engines (outgrown fast), C++ |
| States as async functions | A visitor's journey reads top to bottom; timeouts and waits are one line | Callback state tables |
| Web UI generated from the piece | No UI work per piece; volunteers get big buttons, the artist gets everything | Hand-built panels |
| Each run recorded to JSONL | Every session can be read afterwards (and later replayed) | Scattered serial logs |

## Status

**Built and tested** (`pytest`, plus a live run with mosquitto and simulated nodes):
- the engine: states, timeouts, idle reset, crash fallback, hot reload, param bindings, recovering nodes;
- the web panel and console, the HTTP API and the CLI;
- the recorder, and the Python node agent (simulated nodes, Pi audio).

**Built, compiles for ESP32 / S3 / C3, not yet run on hardware:**
- the generic firmware: Wi-Fi, broker discovery, assignment, LEDs, inputs, OTA, and serial setup.

**Written, not yet run on a Pi:** `deploy/install.sh` (AP, mosquitto, services).

## Next

1. Run the firmware on real boards; measure input-to-LED latency over the AP.
2. Bring up a hub Pi from `deploy/install.sh`; check the AP with an uplink, and with 10+ ESP32s.
3. Replay: run a recorded session against an edited show (in the simulator, at speed).
4. Simulated visitors: scripted and random input generators, for soak tests before an install.
5. Port the Pulleys piece; it is the real test of the show API.
6. Presets: save and recall named sets of params, and commit tuned values back to `params.yaml`.
7. More node capabilities as pieces need them: LED frame streaming (DDP), video on Pi nodes,
   touch and IMU inputs, servos and relays.
8. A larger AP: a travel router as the AP, with the hub Pi as fallback, for pieces with many nodes.

## Open questions

- **Battery-powered nodes** (like the Pulleys travellers): Wi-Fi costs power. These may need
  ESP-NOW or BLE to a gateway node, kept behind the same topics.
- **Timing-critical light shows across nodes:** MQTT is fine for cues (tens of ms). Tightly synced
  animation across many strips probably wants DDP frames from the hub, or shared clocks.
- **Securing the bus:** it is open on a private AP, which is fine. On a shared venue network it
  would want auth.

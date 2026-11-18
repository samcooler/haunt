# synclever

The lever room, formerly Pulleys: ropes are levers, Janis is the boss on the big
screen, and the art spirits' souls are being extracted. The story and the flow
are at the top of [show.py](show.py). This is a port of Pulleys'
`game_state_machine/state_config.yaml` as it stood after the first install,
together with the arbiter's charge model ([game.py](game.py)) and the Overlord's
band, cards and captions ([band.py](band.py)).

## Files

| File | What |
|---|---|
| `show.py` | the states: rounds, party, endgame, and what each reacts to |
| `game.py` | the charge model: pulls fill, charge drains, goals, the purple pool |
| `band.py` | what is drawn over Janis: goal band, round banners, party card, alarm |
| `params.yaml` | every game knob, as tuned at the first install |
| `devices.yaml` | every node, each rope's colour, and the pins |
| `content.yaml` | colours, on-screen messages, TV and queue-sign text, volumes |
| `captions.yaml` | what Janis says in each clip |
| `piece.yaml` | where the media is |

## Try it

```sh
.venv/bin/haunt run pieces/synclever --sim
```

Open http://localhost:8080/console. Each rope has a `lever ↕` button that pulls it.
The other screens are http://localhost:8080/display/queue and `/display/television`.
Without the real media, the log reports each missing clip once.

## On site

1. **Hub:** the Overlord Pi. `sudo deploy/install.sh hub synclever overlord` makes
   it the hub, and also the `overlord` node (Janis's video full screen, the room's sound).
   The diorama's Pi: `sudo deploy/install.sh node diorama_pi`.
2. **Media:** sync the Drive into `/var/lib/haunt/media/synclever`. It uses the same
   folders as Pulleys: `audio_loops/ audio_once/ video_loops/ video_once/`, plus
   `diorama_pi/audio_loops/` for the diorama Pi's own sound.
3. **Boards:** flash each with haunt's firmware once (`firmware/`: `esp32s3` for the
   rope sensors, `esp32` for the extractors, the diorama and the party board).
   Each board appears in the console as unassigned. Put its id on the right node in
   `devices.yaml`, and its pins and LEDs follow.
4. **Ropes:** each rope's colour is `color:` in `devices.yaml`. Edit and save the
   file to re-colour a rope; it applies immediately.
5. **Screens:** the TV and the queue sign are any screen with a browser, pointed at
   `http://haunt.local:8080/display/television` and `/display/queue`.
6. **Tuning:** every knob is a slider in the console. To change the flow, edit
   `show.py`, and `haunt push pieces/synclever --watch` applies each save live.

## What changed from Pulleys

- **No mesh, no arbiter, no bridge.** Every board is on the hub's Wi-Fi, and the
  game runs on the hub in Python. OTA goes over plain HTTP.
- **Rope colours** live in `devices.yaml`, instead of the arbiter's touch screen and
  its NVS.
- **Purple is one pool.** The arbiter worked this way; the Python twin did not.
  Lighting the pool is the win.
- **Rope displays:** each rope shows its colour's charge as a level that drains on
  the board, and pulses while it is a goal. This replaces the per-channel patterns
  and the detection envelope.
- **Extractors:** the liquid is a plain fill, without the slosh. The ring and the
  strip still fill together.
- **Diorama:** the fluorescent flicker, the red stutter when the door opens, and the
  purple flash on the button are kept, and are now show code you can tune.
- **TV and queue sign** are browser pages. The LCD boards cannot run haunt.

## Gaps (not ported yet)

- The playtest system: simulated visitors, the tuner, the live director and the fun
  metrics. The recorder's JSONL logs hold every event for when it returns.
- The arbiter's touch UI for mapping ropes on site. Edit `devices.yaml` instead, or
  build a console page for it.
- Media sync from the Drive (rclone) is not in `deploy/install.sh`.
- The install script has not run on a Pi yet. This includes mpv drawing to the screen
  without a desktop (DRM).
- Nothing here has run on the real hardware yet. The rope pull detector is Pulleys'
  own code, unchanged, but under new firmware.

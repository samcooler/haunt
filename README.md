# haunt

A runtime for interactive, immersive art. Raspberry Pis, ESP32s and browsers share one
MQTT bus. A piece is a folder of Python and YAML that hot-reloads while the show runs.
See [DESIGN.md](DESIGN.md) for the why.

## Try it on a laptop (no hardware)

```sh
brew install mosquitto
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/haunt run pieces/hello --sim
```

Open http://localhost:8080/ for the operator panel, or http://localhost:8080/console
for the artist console. In the console, press `button` on the lamp to start, then
`on` under `pull` a few times. Edit `pieces/hello/show.py` or `params.yaml` while it
runs; changes apply when you save.

From another terminal:

```sh
.venv/bin/haunt status
.venv/bin/haunt tail
.venv/bin/haunt press lamp.button
.venv/bin/haunt set brightness 0.3
.venv/bin/haunt goto win
.venv/bin/haunt new pieces/mypiece
```

## A real installation

**Hub** (a Pi; it becomes the access point `haunt` / `hauntnet` and `haunt.local`):

```sh
git clone <this repo> ~/haunt && cd ~/haunt
sudo deploy/install.sh hub hello
```

**Satellite Pi** (audio): `sudo deploy/install.sh node`. Then add its hostname to `devices.yaml`.

**ESP32**: flash once over USB, then everything else is done from the hub:

```sh
cd firmware && pio run -e esp32 -t upload     # or esp32s3, esp32c3
```

The board joins `haunt` and finds the broker. Its id (e.g. `esp-a1b2c3`) shows up in
the console as unassigned; put `id: esp-a1b2c3` under a node in `devices.yaml`.
Over serial at 115200: `?` prints its id and state, `wifi <ssid> <pass>` and
`broker <host>` point it somewhere else (e.g. your laptop at home), and `forget`
resets it.

**Change the show on site:** edit the piece on your laptop and run
`haunt push pieces/hello --watch`; every save goes to the hub and applies live. Then
commit the changes.

**Firmware update** (rare): `haunt --hub http://haunt.local:8080 ota lamp firmware/.pio/build/esp32/firmware.bin`.

## Layout

| Path | What |
|---|---|
| `haunt/` | the runtime: engine, bus, web UI, node agent, CLI |
| `firmware/` | the one ESP32 firmware (PlatformIO) |
| `pieces/` | pieces; `hello` is the worked example |
| `deploy/` | Pi setup |
| `tests/` | `pytest` |

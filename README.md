# Project-FRED

A control stack for an **InMoov**-style humanoid running on a pair of Raspberry
Pis: servo control, an offline wake-word listener with a Claude-backed
conversational brain, camera face-tracking, a mobile-friendly web panel, an
animated chest display, and a microcontroller sensor node.

The robot this was built for is called FRED. **Nothing in the code depends on
that** — the name lives in a couple of config strings and the spoken wake word,
so call yours whatever you like.

> This is one person's working robot, not a product. It is published in the hope
> that the wiring notes, the failure modes documented in `SERVICE.md`, and the
> two-Pi architecture save someone else a few evenings. Expect to adapt it.

---

## What it does

| | |
|---|---|
| **Servos** | 6 channels via a PCA9685 — eyes (X/Y), jaw, neck rotate, head tilt (L/R and F/B). Soft limits, calibration mode, graceful relax. |
| **Voice in** | Always-on wake word ("Hey FRED") using **Vosk** — fully offline, on-device. No cloud listening. |
| **Brain** | Local regex commands answer instantly and for free; anything else falls through to **Claude**, which gets the same actions as tool definitions so it can actually *drive the robot*, not just talk. |
| **Voice out** | ALSA `aplay`, with a speech envelope published to the chest display so the animation's mouth matches the audio. |
| **Vision** | Camera Module 3 via `picamera2`, MJPEG stream to the panel, plus an OpenCV Haar-cascade face tracker on a PD loop that moves the eyes, neck and head tilt to hold a face centred. |
| **360° surround** | An Insta360 X5 on a mast above the head (UVC webcam mode) gives a full-circle view: Claude can *actually look* in any direction (`look_around` returns real photos), person/motion detection feeds the face tracker a bearing to anyone approaching from any angle, and a safety governor slows or stops the cart when someone is in its path. Mocks itself without the camera. See [`INSTA360.md`](INSTA360.md). |
| **Cart** | Host-side driver for the [FRED-Cart](https://github.com/nledevil/Project-FRED-Cart) hoverboard base: governed drive commands over USB serial with a command TTL, under the Pico's own failsafes. |
| **Web panel** | Flask on `:8080` — live servo sliders, camera view, conversation transcript, calibration mode, and an admin screen. |
| **Chest display** | A second Pi drives a 7" DSI panel with framebuffer animations (arc reactor, flux capacitor, animated face, voice HUD), switchable at runtime from the panel. |
| **Sensors** | A Raspberry Pi Pico reads two HC-SR04 ultrasonics and a PIR, does its own echo timing and event detection, and streams JSON to the robot. Claude can read them, so "is anyone there?" and "did someone walk by?" are answered from actual hardware. |
| **Networking** | Falls back to being its own WiFi access point when no known network is around, plus an always-on Bluetooth PAN between the two Pis. |

---

## Architecture

Two Pis, deliberately. The head runs everything that needs the camera, the
servos and the microphone; the chest Pi does nothing but drive its panel and
relay the sensor node. They are joined by a Bluetooth PAN with fixed addresses,
so the link survives any venue's DHCP — or the total absence of a network.

```
                    ┌──────────────────────────────────────────┐
                    │  HEAD PI  (Pi 4B, DietPi)                │
   servos ──I2C─────┤  PCA9685 · camera · mic · speaker        │
   camera ──CSI─────┤                                          │
                    │  web/app.py — Flask control panel :8080  │
                    │  inmoov/    — the robot's own package    │
                    └───────┬──────────────────────┬───────────┘
                            │                      │
              WiFi / own AP │                      │ Bluetooth PAN
              192.168.x.x   │                      │ 10.0.0.1 ⇄ 10.0.0.2
                            │                      │
                    ┌───────┴──────────┐   ┌───────┴───────────────────┐
                    │  phone / laptop  │   │  CHEST PI                 │
                    │  control panel   │   │  display_control.py :8081 │
                    └──────────────────┘   │  7" DSI panel animations  │
                                           │  sensor relay ────────────┼── USB
                                           └───────────────────────────┘      │
                                                                      ┌───────┴────────┐
                                                                      │  Pico sensor   │
                                                                      │  node: 2×      │
                                                                      │  HC-SR04 + PIR │
                                                                      └────────────────┘
```

The sensor node plugs into the **chest** Pi (that's where the sensors physically
are, and the head is on pan/tilt servos so a USB run into it would fatigue). The
chest Pi relays each reading to the head over the PAN, where the head is always
`10.0.0.1` — no address to chase.

---

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi 4B ×2 | Head and chest. DietPi (Debian 13 "trixie"), Python 3.13. |
| PCA9685 | 16-channel PWM servo driver, I²C address `0x40`, 50 Hz. |
| Servos ×6 | See the channel map below. |
| **5–6 V PSU** | Sized for servo stall current — several amps. **Not** the Pi's 5 V rail. |
| Camera Module 3 | Autofocus, on the head. |
| USB microphone | Any ALSA-visible mic; used by the Vosk listener. |
| USB speaker / DAC | Played through `aplay`. |
| 7" DSI touchscreen | On the chest Pi, 800×480, RGB565 framebuffer. |
| Insta360 X5 *(optional)* | On a mast above the head, USB-C to the head Pi in webcam mode. Bring-up in [`INSTA360.md`](INSTA360.md). |
| Raspberry Pi Pico | Sensor node. A plain Pico is fine — the firmware needs no WiFi. |
| 2× HC-SR04, 1× HC-SR501 | Ultrasonic distance and PIR motion. |

### Servo wiring (PCA9685)

| PCA9685 pin | Pi pin (physical) | Notes |
|---|---|---|
| VCC | 3.3 V (pin 1) | logic power, from the Pi |
| GND | GND (pin 6) | **must share ground with the servo supply** |
| SDA | GPIO2 / SDA (pin 3) | I²C data |
| SCL | GPIO3 / SCL (pin 5) | I²C clock |
| V+ | **external 5–6 V PSU** | servo power — never from the Pi |

> **Power warning.** Servos draw amps under load. Feed the PCA9685's `V+` screw
> terminal from a dedicated supply and tie its ground to the Pi's. Running servos
> off the Pi's 5 V rail will brown it out mid-move.

Default channel map — edit in `config/servos.json`:

| Channel | Servo | Motion |
|---|---|---|
| 0 | `eye_x` | eyes left / right |
| 1 | `eye_y` | eyes up / down |
| 2 | `jaw` | open / close |
| 3 | `neck` | rotate left / right |
| 4 | `head_tilt_lr` | tilt side to side |
| 5 | `head_tilt_fb` | nod forward / back |

### Sensor node wiring

Full pinout, flashing and the HC-SR501's jumper/pot gotchas are in
[`firmware/pico_sensor_node/README.md`](firmware/pico_sensor_node/README.md).

The short version: **buy the 3.3 V HC-SR04P** (one chip on the back), power it
from the Pico's 3V3 rail at pin 36, and ECHO goes straight to the GPIO. The
classic 5 V HC-SR04 needs a 1 kΩ/2 kΩ divider on every ECHO line, because the
Pico's GPIO is not 5 V tolerant — and on this build those dividers were the
worst source of trouble by a wide margin. Measured back to back: the divider
version ran 60–100% dropouts, the 3.3 V parts 0 out of 120 samples.

The PIR is the exception and stays on 5 V (VBUS), since it needs 4.5 V minimum
but outputs 3.3 V logic regardless.

---

## Getting started

### 1. System packages

The camera stack, OpenCV and numpy come from apt rather than pip, so the venv is
created with `--system-site-packages` to see them.

```bash
sudo apt install -y python3-picamera2 python3-opencv python3-numpy \
                    i2c-tools alsa-utils
```

Enable I²C — `dtparam=i2c_arm=on` in `/boot/firmware/config.txt`, `i2c-dev` in
`/etc/modules`, and your user in the `i2c`, `gpio`, `spi` and `video` groups.
**Both need a reboot to take effect.**

```bash
sudo reboot
i2cdetect -y 1          # after reboot: expect the PCA9685 at 0x40
```

### 2. Python environment

```bash
git clone git@github.com:nledevil/Project-FRED.git
cd Project-FRED
python3 -m venv --system-site-packages venv
./venv/bin/pip install flask requests anthropic vosk pyserial RPi.GPIO \
                       adafruit-circuitpython-servokit adafruit-circuitpython-pca9685 \
                       CairoSVG
```

Vosk needs a model. Download a small English one from
[alphacephei.com/vosk/models](https://alphacephei.com/vosk/models) and unpack it
into `models/` (which is gitignored — the models are large).

### 3. Secrets and configuration

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' > config/secrets.env
chmod 600 config/secrets.env
```

Both `config/secrets.env` and `config/settings.json` are **gitignored** —
settings are machine-local (device paths, tuned face-tracking gains, tokens), and
sane defaults live in `inmoov/settings.py`, so a fresh clone runs without them.

### 4. Run it

```bash
./venv/bin/python web/app.py            # panel on http://<pi-ip>:8080
```

With no hardware attached it starts in **mock mode** and prints intended moves
instead of touching I²C — so you can develop the whole control path on a laptop.

### 5. Install as services

`deploy/` holds the systemd units. Full walkthrough in
[`SERVICE.md`](SERVICE.md).

```bash
sudo cp deploy/inmoov.service /etc/systemd/system/
sudo systemctl enable --now inmoov
```

---

## Configuration

| File | Purpose | In git? |
|---|---|---|
| `config/servos.json` | Channel map, soft limits, rest angles, pulse widths | yes |
| `config/settings.json` | Operator preferences, device paths, tuned gains, tokens | **no** |
| `config/secrets.env` | `ANTHROPIC_API_KEY` | **no** |

`servos.json` per-servo keys: `channel`, `min_angle` / `max_angle` (safe limits,
every command is clamped to them), `rest_angle`, `actuation_range`,
`pulse_min_us` / `pulse_max_us`, `invert`, `description`.

> The shipped limits are **deliberately conservative placeholders**. Run
> `calibrate.py` against your own mechanism and tighten them before running at
> speed — a servo driving into a printed part will strip it.

---

## The subsystems

### Web control panel — `:8080`

Live slider per servo, camera view, conversation transcript, and sensor state.
**Calibration mode** unlocks full travel past the soft limits and adds
Set min / Set rest / Set max buttons plus Save, which writes back to
`servos.json`. It's the fastest way to dial in a fresh mechanism.

The **`/admin`** screen holds operator preferences: iris colour, camera defaults
(flip, focus mode, lens position), voice settings, the chest display's address
and animation, and the sensor-overlay toggle.

> No authentication, by design — it is a LAN tool for a single operator.
> **Don't forward port 8080.**

### Voice

`inmoov/listener.py` runs `arecord` into a Vosk recogniser and watches for the
wake word. `inmoov/brain.py` then tries `commands.match_local` first — plain
regex, instant and offline — and only calls Claude when that misses. The same
action registry is exposed to Claude as tools, so an open-ended request can
still move the robot rather than just producing text.

Without an API key the local commands still work; the AI half simply reports as
unavailable.

### Face tracking

A background thread pulls low-resolution grayscale frames from the camera's
`lores` stream (so MJPEG viewers are undisturbed), runs a Haar cascade, and
drives a PD controller on the eyes, neck and head tilt. Because the camera rides
the moving head it is a genuine feedback loop — gains are tunable live from the
panel and persist in `settings.json`.

### Chest display — `:8081`

`deploy/display/display_control.py` supervises the animation as a **child
process** and serves a small stdlib HTTP API, so switching looks is
"kill the child, spawn the next" (~100 ms) rather than editing a unit file.
Presets: arc reactor (cyan/copper), flux capacitor, animated face, voice HUD.

It also carries the **sensor relay** and can overlay a live sensor readout on
top of whichever animation is playing — toggled from the admin page.

### 360° surround vision

`inmoov/camera360.py` consumes the X5 as a plain UVC webcam delivering
stitched equirectangular frames, and dewarps any (yaw, pitch, fov) window into
a normal rectilinear image. `inmoov/surround.py` layers awareness on top —
cheap motion sectors over the whole circle, targeted Haar person/face
detection on the busy directions, and a merged track list with rough
distances. The face tracker takes its "someone is over there" bearing from
here (falling back to the ultrasonics), Claude gets `look_around` (real
images) and `scan_surroundings` (a spoken who's-where), and the cart's speed
is governed by what's in its path. The whole stack runs against a synthetic
mock scene when no camera is attached — `demo360.py` proves the chain end to
end with no hardware.

### Cart drive

`inmoov/cart.py` talks to the [FRED-Cart](https://github.com/nledevil/Project-FRED-Cart)
Pico over USB serial (`"<steer> <speed>"`, `x` = stop). Commands carry a TTL
and decay to a stop unless renewed; a 10 Hz keepalive sits under the Pico's
2 s failsafe; and the surround-vision governor rescales every command against
whoever is standing in the way. Claude may drive only if the admin panel's
**AI driving** toggle is on (off by default) — his `stop_cart` always works.

### Sensor node

The Pico does all the timing-critical work: echo measurement, median filtering,
approach/depart hysteresis with edge confirmation, and PIR debouncing with a
warm-up mute. It emits one JSON object per line over USB serial. Neither Pi ever
times a pulse — Linux scheduling jitter would wreck an HC-SR04 reading.

`firmware/pico_sensor_node/push.py` copies firmware to the board over
MicroPython's raw REPL, standing in for `mpremote` (the chest Pi has no pip).

### Networking

- **Hotspot fallback** — when no known WiFi is in range the head becomes its own
  access point so you can still reach the panel at a venue. See
  [`HOTSPOT.md`](HOTSPOT.md).
- **Bluetooth PAN** — a permanent link between the two Pis at `10.0.0.1` and
  `10.0.0.2`, independent of any WiFi. The installer takes the peer's Bluetooth
  address as an argument, so it works on your pair of adapters:

  ```bash
  sudo deploy/pan/install.sh head  <chest-bdaddr>
  sudo deploy/pan/install.sh chest <head-bdaddr>
  ```

---

## Development

Every hardware wrapper mocks itself out when its device is missing —
`ServoController` without `/dev/i2c-1`, `Camera` without libcamera, `Sound`
without ALSA, `Led` without GPIO. So the whole control path runs on an ordinary
machine:

```bash
./venv/bin/python demo.py               # scripted moves, no hardware needed
./venv/bin/python calibrate.py          # interactive limit-finding
./venv/bin/python -c "from inmoov.servo_controller import ServoController; \
                      c=ServoController(); c.set_angle('jaw', 45)"
```

Force it explicitly with `ServoController(mock=True)`.

## Repository layout

```
inmoov/                  the robot's Python package (servos, camera, voice, sensors)
web/                     Flask control panel — app.py, templates, static
config/                  servos.json (tracked); settings.json + secrets.env (not)
deploy/                  systemd units and installers
  display/               everything that runs on the chest Pi
  hotspot/               WiFi access-point fallback
  pan/                   Bluetooth PAN between the two Pis
firmware/pico_sensor_node/   MicroPython sensor node + its push tool
sounds/                  startup chime and effects
calibrate.py demo.py     bench tools
demo360.py               no-hardware bench test of the 360°/cart stack
```

## Documentation

| File | What's in it |
|---|---|
| [`SERVICE.md`](SERVICE.md) | Running as systemd services, and a long list of real failure modes with their fixes — the most useful file here if something is broken. |
| [`HOTSPOT.md`](HOTSPOT.md) | WiFi access-point fallback and the Bluetooth PAN. |
| [`INSTA360.md`](INSTA360.md) | The 360° surround camera: how it's consumed, the bring-up checklist for the X5, and the design limits. |
| [`firmware/pico_sensor_node/README.md`](firmware/pico_sensor_node/README.md) | Sensor node wiring, flashing, tuning and gotchas. |
| [`TODO.md`](TODO.md) | What's next. |

---

## Licence and credits

Licensed under the **[PolyForm Noncommercial License 1.0.0](LICENSE)** — free for
personal projects, hobby builds, research, education and non-profits;
commercial use is not granted.

Note that "noncommercial" means this is *source-available* rather than
open-source in the OSI sense, which specifically forbids restricting fields of
use. That is a deliberate choice, matching the spirit of the upstream project.

This builds on **[InMoov](https://inmoov.fr/)** by Gaël Langevin — the printable
humanoid this software drives. The physical designs are his and carry their own
licence (CC BY-NC), separate from this repository. If you are printing a robot,
start there.

Vosk, Adafruit's CircuitPython libraries, picamera2 and OpenCV each carry their
own licences.

# Project-FRED

A control stack for an **InMoov**-style humanoid: servo control, an offline
wake-word listener with a Claude-backed conversational brain, camera
face-tracking, a mobile-friendly web panel, an animated chest display, and a
microcontroller sensor node.

It runs either way — all on one Raspberry Pi, or with an x86 box as the brain and
the Pis reduced to device servers for the hardware bolted to them. Which one you
get is a couple of keys in `settings.json`, not a fork.

The robot this was built for is called FRED. **Nothing in the code depends on
that** — the name lives in a couple of config strings and the spoken wake word,
so call yours whatever you like.

> This is one person's working robot, not a product. It is published in the hope
> that the wiring notes, the failure modes documented in `SERVICE.md`, and the
> split-brain architecture save someone else a few evenings. Expect to adapt it.

---

## What it does

| | |
|---|---|
| **Servos** | 6 channels via a PCA9685 — eyes (X/Y), jaw, neck rotate, head tilt (L/R and F/B). Soft limits, calibration mode, graceful relax. |
| **Voice in** | Always-on wake word ("Hey FRED") using **Vosk** — fully offline, on-device. No cloud listening. |
| **Brain** | Local regex commands answer instantly and for free; anything else falls through to **Claude**, which gets the same actions as tool definitions so it can actually *drive the robot*, not just talk. |
| **Voice out** | ALSA `aplay`, with a speech envelope published to the chest display so the animation's mouth matches the audio. |
| **Vision** | Camera Module 3 via `picamera2`, a USB camera via V4L2, or another machine's MJPEG stream — one interface, three backends. Plus an OpenCV Haar-cascade face tracker on a PD loop that moves the eyes, neck and head tilt to hold a face centred. |
| **Web panel** | Flask on `:8080` — live servo sliders, camera view, conversation transcript, calibration mode, and an admin screen. |
| **Chest display** | A second Pi drives a 7" DSI panel with framebuffer animations (arc reactor, flux capacitor, animated face, voice HUD), switchable at runtime from the panel. |
| **Sensors** | A Raspberry Pi Pico reads two HC-SR04 ultrasonics and a PIR, does its own echo timing and event detection, and streams JSON to the robot. Claude can read them, so "is anyone there?" and "did someone walk by?" are answered from actual hardware. |
| **Drive base** | An optional [hoverboard cart](https://github.com/nledevil/Project-FRED-Cart) — two hub motors and a Pico on the chest Pi. Hold-to-drive joystick in the panel, plus drive actions Claude can call. Motion has to be continuously requested or it stops; see below. |
| **Networking** | A private wired robot LAN with the brain serving DHCP, so the machines find each other on any venue's network — or none. Falls back to being its own WiFi access point when no known network is around. |

---

## Architecture

One brain, two device servers. An x86 NUC runs everything that thinks — the
control panel, the voice stack, face tracking. The two Pis keep only the
hardware physically attached to them and expose it over the network: the head Pi
owns the I²C servo bus and the CSI camera, the chest Pi drives its panel and
relays the sensor node. A plain gigabit switch joins all three on a private
segment, with the NUC serving DHCP so the link survives any venue's network — or
the total absence of one.

```
                    ┌────────────────────────────────────────────────┐
   PanaCast ──USB───┤  NUC "fred"  (x86, Ubuntu) — THE BRAIN         │
   PowerConf ─USB───┤  web/app.py — Flask control panel      :8080   │
   (mic+speaker)    │  inmoov/    — voice, face tracking, brain      │
                    │  dnsmasq    — DHCP/DNS for the robot LAN       │
                    └──────┬──────────────────────────────┬──────────┘
                           │ WiFi 192.168.x.x             │ robot LAN
                           │ (internet, management)       │ 10.0.0.1
                   ┌───────┴──────────┐                   │
                   │  phone / laptop  │            ┌──────┴──────┐
                   │  control panel   │            │   SWITCH    │
                   └──────────────────┘            └──┬───────┬──┘
                                                      │       │
                  ┌───────────────────────────────────┘       └──────────────┐
                  │  HEAD PI  10.0.0.10                 CHEST PI  10.0.0.11  │
   servos ──I2C───┤  servo_server.py         :8082      display_control :8081│
   camera ──CSI───┤  camera_stream.py        :8081      7" DSI panel         │
                  │  (imx708 → MJPEG)                   sensor relay ────────┼─USB
                  └──────────────────────────────────────────────────────────┘   │
                                                                        ┌────────┴───────┐
                                                                        │  Pico sensor   │
                                                                        │  node: 2×      │
                                                                        │  HC-SR04 + PIR │
                                                                        └────────────────┘
```

**The NUC is `10.0.0.1`** — the address the head Pi used to hold under the old
Bluetooth PAN. That was deliberate: the chest Pi already posts every sensor
reading to `10.0.0.1:8080`, so moving the brain needed no change on the chest at
all.

The sensor node plugs into the **chest** Pi (that's where the sensors physically
are, and the head is on pan/tilt servos so a USB run into it would fatigue).

Nothing on the Pis decides anything. The head exposes servos as HTTP/JSON and
its camera as MJPEG; the NUC consumes both, and `inmoov/` cannot tell whether
the I²C bus is six inches away or on the far end of an ethernet cable — see
`RemoteServoController` and the `mjpeg` camera backend. The payoff is latency:
the head↔brain round trip went from 7.3 ms over Bluetooth to **0.36 ms** over
ethernet, which is the budget the face tracker's PD loop has to live in.

---

## Drive base (optional)

FRED can be bolted to a [hoverboard cart](https://github.com/nledevil/Project-FRED-Cart):
two hub motors on a mainboard running EFeru FOC firmware, commanded by a Pico that
also owns a PS2 controller for manual driving.

The Pico plugs into the **chest** Pi, for the same reason the sensor node does — the
cart is at the base, and the head is on pan/tilt servos. But the thing deciding where
to go (Claude, the web panel) runs on the brain, so drive intent crosses the robot LAN
to get there. That shapes the whole design:

**The safety layer lives on the chest Pi, not with the brain.**
`deploy/display/cart_driver.py` holds the only handle on the serial port, re-sends the
current command at 10 Hz, and zeroes it if it goes half a second without hearing a
fresh one. Any link between the deciding machine and the wheels is a link that can
fail: this was written when the two Pis were joined by a Bluetooth PAN, which dropped
outright once when the head's Bluetooth controller threw a hardware error. The wired
LAN that replaced it is far better, but it is still a cable, a switch and a NIC, and
the brain is now a separate machine again — one more hop, not fewer. If the deciding
end were the only thing keeping the cart going, a dead link would leave it rolling on
its last command until the Pico's own 2 s host-silence failsafe expired — about 3.3 m
at the firmware's speed cap. With the watchdog on the chest it is under a metre.

Everything else follows from that:

- **Motion must be continuously requested.** The panel's joystick posts while it is
  held; let go, close the tab, or lose WiFi and the cart stops. There is no "release"
  message to fail to arrive.
- **Claude gets `nudge()`, not "go".** `inmoov/cart.py` has no API for "start moving and
  return" — a programmatic move takes a duration, is capped at 5 s, and stops itself.
- **The PS2 controller always wins.** The firmware ignores host drive commands whenever
  a controller is connected, so picking it up takes the robot away from autonomous
  control mid-drive. The panel and FRED both say so rather than appearing to ignore you.

Off by default. Enable it in the admin panel (or `cart.enabled` in
`config/settings.json`) once the Pico is wired up; it reuses the chest Pi's address
from the `display` settings. Speeds are in the firmware's units — roughly 0.64 wheel
RPM each, capped at 300 by the Pico regardless of what is asked for.

Testable with no cart attached:

```bash
python3 deploy/display/tools/test_cart_driver.py   # driver vs a simulated Pico
python3 deploy/display/tools/test_cart_api.py      # the chest Pi's HTTP surface
./venv/bin/python deploy/display/tools/test_cart_head.py   # client + spoken actions
```

---

## Hardware

| Part | Notes |
|---|---|
| x86 mini PC | The brain. Ubuntu 26.04. Anything with a couple of cores and a NIC works. |
| Raspberry Pi 4B ×2 | Head and chest, now device servers only. DietPi (Debian 13 "trixie"). |
| Gigabit switch | Joins the NUC and both Pis on the robot LAN. Unmanaged is fine. |
| PCA9685 | 16-channel PWM servo driver, I²C address `0x40`, 50 Hz. On the head Pi. |
| Servos ×6 | See the channel map below. |
| **5–6 V PSU** | Sized for servo stall current — several amps. **Not** the Pi's 5 V rail. |
| Camera Module 3 | Autofocus, on the head Pi; streamed to the brain as MJPEG. |
| USB microphone | Any ALSA-visible mic. On the **brain**, since that is where voice runs. |
| USB speaker / DAC | Played through `aplay`, on the brain. A USB speakerphone covers both. |
| USB webcam | Optional second camera on the brain (e.g. a wide-angle one), via `v4l2`. |
| 7" DSI touchscreen | On the chest Pi, 800×480, RGB565 framebuffer. |
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

On a **Pi**, where picamera2/OpenCV/numpy come from apt:

```bash
git clone git@github.com:nledevil/Project-FRED.git
cd Project-FRED
python3 -m venv --system-site-packages venv
./venv/bin/pip install flask requests anthropic vosk pyserial RPi.GPIO \
                       adafruit-circuitpython-servokit adafruit-circuitpython-pca9685 \
                       CairoSVG
```

On the **brain** (x86) there is no picamera2 and no apt OpenCV worth inheriting,
so use a plain venv and pip wheels:

```bash
sudo apt install -y python3-venv alsa-utils opencv-data
python3 -m venv venv
./venv/bin/pip install flask requests anthropic vosk pyserial numpy CairoSVG \
                       opencv-contrib-python-headless
```

> **Use `opencv-contrib-python-headless`, not `opencv-python-headless`.** OpenCV
> 5 moved the legacy Haar cascade and HOG detectors out of `objdetect` and into
> contrib's `xobjdetect`, so the plain wheel has no `cv2.CascadeClassifier` and
> face tracking dies at construction. The contrib wheel exposes it at top level
> and needs no code change. (`objdetect` itself is still there — `FaceDetectorYN`
> and friends — so don't read this as "the module was removed".)
>
> `opencv-data` supplies the cascade XML at
> `/usr/share/opencv4/haarcascades/`, which is where `face_tracker.CASCADE_PATH`
> looks. The pip wheels ship no data files.

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

On the **brain** — the control panel:

```bash
sudo cp deploy/fred-panel.service /etc/systemd/system/
sudo systemctl enable --now fred-panel
```

On the **head Pi** — the two device servers, and *not* the panel (its
`inmoov.service` is the old one-Pi topology; leaving it running would fight the
brain for the camera and port 8080):

```bash
sudo systemctl disable --now inmoov            # the head is a device server now
sudo cp deploy/camera-stream.service deploy/servo-server.service /etc/systemd/system/
sudo systemctl enable --now camera-stream servo-server
```

> `servo-server.service` starts with `SERVO_MOVE_TO_REST=0`, so bringing it up
> never drives a servo — unlike `ServoController`'s own default, which sweeps
> everything to rest the moment it is constructed. That is the right default for
> something systemd may start at boot with nobody watching. It also honours
> `SERVO_LOCKED`, a comma-separated list of servos it will refuse to move for
> *any* caller, answering `423`. Use it when a mechanism is physically obstructed
> — enforced on the machine holding the I²C bus rather than trusted to every
> client.

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

`settings.json` keys that decide *where the hardware is* — these are what make
the same code run as a self-contained head Pi or as a brain driving one over the
network:

| Key | Effect |
|---|---|
| `camera.backend` | `auto`, `picamera2` (local CSI), `mjpeg` (a stream), `v4l2` (local USB) |
| `camera.source` | stream URL for `mjpeg`, device index or `/dev/videoN` for `v4l2` |
| `servo.remote_host` | empty = drive the local PCA9685; set = drive `servo_server.py` there |
| `servo.remote_port` | defaults to `8082` |

A head Pi with everything attached needs none of them. A brain wants, e.g.:

```json
{ "camera": { "backend": "mjpeg", "source": "http://10.0.0.10:8081/stream.mjpg" },
  "servo":  { "remote_host": "10.0.0.10" } }
```

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

A background thread pulls low-resolution grayscale frames from the camera (the
`lores` stream on a Pi, so MJPEG viewers are undisturbed; a once-decoded frame on
the other backends), runs a Haar cascade, and drives a PD controller on the eyes,
neck and head tilt. Because the camera rides the moving head it is a genuine
feedback loop — gains are tunable live from the panel and persist in
`settings.json`.

When the brain and the servos are on different machines the loop closes over the
network, so the link has to be quick. Two things keep it that way:
`servo_server.py` sets `disable_nagle_algorithm` (without it every reply costs
~40 ms to Nagle vs delayed-ACK, capping the link at ~24 Hz — below the frame
rate), and `RemoteServoController.set_angles()` sends several servos in one round
trip so `eye_x` and `eye_y` cannot land a request apart. Measured round trip on a
gigabit segment: **~2.4 ms**.

Feedback flows back the same way. Every reply carries the angle the controller
*actually applied* after clamping — ask for 999° and the cache ends up holding
the servo's real maximum — so `get_angle()` on the brain never drifts from the
hardware.

### Chest display — `:8081`

`deploy/display/display_control.py` supervises the animation as a **child
process** and serves a small stdlib HTTP API, so switching looks is
"kill the child, spawn the next" (~100 ms) rather than editing a unit file.
Presets: arc reactor (cyan/copper), flux capacitor, animated face, voice HUD.

It also carries the **sensor relay** and can overlay a live sensor readout on
top of whichever animation is playing — toggled from the admin page.

### Sensor node

The Pico does all the timing-critical work: echo measurement, median filtering,
approach/depart hysteresis with edge confirmation, and PIR debouncing with a
warm-up mute. It emits one JSON object per line over USB serial. Neither Pi ever
times a pulse — Linux scheduling jitter would wreck an HC-SR04 reading.

`firmware/pico_sensor_node/push.py` copies firmware to the board over
MicroPython's raw REPL, standing in for `mpremote` (the chest Pi has no pip).

### Networking

- **Robot LAN** — a private wired segment joining the brain and both Pis through
  a switch, independent of any venue's network:

  | Host | Address | Serves |
  |---|---|---|
  | NUC (brain) | `10.0.0.1` | panel `:8080`, DHCP + DNS |
  | head Pi | `10.0.0.10` | `camera_stream` `:8081`, `servo_server` `:8082` |
  | chest Pi | `10.0.0.11` | `display_control` `:8081` |

  The brain runs `dnsmasq` bound to the robot-LAN interface only, with
  reservations keyed on each Pi's `eth0` MAC so addresses never move. It hands
  out **no default route** on purpose — the Pis keep their own WiFi for internet
  and management, and a second default route would race the one on `wlan0`.

  DietPi ships `eth0` administratively down and static, so each Pi needs
  `allow-hotplug eth0` / `iface eth0 inet dhcp` in `/etc/network/interfaces`
  before it will so much as send a DHCPDISCOVER. A lit cable is not enough.

- **Hotspot fallback** — when no known WiFi is in range the head becomes its own
  access point so you can still reach it at a venue. See
  [`HOTSPOT.md`](HOTSPOT.md).
- **Bluetooth PAN (retired)** — the two Pis used to be joined at `10.0.0.1` and
  `10.0.0.2` over Bluetooth; `deploy/pan/` still installs it if you want the
  old two-Pi topology back. Note that `pan-server` on the head claims
  `10.0.0.1`, which now belongs to the brain — run both and the head will
  resolve the brain's address to its own loopback and never reach it. On the
  chest, `pan-check.timer` rebuilds the link every 60 s, so disable that too:

  ```bash
  sudo systemctl disable --now pan-server.service    # head
  sudo systemctl disable --now pan-client.service pan-check.timer   # chest
  ```

---

## Development

Every hardware wrapper mocks itself out when its device is missing —
`ServoController` without a reachable PCA9685, `Camera` with no working backend,
`Sound` without ALSA, `Led` without GPIO. So the whole control path runs on an
ordinary machine:

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
  camera.py              picamera2 / mjpeg / v4l2 backends behind one interface
  remote_servo.py        client for a head Pi running servo_server.py
web/                     Flask control panel — app.py, templates, static
config/                  servos.json (tracked); settings.json + secrets.env (not)
deploy/                  systemd units and installers
  servo_server.py        head Pi: exposes the PCA9685 as HTTP/JSON on :8082
  camera_stream.py       head Pi: exposes the CSI camera as MJPEG on :8081
  fred-panel.service     brain: the control panel (the x86/NUC unit)
  inmoov.service         head Pi: the control panel, for the old one-Pi topology
  display/               everything that runs on the chest Pi
  hotspot/               WiFi access-point fallback
  pan/                   Bluetooth PAN between the two Pis (retired — see Networking)
firmware/pico_sensor_node/   MicroPython sensor node + its push tool
sounds/                  startup chime and effects
calibrate.py demo.py     bench tools
```

## Documentation

| File | What's in it |
|---|---|
| [`SERVICE.md`](SERVICE.md) | Running as systemd services, and a long list of real failure modes with their fixes — the most useful file here if something is broken. |
| [`HOTSPOT.md`](HOTSPOT.md) | WiFi access-point fallback and the Bluetooth PAN. |
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

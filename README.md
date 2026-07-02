# InMoov on Raspberry Pi 4 (DietPi)

Servo + camera control for an InMoov robot head. Phase 1 controls the **eyes
(2 servos), jaw, and neck** via a PCA9685 16-channel PWM driver on I2C.

- Board: Raspberry Pi 4B, DietPi (Debian 13 "trixie"), Python 3.13
- Servo driver: **PCA9685** @ I2C address `0x40`
- Camera: Raspberry Pi Camera Module 3 (autofocus) via `picamera2` / `rpicam`

## Hardware wiring (PCA9685)

| PCA9685 pin | Pi pin (physical) | Notes |
|-------------|-------------------|-------|
| VCC         | 3.3V (pin 1)      | logic power (from the Pi) |
| GND         | GND (pin 6)       | **must share ground with the servo supply** |
| SDA         | GPIO2 / SDA (pin 3) | I2C data |
| SCL         | GPIO3 / SCL (pin 5) | I2C clock |
| V+          | **external 5–6V PSU** | servo power — do NOT power servos from the Pi |

**Power:** Servos draw amps under load. Feed the PCA9685 **V+** screw terminal
from a dedicated 5–6V supply sized for stall current (a few A for these servos).
Tie that supply's GND to the Pi GND. Powering servos from the Pi's 5V rail will
brown-out the Pi.

Default channel map (edit in `config/servos.json`):

| Channel | Servo  | Motion |
|---------|--------|--------|
| 0 | `eye_x` | eyes left/right |
| 1 | `eye_y` | eyes up/down |
| 2 | `jaw`   | open/close |
| 3 | `neck`  | rotate left/right |

## First boot with hardware

I2C is already enabled in `/boot/firmware/config.txt` (`dtparam=i2c_arm=on`) and
`i2c-dev` is in `/etc/modules`, but **it only goes live after a reboot** and the
`dietpi` user was just added to the `i2c`/`gpio`/`spi`/`video` groups.

```bash
sudo reboot
# after reboot, confirm the bus and find the PCA9685 (expect 0x40):
i2cdetect -y 1
```

## Software

Everything Python lives in a venv at `venv/` (system-site-packages, so the
apt-installed `picamera2`/`libcamera`/OpenCV are visible alongside the
pip-installed Adafruit libraries).

```bash
cd ~/inmoov

# WEB CONTROL PANEL — open http://<pi-ip>:8080 from a phone/laptop on the LAN:
./venv/bin/python web/app.py

# no hardware needed — auto-runs in MOCK mode, printing intended moves:
./venv/bin/python demo.py

# calibrate limits once hardware is wired (see the file's docstring):
./venv/bin/python calibrate.py

# use in your own scripts:
./venv/bin/python -c "from inmoov.servo_controller import ServoController; \
c=ServoController(); c.set_angle('jaw', 45)"
```

### Web control panel

`./venv/bin/python web/app.py` serves a mobile-friendly panel on port 8080:

- A live slider per servo (Rest all / Relax all buttons).
- **Calibration mode** toggle: unlocks the full 0–`actuation_range` travel
  (bypassing the *safe* soft limits), adds Set min / Set rest / Set max buttons
  to record the current angle, and a Save config button that writes the new
  limits back to `config/servos.json`. This is the fastest way to dial in a
  fresh mechanism from the bench.

Flask's built-in server is fine for a single operator on a trusted LAN. It has
no authentication — don't expose port 8080 to the internet.

### Mock mode

`ServoController` auto-detects the absence of `/dev/i2c-1` and runs in **mock
mode**, printing every intended move instead of touching hardware. Force it
with `ServoController(mock=True)`. This lets all control logic be developed and
tested before (and independently of) the physical robot.

## `config/servos.json` schema

Per servo:

- `channel` — PCA9685 output (0–15)
- `min_angle` / `max_angle` — **safe** software limits; all moves are clamped here
- `rest_angle` — startup / idle position
- `actuation_range` — servo's full travel in degrees (usually 180)
- `pulse_min_us` / `pulse_max_us` — pulse width at 0° / full travel (typ. 500 / 2500)
- `invert` — `true` if the servo is mounted so angles run backwards
- `description` — human note

The `i2c.address` is stored as a decimal (`64` = `0x40`) because JSON has no hex.

> The shipped limits are **deliberately conservative starting points**. Run
> `calibrate.py` and tighten them to your actual mechanism before running at speed.

## Safety notes

- Always `calibrate.py` a fresh mechanism before trusting angles.
- The controller clamps every commanded angle to `[min_angle, max_angle]`.
- `ServoController` used as a context manager relaxes (de-energises) all servos
  on exit; call `.relax()` yourself to stop holding torque / cool motors.

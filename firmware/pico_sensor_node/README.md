# FRED stomach sensor node (Raspberry Pi Pico W)

A **smart sensor node**: the Pico W reads the HC-SR04 ultrasonic + PIR motion
sensors, does the echo timing / filtering / event detection itself, and pushes
clean JSON to the InMoov app. The Pi does no GPIO and no pulse timing — the whole
point is to offload the timing-critical work to a real microcontroller (an RP2040
hardware-timed `time_pulse_us` is accurate; a Linux GPIO read on the Pi jitters).

- **Primary transport:** WiFi → `POST http://<pi>:8080/api/sensors/ingest`
- **Fallback (no network):** the same JSON printed one-object-per-line over USB
  serial; the Pi reads `/dev/ttyACM0` (see below).

Firmware: [`main.py`](main.py) (MicroPython). Pi side: `inmoov/sensors.py`.

## Wiring

**⚠ The Pico's GPIO is 3.3 V and NOT 5 V-tolerant.** The HC-SR04 runs at 5 V and
its ECHO pin outputs 5 V — that **must** go through a divider before the Pico, or
you'll damage the pin.

```
HC-SR04                          Pico W
  VCC  ───────────────────────── VBUS (pin 40, 5V)
  GND  ───────────────────────── GND
  TRIG ───────────────────────── GP3        (3.3V drive is fine for TRIG)
  ECHO ──┬──[ 1kΩ ]──┬────────── GP2        (divider: 5V·2k/(1k+2k) = 3.3V)
         │           │
      (to ECHO)   [ 2kΩ ]
                     │
                    GND

PIR HC-SR501
  VCC ─── VBUS (5V)   GND ─── GND   OUT ─── GP4   (3.3V output, safe direct)
```

Pins are set at the top of `main.py` (`TRIG_PIN=3, ECHO_PIN=2, PIR_PIN=4`); change
to taste. A single Pico can read several HC-SR04s + PIRs — add more pins and more
entries to the `readings`/`events` the sketch builds.

## Flash + install

1. Hold BOOTSEL, plug the Pico W into USB, drop the **MicroPython (Pico W) UF2**
   onto the `RPI-RP2` drive (get it from micropython.org/download/RPI_PICO_W).
2. Copy the firmware as `main.py` so it runs on boot:
   - **Thonny:** open `main.py`, *File → Save as → Raspberry Pi Pico → `main.py`*, or
   - **mpremote:** `mpremote cp main.py :main.py`
3. Edit the `CONFIG` block in `main.py`: `WIFI_SSID`, `WIFI_PASS`, `PI_HOST` (the
   Pi's LAN IP), and `TOKEN` (must match `settings.json` → `sensors.token`, or
   leave both empty). Tune `NEAR_CM` (approach distance) etc.

The onboard LED is **solid** when streaming over WiFi, **blinking** when it has
fallen back to USB serial.

## Enabling the serial fallback on the Pi

WiFi needs nothing extra — the `/api/sensors/ingest` endpoint is always on. For
the **USB-serial** fallback, plug the Pico into the Pi and set in
`config/settings.json`:

```json
"sensors": { "serial_enabled": true, "serial_port": "/dev/ttyACM0" }
```

then `sudo systemctl restart inmoov`. (`pyserial` is already in the venv.) The app
reads JSON lines off that port into the same sensor hub. Find the port with
`ls /dev/ttyACM*` after plugging the Pico in.

## Verifying

```bash
# what the Pico would send (simulate without hardware):
curl -sX POST http://localhost:8080/api/sensors/ingest -H 'Content-Type: application/json' \
  -d '{"node":"stomach","uptime_ms":1000,"readings":{"dist_center":{"type":"distance","cm":80}},
       "events":[{"sensor":"dist_center","event":"approach","cm":80}]}'

curl -s http://localhost:8080/api/sensors            # latest node state + online flags
curl -s "http://localhost:8080/api/log?since=0"      # "Someone approached the stomach (80 cm)."
```

`token`: if `sensors.token` is set, send it as the `X-Sensor-Token` header (the
firmware does this automatically) — LAN-only regardless.

## Message format

The node sends (all fields optional except `node`):

```json
{
  "node": "stomach",
  "uptime_ms": 45000,
  "readings": { "dist_center": {"type":"distance","cm":95.0},
                "pir_center":  {"type":"motion","active":true} },
  "events":   [ {"sensor":"dist_center","event":"approach","cm":95.0} ]
}
```

`readings` = current value of each sensor (sent every ~1 s heartbeat); `events` =
edges the node decided are worth reporting (`approach`/`depart`,
`motion_start`/`motion_stop`), sent the instant they happen.

## Where it plugs in on the Pi

`SensorHub` (in `inmoov/sensors.py`) keeps the latest reading per node, marks a
node **offline** after `sensors.offline_after` seconds of silence, and logs edge
events to the transcript. The `on_event` hook in `web/app.py` is the attach point
for behaviours — e.g. fire the auto-greeting (TODO Priority 2) on `approach`, or
wake/turn toward `motion_start`.

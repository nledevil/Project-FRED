# FRED stomach sensor node (Raspberry Pi Pico / Pico W)

A **smart sensor node**: a Pico reads two HC-SR04 ultrasonics + an HC-SR501 PIR,
does the echo timing / filtering / event detection itself, and prints clean JSON
to USB serial. Neither Pi does GPIO or pulse timing — the whole point is to
offload the timing-critical work to a real microcontroller (an RP2040
hardware-timed `time_pulse_us` is accurate; a Linux GPIO read jitters).

Firmware: [`main.py`](main.py) (MicroPython). Pi side: `inmoov/sensors.py`.

## How it gets to the head

The Pico lives in the chest and plugs into the **chest Pi**, not the head:

```
  Pico ──USB serial──> chest Pi ──HTTP over Bluetooth PAN──> head Pi
                    (sensor_relay in          (10.0.0.1:8080
                     display_control.py)       /api/sensors/ingest)
```

The head is *always* `10.0.0.1` on `pan0`, so there is no DHCP lease to chase,
no mDNS, and no WiFi credentials on the node — it works at a venue with no
network at all. The relay rides inside `display_control.py` (see
`deploy/display/`) because that's already the supervised always-on process on
the chest Pi.

Two things follow from this that are easy to forget:

- **The head's `sensors.serial_enabled` stays off.** `SerialSensorReader` opens
  a *local* device path; it can't see the chest Pi's tty. That setting is for a
  node plugged into the head itself.
- **The node has no WiFi code**, so this firmware runs unmodified on a plain
  Pico *or* a Pico W. Flash the **`RPI_PICO`** UF2 either way — the `RPI_PICO_W`
  build won't boot on a non-W board, and the W gains nothing here.

## Wiring

**⚠ The Pico's GPIO is 3.3 V and NOT 5 V-tolerant.** The HC-SR04 runs at 5 V and
its ECHO pin outputs 5 V — that **must** go through a divider, one per sensor.
TRIG is an input to the sensor and drives fine from 3.3 V; the HC-SR501's output
is 3.3 V logic even on a 5 V supply, so it goes straight in.

| Signal | GP | Physical pin |
|---|---|---|
| Ultrasonic **A** TRIG | GP3 | 5 |
| Ultrasonic **A** ECHO | GP2 | 4 (via divider) |
| Ultrasonic **B** TRIG | GP7 | 10 |
| Ultrasonic **B** ECHO | GP6 | 9 (via divider) |
| PIR OUT | GP4 | 6 |
| 5 V for all three sensors | VBUS | 40 |
| Ground | GND | 3 / 8 / 38 |

```
HC-SR04 (x2)                     Pico
  VCC  ───────────────────────── VBUS (pin 40, 5V)
  GND  ───────────────────────── GND
  TRIG ───────────────────────── GP3  / GP7
  ECHO ──┬──[ 1kΩ ]──┬────────── GP2  / GP6
         │           │            (divider: 5V·2k/(1k+2k) = 3.33V)
      (to ECHO)   [ 2kΩ ]
                     │
                    GND

PIR HC-SR501
  VCC ─── VBUS (5V)   GND ─── GND   OUT ─── GP4
```

If 2 kΩ isn't in your kit, 1.8k/3.3k gives 3.23 V, or use two 1 kΩ in series for
the lower leg. **Avoid 1k/2.2k** (3.44 V) — that sits above the Pico's rail and
leaks into the clamp diode. Build the dividers at the *Pico* end so the long run
carries the already-divided signal, and ohm them out before connecting anything:
tap-to-ground should read ~2 kΩ. A swapped pair reads 1.67 V, which the Pico
sees as ambiguous — garbage distances rather than an obvious failure.

Pins are set in `ULTRASONICS` / `PIR_PIN` at the top of `main.py`. A single Pico
can read more of both; add entries and the payload grows to match.

### HC-SR501 gotchas

- **Check the silkscreen.** Clones are *not* consistent — some are VCC/OUT/GND,
  others GND/OUT/VCC. The labels are under the white dome (it's a friction fit).
  Reversing VCC and GND kills the module.
- **Set the jumper to H** (repeat trigger). On L it fires one fixed-length pulse
  and ignores continued motion, which breaks `motion_start`/`motion_stop`.
- **Turn the time-delay pot to minimum**, or `motion_stop` arrives minutes late.
- It needs ~a minute to settle after power-up and throws false positives while it
  does. The firmware mutes it for `PIR_WARMUP_MS` and flags the reading
  `"warming": true` so the head doesn't fire a phantom greeting on every reboot.

### Why the two ultrasonics don't fire together

Both burst at 40 kHz, so a simultaneous pair means sensor B hears A's ping and
reports a phantom short distance. `main.py` fires them sequentially with
`PING_GAP_MS` of silence between. Aim them apart as well — overlapping cones make
crosstalk worse even when the timing is right.

## Flash + install

1. Hold BOOTSEL, plug the Pico into the chest Pi. It appears as USB `2e8a:0003`
   and a `RPI-RP2` mass-storage volume.
2. Drop the **`RPI_PICO`** UF2 on it (from micropython.org/download/RPI_PICO):
   ```bash
   sudo mount /dev/sda1 /mnt/rp2 && sudo cp RPI_PICO-*.uf2 /mnt/rp2/
   ```
   The board reboots mid-write — `cp` and `umount` reporting an error there is
   normal. It comes back as `2e8a:0005 MicroPython Board in FS mode`.
3. Copy the firmware as `main.py` so it runs on boot:
   ```bash
   sudo systemctl stop inmoov-display          # the relay holds the port
   sudo python3 push.py main.py
   sudo systemctl start inmoov-display
   ```

[`push.py`](push.py) stands in for `mpremote cp`, which isn't available on the
chest Pi — it has no pip and no ensurepip, and keeping that box stdlib-only is
deliberate. It drives MicroPython's raw REPL directly, verifies the written size,
and soft-resets the board so `main.py` starts.

## Verifying

```bash
# 1. Is the node talking? (on the chest Pi, with the relay stopped)
P=$(ls /dev/serial/by-id/*MicroPython*); sudo stty -F "$P" 115200 raw -echo
sudo timeout 4 cat "$P"

# 2. Is the relay healthy? Tells "Pico unplugged" from "head unreachable".
curl -s http://10.0.0.2:8081/api/sensors

# 3. Did it land on the head?
curl -s http://localhost:8080/api/sensors        # node state + online flags
curl -s "http://localhost:8080/api/log?since=0"  # "Someone approached the stomach (80 cm)."
```

With nothing wired, both distances read `400.0` — that's correct, not a fault: no
echo returned means nothing is in range, which the firmware reports as `MAX_CM`
so that a depart event can fire when someone walks out of the cone.

You can also simulate a node without any hardware:

```bash
curl -sX POST http://localhost:8080/api/sensors/ingest -H 'Content-Type: application/json' \
  -d '{"node":"stomach","uptime_ms":1000,"readings":{"dist_left":{"type":"distance","cm":80}},
       "events":[{"sensor":"dist_left","event":"approach","cm":80}]}'
```

`token`: if `sensors.token` is set on the head, the relay must send it too — set
`Environment=SENSOR_TOKEN=...` in `inmoov-display.service`. LAN-only regardless.

## Message format

The node sends (all fields optional except `node`):

```json
{
  "node": "stomach",
  "uptime_ms": 45000,
  "readings": { "dist_left":  {"type":"distance","cm":95.0},
                "dist_right": {"type":"distance","cm":400.0},
                "pir_center": {"type":"motion","active":true} },
  "events":   [ {"sensor":"dist_left","event":"approach","cm":95.0} ]
}
```

`readings` = current value of each sensor (sent every ~1 s heartbeat); `events` =
edges the node decided are worth reporting (`approach`/`depart`,
`motion_start`/`motion_stop`), sent the instant they happen. The relay adds
`"transport": "serial-relay"` on the way through so the head's panel shows the
real path rather than reporting it as WiFi.

Distances are a rolling **median** of the last `MEDIAN_N` pings, not a mean:
ultrasonics drop the occasional wild outlier, and an average smears it across the
following readings instead of discarding it. Approach/depart use hysteresis
(`NEAR_CM` in, `NEAR_CM + HYST_CM` back out) so a reading hovering at the
threshold doesn't chatter.

## Where it plugs in on the Pi

`SensorHub` (in `inmoov/sensors.py`) keeps the latest reading per node, marks a
node **offline** after `sensors.offline_after` seconds of silence, and logs edge
events to the transcript. The `on_event` hook in `web/app.py` is the attach point
for behaviours — e.g. fire the auto-greeting (TODO Priority 2) on `approach`, or
wake/turn toward `motion_start`.

## Notes / gotchas

- **Unconnected inputs are pulled down on purpose.** A bare `Pin.IN` floats, and
  `time_pulse_us` waiting for a rising edge will latch onto coupled noise — a
  hand near the board is enough. An ECHO lead that was never wired (or that
  falls off in service) would then invent distances and fire phantom approach
  events. `PULL_DOWN` makes a missing sensor time out into `MAX_CM`, and a
  missing PIR read "no motion", which is the honest answer in both cases. The
  internal pull (~60kΩ) is far weaker than the divider's 2kΩ leg, so it moves a
  real reading by hundredths of a volt.
- **Events fired while the relay is down are lost.** The stream is
  fire-and-forget: `readings` are re-sent every heartbeat so the head's *state*
  recovers on its own, but a discrete `approach`/`motion_start` that happened
  during a restart is simply gone. Behaviours that must not miss an arrival
  should look at the reading, not only the edge.
- **Both ultrasonics log identically.** `SensorHub._describe` doesn't name the
  sensor for approach/depart, so "Someone approached the stomach" could be
  either one. Harmless in the transcript, confusing when debugging — check
  `/api/sensors` for the per-sensor distances.
- **Ctrl-C on the port drops the node to the REPL** and the sensors stop until
  it's reset. That's intentional — it's how `push.py` breaks in to update the
  firmware. It's safe in service because the relay opens the tty **read-only**
  and so physically cannot send one. Don't attach `minicom`/`screen` to a live
  node and expect it to keep streaming.
- **Never set 1200 baud on this port.** On an RP2040 that's the magic touch that
  reboots the board into its UF2 bootloader; you'd be reflashing MicroPython
  instead of reading it.
- The node runs a `machine.WDT(timeout=8000)`, so a wedged loop resets it rather
  than going quiet. The relay reconnects on its own, and the head's
  `offline_after` catches whatever gap is left.

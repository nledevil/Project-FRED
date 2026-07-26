# FRED stomach sensor node — Raspberry Pi Pico / Pico W (MicroPython)
#
# A "smart sensor node": it reads two HC-SR04 ultrasonics + an HC-SR501 PIR,
# does the echo timing / filtering / event detection itself, and prints clean
# JSON — one object per line — to USB serial. An RP2040 hardware-timed
# measurement (time_pulse_us) is far more accurate than a jittery Linux GPIO
# read, which is the whole point of offloading it here.
#
# TRANSPORT: serial only, deliberately. The Pico lives in the chest and plugs
# into the *chest* Pi, which runs display_control.py; that app's sensor_relay
# reads this stream and POSTs it to the head at 10.0.0.1:8080 over the Bluetooth
# PAN. The head is always 10.0.0.1 there — no DHCP lease to chase, no WiFi
# credentials on the node, and it keeps working at a venue with no network at
# all. With no WiFi here, this runs unmodified on a plain Pico or a Pico W.
#
# WIRING — use the 3.3V ultrasonics (HC-SR04P / RCWL-1601, one chip on the back):
#   HC-SR04P x2  VCC -> 3V3 OUT (pin 36, NOT VBUS)   GND -> GND
#                TRIG -> GP3 / GP7
#                ECHO -> GP2 / GP6      (direct — no divider needed at 3.3V)
#   PIR HC-SR501 VCC -> VBUS (pin 40, 5V)   GND -> GND   OUT -> GP4
#                (the PIR needs 4.5V+, but its output is 3.3V logic either way)
#
#   With classic 5V HC-SR04s instead: power them from VBUS and put a 1k/2k
#   divider on each ECHO — the Pico's GPIO is NOT 5V tolerant. See the README;
#   those dividers were by far the least reliable part of this build.
#
#   Set the PIR's jumper to H (repeat trigger) and its time-delay pot to minimum,
#   or "motion_stop" arrives minutes late. Its output is meaningless for the
#   first minute after power-up — see PIR_WARMUP_MS.
#
# SETUP: flash MicroPython (RPI_PICO UF2) then copy this file as main.py:
#   mpremote connect <port> cp main.py :main.py

import json
import time

import machine

# ---- CONFIG (edit these) --------------------------------------------------
NODE_ID = "stomach"

# (reading name, TRIG gpio, ECHO gpio). Rename these to match where you
# actually aimed them — the name is what shows up in the head's panel.
ULTRASONICS = (
    ("dist_left",  3, 2),
    ("dist_right", 7, 6),
)
PIR_PIN, PIR_NAME = 4, "pir_center"

# behaviour tuning
NEAR_CM         = 120.0    # closer than this fires an "approach" event
HYST_CM         = 25.0     # must back out past NEAR_CM+HYST before "depart"
MAX_CM          = 400.0    # readings beyond this are treated as "nothing there"
LOOP_MS         = 150      # sample period
PING_GAP_MS     = 60       # silence between the two ultrasonics (anti-crosstalk)
HEARTBEAT_MS    = 1000     # publish readings at least this often, event or not
PIR_WARMUP_MS   = 60000    # ignore the PIR's output while it settles
MEDIAN_N        = 3        # rolling median window per ultrasonic
EDGE_CONFIRM    = 3        # cycles an approach/depart must hold before it counts
ECHO_TIMEOUT_US = 30000    # ~5 m round trip; the sensor itself gives up at ~38ms
# ---------------------------------------------------------------------------


class Ultrasonic:
    """One HC-SR04, with a rolling median and approach/depart hysteresis."""

    def __init__(self, name, trig_pin, echo_pin):
        self.name = name
        self._trig = machine.Pin(trig_pin, machine.Pin.OUT, value=0)
        # PULL_DOWN matters when the sensor *isn't* there. A bare input floats,
        # and time_pulse_us waiting for a rising edge will happily latch onto
        # coupled noise — an unplugged or fallen-off ECHO lead then invents
        # distances and fires phantom approach events. Held low, a missing
        # sensor times out into MAX_CM, which is the honest answer. It costs
        # nothing when a sensor *is* attached: the module drives ECHO actively,
        # and even behind a 5V divider the internal pull (~60k) is far weaker
        # than the 2k lower leg.
        self._echo = machine.Pin(echo_pin, machine.Pin.IN, machine.Pin.PULL_DOWN)
        self._window = []
        self._agree = 0
        self.cm = MAX_CM
        self.near = False

    def ping(self):
        """Fire once and fold the result into the median window."""
        self._trig.value(0)
        time.sleep_us(3)
        self._trig.value(1)
        time.sleep_us(10)
        self._trig.value(0)
        us = machine.time_pulse_us(self._echo, 1, ECHO_TIMEOUT_US)
        # A negative result means no echo came back, i.e. nothing is within
        # range. That's "far", not "unknown" — feeding MAX_CM in is precisely
        # what lets a depart event fire when someone walks out of the cone.
        cm = MAX_CM if us < 0 else min((us * 0.0343) / 2.0, MAX_CM)
        self._window.append(cm)
        if len(self._window) > MEDIAN_N:
            self._window.pop(0)
        # Median, not mean: ultrasonics drop the occasional wild outlier, and an
        # average smears it across the next few readings instead of discarding it.
        self.cm = sorted(self._window)[len(self._window) // 2]
        return self.cm

    def edge(self):
        """The approach/depart transition this ping confirmed, or None."""
        if not self.near and self.cm < NEAR_CM:
            want = True
        elif self.near and self.cm > NEAR_CM + HYST_CM:
            want = False
        else:
            self._agree = 0
            return None
        # Hysteresis alone isn't enough. A dropped echo reads as MAX_CM, which
        # is indistinguishable from someone walking out of the cone, so a lost
        # ping would otherwise emit a depart and the next good one an approach.
        # Holding the new state for EDGE_CONFIRM cycles costs ~450ms of latency
        # and absorbs dropouts shorter than that.
        #
        # It is NOT a fix for a failing sensor: measured against one running 76%
        # dropouts, in bursts of ~1.5s, this cut spurious events by about 2.5x
        # and no more — the bursts are simply longer than the window. Sizing
        # EDGE_CONFIRM to span them would delay every real detection by as much.
        # If this sensor is chattering, suspect the wiring before this number.
        self._agree += 1
        if self._agree < EDGE_CONFIRM:
            return None
        self._agree = 0
        self.near = want
        return {"sensor": self.name,
                "event": "approach" if want else "depart",
                "cm": round(self.cm, 1)}

    def reading(self):
        return {"type": "distance", "cm": round(self.cm, 1)}


class Motion:
    """The HC-SR501, debounced and muted until it has warmed up."""

    def __init__(self, name, pin):
        self.name = name
        # Pulled down for the same reason as ECHO: a disconnected PIR should
        # read "no motion", not noise. The HC-SR501 drives its output both ways,
        # so it overrides this without a fight.
        self._pin = machine.Pin(pin, machine.Pin.IN, machine.Pin.PULL_DOWN)
        self.active = False
        self._agree = 0
        self._boot = time.ticks_ms()

    def warming(self):
        return time.ticks_diff(time.ticks_ms(), self._boot) < PIR_WARMUP_MS

    def sample(self):
        """The motion edge this sample confirmed, or None."""
        raw = bool(self._pin.value())
        if raw == self.active:
            self._agree = 0
            return None
        self._agree += 1
        if self._agree < 2:
            return None                # one disagreeing sample is noise, not an edge
        self._agree = 0
        self.active = raw
        # Track the level while warming so the first real reading is honest, but
        # publish no events: a settling HC-SR501 fires constantly, and every one
        # of those would be a phantom greeting on the head.
        if self.warming():
            return None
        return {"sensor": self.name,
                "event": "motion_start" if raw else "motion_stop"}

    def reading(self):
        r = {"type": "motion", "active": self.active}
        if self.warming():
            r["warming"] = True
        return r


def status_led():
    """The onboard LED if this board has one on a plain GPIO, else None.

    On a Pico W running the RPI_PICO build the LED hangs off the CYW43 rather
    than GP25, so there simply isn't one to blink. Not worth failing over.
    """
    for spec in ("LED", 25):
        try:
            return machine.Pin(spec, machine.Pin.OUT)
        except (ValueError, TypeError):
            continue
    return None


def emit(payload):
    """One JSON object, one line. Never let the host's absence wedge the loop."""
    try:
        print(json.dumps(payload))
    except Exception:
        pass


def main():
    sensors = [Ultrasonic(name, trig, echo) for (name, trig, echo) in ULTRASONICS]
    pir = Motion(PIR_NAME, PIR_PIN)
    led = status_led()
    wdt = machine.WDT(timeout=8000)

    last_tick = time.ticks_ms()
    uptime_ms = 0                      # accumulated, so it survives the ticks_ms wrap
    last_beat = 0

    # Ctrl-C is left to do its normal thing (drop to the REPL) so push.py can
    # break in and update this file in place. That's safe because the only thing
    # that ever opens this port in service — the chest Pi's sensor relay — opens
    # it read-only and so physically cannot send one.
    while True:
        cycle_start = time.ticks_ms()
        events = []

        # One ultrasonic at a time, with silence between them: both burst at
        # 40kHz, so firing together means the second sensor hears the first
        # one's ping and reports a phantom short distance.
        for i, s in enumerate(sensors):
            if i:
                time.sleep_ms(PING_GAP_MS)
            s.ping()
            ev = s.edge()
            if ev:
                events.append(ev)

        ev = pir.sample()
        if ev:
            events.append(ev)

        now = time.ticks_ms()
        uptime_ms += time.ticks_diff(now, last_tick)
        last_tick = now

        if events or time.ticks_diff(now, last_beat) >= HEARTBEAT_MS:
            readings = {s.name: s.reading() for s in sensors}
            readings[pir.name] = pir.reading()
            emit({"node": NODE_ID, "uptime_ms": uptime_ms,
                  "readings": readings, "events": events})
            last_beat = now

        if led:
            led.value(not led.value())
        wdt.feed()

        spent = time.ticks_diff(time.ticks_ms(), cycle_start)
        if spent < LOOP_MS:
            time.sleep_ms(LOOP_MS - spent)


if __name__ == "__main__":
    main()

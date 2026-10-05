# FRED arm servo node — Waveshare RP2040-Zero (MicroPython)
#
# A deliberately dumb node: it turns "channel N, this many microseconds" from
# the NUC into pulses on a PCA9685, and it owns the one decision that must not
# depend on the NUC being alive — whether the outputs are enabled at all.
# Angles, limits, names and calibration all live on the NUC
# (inmoov/servo_boards.py, config/servo_boards.json), so tuning an arm never
# means reflashing this.
#
# The same file goes on every arm's board, unedited. A board does not know
# which arm it is: it reports the RP2040's unique id, and the NUC's admin page
# is where that id is told "you are the right arm" and which joint is on which
# channel.
#
# SAFETY, in order of how much it matters:
#   * OE is driven HIGH before anything else at boot. The arm is dead until the
#     host says {"op":"enable","on":true}.
#   * If the host says nothing for HOST_TIMEOUT_MS the outputs are cut and every
#     channel is cleared, so a crashed brain cannot leave an arm powered and
#     holding forever, and re-enabling later cannot snap ten joints back to
#     stale positions at once. The arm goes LIMP when this happens — it will
#     drop under its own weight.
#   * Pulses are clamped to PULSE_MIN_US..PULSE_MAX_US whatever the host asks.
#   * A move sent with a "rate" is walked there in small steps, here, 50 times
#     a second. This is what makes a kill mean anything: a hobby servo — a
#     digital one above all — finishes the move to the last pulse it was sent
#     even after the signal is cut, so stopping the signal mid-travel stops
#     nothing. Walked, the servo is never asked for more than one small step
#     ahead of where it is, and cutting the signal leaves it one step from
#     where it stood.
#
# OE only does its job while this board is running. Held in reset or unplugged
# from 3V3, GP6 floats and the breakout's own pull-down re-enables the outputs;
# the PCA9685 then keeps emitting whatever it last held.
#
# WIRING (pads are labelled by GPIO number on the Zero):
#   PCA9685 VCC -> 3V3 (NOT 5V: the pull-ups follow VCC, GPIO is not 5V tolerant)
#           GND -> GND          SDA -> GP4          SCL -> GP5
#           OE  -> GP6          V+  -> separate 5-6V servo supply, screw terminal
#
# PROTOCOL: one JSON object per line each way over USB serial. Every command is
# answered with one status line.
#   {"op":"ping"}
#   {"op":"enable","on":true}
#   {"op":"set","us":{"0":1500,"4":1200}}     pulse width per channel, at once
#   {"op":"set","us":{"0":1500},"rate":600,   walk there at 600 us per second;
#        "from":{"0":1300}}                   a channel with no pulse yet starts
#                                             at its "from" (or jumps, without one)
#   {"op":"relax","ch":[0,4]}                 no "ch" = every channel; the reply
#                                             carries "prev", the pulses it cut
#   -> {"node":"servo_board","uid":"e66...","ok":true,"enabled":false,
#       "pca":true,"us":[0,...16...],"uptime_ms":1234}
#       (+ "error", "tripped" when set)
#
# SETUP: flash MicroPython (WAVESHARE_RP2040_ZERO UF2), then from the repo:
#   python3 firmware/pico_sensor_node/push.py firmware/servo_board_node/main.py

import json
import select
import sys
import time

from machine import I2C, Pin, unique_id

# ---- CONFIG (edit these) --------------------------------------------------
NODE_ID = "servo_board"     # what kind of node this is, not which arm
SDA_PIN, SCL_PIN, OE_PIN = 4, 5, 6
PCA_ADDR        = 0x40
PWM_HZ          = 50
CHANNELS        = 16
PULSE_MIN_US    = 400      # hard floor/ceiling, whatever the host asks for
PULSE_MAX_US    = 2600
HOST_TIMEOUT_MS = 3000     # silence from the host before the outputs are cut
                           # (0 = never; the host driver pings once a second)
MAX_LINE        = 512      # a longer line is noise, not a command
STEP_MS         = 20       # how often a walked move takes a step (one PWM frame)
# ---------------------------------------------------------------------------

_MODE1, _PRESCALE, _LED0, _ALL_LED = 0x00, 0xFE, 0x06, 0xFA
_FULL_OFF = bytes([0, 0, 0, 0x10])
_PERIOD_US = 1000000 // PWM_HZ
_UID = "".join("%02x" % b for b in unique_id())


class Arm:
    def __init__(self):
        # First line of real work: outputs off, before the chip is even touched.
        self._oe = Pin(OE_PIN, Pin.OUT, value=1)
        self._i2c = I2C(0, sda=Pin(SDA_PIN), scl=Pin(SCL_PIN), freq=100000)
        self.enabled = False
        self.pca = False
        self.tripped = None            # why the outputs were last cut, if we cut them
        self.us = [0] * CHANNELS       # the pulse going out now; 0 = none (relaxed)
        self.target = [0] * CHANNELS   # where a walked move is heading
        self.rate = [0] * CHANNELS     # us per second; 0 = not walking
        self.prev = None               # the pulses the last relax cut
        self.setup()

    def setup(self):
        """(Re)configure the PCA9685. Safe to repeat: it clears every channel."""
        try:
            self._i2c.writeto_mem(PCA_ADDR, _MODE1, b"\x10")           # sleep
            prescale = round(25000000 / (4096 * PWM_HZ)) - 1
            self._i2c.writeto_mem(PCA_ADDR, _PRESCALE, bytes([prescale]))
            self._i2c.writeto_mem(PCA_ADDR, _MODE1, b"\x20")           # awake, auto-inc
            time.sleep_ms(5)
            self._i2c.writeto_mem(PCA_ADDR, _ALL_LED, _FULL_OFF)
            self.us = [0] * CHANNELS
            self.rate = [0] * CHANNELS
            self.pca = True
        except OSError:
            self.pca = False
        return self.pca

    def enable(self, on):
        if on and not self.pca and not self.setup():
            raise OSError("PCA9685 not answering")
        self._oe.value(0 if on else 1)
        self.enabled = bool(on)
        if on:
            self.tripped = None

    def _write(self, ch, us):
        count = us * 4096 // _PERIOD_US
        self._i2c.writeto_mem(PCA_ADDR, _LED0 + 4 * ch,
                              bytes([0, 0, count & 0xFF, count >> 8]))
        self.us[ch] = us

    def set_us(self, ch, us, rate=0, start=None):
        us = max(PULSE_MIN_US, min(PULSE_MAX_US, int(us)))
        if rate > 0 and not self.us[ch] and start:
            # Nothing going out yet, but the host knows where the joint was
            # left: begin there and walk, instead of jumping to the target.
            self._write(ch, max(PULSE_MIN_US, min(PULSE_MAX_US, int(start))))
        if rate > 0 and self.us[ch]:
            self.target[ch] = us
            self.rate[ch] = int(rate)
        else:
            self.rate[ch] = 0
            self._write(ch, us)

    def step(self, dt_ms):
        """Advance every walked move by one step."""
        for ch in range(CHANNELS):
            rate = self.rate[ch]
            if not rate:
                continue
            us, target = self.us[ch], self.target[ch]
            if not us or us == target:
                self.rate[ch] = 0
                continue
            d = max(1, rate * dt_ms // 1000)
            self._write(ch, min(target, us + d) if target > us else max(target, us - d))

    def relax(self, channels=None):
        self.prev = list(self.us)
        if channels is None:
            self.rate = [0] * CHANNELS         # stop walking before anything else
            self._i2c.writeto_mem(PCA_ADDR, _ALL_LED, _FULL_OFF)
            self.us = [0] * CHANNELS
            return
        for ch in channels:
            self.rate[ch] = 0
            self._i2c.writeto_mem(PCA_ADDR, _LED0 + 4 * ch, _FULL_OFF)
            self.us[ch] = 0

    def trip(self, reason):
        """Cut the outputs ourselves. OE first — it needs no working bus."""
        self._oe.value(1)
        self.enabled = False
        self.tripped = reason
        try:
            self.relax()
        except OSError:
            self.pca = False


def channel(value):
    ch = int(value)
    if not 0 <= ch < CHANNELS:
        raise ValueError("channel must be 0..%d" % (CHANNELS - 1))
    return ch


def handle(arm, line):
    """Run one command line. Returns an error string, or None."""
    try:
        cmd = json.loads(line)
        op = cmd.get("op")
        if op == "ping":
            pass
        elif op == "enable":
            arm.enable(bool(cmd.get("on")))
        elif op == "set":
            # Validate the whole batch before moving anything: half a pose is
            # worse than none.
            moves = [(channel(ch), us) for ch, us in cmd["us"].items()]
            rate = int(cmd.get("rate") or 0)
            start = {channel(ch): us for ch, us in (cmd.get("from") or {}).items()}
            for ch, us in moves:
                arm.set_us(ch, us, rate, start.get(ch))
        elif op == "relax":
            chs = cmd.get("ch")
            arm.relax(None if chs is None else [channel(c) for c in chs])
        else:
            return "unknown op %r" % (op,)
    except OSError as exc:
        arm.pca = False
        return "i2c: %s" % (exc,)
    except Exception as exc:
        return "%s: %s" % (type(exc).__name__, exc)
    return None


def reply(arm, uptime_ms, error=None):
    out = {"node": NODE_ID, "uid": _UID, "ok": error is None, "enabled": arm.enabled,
           "pca": arm.pca, "us": arm.us, "uptime_ms": uptime_ms}
    if error:
        out["error"] = error
    if arm.tripped:
        out["tripped"] = arm.tripped
    if arm.prev is not None:           # only on the reply to the relax that cut them
        out["prev"], arm.prev = arm.prev, None
    try:
        print(json.dumps(out))
    except Exception:
        pass


def main():
    arm = Arm()
    poll = select.poll()
    poll.register(sys.stdin, select.POLLIN)
    buf = ""
    overflow = False
    last_tick = time.ticks_ms()
    uptime_ms = 0                      # accumulated, so it survives the ticks_ms wrap
    last_heard = last_tick
    last_step = last_tick

    # Ctrl-C is left alone so push.py can break in and replace this file. A
    # KeyboardInterrupt drops to the REPL with OE in whatever state it was in,
    # so push.py should only be run with the arm supported.
    while True:
        ready = poll.poll(5)
        now = time.ticks_ms()
        uptime_ms += time.ticks_diff(now, last_tick)
        last_tick = now

        while ready:
            c = sys.stdin.read(1)
            if c in ("\n", "\r"):
                line, buf = buf.strip(), ""
                if overflow:
                    overflow = False
                    reply(arm, uptime_ms, "line too long")
                elif line:
                    last_heard = now
                    reply(arm, uptime_ms, handle(arm, line))
            elif len(buf) < MAX_LINE:
                buf += c
            else:
                overflow = True
            ready = poll.poll(0)

        dt = time.ticks_diff(now, last_step)
        if dt >= STEP_MS:
            last_step = now
            try:
                arm.step(dt)
            except OSError:
                arm.pca = False

        if (HOST_TIMEOUT_MS and arm.enabled
                and time.ticks_diff(now, last_heard) > HOST_TIMEOUT_MS):
            arm.trip("host_silent")
            reply(arm, uptime_ms)


if __name__ == "__main__":
    main()

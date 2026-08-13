"""Read the 8BitDo controller on the chest Pi. Stdlib only, like everything here.

The cart's manual control used to live in the Pico's firmware, where a PS2
receiver outranked the host in hardware. That receiver stopped answering (see
Project-FRED-Cart, PS2 disabled 2026-08-12) and its replacement is a USB dongle
on *this* Pi, which moves the arbitration into software — specifically into
cart_driver, the process that already owns the serial port, the watchdog and the
e-stop. That is the right home for it: the thing that can stop the cart is the
thing that decides who may move it.

The dongle needs no driver work. It presents as a DInput HID gamepad through
hid-generic, so the kernel hands us an ordinary evdev node and this is the same
24-byte ``input_event`` decode as touch.py. **xpad is not involved** — worth
recording, because the dongle enumerates as "8BitDo IDLE" until the controller
is switched on, which reads like a driver problem and isn't one.

**Controllers differ, so nothing about the axes is hardcoded.** Two things vary
between pads and both are read rather than assumed:

* *Range.* The 8BitDo in DInput reports 8-bit axes (0..255, centre 127); an Xbox
  pad reports 16-bit sticks. The kernel knows the real range for every axis, so
  ``EVIOCGABS`` is asked at open time and everything normalises from that. A
  hardcoded 127 would read an Xbox stick as pinned hard over.
* *Role.* Which axis is "right stick X" is a property of the pad, not a
  standard: DInput puts it on ``ABS_Z``, XInput on ``ABS_RX`` (where DInput
  keeps a trigger). That is what PROFILES below is for.

Mapping captured from the real 8BitDo Ultimate 2C on 2026-08-12; the Xbox
profile follows the kernel's standard XInput layout. Both keep the same scheme
as the retired PS2 pad — hold R1, left stick speed, right stick steer — so the
muscle memory carries over.

Everything is best-effort: an absent dongle is a controller nobody is holding,
not an error, and it must never take the display daemon down with it.
"""
from __future__ import annotations

import ctypes
import fcntl
import glob
import os
import struct
import threading
import time

DEVICE_GLOB = "/dev/input/by-id/*event-joystick"

_EVENT_FMT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)

_EV_KEY, _EV_ABS = 0x01, 0x03
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0, 1, 2, 3, 4, 5
BTN_TL, BTN_TR = 310, 311                   # L1 / LB, R1 / RB
BTN_SOUTH = 0x130                           # A / cross: marks a device as a pad
_KEY_BYTES = (0x2FF // 8) + 1               # KEY_MAX, for EVIOCGBIT(EV_KEY)

DEADZONE_FRAC = 0.10                        # of half-range; sticks do not sit still
RESCAN_EVERY = 2.0                          # s between looks for a plugged-in pad

# EVIOCGABS(axis) — struct input_absinfo { s32 value, min, max, fuzz, flat, res }
_EVIOCGABS_BASE = 0x80184540


class Profile:
    """How one family of controller lays out the two axes we drive with.

    Only the roles live here. Ranges come from the driver, so a profile stays
    valid across DInput/XInput firmware revisions that move the numbers around.
    """

    def __init__(self, name: str, match: tuple, speed_axis: int, steer_axis: int,
                 deadman: int = BTN_TR, boost: int = BTN_TL, invert_speed: bool = True):
        self.name = name
        self.match = match                  # lowercase substrings of the device name
        self.speed_axis = speed_axis
        self.steer_axis = steer_axis
        self.deadman = deadman
        self.boost = boost
        # Sticks report "up" as the *low* end on every pad we have met, and up
        # should mean forward, so the speed axis is negated by default.
        self.invert_speed = invert_speed


PROFILES = (
    # DInput: right stick X lands on ABS_Z. Verified against the real hardware.
    Profile("8bitdo-dinput", ("8bitdo",), speed_axis=ABS_Y, steer_axis=ABS_Z),
    # XInput: right stick is RX/RY, and ABS_Z/RZ become the analogue triggers —
    # so an 8BitDo profile applied to an Xbox pad would steer with a trigger.
    Profile("xbox", ("xbox", "x-box", "microsoft"), speed_axis=ABS_Y, steer_axis=ABS_RX),
)
# Anything else: assume the XInput layout, which is what most pads speak.
FALLBACK = Profile("generic", (), speed_axis=ABS_Y, steer_axis=ABS_RX)


def profile_for(name: str) -> Profile:
    low = (name or "").lower()
    for prof in PROFILES:
        if any(token in low for token in prof.match):
            return prof
    return FALLBACK


def _ioc(direction: int, typ: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


def is_gamepad(fd: int) -> bool:
    """Does this device have gamepad buttons?

    The discriminator is BTN_SOUTH (0x130), the first of the kernel's gamepad
    button range. The touchscreen on this Pi has absolute axes too, so "has ABS
    axes" would happily match it and we would drive the cart with a fingertip;
    only a pad claims these key codes.
    """
    buf = ctypes.create_string_buffer(_KEY_BYTES)
    try:
        fcntl.ioctl(fd, _ioc(2, "E", 0x20 + _EV_KEY, _KEY_BYTES), buf)
    except OSError:
        return False
    return bool(buf.raw[BTN_SOUTH // 8] & (1 << (BTN_SOUTH % 8)))


def find_device() -> str:
    """A controller's evdev node, or "" if none is there.

    Two ways of looking, because the two controllers we have arrive differently:

    * ``/dev/input/by-id/*event-joystick`` — udev's stable name, which is what a
      *USB* pad gets. Preferred, because event numbers move with plug order.
    * failing that, every ``event*`` node, asking each whether it has gamepad
      buttons. A **Bluetooth** pad has no by-id symlink at all — udev builds
      those from USB serial numbers — so the paired Xbox appears only here, and
      matching by-id alone would never find it.

    The capability check is what keeps this safe: the touchscreen is also an
    absolute-axis device on an adjacent event number.
    """
    for path in sorted(glob.glob(DEVICE_GLOB)):
        return os.path.realpath(path)
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            if is_gamepad(fd):
                return path
        finally:
            os.close(fd)
    return ""


def device_name(fd: int) -> str:
    buf = ctypes.create_string_buffer(256)
    try:
        fcntl.ioctl(fd, 0x81004506, buf)    # EVIOCGNAME(256)
    except OSError:
        return ""
    return buf.value.decode("utf-8", "replace")


def axis_range(fd: int, axis: int) -> tuple[int, int, int]:
    """(min, max, flat) for one axis, straight from the driver.

    ``flat`` is the driver's own idea of the deadzone; where it offers one we
    use it, because the pad's manufacturer knows its sticks better than a
    constant here does.
    """
    buf = ctypes.create_string_buffer(24)
    try:
        fcntl.ioctl(fd, _EVIOCGABS_BASE + axis, buf)
    except OSError:
        return (0, 255, 0)                  # a guess, but only for a pad that has no answer
    _value, minimum, maximum, _fuzz, flat, _res = struct.unpack("6i", buf.raw)
    if maximum <= minimum:
        return (0, 255, 0)
    return (minimum, maximum, flat)


class Gamepad:
    """Live controller state, polled off a background thread.

    ``state()`` is a snapshot: present, whether the deadman is held, and the two
    axes already scaled to -1.0..1.0 with the deadzone removed.
    """

    def __init__(self, log=print):
        self._log = log
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd = -1
        self._path = ""
        self._name = ""
        self._profile = FALLBACK
        # axis -> (min, max, flat), learned from the driver at open time
        self._ranges: dict[int, tuple[int, int, int]] = {}
        self._axes: dict[int, int] = {}
        self._buttons: set[int] = set()
        self._last_event = 0.0
        self._connected = False

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="gamepad", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    # ---- reading ---------------------------------------------------------
    def _open(self) -> bool:
        path = find_device()
        if not path:
            return False
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return False
        name = device_name(fd)
        prof = profile_for(name)
        ranges = {axis: axis_range(fd, axis)
                  for axis in (prof.speed_axis, prof.steer_axis)}
        with self._lock:
            self._fd, self._path, self._name, self._profile = fd, path, name, prof
            self._ranges = ranges
            # Assume nothing about position on connect: a stick we have not been
            # told about is centred, not wherever it was when it vanished.
            self._axes = {axis: self._centre(axis) for axis in ranges}
            self._buttons.clear()
            self._connected = True
        spans = " ".join(f"{a}:{lo}..{hi}" for a, (lo, hi, _f) in ranges.items())
        self._log(f"gamepad: {name or os.path.basename(path)} "
                  f"[profile {prof.name}] connected ({spans})")
        return True

    def _close(self, why: str) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._fd = -1
        with self._lock:
            was, self._connected = self._connected, False
            self._axes = {axis: self._centre(axis) for axis in self._ranges}
            self._buttons.clear()
        if was:
            self._log(f"gamepad: disconnected ({why})")

    def _loop(self) -> None:
        last_scan = 0.0
        while not self._stop.is_set():
            if self._fd < 0:
                now = time.monotonic()
                if now - last_scan < RESCAN_EVERY:
                    self._stop.wait(0.2)
                    continue
                last_scan = now
                if not self._open():
                    continue
            try:
                data = os.read(self._fd, _EVENT_SIZE * 64)
            except BlockingIOError:
                self._stop.wait(0.01)
                continue
            except OSError:
                self._close("read failed")     # unplugged mid-stream
                continue
            if not data:
                self._stop.wait(0.01)
                continue
            now = time.monotonic()
            with self._lock:
                for off in range(0, len(data) - _EVENT_SIZE + 1, _EVENT_SIZE):
                    _s, _us, etype, code, value = struct.unpack_from(_EVENT_FMT, data, off)
                    if etype == _EV_ABS and code in self._ranges:
                        self._axes[code] = value
                    elif etype == _EV_KEY:
                        if value:
                            self._buttons.add(code)
                        else:
                            self._buttons.discard(code)
                    else:
                        continue
                    self._last_event = now

    # ---- state -----------------------------------------------------------
    def _centre(self, axis: int) -> int:
        lo, hi, _flat = self._ranges.get(axis, (0, 255, 0))
        return (lo + hi) // 2

    def _scale(self, axis: int, raw: int) -> float:
        """One axis to -1.0..1.0, using the range the driver reported.

        Deadzone is the driver's own ``flat`` where it gives one, else a
        fraction of half-range — so it scales with the pad rather than being an
        8-bit constant that would be invisible on a 16-bit Xbox stick.

        The remainder is restretched to the full 0..1. Without that the stick
        jumps to deadzone/half-range of full speed the moment it leaves the
        deadzone, which on a 350 lb base is a lurch rather than a start.
        """
        lo, hi, flat = self._ranges.get(axis, (0, 255, 0))
        centre = (lo + hi) / 2.0
        half = max(1.0, (hi - lo) / 2.0)
        dead = float(flat) if flat > 0 else half * DEADZONE_FRAC
        offset = raw - centre
        if abs(offset) <= dead:
            return 0.0
        span = max(1.0, half - dead)
        value = (abs(offset) - dead) / span
        value = max(0.0, min(1.0, value))
        return value if offset > 0 else -value

    def state(self) -> dict:
        with self._lock:
            connected = self._connected
            axes = dict(self._axes)
            buttons = set(self._buttons)
            last = self._last_event
        idle_for = (time.monotonic() - last) if last else None
        prof = self._profile
        speed = self._scale(prof.speed_axis, axes.get(prof.speed_axis, self._centre(prof.speed_axis)))
        steer = self._scale(prof.steer_axis, axes.get(prof.steer_axis, self._centre(prof.steer_axis)))
        if prof.invert_speed:
            speed = -speed
        if speed == 0.0:
            speed = 0.0                 # negating zero yields -0.0, which prints as -0.00
        return {
            "connected": connected,
            "device": self._name or (os.path.basename(self._path) if connected else ""),
            "profile": prof.name,
            "deadman": prof.deadman in buttons,
            "boost": prof.boost in buttons,
            "speed": round(speed, 3),
            "steer": round(steer, 3),
            "raw": {"speed_axis": axes.get(prof.speed_axis),
                    "steer_axis": axes.get(prof.steer_axis)},
            "idle_for": round(idle_for, 1) if idle_for is not None else None,
        }

    def wants_control(self) -> bool:
        """True when the operator is actively asking to drive.

        Deliberately narrow: the deadman must be *held*. A controller lying on a
        bench with a drifting stick does not take the robot away from the panel,
        and letting go is the stop — the same contract the PS2 firmware had.
        """
        s = self.state()
        return bool(s["connected"] and s["deadman"])

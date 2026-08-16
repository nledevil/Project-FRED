"""Reads the 7" panel's touchscreen. Stdlib only — there is no python-evdev here.

The kernel hands us fixed-size ``input_event`` records on /dev/input/event0 and
that is the whole protocol, so a dependency would buy nothing: this file is the
struct format, three event codes, and the rule that a report ends at SYN_REPORT.

**No calibration.** The official DSI panel reports ABS_X 0-799 and ABS_Y 0-479 —
already screen pixels, one to one — so there is no transform to get wrong. If a
different panel ever turns up, that assumption is checked at open() and reported
rather than silently producing taps in the wrong place.

**Single touch on purpose.** The panel also speaks the multitouch protocol, but
the menu is buttons and there is no gesture worth a second finger. The kernel's
single-touch emulation (ABS_X/ABS_Y + BTN_TOUCH) tracks the first finger down,
which is exactly what a button wants.

Two processes read this device at once — the display daemon watching for a tap
on the cog, and the settings menu while it is open. That is safe: each open fd
gets its own event queue in the kernel, so neither steals events from the other.

    with Touch() as t:
        for kind, x, y in t.poll(timeout=0.03):   # 'down' | 'move' | 'up'
            ...
"""
from __future__ import annotations

import ctypes
import fcntl
import os
import select
import struct

DEVICE = "/dev/input/event0"      # only the fallback; see find_device()


def find_device(default: str = DEVICE) -> str:
    """The touchscreen's event node, found by what it *is* rather than by index.

    This was hard-coded to event0 for as long as event0 was the touchscreen.
    Loading vc4-kms-v3d for the GPU added four HDMI input devices — CEC remotes
    and jack-detect switches — which took event0 through event3 and pushed the
    panel to event4. The cog then stopped responding, because the daemon was
    faithfully reading an HDMI remote control.

    A touchscreen is the device with absolute axes; the things that displaced it
    are keyboards and switches. That is a property of the hardware and does not
    renumber, so match on it.
    """
    try:
        blocks = open("/proc/bus/input/devices").read().split("\n\n")
    except OSError:
        return default
    for block in blocks:
        name = handlers = abs_bits = ""
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line[8:].strip().strip('"').lower()
            elif line.startswith("H: Handlers="):
                handlers = line[12:]
            elif line.startswith("B: ABS="):
                abs_bits = line[7:].strip()
        node = next((h for h in handlers.split() if h.startswith("event")), "")
        if not node or not abs_bits or abs_bits.strip("0 ") == "":
            continue                      # no event node, or no absolute axes
        if "kbd" in handlers.split():
            continue                      # a remote with a d-pad, not a panel
        path = f"/dev/input/{node}"
        if path != default:
            print(f"[touch] using {path} ({name})", flush=True)
        return path
    return default

# struct input_event { struct timeval time; __u16 type, code; __s32 value; }
# 24 bytes on 64-bit Linux (two 8-byte time fields). Checked at import rather
# than assumed, so a 32-bit build fails loudly here instead of decoding garbage.
_EVENT_FMT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)
assert _EVENT_SIZE == 24, f"unexpected input_event size {_EVENT_SIZE}"

_EV_SYN, _EV_KEY, _EV_ABS = 0x00, 0x01, 0x03
_SYN_REPORT = 0x00
_BTN_TOUCH = 0x14A
_ABS_X, _ABS_Y = 0x00, 0x01

_EVIOCGABS_X = 0x80184540 + _ABS_X          # EVIOCGABS(ABS_X)
_EVIOCGABS_Y = 0x80184540 + _ABS_Y


class Touch:
    """Screen-pixel touch events from ``path``.

    Raises OSError if the device can't be opened — callers decide whether that
    is fatal. For the daemon it isn't: no touchscreen just means no cog.
    """

    def __init__(self, path: str = DEVICE, width: int = 800, height: int = 480):
        self._fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.path = path
        self._buf = b""
        self._x = self._y = 0
        self._down = False          # what we last reported
        self._down_next = False     # what BTN_TOUCH last said, applied at SYN
        self._pending: list[tuple[str, int, int]] = []
        # What the driver claims its axes are. If they aren't the panel's
        # pixels we scale rather than pretend, so a swapped panel is wrong-ish
        # instead of unusable — and we say so.
        self._max_x = self._abs_max(_EVIOCGABS_X, width - 1)
        self._max_y = self._abs_max(_EVIOCGABS_Y, height - 1)
        self._w, self._h = width, height

    def _abs_max(self, request: int, fallback: int) -> int:
        buf = ctypes.create_string_buffer(24)   # struct input_absinfo
        try:
            fcntl.ioctl(self._fd, request, buf)
        except OSError:
            return fallback
        _value, _minimum, maximum, _fuzz, _flat, _res = struct.unpack("6i", buf.raw)
        return maximum or fallback

    def _scale(self, x: int, y: int) -> tuple[int, int]:
        if self._max_x != self._w - 1:
            x = x * (self._w - 1) // max(1, self._max_x)
        if self._max_y != self._h - 1:
            y = y * (self._h - 1) // max(1, self._max_y)
        return x, y

    # ---- reading ---------------------------------------------------------
    def poll(self, timeout: float = 0.0) -> list[tuple[str, int, int]]:
        """Drain pending events, waiting up to ``timeout`` seconds for the first.

        Returns a list of ``(kind, x, y)`` where kind is 'down', 'move' or 'up'.
        Never blocks longer than ``timeout``, so a render loop can call this
        every frame and keep its own pace.
        """
        if not self._pending:
            try:
                ready, _, _ = select.select([self._fd], [], [], timeout)
            except (OSError, ValueError):
                return []
            if not ready:
                return []
        self._read_available()
        out, self._pending = self._pending, []
        return out

    def _read_available(self) -> None:
        while True:
            try:
                chunk = os.read(self._fd, _EVENT_SIZE * 64)
            except BlockingIOError:
                break
            except OSError:
                break
            if not chunk:
                break
            self._buf += chunk
            if len(chunk) < _EVENT_SIZE * 64:
                break
        # Whole records only; a partial tail waits for the next read.
        usable = len(self._buf) - (len(self._buf) % _EVENT_SIZE)
        records, self._buf = self._buf[:usable], self._buf[usable:]
        for off in range(0, usable, _EVENT_SIZE):
            _s, _us, etype, code, value = struct.unpack_from(
                _EVENT_FMT, records, off)
            self._feed(etype, code, value)

    def _feed(self, etype: int, code: int, value: int) -> None:
        """One decoded record. Position and press state are accumulated; the
        event is only emitted at SYN_REPORT, which is where the kernel says the
        picture is consistent. Emitting per-record would report a finger at last
        frame's Y for the instant between the X and Y updates."""
        if etype == _EV_ABS:
            if code == _ABS_X:
                self._x = value
            elif code == _ABS_Y:
                self._y = value
        elif etype == _EV_KEY and code == _BTN_TOUCH:
            self._down_next = bool(value)
        elif etype == _EV_SYN and code == _SYN_REPORT:
            x, y = self._scale(self._x, self._y)
            pressed = self._down_next
            if pressed and not self._down:
                self._pending.append(("down", x, y))
            elif not pressed and self._down:
                # An 'up' carries the last known position: the driver doesn't
                # resend coordinates on release, and a button needs to know
                # where the finger left, not where it started.
                self._pending.append(("up", x, y))
            elif pressed:
                self._pending.append(("move", x, y))
            self._down = pressed

    # ---- lifecycle -------------------------------------------------------
    def close(self) -> None:
        if self._fd >= 0:
            try:
                os.close(self._fd)
            finally:
                self._fd = -1

    def __enter__(self) -> "Touch":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


def open_touch(path: str | None = None, **kw) -> Touch | None:
    """``Touch(path)`` or None if it isn't there. For callers to whom a missing
    touchscreen is a missing feature rather than an error — the daemon keeps
    animating without a cog, and says so once in its log."""
    path = path or find_device()
    try:
        return Touch(path, **kw)
    except OSError as exc:
        print(f"[touch] {path} unavailable: {exc}", flush=True)
        return None

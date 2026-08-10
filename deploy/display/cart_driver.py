#!/usr/bin/env python3
"""Drive the hoverboard cart's Pico, and be the thing that stops it.

The cart (see github.com/nledevil/Project-FRED-Cart) is a hoverboard base: two
hub motors on a mainboard running EFeru FOC firmware, commanded over UART by a
Pico that also owns a PS2 controller for manual driving. The Pico exposes a USB
serial port; this is the other end of it.

The Pico plugs into *this* Pi because the cart is at the base and the head is on
pan/tilt servos, the same reason the stomach sensor node lives here. But the
thing deciding where to go — Claude, the web panel — runs on the brain, so drive
intent has to cross the network to get here.

**That is why the safety layer is here and not at the deciding end.** That link
is a real link with real failure modes. This was written when the two Pis were
joined by a Bluetooth PAN, which dropped entirely once during development when
the head's Bluetooth controller threw a hardware error and needed a manual
module reload; the wired robot LAN that replaced it is far better, but it is
still a cable, a switch and a NIC — and the brain is a separate machine again,
so there is one more hop between the decision and the wheels, not fewer. If the
deciding end were the only thing telling the cart to keep going, a dead link
would leave it rolling on its last command until the Pico's own 2 s
host-silence failsafe expired — about 3.3 m at the firmware's 300 speed cap.

So: the brain sends intent and has to keep saying so. This module holds the only
handle on the serial port, re-sends the current command at ``SEND_HZ``, and
zeroes it if it goes quiet for ``WATCHDOG_S``. Link loss costs well under a
metre instead of three, and it layers *under* the firmware's three existing
safeties (deadman, controller-loss, mainboard backstop) rather than replacing
any of them.

The PS2 controller still outranks everything. The firmware ignores host drive
commands whenever a controller is connected (``x`` is still honoured), so this
tracks that state off the telemetry stream and reports it — otherwise "I sent a
drive command and nothing moved" is a mystery instead of "someone picked up the
controller".

Stdlib only, like the rest of this Pi: no pyserial, tty set up via termios.
"""
from __future__ import annotations

import glob
import os
import re
import select
import termios
import threading
import time

# The firmware's own clamps (pico-hover.ino). Mirrored so the caller gets told
# it was clamped rather than silently having it done downstream.
STEER_LIMIT = 250
SPEED_LIMIT = 300

SEND_HZ = 10.0                # command re-send rate; also refreshes the Pico's
                              # 2 s failsafe, which any received line resets
WATCHDOG_S = 0.5              # head silence before we stop on our own. At 10 Hz
                              # from the head that tolerates 4 lost commands;
                              # 0.5 s at the 300 cap is ~0.8 m of coast.
RECONNECT_DELAY = 3.0
MAX_LINE = 4096
ERROR_QUIET = 30.0

# Telemetry from the Pico is human-readable text, not JSON — parse what matters.
_RE_FB = re.compile(
    r"fb\s+src:\s*(?P<src>\w+)\s+in1:\s*(?P<in1>-?\d+)\s+in2:\s*(?P<in2>-?\d+)"
    r"\s+speedR:\s*(?P<speedR>-?\d+)\s+speedL:\s*(?P<speedL>-?\d+)"
    r"\s+bat:\s*(?P<bat>[\d.]+)V\s+temp:\s*(?P<temp>-?[\d.]+)C")


def resolve_port(spec: str = "auto") -> str:
    """Find the cart Pico's tty ('' if it isn't plugged in).

    ``auto`` deliberately refuses anything whose by-id name says MicroPython.
    The stomach sensor node is a MicroPython board on this same Pi, and this
    module opens its port for *writing* — sending "0 150" at a MicroPython REPL
    would stop the sensors and leave a very confusing pair of symptoms. The
    cart Pico runs the Arduino core and enumerates under a different by-id name,
    so the two are distinguishable; when in doubt we open nothing.
    """
    if spec and spec != "auto":
        matches = sorted(glob.glob(spec))
        path = matches[0] if matches else spec
        return "" if _is_micropython(path) else path
    for pattern in ("/dev/serial/by-id/*Pico*", "/dev/serial/by-id/*RP2040*",
                    "/dev/serial/by-id/*Arduino*", "/dev/ttyACM*"):
        for path in sorted(glob.glob(pattern)):
            if not _is_micropython(path):
                return path
    return ""


def _is_micropython(path: str) -> bool:
    """True if this tty is (or resolves to) the MicroPython sensor node.

    Checks the by-id name directly, and for a bare /dev/ttyACMn walks the by-id
    directory to find whatever symlink points at it — the sensor node is usually
    discovered by its stable name but may be handed to us as a raw path.
    """
    if "micropython" in path.lower():
        return True
    try:
        target = os.path.realpath(path)
    except OSError:
        return False
    for link in glob.glob("/dev/serial/by-id/*"):
        if "micropython" in link.lower() and os.path.realpath(link) == target:
            return True
    return False


def open_serial(path: str, baud: int = 115200) -> int:
    """Open the Pico's CDC tty read/write in raw mode. Returns a non-blocking fd.

    Unlike the sensor relay this must be writable — it is the command path. Note
    the RP2040 bootloader trick: setting 1200 baud on one of these reboots the
    board into mass-storage mode, so the baud is fixed at 115200 and never
    touched again.
    """
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        speed = getattr(termios, f"B{baud}")
        iflag, oflag, cflag, lflag, _isp, _osp, cc = termios.tcgetattr(fd)
        iflag = 0            # no CR/NL translation, no flow control
        oflag = 0            # no output post-processing
        lflag = 0            # non-canonical, no echo
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        cc = list(cc)
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])
        termios.tcflush(fd, termios.TCIOFLUSH)
    except Exception:
        os.close(fd)
        raise
    return fd


def clamp(value, lo, hi):
    return lo if value < lo else (hi if value > hi else value)


class CartDriver:
    """Owns the cart Pico's serial link, the command watchdog, and telemetry.

    Best-effort about the hardware and strict about the safety: a missing Pico
    or a closed port must never raise into display_control, but a lapsed
    heartbeat always stops the cart.
    """

    def __init__(self, port: str = "auto", baud: int = 115200,
                 watchdog: float = WATCHDOG_S, log=print):
        self._port_spec = port
        self._baud = int(baud)
        self._watchdog = float(watchdog)
        self._log = log

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._fd = -1

        # Commanded state. _deadline is when the head's authority expires; it is
        # the whole safety mechanism, so everything that grants motion sets it.
        self._steer = 0
        self._speed = 0
        self._deadline = 0.0
        self._estop = False           # latched; only an explicit clear releases it

        self._tel = {
            "connected": False, "port": "", "ps2_active": False,
            "battery_v": None, "board_temp_c": None,
            "speed_l": None, "speed_r": None, "source": None,
            "last_telemetry": 0.0, "last_line": "",
            "mainboard_seen": False,
            "commands_sent": 0, "watchdog_stops": 0, "last_error": "",
        }
        self._last_moan: dict[str, float] = {}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._threads:
            return
        for target, name in ((self._io_loop, "cart-io"), (self._send_loop, "cart-send")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        self._log(f"cart driver: {self._port_spec} (watchdog {self._watchdog:.1f}s)")

    def shutdown(self) -> None:
        """Stop the cart, then stop ourselves. Order matters."""
        try:
            self._write_line("x")
        except Exception:                       # noqa: BLE001 - shutting down anyway
            pass
        self._stop.set()

    # -- command surface ---------------------------------------------------
    def drive(self, steer, speed) -> dict:
        """Set the drive target and refresh the caller's authority for WATCHDOG_S.

        This is the only way to make the cart move, and it has to be called
        again before the deadline or the watchdog zeroes it. That repetition is
        the point: it is what makes a dead link a stop rather than a runaway.
        """
        try:
            steer = int(round(float(steer)))
            speed = int(round(float(speed)))
        except (TypeError, ValueError):
            return {"error": "steer and speed must be numbers"}

        with self._lock:
            if self._estop:
                return {"error": "emergency stop latched; clear it first",
                        "estop": True}
            cs, cv = clamp(steer, -STEER_LIMIT, STEER_LIMIT), clamp(speed, -SPEED_LIMIT, SPEED_LIMIT)
            self._steer, self._speed = cs, cv
            self._deadline = time.monotonic() + self._watchdog
            ps2 = self._tel["ps2_active"]
        out = {"ok": True, "steer": cs, "speed": cv,
               "clamped": (cs != steer or cv != speed),
               "expires_in": self._watchdog}
        if ps2:
            # Not an error — the firmware is doing exactly what it should. But
            # the caller needs to know its command went nowhere.
            out["ignored"] = "PS2 controller connected; it has priority"
        return out

    def stop(self, estop: bool = False) -> dict:
        """Zero the target now. ``estop`` latches until explicitly cleared."""
        with self._lock:
            self._steer = self._speed = 0
            self._deadline = 0.0
            if estop:
                self._estop = True
        self._write_line("x")           # 'x' is honoured even with PS2 connected
        return {"ok": True, "stopped": True, "estop": self._estop}

    def clear_estop(self) -> dict:
        with self._lock:
            self._estop = False
        return {"ok": True, "estop": False}

    def state(self) -> dict:
        with self._lock:
            s = dict(self._tel)
            s["steer"] = self._steer
            s["speed"] = self._speed
            s["estop"] = self._estop
            remaining = self._deadline - time.monotonic()
        last = s.pop("last_telemetry")
        s["telemetry_age"] = round(time.monotonic() - last, 2) if last else None
        s["authority_s"] = round(remaining, 2) if remaining > 0 else 0.0
        s["moving"] = bool(s["speed"] or s["steer"])
        s["limits"] = {"steer": STEER_LIMIT, "speed": SPEED_LIMIT}
        return s

    # -- internals ---------------------------------------------------------
    def _moan(self, key: str, message: str) -> None:
        now = time.monotonic()
        if now - self._last_moan.get(key, 0.0) < ERROR_QUIET:
            return
        self._last_moan[key] = now
        self._log(message)

    def _note_error(self, text: str) -> None:
        with self._lock:
            self._tel["last_error"] = text[:200]

    def _write_line(self, text: str) -> bool:
        with self._lock:
            fd = self._fd
        if fd < 0:
            return False
        try:
            os.write(fd, (text + "\n").encode())
            return True
        except OSError as exc:
            self._note_error(f"write: {exc}")
            return False

    def _send_loop(self) -> None:
        """Re-send the current command, and enforce the watchdog.

        Re-sending is not redundant: every line the Pico receives also resets
        its own 2 s host-silence timer, so a steady stream is what keeps the
        firmware's failsafe from firing under normal operation.
        """
        period = 1.0 / SEND_HZ
        while not self._stop.wait(period):
            now = time.monotonic()
            with self._lock:
                expired = self._deadline and now > self._deadline
                if expired:
                    if self._speed or self._steer:
                        self._tel["watchdog_stops"] += 1
                        self._moan("watchdog",
                                   "cart: no command from the head within "
                                   f"{self._watchdog:.1f}s - stopping")
                    self._steer = self._speed = 0
                    self._deadline = 0.0
                steer, speed, estop = self._steer, self._speed, self._estop
                connected = self._tel["connected"]
            if not connected:
                continue
            if estop:
                self._write_line("x")
                continue
            if self._write_line(f"{steer} {speed}"):
                with self._lock:
                    self._tel["commands_sent"] += 1

    def _io_loop(self) -> None:
        """Keep a tty open and pump its lines. Reconnects for the life of the process."""
        while not self._stop.is_set():
            path = resolve_port(self._port_spec)
            with self._lock:
                self._tel["port"] = path
            if not path:
                self._moan("noport", "cart driver: no cart Pico found "
                                     f"(spec {self._port_spec!r}); retrying")
                self._note_error("no serial device matched")
                self._stop.wait(RECONNECT_DELAY)
                continue
            try:
                fd = open_serial(path, self._baud)
            except OSError as exc:
                self._moan("open", f"cart driver: cannot open {path} ({exc}); retrying")
                self._note_error(f"open {path}: {exc}")
                self._stop.wait(RECONNECT_DELAY)
                continue

            self._log(f"cart driver: connected to {path}")
            with self._lock:
                self._fd = fd
                self._tel["connected"] = True
                self._tel["last_error"] = ""
            self._last_moan.clear()
            try:
                self._pump(fd)
            finally:
                # Try to leave the cart stopped even if the link is going away.
                try:
                    os.write(fd, b"x\n")
                except OSError:
                    pass
                with self._lock:
                    self._fd = -1
                    self._tel["connected"] = False
                    self._tel["ps2_active"] = False
                    self._steer = self._speed = 0
                    self._deadline = 0.0
                os.close(fd)
            if not self._stop.is_set():
                self._stop.wait(RECONNECT_DELAY)

    def _pump(self, fd: int) -> None:
        buf = bytearray()
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 1.0)
            except OSError as exc:
                self._note_error(f"select: {exc}")
                return
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            except OSError as exc:
                self._note_error(f"read: {exc}")
                self._log(f"cart driver: serial read failed ({exc}); reconnecting")
                return
            if not chunk:
                self._log("cart driver: serial closed; reconnecting")
                return
            buf.extend(chunk)
            while b"\n" in buf:
                raw, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                self._on_line(raw.decode("utf-8", "replace").strip())
            if len(buf) > MAX_LINE:
                del buf[:-MAX_LINE // 2]

    def _on_line(self, text: str) -> None:
        """Interpret one telemetry line from the Pico."""
        if not text:
            return
        with self._lock:
            self._tel["last_line"] = text[:200]

        m = _RE_FB.search(text)
        if m:
            with self._lock:
                self._tel.update(
                    battery_v=float(m["bat"]), board_temp_c=float(m["temp"]),
                    speed_l=int(m["speedL"]), speed_r=int(m["speedR"]),
                    source=m["src"], mainboard_seen=True,
                    last_telemetry=time.monotonic(),
                    ps2_active=(m["src"].upper() == "PS2"))
            return

        # State changes worth reacting to rather than just logging.
        low = text.lower()
        if "ps2 controller connected" in low or "ps2 controller active" in low:
            with self._lock:
                self._tel["ps2_active"] = True
        elif "ps2 controller lost" in low:
            with self._lock:
                self._tel["ps2_active"] = False
        elif low.startswith("failsafe:"):
            # The firmware's own 2 s timer beat us to it — means our send loop
            # stalled, which is worth surfacing rather than swallowing.
            self._moan("fw-failsafe", f"cart: firmware failsafe fired ({text})")

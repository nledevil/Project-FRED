"""Host-side driver for the FRED-Cart hoverboard base, vision-governed.

The cart (see the Project-FRED-Cart repo) is a Pico that owns the hoverboard
mainboard; this host talks to it over USB serial with a deliberately tiny text
protocol:

    "<steer> <speed>\\n"   drive command (ignored while a PS2 controller is on)
    "x\\n"                 stop — always honoured

The Pico stops on its own after **2 s of host silence**, and the mainboard
stops if the Pico's stream dies — so this driver's keepalive thread re-sends
the current command at 10 Hz, and simply *going quiet* is itself a stop. The
PS2 controller always outranks us: an operator grabbing the controller takes
the cart away mid-move, which is the desired failure mode around people.

**Every command passes through the safety governor.** ``guard_cb`` (wired to
``SurroundVision.nav_assess``) is consulted on every keepalive tick — not just
when a command arrives — so a person stepping into the path *mid-move* scales
the speed down or stops the cart within a tick, whatever the original command
said. The governor can only ever reduce speed, never add motion.

Commands also carry a **time-to-live** (default 1 s): motion decays to a stop
unless the caller keeps renewing it. A web slider held down or an autonomy
loop refreshes naturally; a crashed caller, a dropped WiFi panel, or a
one-shot LLM tool call cannot leave the cart driving.

Degrades gracefully, same as every wrapper here: no pyserial or no Pico →
mock mode, commands are printed instead of sent, and the whole governor path
still runs — which is how this gets developed before the cart is plugged in.
"""
from __future__ import annotations

import threading
import time

try:
    import serial  # pyserial — shared with the sensor-node reader
    _SERIAL_ERR = None
except Exception as exc:  # noqa: BLE001
    serial = None
    _SERIAL_ERR = exc

# Firmware full scale is ±1000, but ~150 is already walking pace (~73 wheel
# RPM measured) and this base shares floors with children. Hard caps here,
# beneath whatever the caller asks for; the Pico ramps, so no lurching.
SPEED_CAP = 250
STEER_CAP = 400
KEEPALIVE_HZ = 10.0
DEFAULT_TTL = 1.0        # seconds a command survives without renewal


class Cart:
    """Serial link to the cart Pico: governed drive commands + status."""

    def __init__(self, port: str = "/dev/ttyACM1", baud: int = 115200,
                 guard_cb=None, log=None, max_speed: int = 150,
                 max_steer: int = 300, enabled: bool = False):
        self._port = str(port)
        self._baud = int(baud)
        # guard_cb(speed:int) -> {"factor": 0..1, "why": str} — SurroundVision.
        self._guard_cb = guard_cb
        self._log = log
        self.enabled = bool(enabled)      # master switch, persisted in settings
        self.max_speed = min(SPEED_CAP, abs(int(max_speed)))
        self.max_steer = min(STEER_CAP, abs(int(max_steer)))

        self._ser = None
        self._mock = serial is None
        self._lock = threading.Lock()     # guards _ser + targets
        self._steer = 0
        self._speed = 0
        self._until = 0.0                 # monotonic deadline for the command
        self._governed = 0                # last speed actually sent
        self._guard = {"factor": 1.0, "why": ""}
        self._last_feedback = ""          # latest raw line back from the Pico
        self._error = ""
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()

    # ---- capability / status ----------------------------------------------
    def available(self) -> bool:
        return self.enabled

    def status(self) -> dict:
        with self._lock:
            connected = self._ser is not None
            return {"enabled": self.enabled, "connected": connected,
                    "mock": self._mock or not connected,
                    "port": self._port, "steer": self._steer, "speed": self._speed,
                    "governed_speed": self._governed,
                    "guard": dict(self._guard),
                    "ttl_left": round(max(0.0, self._until - time.monotonic()), 2),
                    "feedback": self._last_feedback, "error": self._error}

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> bool:
        """Open the link and start the keepalive loop. Safe to call again."""
        if not self.enabled:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop_evt.clear()
            self._thread = threading.Thread(target=self._run, name="cart",
                                            daemon=True)
            self._thread.start()
        return True

    def shutdown(self) -> None:
        """Stop the cart and the loop (app exit / disable toggle)."""
        self.stop()
        self._stop_evt.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        with self._lock:
            self._thread = None
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._ser = None

    def set_port(self, port: str) -> None:
        """Point at a different serial device; the keepalive loop reconnects."""
        with self._lock:
            self._port = str(port)
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._ser = None

    def set_limits(self, max_speed: int | None = None,
                   max_steer: int | None = None) -> None:
        """Adjust the operator caps, still bounded by the hard module caps."""
        if max_speed is not None:
            self.max_speed = min(SPEED_CAP, abs(int(max_speed)))
        if max_steer is not None:
            self.max_steer = min(STEER_CAP, abs(int(max_steer)))

    # ---- commands -----------------------------------------------------------
    def drive(self, steer: int, speed: int, ttl: float = DEFAULT_TTL) -> dict:
        """Set the motion target (renew before ``ttl`` expires to keep moving).

        Clamped to the configured caps, then governed live by the vision guard.
        Returns the status dict so callers can see what the governor did.
        """
        if not self.enabled:
            return self.status()
        self.start()
        with self._lock:
            self._steer = max(-self.max_steer, min(self.max_steer, int(steer)))
            self._speed = max(-self.max_speed, min(self.max_speed, int(speed)))
            self._until = time.monotonic() + max(0.1, min(5.0, float(ttl)))
        return self.status()

    def stop(self) -> dict:
        """Zero the target and send the always-honoured 'x' immediately."""
        with self._lock:
            self._steer = self._speed = 0
            self._until = 0.0
            self._governed = 0
        self._send_line("x")
        return self.status()

    # ---- the loop -----------------------------------------------------------
    def _run(self) -> None:
        period = 1.0 / KEEPALIVE_HZ
        while not self._stop_evt.is_set():
            t0 = time.monotonic()
            self._connect_if_needed()
            with self._lock:
                steer, speed = self._steer, self._speed
                expired = t0 >= self._until
            if expired and (steer or speed):
                with self._lock:            # TTL ran out: decay to a stop
                    self._steer = self._speed = 0
                steer = speed = 0
            # Governor: consult the 360 camera every tick, moving or not-yet-
            # moving, so the factor in status() is always current.
            guard = {"factor": 1.0, "why": ""}
            if self._guard_cb is not None and speed != 0:
                try:
                    g = self._guard_cb(speed)
                    if isinstance(g, dict):
                        guard = {"factor": float(g.get("factor", 1.0)),
                                 "why": str(g.get("why", ""))}
                except Exception as exc:  # noqa: BLE001 - a broken guard must FAIL SAFE
                    guard = {"factor": 0.0, "why": f"guard error: {exc}"}
            governed = int(speed * max(0.0, min(1.0, guard["factor"])))
            with self._lock:
                was = self._guard
                self._guard = guard
                self._governed = governed
            if guard["why"] and guard["why"] != was.get("why") and self._log:
                self._log.event(f"🛞 Cart governor: {guard['why']}"
                                + (" — stopping" if governed == 0 else " — slowing"))
            self._send_line(f"{steer} {governed}" if (steer or governed) else "0 0")
            self._read_feedback()
            dt = time.monotonic() - t0
            if dt < period:
                self._stop_evt.wait(period - dt)
        self._send_line("x")               # loop exiting: leave the cart stopped

    def _connect_if_needed(self) -> None:
        if serial is None:
            return
        with self._lock:
            if self._ser is not None:
                return
        try:
            ser = serial.Serial(self._port, self._baud, timeout=0.05,
                                write_timeout=0.2)
            with self._lock:
                self._ser = ser
                self._error = ""
            print(f"[Cart] connected to {self._port} @ {self._baud}")
        except (OSError, serial.SerialException) as exc:
            with self._lock:
                if not self._error:
                    print(f"[Cart] {self._port} unavailable ({exc}) — mock mode "
                          f"until it appears")
                self._error = str(exc)

    def _send_line(self, line: str) -> None:
        with self._lock:
            ser = self._ser
        if ser is None:
            # Mock: log only *changes* so a bench run isn't 10 Hz of spam.
            if line != getattr(self, "_mock_last", None):
                self._mock_last = line
                if line not in ("0 0", "x"):
                    print(f"[Cart mock] -> {line}")
            return
        try:
            ser.write((line + "\n").encode("ascii"))
        except (OSError, serial.SerialException) as exc:
            with self._lock:
                self._error = str(exc)
                try:
                    ser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._ser = None               # keepalive loop reconnects

    def _read_feedback(self) -> None:
        """Drain whatever the Pico printed (debug/feedback lines) non-blockingly;
        keep the last line for the panel. Its absence is fine — the Pico only
        prints when something changes or debug output is on."""
        with self._lock:
            ser = self._ser
        if ser is None:
            return
        try:
            while ser.in_waiting:
                line = ser.readline().decode("utf-8", "replace").strip()
                if line:
                    with self._lock:
                        self._last_feedback = line
        except (OSError, serial.SerialException):
            pass                                # unplug is handled by the writer

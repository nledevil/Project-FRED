"""InMoov servo controller — a thin, safety-first wrapper around Adafruit ServoKit.

Design goals:
  * Never command a servo past its configured mechanical limits (angles are clamped).
  * Work with NO hardware attached (mock mode) so control logic can be developed and
    tested on any machine. Mock mode is auto-detected when /dev/i2c-1 is absent.
  * Address servos by name ("eye_x", "jaw", ...) rather than raw channel numbers.

Configuration lives in config/servos.json. See README.md for the schema.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "servos.json"


def load_config(path: str | Path = CONFIG_PATH) -> dict:
    with open(path) as f:
        return json.load(f)


class ServoController:
    """Controls named servos on a PCA9685 via Adafruit ServoKit.

    Parameters
    ----------
    config : dict, optional
        Parsed servos.json. Loaded from CONFIG_PATH if omitted.
    mock : bool, optional
        Force mock (no hardware I/O). If None, auto-detected from /dev/i2c-1.
    move_to_rest : bool
        Drive every servo to its rest_angle on startup (default True).
    """

    def __init__(self, config: dict | None = None, mock: bool | None = None,
                 move_to_rest: bool = True):
        self.config = config or load_config()
        self.servos = self.config["servos"]
        i2c = self.config.get("i2c", {})

        if mock is None:
            mock = not os.path.exists("/dev/i2c-1")
        self.mock = mock

        self._current: dict[str, float] = {}
        # Suspended = we've released the I2C bus / PCA9685 to another owner (e.g.
        # MyRobotLab). While suspended, _kit is None and every write is a no-op.
        self._suspended = False
        # Audit (dry run): the bus stays ours and every command is still clamped
        # and recorded, but no pulse is emitted. Unlike suspend(), get_angle()
        # keeps tracking the commanded position so the UI mirrors intent.
        self._audit = False
        # Serialises hardware I2C writes. Several threads drive servos without
        # coordinating through the web app's lock — the lip-sync jaw animation,
        # the face tracker, and the request handlers — and concurrent I2C
        # transactions on the PCA9685 corrupt/raise. This makes every write safe.
        self._io_lock = threading.Lock()

        if self.mock:
            self._kit = None
            print("[ServoController] MOCK mode — no hardware I/O "
                  "(no /dev/i2c-1 or mock forced).")
        else:
            # /dev/i2c-1 existing does not mean a PCA9685 is reachable: on x86 it
            # is an unrelated platform bus (SMBus/i915), and Blinka has no board
            # pins there at all. Fall back to mock rather than failing to boot —
            # the docstring promises this class runs on any machine.
            try:
                self._build_kit()
            except Exception as e:
                self.mock = True
                self._kit = None
                print(f"[ServoController] MOCK mode — /dev/i2c-1 present but the "
                      f"PCA9685 is unreachable ({type(e).__name__}: {e}).")

        if move_to_rest:
            self.rest()

    def _build_kit(self) -> None:
        """Open the PCA9685 over I2C and apply each servo's pulse/actuation range.
        Used at construction and again on resume() after a suspend() released it."""
        from adafruit_servokit import ServoKit
        i2c = self.config.get("i2c", {})
        self._kit = ServoKit(
            channels=i2c.get("channels", 16),
            address=i2c.get("address", 0x40),
            frequency=i2c.get("frequency", 50),
        )
        for name, s in self.servos.items():
            ch = s["channel"]
            self._kit.servo[ch].set_pulse_width_range(
                s["pulse_min_us"], s["pulse_max_us"])
            self._kit.servo[ch].actuation_range = s.get("actuation_range", 180)

    # ---- internal helpers -------------------------------------------------
    def _require(self, name: str) -> dict:
        if name not in self.servos:
            raise KeyError(f"Unknown servo {name!r}. "
                           f"Known: {sorted(self.servos)}")
        return self.servos[name]

    def _clamp(self, s: dict, angle: float) -> float:
        return max(s["min_angle"], min(s["max_angle"], angle))

    def _physical(self, s: dict, angle: float) -> float:
        """Apply the invert flag to map a logical angle to what the servo sees."""
        if s.get("invert"):
            return s.get("actuation_range", 180) - angle
        return angle

    # ---- public API -------------------------------------------------------
    def set_angle(self, name: str, angle: float, enforce_limits: bool = True) -> float:
        """Move `name` to `angle` degrees. Returns the actual (clamped) angle.

        With enforce_limits=True (default) the angle is clamped to the servo's
        configured [min_angle, max_angle]. With False it is clamped only to the
        servo's physical range [0, actuation_range] — used by calibration to
        find limits safely without letting them wander past the servo's travel.
        """
        s = self._require(name)
        if enforce_limits:
            target = self._clamp(s, angle)
        else:
            target = max(0.0, min(s.get("actuation_range", 180), angle))
        if self._suspended:
            return target        # bus released to another owner — don't touch it
        if target != angle:
            print(f"[ServoController] {name}: {angle:.1f}° clamped to {target:.1f}° "
                  f"(limits {s['min_angle']}–{s['max_angle']})")
        if self._audit:
            # Dry run: record where he *would* be, drive nothing. Recording is the
            # difference from suspend() — the panel's readout has to keep moving.
            self._current[name] = target
            return target
        phys = self._physical(s, target)
        if self.mock:
            print(f"[MOCK] {name} (ch{s['channel']}) -> {target:.1f}° (phys {phys:.1f}°)")
        else:
            with self._io_lock:
                self._kit.servo[s["channel"]].angle = phys
        self._current[name] = target
        return target

    def get_angle(self, name: str) -> float | None:
        """Last commanded angle for `name` (None if never moved)."""
        return self._current.get(name)

    def move_smooth(self, name: str, angle: float, duration: float = 0.5,
                    steps: int = 25) -> float:
        """Ease `name` from its current angle to `angle` over `duration` seconds."""
        s = self._require(name)
        target = self._clamp(s, angle)
        start = self._current.get(name, s["rest_angle"])
        if steps < 1:
            steps = 1
        for i in range(1, steps + 1):
            self.set_angle(name, start + (target - start) * i / steps)
            time.sleep(duration / steps)
        return target

    def rest(self) -> None:
        """Move every servo to its configured rest position."""
        for name, s in self.servos.items():
            self.set_angle(name, s["rest_angle"])

    def relax(self, name: str | None = None) -> None:
        """Cut the pulse so the servo(s) stop holding torque (frees them / saves power).

        Pass a name to relax one servo, or nothing to relax all.
        """
        if self._suspended or self._audit:
            return                          # bus released, or dry run: touch nothing
        names = [name] if name else list(self.servos)
        for n in names:
            s = self._require(n)
            if self.mock:
                print(f"[MOCK] relax {n} (ch{s['channel']})")
            else:
                with self._io_lock:
                    self._kit.servo[s["channel"]].angle = None  # release: no pulse

    def set_channel(self, name: str, channel: int) -> int:
        """Reassign which PCA9685 port drives `name`. Returns the new channel.

        Relaxes the old port (cuts its pulse) and applies this servo's pulse
        range + actuation range to the new port, mirroring what __init__ does.
        The servo's position on the new port is unknown until the next move, so
        the cached angle is cleared.
        """
        s = self._require(name)
        channel = int(channel)
        n_ch = self.config.get("i2c", {}).get("channels", 16)
        if not 0 <= channel < n_ch:
            raise ValueError(f"channel must be 0..{n_ch - 1}, got {channel}")
        old = s["channel"]
        if channel == old:
            return old
        # Not gated on audit: remapping a port cuts a pulse and sets pulse ranges,
        # it never commands an angle — so it moves nothing, and blocking it would
        # break calibration during an audit for no safety gain.
        if not self.mock and not self._suspended:
            with self._io_lock:
                self._kit.servo[old].angle = None            # stop driving old port
                self._kit.servo[channel].set_pulse_width_range(
                    s["pulse_min_us"], s["pulse_max_us"])
                self._kit.servo[channel].actuation_range = s.get("actuation_range", 180)
        else:
            print(f"[MOCK] remap {name}: ch{old} -> ch{channel}")
        s["channel"] = channel
        self._current.pop(name, None)                        # position now unknown
        return channel

    def identify(self, name: str, sweep: float = 12.0, cycles: int = 3,
                 dwell: float = 0.13) -> None:
        """Wiggle `name` around its rest angle so you can spot which servo it is.

        Stays within the servo's soft [min_angle, max_angle] limits, then returns
        to rest — a safe way to confirm a port maps to the servo you expect.
        """
        s = self._require(name)
        rest = s["rest_angle"]
        lo = max(s["min_angle"], rest - sweep)
        hi = min(s["max_angle"], rest + sweep)
        for _ in range(max(1, cycles)):
            self.set_angle(name, lo)
            time.sleep(dwell)
            self.set_angle(name, hi)
            time.sleep(dwell)
        self.set_angle(name, rest)

    # ---- audit (dry run) --------------------------------------------------
    def is_audit(self) -> bool:
        return self._audit

    def set_audit(self, on: bool) -> None:
        """Turn audit mode on/off. Idempotent.

        Entering audit deliberately does NOT relax: cutting the pulses would let
        the head sag under its own weight, and visible motion is the one thing
        this mode exists to prevent. The servos simply hold the pose they are
        already in and stop receiving new commands.
        """
        on = bool(on)
        if on == self._audit:
            return
        self._audit = on
        print(f"[ServoController] audit mode {'ON — motion suppressed' if on else 'OFF'}.")

    # ---- hardware handoff -------------------------------------------------
    def is_suspended(self) -> bool:
        return self._suspended

    def refresh_state(self) -> None:
        """No-op: this controller owns the hardware, so `mock`/`locked` are never
        out of date. Exists so reporting paths can call it on either controller
        without caring which one they hold (RemoteServoController's does work)."""

    def status(self) -> dict:
        """Link health, in RemoteServoController.status()'s shape. There is no
        link — the bus is on this machine — so it reports as permanently online
        with no host, and callers get one answer to ask either controller."""
        return {"host": None, "port": None, "online": True, "mock": self.mock,
                "suspended": self._suspended, "audit": self._audit,
                "locked": [], "error": None}

    def suspend(self) -> None:
        """Release the I2C bus / PCA9685 so another process (e.g. MyRobotLab) can
        drive the servos. Relaxes every servo (cuts pulses) while we still own the
        bus, then drops our handle. set_angle()/relax() become no-ops until
        resume(). Idempotent; safe in mock mode."""
        if self._suspended:
            return
        if not self.mock and self._kit is not None:
            self.relax()                     # cut all pulses before we let go
            with self._io_lock:
                try:
                    self._kit._pca.deinit()  # close the underlying I2C handle
                except Exception:            # noqa: BLE001 - best-effort; drop it regardless
                    pass
                self._kit = None
        self._suspended = True
        print("[ServoController] suspended — I2C/PCA9685 released.")

    def resume(self) -> None:
        """Re-open the I2C bus, re-apply servo ranges, and take the servos back to
        rest. Idempotent; safe in mock mode."""
        if not self._suspended:
            return
        self._suspended = False
        if not self.mock:
            with self._io_lock:
                self._build_kit()
        self.rest()
        print("[ServoController] resumed — I2C/PCA9685 reacquired.")

    # context-manager sugar: relax everything on exit
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.relax()
        return False

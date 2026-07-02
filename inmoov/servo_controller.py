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

        if self.mock:
            self._kit = None
            print("[ServoController] MOCK mode — no hardware I/O "
                  "(no /dev/i2c-1 or mock forced).")
        else:
            from adafruit_servokit import ServoKit
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

        if move_to_rest:
            self.rest()

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
        if target != angle:
            print(f"[ServoController] {name}: {angle:.1f}° clamped to {target:.1f}° "
                  f"(limits {s['min_angle']}–{s['max_angle']})")
        phys = self._physical(s, target)
        if self.mock:
            print(f"[MOCK] {name} (ch{s['channel']}) -> {target:.1f}° (phys {phys:.1f}°)")
        else:
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
        names = [name] if name else list(self.servos)
        for n in names:
            s = self._require(n)
            if self.mock:
                print(f"[MOCK] relax {n} (ch{s['channel']})")
            else:
                self._kit.servo[s["channel"]].angle = None  # release: no pulse

    # context-manager sugar: relax everything on exit
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.relax()
        return False

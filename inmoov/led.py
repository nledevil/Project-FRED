"""Tiny GPIO LED indicator.

Wraps RPi.GPIO behind on()/off() and degrades gracefully to a no-op when GPIO
isn't available (no hardware, wrong kernel, running on a laptop), mirroring how
``camera.py`` and ``servo_controller.py`` mock themselves out. RPi.GPIO is the
only GPIO backend that works in the venv on this rig's kernel.
"""
from __future__ import annotations

import threading

try:
    import RPi.GPIO as GPIO
    _IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001 - no GPIO here just means "no LED"
    GPIO = None
    _IMPORT_ERROR = exc


class Led:
    """A single GPIO output line driven high (on) / low (off).

    ``pin`` is a BCM GPIO number. ``available()`` is False when GPIO couldn't be
    set up, in which case on()/off() are silent no-ops.

    ``camera_indicator`` gates the camera-driven ``notify_camera()`` calls: when
    False the camera stops touching the LED, leaving it under manual control
    (``set()``/``on()``/``off()``). It does NOT affect direct manual calls.
    """

    def __init__(self, pin: int = 16, camera_indicator: bool = True):
        self.pin = pin
        self.camera_indicator = bool(camera_indicator)
        self._on = False
        self._ok = False
        self._lock = threading.Lock()
        if GPIO is not None:
            try:
                GPIO.setwarnings(False)
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
                self._ok = True
            except Exception:      # noqa: BLE001 - treat any setup failure as "no LED"
                self._ok = False

    def available(self) -> bool:
        return self._ok

    def set(self, on: bool) -> None:
        with self._lock:
            if not self._ok:
                return
            GPIO.output(self.pin, GPIO.HIGH if on else GPIO.LOW)
            self._on = bool(on)

    def on(self) -> None:
        self.set(True)

    def off(self) -> None:
        self.set(False)

    def notify_camera(self, on: bool) -> None:
        """Camera stream state changed. Drives the LED only when
        ``camera_indicator`` is enabled; otherwise it's a no-op so manual
        control isn't clobbered by the camera starting/stopping."""
        if self.camera_indicator:
            self.set(on)

    def status(self) -> dict:
        return {"available": self._ok, "on": self._on,
                "camera_indicator": self.camera_indicator}

    @property
    def is_on(self) -> bool:
        return self._on

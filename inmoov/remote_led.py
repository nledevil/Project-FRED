"""Client for the head Pi's LED device server — the status LED over the wire.

Drop-in for ``inmoov.led.Led``: same ``available()/set()/on()/off()/status()/
notify_camera()/is_on``, so ``web/app.py`` and ``camera.py`` cannot tell whether
the pin is on this machine's header or across the robot LAN. Pair with
``deploy/led_server.py`` on the head Pi.

**``camera_indicator`` stays here, not on the server.** The camera runs on the
NUC, so the question "should the stream starting light the LED?" is answered
next to the camera; the server only ever obeys. That also means the admin toggle
keeps behaving sensibly while the head is unreachable.

**Failure is a no-op, never an exception.** ``Led`` is the quietest thing in the
stack — it degrades to nothing when there is no GPIO, and every caller is
written expecting that. A network drop must look identical: the camera calls
``notify_camera()`` on every first/last viewer, and an unreachable Pi must not
turn a stream start into a traceback. ``available()`` therefore reports the last
known health rather than probing on the hot path, and errors print once per
transition instead of once per request.
"""
from __future__ import annotations

import threading
import time

import requests

_HEALTH_TTL = 5.0        # seconds; the LED is not on any control loop, so a
                         # slightly stale "is it there" answer costs nothing


class RemoteLed:
    """Drives the LED on a remote ``led_server.py``.

    Parameters
    ----------
    host, port : str, int
        Where ``led_server.py`` is listening (head Pi, :8083 by default).
    token : str
        Shared secret, sent as ``X-Led-Token``. Empty = no auth.
    timeout : float
        Per-request timeout. Short on purpose: a wedged LED write must not stall
        a camera start.
    camera_indicator : bool
        Whether ``notify_camera()`` drives the LED. Mirrors ``Led``.
    """

    def __init__(self, host: str, port: int = 8083, token: str = "",
                 timeout: float = 1.0, camera_indicator: bool = True):
        self.host = str(host).strip()
        self.port = int(port)
        self.token = token
        self.timeout = float(timeout)
        self.camera_indicator = bool(camera_indicator)
        self.pin: int | None = None
        self.online = False
        self.last_error: str | None = None
        self._on = False
        self._ok = False              # remote reports a working GPIO
        self._checked = 0.0
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._refresh(force=True)

    # ---- plumbing ---------------------------------------------------------
    @property
    def _base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _headers(self) -> dict:
        return {"X-Led-Token": self.token} if self.token else {}

    def _note(self, online: bool, error: str | None = None) -> None:
        """Log only on a transition, so a long outage prints once."""
        if online != self.online:
            if online:
                print(f"[RemoteLed] {self.host}:{self.port} online")
            else:
                print(f"[RemoteLed] {self.host}:{self.port} offline: {error}")
        self.online = online
        self.last_error = error

    def _request(self, method: str, path: str, payload: dict | None = None):
        resp = self._session.request(method, f"{self._base}{path}", json=payload,
                                     headers=self._headers(), timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _apply(self, data: dict) -> None:
        self._ok = bool(data.get("available", False))
        self._on = bool(data.get("on", False))
        if data.get("pin") is not None:
            self.pin = int(data["pin"])

    def _refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._checked) < _HEALTH_TTL:
            return
        self._checked = now
        try:
            self._apply(self._request("GET", "/api/health"))
            self._note(True)
        except Exception as exc:      # noqa: BLE001 - any failure means "no LED"
            self._ok = False
            self._note(False, str(exc))

    # ---- the Led interface ------------------------------------------------
    def available(self) -> bool:
        self._refresh()
        return self._ok

    def set(self, on: bool) -> None:
        with self._lock:
            try:
                self._apply(self._request("POST", "/api/led", {"on": bool(on)}))
                self._note(True)
            except Exception as exc:  # noqa: BLE001 - mirror Led's silence
                self._ok = False
                self._note(False, str(exc))

    def on(self) -> None:
        self.set(True)

    def off(self) -> None:
        self.set(False)

    def notify_camera(self, on: bool) -> None:
        """Camera stream state changed. Drives the LED only when
        ``camera_indicator`` is enabled, so manual control isn't clobbered."""
        if self.camera_indicator:
            self.set(on)

    def status(self) -> dict:
        return {"available": self.available(), "on": self._on,
                "camera_indicator": self.camera_indicator}

    @property
    def is_on(self) -> bool:
        return self._on

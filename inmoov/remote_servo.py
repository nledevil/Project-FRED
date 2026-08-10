"""Client for the head Pi's servo device server — servos over the wire.

The brain moved to the NUC; the PCA9685 did not, because the wiring is in the
head. This presents the head Pi's servos to the rest of the stack with the same
interface as ``ServoController``, so ``FaceTracker``, ``Assistant`` and the web
app cannot tell whether the I2C bus is six inches away or on the other end of an
ethernet cable. Pair with ``deploy/servo_server.py`` on the head.

Unlike ``DisplayClient`` — which talks to a decoration and can afford to be
stateless — this is on the face tracker's control path at ~15 Hz, so it keeps a
pooled keep-alive session. Re-opening a TCP connection per servo command would
add a handshake to every step of a PD loop.

**Feedback.** Every reply carries the angle the head *actually* applied after
clamping, and that is what gets cached — not what we asked for. Ask for 999° and
the cache ends up holding 96°, so ``get_angle()`` on the NUC keeps telling the
truth about where the hardware is. The alternative — trusting the request — would
drift silently from reality every time a limit bites.

**When the head is unreachable** the link degrades instead of exploding: moves
are clamped locally against the servo config we already hold, ``online`` goes
False, and the error is printed once per transition rather than per request. The
face tracker runs a loop; a dead network must not turn into a stack trace 15
times a second, and it must heal by itself when the Pi comes back.
"""
from __future__ import annotations

import threading

import requests


class RemoteServoError(Exception):
    """The servo server is unreachable, unauthorised, or refused the command."""


class RemoteServoController:
    """Drives servos on a remote ``servo_server.py``. Interface-compatible with
    ``ServoController`` so it can be swapped in wherever one is expected.

    Parameters
    ----------
    host, port : str, int
        Where ``servo_server.py`` is listening (head Pi, :8082 by default).
    token : str
        Shared secret, sent as ``X-Servo-Token``. Empty = no auth.
    timeout : float
        Per-request timeout. Deliberately short — a stalled servo write must not
        wedge the tracker loop.
    config : dict, optional
        Local ``servos.json``. Used for local clamping while offline; when the
        server is reachable its config wins, since it owns the hardware.
    """

    def __init__(self, host: str, port: int = 8082, token: str = "",
                 timeout: float = 1.5, config: dict | None = None):
        self.host = str(host).strip()
        self.port = int(port)
        self.token = token
        self.timeout = float(timeout)
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._current: dict[str, float] = {}
        self._suspended = False
        self.online = False
        self.last_error: str | None = None
        self.locked: set[str] = set()
        # Mirrors ServoController.mock: True means commands reach a controller
        # that isn't driving real hardware. Refreshed from the server's state.
        self.mock = True
        self._stale = False              # set on reconnect: config/locks need re-reading
        self._refreshing = False

        self.config = config or {"servos": {}}
        self.servos = self.config.get("servos", {})
        self._refresh()
        self._stale = False              # constructor just did it

    # ---- plumbing ---------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def _headers(self) -> dict:
        return {"X-Servo-Token": self.token} if self.token else {}

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """One call to the head. Raises RemoteServoError; callers on the hot path
        catch it and degrade rather than propagate."""
        try:
            r = self._session.request(method, self._url(path), json=payload,
                                      headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as e:
            self._go_offline(f"cannot reach servo server at {self.host}:{self.port} ({e})")
            raise RemoteServoError(f"servo server unreachable: {e}") from e
        try:
            data = r.json()
        except ValueError:
            self._go_offline(f"non-JSON reply (HTTP {r.status_code})")
            raise RemoteServoError(f"servo server returned non-JSON (HTTP {r.status_code})")
        # A 4xx means we reached a healthy server that refused this command (a
        # locked servo, a bad name) — that is "online" plus a rejected request,
        # not a broken link, so don't flap the connection state on it.
        self._go_online()
        if r.status_code >= 400 and r.status_code != 207:
            raise RemoteServoError(data.get("error") or f"servo error (HTTP {r.status_code})")
        return data

    def _go_offline(self, msg: str) -> None:
        if self.online or self.last_error is None:
            print(f"[RemoteServo] offline — {msg}")
        self.online = False
        self.last_error = msg

    def _go_online(self) -> None:
        if not self.online:
            print(f"[RemoteServo] online — {self.host}:{self.port}")
            # Re-read config/locks on every reconnect. If the NUC booted while
            # the head was down we never learned which servos are locked, and
            # after a head reboot that list may simply have changed. The server
            # refuses locked servos regardless, so this is about not sending
            # doomed requests (and not reporting stale locks in status()).
            self._stale = True
        self.online = True
        self.last_error = None

    def _refresh_if_stale(self) -> None:
        if not self._stale or self._refreshing:
            return
        self._refreshing = True          # _refresh() issues a request, which calls
        try:                             # _go_online() again — don't recurse
            self._refresh()
            self._stale = False
        finally:
            self._refreshing = False

    def _refresh(self) -> None:
        """Pull config + current angles from the head. The server owns the
        hardware, so its servo config is authoritative over any local copy."""
        try:
            state = self._request("GET", "/api/servos")
        except RemoteServoError:
            return
        servos = state.get("servos") or {}
        if servos:
            self.servos = {name: {k: v for k, v in s.items()
                                  if k not in ("current", "locked")}
                           for name, s in servos.items()}
            self.config = dict(self.config, servos=self.servos)
            for name, s in servos.items():
                if s.get("current") is not None:
                    self._current[name] = float(s["current"])
        self.locked = set(state.get("locked") or [])
        self.mock = bool(state.get("mock", True))
        self._suspended = bool(state.get("suspended", False))

    # ---- local fallbacks --------------------------------------------------
    def _require(self, name: str) -> dict:
        if name not in self.servos:
            raise KeyError(f"Unknown servo {name!r}. Known: {sorted(self.servos)}")
        return self.servos[name]

    def _clamp(self, s: dict, angle: float) -> float:
        return max(s["min_angle"], min(s["max_angle"], angle))

    # ---- public API (mirrors ServoController) -----------------------------
    def set_angle(self, name: str, angle: float, enforce_limits: bool = True) -> float:
        """Move `name` to `angle` degrees. Returns the angle the head actually
        applied, or — if the head is unreachable — the locally-clamped target."""
        self._refresh_if_stale()
        s = self._require(name)
        if enforce_limits:
            local = self._clamp(s, angle)
        else:
            local = max(0.0, min(s.get("actuation_range", 180), angle))
        if self._suspended or name in self.locked:
            return local
        try:
            data = self._request("POST", f"/api/servos/{name}",
                                 {"angle": float(angle),
                                  "enforce_limits": bool(enforce_limits)})
        except RemoteServoError:
            with self._lock:
                self._current[name] = local
            return local
        applied = float(data.get("angle", local))
        with self._lock:
            self._current[name] = applied
        return applied

    def set_angles(self, angles: dict[str, float],
                   enforce_limits: bool = True) -> dict[str, float]:
        """Move several servos in ONE round trip. Returns the applied angles.

        This is the call the face tracker wants: sending eye_x and eye_y
        separately doubles the round trips and lets the two axes land a request
        apart, which on a moving head is a visible stagger.
        """
        self._refresh_if_stale()
        wanted = {n: float(a) for n, a in angles.items()
                  if n in self.servos and n not in self.locked}
        if not wanted or self._suspended:
            return {}
        try:
            data = self._request("POST", "/api/servos",
                                 {"angles": wanted,
                                  "enforce_limits": bool(enforce_limits)})
        except RemoteServoError:
            applied = {n: self._clamp(self.servos[n], a) for n, a in wanted.items()}
            with self._lock:
                self._current.update(applied)
            return applied
        applied = {n: float(v) for n, v in (data.get("angles") or {}).items()}
        with self._lock:
            self._current.update(applied)
        return applied

    def get_angle(self, name: str) -> float | None:
        """Last angle the head reported applying (None if never moved)."""
        return self._current.get(name)

    def move_smooth(self, name: str, angle: float, duration: float = 0.5,
                    steps: int = 25) -> float:
        """Ease `name` to `angle` over `duration` seconds."""
        import time
        s = self._require(name)
        target = self._clamp(s, angle)
        start = self._current.get(name, s["rest_angle"])
        steps = max(1, steps)
        for i in range(1, steps + 1):
            self.set_angle(name, start + (target - start) * i / steps)
            time.sleep(duration / steps)
        return target

    def push_config(self, config: dict | None = None) -> dict:
        """Make `config` (default: our local copy) the head's servo config.

        The counterpart to `_refresh`: because the head owns the hardware, its
        servos.json wins on every refresh, so a calibration saved on this
        machine is invisible to the servos until it is pushed. The head
        validates, persists, and adopts it without moving anything; we then
        re-read so our cached limits are the head's, not our own.

        Returns the head's summary (updated/unchanged/outside_limits/...).
        Raises RemoteServoError if the head is unreachable or rejects it.
        """
        cfg = config if config is not None else self.config
        servos = cfg.get("servos") if isinstance(cfg, dict) else None
        if not isinstance(servos, dict) or not servos:
            raise RemoteServoError("nothing to push: config has no 'servos'")
        data = self._request("POST", "/api/config", {"servos": servos})
        self._stale = True
        self._refresh_if_stale()
        return data

    def rest(self) -> None:
        """Every servo to its rest position (locked ones are skipped by the head)."""
        try:
            data = self._request("POST", "/api/rest")
        except RemoteServoError:
            return
        with self._lock:
            self._current.update({n: float(v) for n, v in (data.get("angles") or {}).items()})

    def relax(self, name: str | None = None) -> None:
        """Cut the pulse so the servo(s) stop holding torque."""
        try:
            self._request("POST", "/api/relax", {"name": name} if name else {})
        except RemoteServoError:
            pass

    def set_channel(self, name: str, channel: int) -> int:
        """Reassign which PCA9685 port drives `name`."""
        self._require(name)
        data = self._request("POST", f"/api/servos/{name}/channel",
                             {"channel": int(channel)})
        ch = int(data.get("channel", channel))
        self.servos[name]["channel"] = ch
        self._current.pop(name, None)          # position on the new port is unknown
        return ch

    def identify(self, name: str, sweep: float = 12.0, cycles: int = 3,
                 dwell: float = 0.13) -> None:
        """Wiggle `name` so you can spot which servo it is."""
        self._require(name)
        self._request("POST", f"/api/servos/{name}/identify",
                      {"sweep": sweep, "cycles": cycles, "dwell": dwell})

    # ---- hardware handoff -------------------------------------------------
    def is_suspended(self) -> bool:
        return self._suspended

    def suspend(self) -> None:
        """Release the head's I2C bus to another owner (MyRobotLab). Idempotent."""
        if self._suspended:
            return
        try:
            self._request("POST", "/api/suspend")
        except RemoteServoError:
            pass
        self._suspended = True          # local intent holds even if the head is down,
                                        # so we don't keep writing to a released bus

    def resume(self) -> None:
        """Take the head's servos back. Idempotent."""
        if not self._suspended:
            return
        try:
            self._request("POST", "/api/resume")
        except RemoteServoError:
            return                      # stay suspended: we could not confirm the retake
        self._suspended = False
        self._refresh()

    def status(self) -> dict:
        """Link health, for the admin panel."""
        return {"host": self.host, "port": self.port, "online": self.online,
                "mock": self.mock, "suspended": self._suspended,
                "locked": sorted(self.locked), "error": self.last_error}

    # context-manager sugar: relax everything on exit
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.relax()
        return False

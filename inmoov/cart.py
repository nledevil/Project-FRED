"""The brain's client for the hoverboard cart on the chest Pi.

The cart's Pico hangs off the chest Pi (see deploy/display/cart_driver.py); this
is the deciding end of that link, and it is the same shape as display.py — short
timeouts, never raises into the web app, the base is not a dependency of the
robot standing still.

One thing here is genuinely different from every other client in this package,
and it is deliberate: **a single call cannot make the cart move for long.** The
chest Pi stops the cart if it does not hear a fresh command within its watchdog
window, so motion only continues while something keeps asking. That means:

* Teleop is naturally safe. The panel's joystick posts while it is held; let go,
  close the tab, walk out of WiFi range, and the cart stops on its own.
* Anything programmatic has to opt into a *duration*. ``nudge()`` is the only
  way to move without a human holding something down, and it takes a bounded
  time, runs the repeat loop itself, and stops at the end. There is no API here
  for "start moving and return" — that shape is how robots get away from you.

Only one motion runs at a time: a new nudge cancels the one in flight, so
"forward" immediately after "left" does what you meant rather than fighting.
"""
from __future__ import annotations

import threading
import time

import requests

# Matches cart_driver / the Pico firmware. Mirrored so the head can describe the
# range without a round trip; the chest clamps regardless.
STEER_LIMIT = 250
SPEED_LIMIT = 300

SEND_HZ = 10.0                # how fast nudge() repeats; must beat the watchdog
MAX_NUDGE_S = 5.0             # hard ceiling on one programmatic move


class CartError(Exception):
    """The cart is unreachable, disabled, or refusing to move."""


class CartClient:
    """Talks to the cart driver on the chest Pi."""

    def __init__(self, host: str = "", port: int = 8081, token: str = "",
                 timeout: float = 2.0):
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout
        self._lock = threading.Lock()
        self._motion: threading.Thread | None = None
        self._cancel = threading.Event()
        # Audit (dry run): drive commands are acknowledged but never sent. Stops
        # are deliberately NOT suppressed — see stop().
        self._audit = False
        # An optional extra speed cap on top of STEER_LIMIT/SPEED_LIMIT, set by
        # whoever owns policy (web/app.py, from event mode). None = firmware
        # limits only. Callable so it stays live; see _ceiling().
        self.speed_ceiling = None

    def configure(self, host: str | None = None, port: int | None = None,
                  token: str | None = None) -> None:
        if host is not None:
            self.host = host.strip()
        if port is not None:
            self.port = int(port)
        if token is not None:
            self.token = token

    def configured(self) -> bool:
        return bool(self.host)

    # -- transport ---------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self.configured():
            raise CartError("no cart configured")
        headers = {"X-Display-Token": self.token} if self.token else {}
        try:
            r = requests.request(method, f"http://{self.host}:{self.port}{path}",
                                 json=payload, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise CartError(f"cannot reach the cart at {self.host}:{self.port}") from e
        try:
            data = r.json()
        except ValueError:
            raise CartError(f"cart returned non-JSON (HTTP {r.status_code})") from None
        if r.status_code >= 400:
            raise CartError(data.get("error") or f"cart error (HTTP {r.status_code})")
        return data

    # -- state -------------------------------------------------------------
    def state(self) -> dict:
        return self._request("GET", "/api/cart")

    # -- audit (dry run) ---------------------------------------------------
    def is_audit(self) -> bool:
        return self._audit

    def set_audit(self, on: bool) -> None:
        """Turn audit mode on/off. Any nudge in flight is cancelled and the cart
        is really stopped on the way in, so nothing keeps rolling on a watchdog
        the audit is no longer feeding. Idempotent."""
        on = bool(on)
        if on == self._audit:
            return
        if on and self.configured():
            try:
                self.stop()                 # real stop, before drives go quiet
            except CartError:
                pass                        # unreachable: the watchdog has it anyway
        self._audit = on

    # -- motion ------------------------------------------------------------
    def _ceiling(self) -> int | None:
        """The current extra speed cap, or None for the firmware's own limits.

        Held as a callable rather than a number so a live switch cannot go stale
        here: event mode is flipped from the panel mid-event, and a copy taken
        at construction would keep driving at workshop speed in a hall full of
        children. See inmoov/event.py and where app.py assigns this.
        """
        source = self.speed_ceiling
        if source is None:
            return None
        try:
            value = source() if callable(source) else source
        except Exception:                       # noqa: BLE001 - never block a drive
            return None
        return int(value) if value else None

    def drive(self, steer, speed) -> dict:
        """One command. Authority expires at the chest's watchdog unless repeated.

        This is what the panel's joystick calls on every tick. Callers that are
        not a human holding a control should use nudge() instead.

        The cap is applied *here* rather than in nudge(), because nudge() calls
        this — so every path out of this class passes through one clamp, and a
        caller added later cannot route around it by accident.
        """
        cap = self._ceiling()
        if cap is not None:
            # Steer as well as speed: a fast spin next to a child is exactly as
            # bad as a fast run at one, and capping only forward motion would
            # have left the sharpest thing this base does uncapped.
            steer = max(-cap, min(int(steer), cap))
            speed = max(-cap, min(int(speed), cap))
        if self._audit:
            # Acknowledge without sending: nudge() checks this reply for an error
            # before it spawns, and the joystick expects a dict back every tick.
            return {"audit": True, "steer": steer, "speed": speed}
        return self._request("POST", "/api/cart/drive",
                             {"steer": steer, "speed": speed})

    def stop(self, estop: bool = False) -> dict:
        """Stop now, cancelling any nudge in flight.

        Never suppressed by audit mode. A stop that doesn't reach the hardware is
        the one failure this class must not have — and since audit never sends a
        drive, a stop it doesn't need is harmless.
        """
        self._cancel.set()
        return self._request("POST", "/api/cart/stop", {"estop": bool(estop)})

    def clear_estop(self) -> dict:
        return self._request("POST", "/api/cart/stop", {"clear_estop": True})

    def set_controller_mode(self, mode: str) -> dict:
        """Choose who may drive: ``off``, ``takeover`` or ``only``.

        Not suppressed by audit mode: this decides who is *allowed* to move the
        cart, it does not move it. Refusing to record that during a dry run
        would leave the panel showing a mode the chest never adopted.
        """
        return self._request("POST", "/api/cart/controller", {"mode": str(mode)})

    def nudge(self, steer, speed, seconds: float) -> dict:
        """Move for ``seconds``, then stop. The only programmatic way to move.

        Repeats the command at SEND_HZ for the duration — which is what keeps
        the chest's watchdog satisfied — and always issues a stop at the end,
        including if the caller cancels or the link fails partway through.

        Returns as soon as the motion is *started*, so a spoken reply isn't held
        up for the whole move; the caller gets told what was started, and the
        cart stops on its own regardless of what happens up here.
        """
        seconds = max(0.1, min(float(seconds), MAX_NUDGE_S))
        steer = max(-STEER_LIMIT, min(int(steer), STEER_LIMIT))
        speed = max(-SPEED_LIMIT, min(int(speed), SPEED_LIMIT))

        first = self.drive(steer, speed)        # fail fast, before spawning
        if first.get("error"):
            raise CartError(first["error"])

        with self._lock:
            self._cancel.set()                  # retire whatever was running
            if self._motion and self._motion.is_alive():
                self._motion.join(timeout=1.0)
            self._cancel = threading.Event()
            cancel = self._cancel
            t = threading.Thread(target=self._run_motion,
                                 args=(steer, speed, seconds, cancel),
                                 name="cart-nudge", daemon=True)
            self._motion = t
            t.start()

        out = dict(first)
        out["seconds"] = seconds
        return out

    def _run_motion(self, steer: int, speed: int, seconds: float,
                    cancel: threading.Event) -> None:
        deadline = time.monotonic() + seconds
        period = 1.0 / SEND_HZ
        try:
            while not cancel.is_set() and time.monotonic() < deadline:
                if cancel.wait(period):
                    break
                try:
                    self.drive(steer, speed)
                except CartError:
                    # The link went away. Stop trying — and note that the cart is
                    # already stopping on its own: the chest's watchdog expires
                    # in well under a second without us. That is the whole point
                    # of putting the watchdog down there.
                    return
        finally:
            if not cancel.is_set():
                try:
                    self.stop()
                except CartError:
                    pass                        # the watchdog has it

    def busy(self) -> bool:
        """True while a nudge is still running."""
        with self._lock:
            return bool(self._motion and self._motion.is_alive())

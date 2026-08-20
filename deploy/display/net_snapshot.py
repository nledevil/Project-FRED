"""Where the panel's facts come from: the three machines, polled off-thread.

This was settings_menu.py's network layer until the numpy menu retired; the Qt
panel is its only caller now. Nothing here draws.

Reads run on a background thread and writes are fire-and-forget on their own,
for the same reason: a POST that takes two seconds must not drop the frame
rate, and the poller reports what actually happened soon enough.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

NUC = "http://10.0.0.1:8080"
HEAD = "http://10.0.0.10:8082"
# Fetched once (see _chest): the preset list is fixed for our lifetime.
_ANIMATIONS: list = [None]

LOCAL = "http://127.0.0.1:8081"             # our own daemon, for close + chest state

POLL_EVERY = 2.0                            # seconds between refreshes
NET_TIMEOUT = 2.0                           # per request; the poller has its own thread


def _get(url: str, timeout: float = NET_TIMEOUT) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _post(url: str, payload: dict, timeout: float = NET_TIMEOUT) -> dict | None:
    headers = {"Content-Type": "application/json"}
    # Our own daemon may require a token. It spawned us, so we inherited it in
    # the environment — the head's panel gets it from its settings instead.
    if url.startswith(LOCAL) and os.environ.get("DISPLAY_TOKEN"):
        headers["X-Display-Token"] = os.environ["DISPLAY_TOKEN"]
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


class Net:
    """Polls the three machines on a background thread; pages read the snapshot.

    Writes are fire-and-forget on their own thread for the same reason reads are
    off the drawing path: a POST that takes two seconds must not drop the frame
    rate, and the poller will report what actually happened soon enough.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._snap: dict = {}
        self._networks: list = []
        self._scanning = False
        self._scanned_at = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="net-poll",
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            snap = dict(self._snap)
        at = snap.get("at")
        snap["age"] = (time.monotonic() - at) if at else None
        return snap

    def _loop(self) -> None:
        while not self._stop.is_set():
            snap: dict = {"at": time.monotonic()}
            state = _get(f"{NUC}/api/state")
            snap["nuc"] = state
            if state is None:
                snap["nuc_error"] = "NO ROUTE TO BRAIN"
            else:
                health = _get(f"{NUC}/api/health", timeout=1.5) or {}
                snap["nuc_temp"] = health.get("temp_c")
                # The brain's own view of the cart. Separate from the chest's
                # driver because they can disagree in a way that matters: the
                # cart can be plugged in and working while the brain has it
                # switched off in settings, and then nothing drives it.
                snap["nuc_cart"] = _get(f"{NUC}/api/cart", timeout=1.5)
                snap["hotspot"] = _get(f"{NUC}/api/hotspot", timeout=1.5)
                # The other radio: what FRED has joined, not what he hosts.
                snap["uplink"] = _get(f"{NUC}/api/uplink", timeout=1.5)
                # Names, addresses, versions and the inference device,
                # for the INFO tab. Brain-side because only it knows.
                snap["whoami"] = _get(f"{NUC}/api/whoami", timeout=1.5)

            head = _get(f"{HEAD}/api/health", timeout=1.5)
            snap["head"] = head
            if head is None:
                snap["head_error"] = "NO ROUTE TO HEAD"

            snap["chest"] = self._chest()
            snap["chest_at"] = time.monotonic()
            with self._lock:
                self._snap = snap
            self._stop.wait(POLL_EVERY)

    @staticmethod
    def _chest() -> dict:
        """Local facts, from our own daemon's API — the sensor relay's health.

        Asking over HTTP rather than importing sensor_relay: the relay's state
        lives in the *daemon's* process, and this is a different one.
        """
        out: dict = {}
        cart = _get(f"{LOCAL}/api/cart", timeout=1.0)
        if isinstance(cart, dict):
            out["cart"] = cart
        state = _get(f"{LOCAL}/api/state", timeout=1.0)
        if isinstance(state, dict):
            out["display"] = state
        # The preset list never changes while we are running, so it is fetched
        # once and carried in the snapshot rather than re-asked every 2s.
        if _ANIMATIONS[0] is None:
            listing = _get(f"{LOCAL}/api/animations", timeout=1.0)
            if isinstance(listing, dict) and isinstance(listing.get("animations"), list):
                _ANIMATIONS[0] = listing["animations"]
        if _ANIMATIONS[0]:
            out["animations"] = _ANIMATIONS[0]
        sensors = _get(f"{LOCAL}/api/sensors", timeout=1.0)
        if isinstance(sensors, dict):
            out["sensors"] = {
                "connected": bool(sensors.get("connected")),
                # Seconds since the last line off the Pico — the relay already
                # works this out, so we don't need a clock that agrees with it.
                "age": sensors.get("last_line_ago"),
                "node": ((sensors.get("last_payload") or {}).get("node") or ""),
                "error": sensors.get("last_error") or "",
            }
        return out

    # ---- writes -----------------------------------------------------------
    def post_voice(self, on: bool) -> None:
        self._fire(f"{NUC}/api/voice", {"on": bool(on)})

    def post_voice_at_boot(self, on: bool) -> None:
        """Whether the wake-word listener starts by itself after a reboot.

        A different thing from post_voice, which is about right now — this one
        is persisted on the brain and read by the boot path."""
        self._fire(f"{NUC}/api/voice", {"at_boot": bool(on)})

    def post_move(self, name: str, angle: float) -> None:
        """Move one servo. Fired and forgotten — see _fire."""
        self._fire(f"{NUC}/api/move", {"name": name, "angle": float(angle)})

    def post_rest(self) -> None:
        self._fire(f"{NUC}/api/rest", {})

    def post_relax(self) -> None:
        """Cut every servo's pulse. Brain-side, like rest — and the brain is
        also what stops face tracking first, so the loop cannot re-drive what
        this just released."""
        self._fire(f"{NUC}/api/relax", {})

    def post_hotspot(self, on: bool) -> None:
        """The access point lives on the *brain* — it has the AP-capable radio."""
        self._fire(f"{NUC}/api/hotspot", {"enabled": bool(on)})

    def post_hotspot_config(self, ssid: str, passphrase: str) -> None:
        """Rename the AP or change its password. Brain-side, like the toggle —
        the config and the radio are both there."""
        self._fire(f"{NUC}/api/hotspot/config",
                   {"ssid": str(ssid), "passphrase": str(passphrase)})

    def post_uplink_join(self, ssid: str, password: str) -> None:
        """Join a WiFi network. Brain-side: the client radio is the NUC's."""
        self._fire(f"{NUC}/api/uplink/join",
                   {"ssid": str(ssid), "password": str(password)})

    def post_uplink_forget(self, ssid: str) -> None:
        self._fire(f"{NUC}/api/uplink/forget", {"ssid": str(ssid)})

    def scan_uplink(self) -> None:
        """Start a scan on its own thread.

        A scan sweeps the band and takes seconds. Doing it on the drawing path
        would freeze the panel mid-tap, and doing it on the poller would make
        every page wait for something only one page cares about — so it runs
        when asked and leaves its answer where the page can pick it up.
        """
        with self._lock:
            if self._scanning:
                return
            self._scanning = True
        def work():
            got = _get(f"{NUC}/api/uplink/scan", timeout=40.0) or {}
            with self._lock:
                self._networks = got.get("networks") or []
                self._scanned_at = time.monotonic()
                self._scanning = False
        threading.Thread(target=work, name="uplink-scan", daemon=True).start()

    def scan_state(self) -> dict:
        with self._lock:
            return {"busy": self._scanning, "networks": list(self._networks),
                    "at": self._scanned_at}

    def post_restore(self) -> None:
        """Hand the screen back to whatever was showing before the menu.
        Best effort by design — if the daemon is gone, so is the screen."""
        self._fire(f"{LOCAL}/api/animation/restore", {})

    def post_animation(self, animation: str) -> None:
        """Switch what the chest screen is showing. Local, like the cart mode:
        the animation child is ours, so this works with the brain switched off."""
        self._fire(f"{LOCAL}/api/animation", {"animation": str(animation)})

    def post_cart_controller(self, mode: str) -> None:
        """Set who may drive. Sent to our *own* daemon, not the brain: the chest
        owns the arbitration, so this still works with the brain switched off."""
        self._fire(f"{LOCAL}/api/cart/controller", {"mode": str(mode)})

    def post_cart_stop(self) -> None:
        """Latch the e-stop. Local for the same reason the mode is, and more so:
        this is the control you reach for when something is going wrong, which is
        exactly when the brain or the wire to it may be part of what is wrong."""
        self._fire(f"{LOCAL}/api/cart/stop", {"estop": True})

    def post_poweroff(self, machine: str) -> bool:
        """Ask a machine to power itself off. True if it accepted.

        Synchronous, unlike every other write here, because the caller is
        stepping through the machines in a required order and needs to know each
        one took the request before it takes away that machine's network. A
        failure has to be visible too — a head that quietly 401s is a head left
        running in a crate, which is the whole thing this is meant to prevent.
        """
        url = {"nuc": f"{NUC}/api/poweroff", "head": f"{HEAD}/api/poweroff"}.get(machine)
        if url is None:
            return False
        return _post(url, {}, timeout=6.0) is not None

    def post_cart_clear_estop(self) -> None:
        self._fire(f"{LOCAL}/api/cart/stop", {"clear_estop": True})

    @staticmethod
    def _fire(url: str, payload: dict) -> None:
        """Send without waiting. A servo drag posts every 60ms and the reply
        carries nothing the panel needs — the poller reports where the servo
        actually ended up, which is the honest answer anyway. Blocking here
        would tie the frame rate to the round trip to the head."""
        threading.Thread(target=_post, args=(url, payload), daemon=True).start()

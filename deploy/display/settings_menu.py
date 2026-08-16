#!/usr/bin/env python3
"""The chest panel's settings menu — FRED's health and controls, on the robot.

Runs as an animation child of display_control.py and keeps that contract exactly:
it owns /dev/fb0 while it lives, and it exits on SIGTERM. The supervisor cannot
tell it apart from the arc reactor, which is why it needed no new lifecycle.

Why a child at all, rather than something the daemon draws: the animation child
mmaps the framebuffer *exclusively*, so while one is alive nothing else can put
a pixel on the screen. A menu that overlaid the animation would have to be drawn
by every animation, which is fine for a 48-pixel cog and absurd for a UI. So the
menu takes the screen, and hands it back on close.

**Closing.** There is no "previous animation" recorded here; the daemon knows
what was showing and restores it. This just asks its own daemon on localhost to
switch away, then exits. If that call fails it exits anyway — the supervisor's
watchdog respawns *something*, and a menu you cannot leave is the worst outcome.

**The network is never on the drawing path.** A poller thread refreshes a
snapshot on its own clock with short timeouts; draw() only ever reads the last
snapshot. A brain that has gone away therefore makes the page say so, at 30fps,
instead of freezing the panel for the length of a TCP timeout.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import menu_ui as ui                        # noqa: E402 — sibling module
from fb import Framebuffer, hide_cursor     # noqa: E402
from page_cart import CartPage              # noqa: E402
from page_display import DisplayPage        # noqa: E402
from page_info import InfoPage              # noqa: E402
from power_menu import PowerMenu            # noqa: E402
from page_wireless import WirelessPage      # noqa: E402
from page_servos import ServosPage          # noqa: E402
from page_status import StatusPage          # noqa: E402
from page_voice import VoicePage            # noqa: E402
from touch import open_touch                # noqa: E402
import pin_gate                             # noqa: E402 — sibling module

NUC = "http://10.0.0.1:8080"
HEAD = "http://10.0.0.10:8082"
# Fetched once (see _chest): the preset list is fixed for our lifetime.
_ANIMATIONS: list = [None]

LOCAL = "http://127.0.0.1:8081"             # our own daemon, for close + chest state

POLL_EVERY = 2.0                            # seconds between refreshes
NET_TIMEOUT = 2.0                           # per request; the poller has its own thread
FPS = 30

# The tabs, left to right. Module level so a harness can render the real strip
# — order and labels included — instead of keeping its own copy, which is how a
# tool ended up drawing a WIRELESS tab months after it became WIFI.
PAGES = (StatusPage, VoicePage, ServosPage, CartPage,
         DisplayPage, WirelessPage, InfoPage)

# Chrome layout, 800x480.
TITLE_H = 56
# The tabs start 8px *below* the title bar, not on its last pixel. At TAB_Y=56
# they shared an edge with it, and in the soft theme — where both the bar and a
# tab are filled panels in nearly the same tone — the row read as part of the
# header rather than as controls under it. The outlined themes got away with it.
TAB_Y, TAB_H, TAB_GAP = 64, 30, 8
TAB_X0, TAB_X1 = 24, 776        # the strip's span; tabs divide it evenly
# The gap between tabs must clear the HUD theme's glow on both sides, or the
# selected tab lights up its neighbours and the highlight looks like it is on
# the wrong button. Narrowing it to 4 bought label width nothing needed: the
# widest tab is DISPLAY at 80px of the 84 available, and it fits at 8.
# The strip is 30 tall rather than 34 for the same reason vertically: its glow
# has to stop above the first row of whatever page is showing.
CLOSE = (700, 8, 792, 48)
POWER = (596, 8, 692, 48)       # left of the X; see power_menu.py


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

    def post_move(self, name: str, angle: float) -> None:
        """Move one servo. Fired and forgotten — see _fire."""
        self._fire(f"{NUC}/api/move", {"name": name, "angle": float(angle)})

    def post_rest(self) -> None:
        self._fire(f"{NUC}/api/rest", {})

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


def close_menu() -> None:
    """Ask our daemon to put the animation back. Best effort by design."""
    _post(f"{LOCAL}/api/animation/restore", {}, timeout=1.5)


def main() -> int:
    ap = argparse.ArgumentParser(description="Chest panel settings menu")
    ap.add_argument("--fps", type=int, default=FPS)
    ap.add_argument("--page", default="",
                    help="open on this tab (status/voice/servos). For testing a "
                         "page without a finger — the daemon never passes it.")
    args = ap.parse_args()

    fb = Framebuffer()
    hide_cursor()
    touch = open_touch(width=fb.w, height=fb.h)

    pages = [cls() for cls in PAGES]
    current = next((i for i, p in enumerate(pages)
                    if p.title.lower() == args.page.strip().lower()), 0)
    # Width is computed, not fixed: five tabs at the old 150px ran off the
    # panel, and the next page added would have done it again silently.
    span = TAB_X1 - TAB_X0
    tab_w = (span - TAB_GAP * (len(pages) - 1)) // len(pages)
    # Scale is picked to fit, not fixed. The width already shrinks with each
    # page added (five tabs at the old fixed 150px ran off the panel); the
    # labels did not, so the next tab would have overflowed its box onto its
    # neighbour instead — visibly wrong, and silently so in a test that only
    # checked geometry. One scale for the whole row: tabs at mixed sizes look
    # broken rather than tidy, so the widest label sets the size for all seven.
    tab_scale = ui.scale_to_fit_all([p.title for p in pages], tab_w, 2)
    tabs = [ui.Button(TAB_X0 + i * (tab_w + TAB_GAP), TAB_Y,
                      TAB_X0 + i * (tab_w + TAB_GAP) + tab_w, TAB_Y + TAB_H,
                      p.title, scale=tab_scale)
            for i, p in enumerate(pages)]
    close = ui.Button(*CLOSE, "X", scale=3)
    power = ui.Button(*POWER, "POWER", scale=2)
    # log defaults to print, which is where the rest of this file's diagnostics
    # already go — the daemon captures the child's stdout.
    power_menu = PowerMenu()

    net = Net()
    net.start()

    # The gate, if there is one. Built before the loop so the brain is asked
    # once, here, rather than on every frame — and so a slow answer costs the
    # menu's opening beat instead of its frame rate.
    gate = pin_gate.PinPad(pin_gate.material(NUC))
    if not gate.unlocked:
        print("menu: locked - PIN required")

    running = [True]
    signal.signal(signal.SIGTERM, lambda *a: running.__setitem__(0, False))
    signal.signal(signal.SIGINT, lambda *a: running.__setitem__(0, False))

    frame = np.zeros((fb.h, fb.w, 3), dtype=np.float32)
    period = 1.0 / max(1, args.fps)
    leaving = False

    try:
        while running[0]:
            started = time.monotonic()

            for kind, x, y in (touch.poll(0.0) if touch else []):
                # The chrome only reacts to a press. Everything else — including
                # 'move' and 'up', which a slider needs and a button does not —
                # goes to the page, which decides what it cares about.
                if power_menu.on_touch(kind, x, y, net):
                    continue
                if kind == "down":
                    if gate.unlocked and power.hit(x, y):
                        power_menu.show()
                        continue
                    if close.hit(x, y):
                        leaving = True
                        running[0] = False
                        break
                    # X closes the menu while locked — being unable to leave a
                    # screen you cannot get past would be its own trap — but the
                    # tabs do not exist yet.
                    if gate.unlocked and any(tab.hit(x, y) for tab in tabs):
                        current = next(i for i, t in enumerate(tabs) if t.hit(x, y))
                        continue
                if gate.unlocked:
                    # getattr because a page with nothing to press is a normal
                    # kind of page, and forgetting the empty method should cost
                    # a dead tab at worst. It cost the whole menu: INFO shipped
                    # without one, the 'up' of the tap that selected it landed
                    # here, and the crash took the process down to the PIN gate.
                    handler = getattr(pages[current], "on_touch", None)
                    if handler is not None:
                        handler(kind, x, y, net)
                else:
                    gate.on_touch(kind, x, y, net)

            snap = net.snapshot()
            frame[:] = np.asarray(ui.BG, dtype=np.float32)
            ui.fill(frame, 0, 0, fb.w, TITLE_H, ui.PANEL)
            ui.text(frame, "FRED SETTINGS", 24, 16, ui.INK, 3)
            close.draw(frame, ink=ui.INK)
            if gate.unlocked:
                power.draw(frame, ink=ui.INK)
            if gate.unlocked:
                for i, tab in enumerate(tabs):
                    tab.draw(frame, on=(i == current),
                             ink=ui.INK if i == current else ui.DIM_INK)
                pages[current].draw(frame, snap)
            else:
                gate.draw(frame, snap)
            power_menu.draw(frame, snap)
            # Clip before the blit, exactly as every animation does. Text is
            # *added* into the frame, so ink on a panel background runs past 255
            # (120+18, 210+40, 255+54) and astype(uint8) wraps rather than
            # saturates: blue 309 becomes 53, and the menu comes out green with
            # magenta labels. It looks like a colour-order bug and isn't.
            np.clip(frame, 0, 255, out=frame)
            fb.show(frame.astype(np.uint8))

            slack = period - (time.monotonic() - started)
            if slack > 0:
                time.sleep(slack)
    finally:
        net.stop()
        if touch:
            touch.close()
        fb.clear()
        fb.close()
        if leaving:
            # Only when the user pressed X. On SIGTERM the daemon is already
            # switching us out, and asking it to switch again would race.
            close_menu()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

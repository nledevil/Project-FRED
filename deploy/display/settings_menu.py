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
from page_wireless import WirelessPage      # noqa: E402
from page_servos import ServosPage          # noqa: E402
from page_status import StatusPage          # noqa: E402
from page_voice import VoicePage            # noqa: E402
from touch import open_touch                # noqa: E402

NUC = "http://10.0.0.1:8080"
HEAD = "http://10.0.0.10:8082"
LOCAL = "http://127.0.0.1:8081"             # our own daemon, for close + chest state

POLL_EVERY = 2.0                            # seconds between refreshes
NET_TIMEOUT = 2.0                           # per request; the poller has its own thread
FPS = 30

# Chrome layout, 800x480.
TITLE_H = 56
TAB_Y, TAB_H, TAB_GAP = 56, 34, 8
TAB_X0, TAB_X1 = 24, 776        # the strip's span; tabs divide it evenly
CLOSE = (700, 8, 792, 48)


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

    def post_cart_controller(self, mode: str) -> None:
        """Set who may drive. Sent to our *own* daemon, not the brain: the chest
        owns the arbitration, so this still works with the brain switched off."""
        self._fire(f"{LOCAL}/api/cart/controller", {"mode": str(mode)})

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

    pages = [StatusPage(), VoicePage(), ServosPage(), CartPage(),
             WirelessPage()]
    current = next((i for i, p in enumerate(pages)
                    if p.title.lower() == args.page.strip().lower()), 0)
    # Width is computed, not fixed: five tabs at the old 150px ran off the
    # panel, and the next page added would have done it again silently.
    span = TAB_X1 - TAB_X0
    tab_w = (span - TAB_GAP * (len(pages) - 1)) // len(pages)
    tabs = [ui.Button(TAB_X0 + i * (tab_w + TAB_GAP), TAB_Y,
                      TAB_X0 + i * (tab_w + TAB_GAP) + tab_w, TAB_Y + TAB_H,
                      p.title, scale=2)
            for i, p in enumerate(pages)]
    close = ui.Button(*CLOSE, "X", scale=3)

    net = Net()
    net.start()

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
                if kind == "down":
                    if close.hit(x, y):
                        leaving = True
                        running[0] = False
                        break
                    if any(tab.hit(x, y) for tab in tabs):
                        current = next(i for i, t in enumerate(tabs) if t.hit(x, y))
                        continue
                pages[current].on_touch(kind, x, y, net)

            snap = net.snapshot()
            frame[:] = np.asarray(ui.BG, dtype=np.float32)
            ui.fill(frame, 0, 0, fb.w, TITLE_H, ui.PANEL)
            ui.text(frame, "FRED SETTINGS", 24, 16, ui.INK, 3)
            close.draw(frame, ink=ui.INK)
            for i, tab in enumerate(tabs):
                tab.draw(frame, on=(i == current),
                         ink=ui.INK if i == current else ui.DIM_INK)
            pages[current].draw(frame, snap)
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

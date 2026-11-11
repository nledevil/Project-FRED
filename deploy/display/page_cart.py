"""Who may drive the cart, and how to stop it — chosen from the robot itself.

Three modes, as three buttons rather than a dropdown: this is a 7" panel you
prod with a thumb while standing over a 350 lb base, and a list that opens,
scrolls and closes is the wrong control for that. Each mode is one tap, and the
one in force is lit.

    DISABLED    the hand controller is ignored; the panel and Claude drive
    MAY TAKE OVER   holding R1 seizes the cart mid-drive, as the PS2 pad did
    CONTROLLER ONLY host drive commands are refused outright

The mode lives on the chest (cart_driver owns the arbitration, the watchdog and
the e-stop), so this page talks to the *local* daemon rather than the brain —
it keeps working with the brain switched off, which is exactly when you are
most likely to be standing here wanting the controller.

**The right half is the e-stop**, and it is the reason the modes gave up their
width. The person standing next to a moving base is the one holding this
touchscreen; until now the only stop control was on the web panel, i.e. on a
laptop, i.e. not in the hands of whoever can see the problem. It posts the same
``/api/cart/stop`` the panel does, to our own daemon — the control you reach for
when something is going wrong should not depend on the brain, or the wire to it,
being among the things that are still working.

Stopping and un-stopping are deliberately asymmetric:

* **Stop is one tap, always live.** No confirm, never greyed out, no check that
  the cart looks reachable first — a stop that hesitates is not a stop, and
  ``/api/cart/stop`` answers even with no driver running precisely so that a
  caller in trouble always has something to press.
* **Clearing asks twice.** Releasing a latched e-stop re-arms a 350 lb machine,
  which is not something a stray thumb on a screen you are carrying should be
  able to do. The second tap must land within CONFIRM_S or the arming lapses.

Under the modes: battery voltage and board temperature. They were only on the
STATUS page, which is the wrong tab to be reading while a 350 lb base is moving
— this is where your eyes already are.

Below all of it: whether a controller is actually connected and whether the
deadman is held right now. "I selected the mode and nothing happens" is then
answerable on the same screen.
"""
from __future__ import annotations

import time


MODES = (
    ("off", "DISABLED", "CONTROLLER IGNORED"),
    ("takeover", "MAY TAKE OVER", "HOLD R1 TO SEIZE THE CART"),
    ("only", "CONTROLLER ONLY", "PANEL AND CLAUDE REFUSED"),
)

BTN_X0, BTN_X1 = 24, 470            # the mode column; the stop button owns the rest
BTN_Y0, BTN_H, BTN_GAP = 110, 56, 10
TEL_Y0, TEL_Y1 = 302, 356           # battery and board temperature, under the modes
STOP_X0, STOP_X1 = 490, 776
STOP_Y0, STOP_Y1 = 110, 356
STATUS_Y = 370
LINE_H = 26

# The pack, from Project-FRED-Cart's README: 10S Li-ion, ~36 V nominal, 42 V
# charged. LOW_V is 3.3 V/cell, the conventional point to stop drawing from
# Li-ion — not a firmware constant, so it is a hint and coloured like one.
FULL_V, NOMINAL_V, LOW_V = 42.0, 36.0, 33.0

CONFIRM_S = 4.0                     # how long a "tap again to clear" stays armed


class CartPage:
    title = "CART"

    def __init__(self):
        self._pending: str | None = None    # mode asked for, until the poll confirms
        self._armed_at = 0.0                # monotonic time of the first CLEAR tap
        self._latched = False
    def _on_stop_tap(self, net) -> None:
        if not self._latched:
            self._armed_at = 0.0
            net.post_cart_stop()
            return
        if self._is_armed():
            self._armed_at = 0.0
            net.post_cart_clear_estop()
        else:
            self._armed_at = time.monotonic()

    def _is_armed(self) -> bool:
        return bool(self._armed_at) and (time.monotonic() - self._armed_at) <= CONFIRM_S

    def view(self, snap: dict) -> dict:
        """The page as data: the modes, the stop, the telemetry and the reason
        the cart is not moving.

        Split out of draw() so the Qt panel reaches the same conclusions —
        which of the three modes hands a moving machine to a hand controller,
        and why a command would be refused — without a second copy of them.
        """
        cart = (snap.get("chest") or {}).get("cart") or {}
        current = cart.get("controller_mode")
        if current and current == self._pending:
            self._pending = None            # the chest caught up
        shown = self._pending or current
        reachable = bool(cart)

        self._latched = bool(cart.get("estop"))
        if not self._latched:
            self._armed_at = 0.0            # cleared, by us or by anyone else

        modes = []
        for mode, label, _hint in MODES:
            on = (mode == shown)
            ink = "ink" if reachable else "dim"
            if on and mode != "off":
                ink = "warn" if mode == "takeover" else "ok"
            modes.append({"mode": mode, "label": label, "on": on, "ink": ink})

        pad = cart.get("controller") or {}
        if not pad.get("connected"):
            pad_line, pad_ink = "CONTROLLER NOT CONNECTED", "dim"
        elif pad.get("deadman"):
            # Words, not signed numbers: a sign is something you decode, and
            # this is read while a 350 lb base is moving.
            speed, steer = pad.get("speed", 0.0), pad.get("steer", 0.0)
            way = "FWD" if speed > 0 else ("REV" if speed < 0 else "IDLE")
            turn = "RIGHT" if steer > 0 else ("LEFT" if steer < 0 else "STRAIGHT")
            pad_line = f"DRIVING - {way} {abs(speed):.2f} {turn} {abs(steer):.2f}"
            pad_ink = "ok"
        else:
            pad_line, pad_ink = "CONTROLLER CONNECTED - R1 NOT HELD", "ink"

        if self._latched:
            why, why_ink = "E-STOP LATCHED - NOTHING WILL MOVE", "bad"
        elif cart.get("host_locked"):
            why, why_ink = "DEADMAN RELEASED - HOST MUST COMMAND AGAIN", "warn"
        elif not cart.get("connected"):
            why, why_ink = "CART PICO NOT PLUGGED IN", "dim"
        else:
            why, why_ink = "", "dim"

        return {
            "modes": modes,
            "hint": next((h for m, _l, h in MODES if m == shown), ""),
            "reachable": reachable,
            "saving": bool(self._pending),
            "latched": self._latched,
            "armed": self._is_armed(),
            "stopLabel": ("TAP AGAIN" if self._is_armed()
                          else ("CLEAR" if self._latched else "STOP")),
            "padLine": pad_line, "padInk": pad_ink,
            "why": why, "whyInk": why_ink,
            "volts": cart.get("battery_v"), "tempC": cart.get("board_temp_c"),
        }

    def pick_mode(self, mode: str, net) -> None:
        self._armed_at = 0.0        # a tap elsewhere is not a confirmation
        self._pending = mode        # lit immediately; the poll confirms
        net.post_cart_controller(mode)

    def stop_tap(self, net) -> None:
        self._on_stop_tap(net)

    # ---- drawing ----------------------------------------------------------


"""Who may drive the cart, chosen from the robot itself.

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

Below the buttons: whether a controller is actually connected and whether the
deadman is held right now. "I selected the mode and nothing happens" is then
answerable on the same screen.
"""
from __future__ import annotations

import menu_ui as ui

MODES = (
    ("off", "DISABLED", "CONTROLLER IGNORED"),
    ("takeover", "MAY TAKE OVER", "HOLD R1 TO SEIZE THE CART"),
    ("only", "CONTROLLER ONLY", "PANEL AND CLAUDE REFUSED"),
)

BTN_X0, BTN_X1 = 24, 776
BTN_Y0, BTN_H, BTN_GAP = 104, 64, 12
STATUS_Y = 330


class CartPage:
    title = "CART"

    def __init__(self):
        self._buttons = []
        for i, (mode, label, _hint) in enumerate(MODES):
            y = BTN_Y0 + i * (BTN_H + BTN_GAP)
            self._buttons.append((mode, ui.Button(BTN_X0, y, BTN_X1, y + BTN_H,
                                                  label, scale=3)))
        self._pending: str | None = None

    # ---- input ------------------------------------------------------------
    def on_touch(self, kind: str, x: int, y: int, net) -> None:
        if kind != "down":
            return
        for mode, button in self._buttons:
            if button.hit(x, y):
                self._pending = mode        # lit immediately; the poll confirms
                net.post_cart_controller(mode)
                return

    # ---- drawing ----------------------------------------------------------
    def draw(self, frame, snap: dict) -> None:
        cart = (snap.get("chest") or {}).get("cart") or {}
        current = cart.get("controller_mode")
        if current and current == self._pending:
            self._pending = None            # the chest caught up
        shown = self._pending or current

        reachable = bool(cart)
        for mode, button in self._buttons:
            on = (mode == shown)
            ink = ui.INK if reachable else ui.DIM_INK
            if on and mode != "off":
                # The two modes that hand a moving machine to a hand controller
                # are worth colouring differently from "off".
                ink = ui.WARN_INK if mode == "takeover" else ui.OK_INK
            button.draw(frame, on=on, ink=ink)

        hint = next((h for m, _l, h in MODES if m == shown), "")
        if hint:
            ui.text(frame, hint, BTN_X0, STATUS_Y, ui.DIM_INK, 2)

        if not reachable:
            ui.text(frame, "NO CART DRIVER ON THIS PI", BTN_X0, STATUS_Y + 30,
                    ui.BAD_INK, 2)
            return
        if self._pending:
            ui.text(frame, "SAVING...", BTN_X0, STATUS_Y + 30, ui.DIM_INK, 2)

        pad = cart.get("controller") or {}
        if not pad.get("connected"):
            line, ink = "CONTROLLER NOT CONNECTED", ui.DIM_INK
        elif pad.get("deadman"):
            # Words, not signed numbers: the font has no "+" glyph (it would
            # render as a blank and read as a lost character), and "FWD / LEFT"
            # is what you actually want to read while watching the base move.
            speed, steer = pad.get("speed", 0.0), pad.get("steer", 0.0)
            way = "FWD" if speed > 0 else ("REV" if speed < 0 else "IDLE")
            turn = "RIGHT" if steer > 0 else ("LEFT" if steer < 0 else "STRAIGHT")
            line, ink = (f"DRIVING - {way} {abs(speed):.2f} {turn} {abs(steer):.2f}",
                         ui.OK_INK)
        else:
            line, ink = "CONTROLLER CONNECTED - R1 NOT HELD", ui.INK
        ui.text(frame, line, BTN_X0, STATUS_Y + 60, ink, 2)

        if cart.get("estop"):
            ui.text(frame, "E-STOP LATCHED - NOTHING WILL MOVE", BTN_X0,
                    STATUS_Y + 90, ui.BAD_INK, 2)
        elif not cart.get("connected"):
            ui.text(frame, "CART PICO NOT PLUGGED IN", BTN_X0, STATUS_Y + 90,
                    ui.DIM_INK, 2)

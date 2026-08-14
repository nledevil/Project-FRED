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

import menu_ui as ui

MODES = (
    ("off", "DISABLED", "CONTROLLER IGNORED"),
    ("takeover", "MAY TAKE OVER", "HOLD R1 TO SEIZE THE CART"),
    ("only", "CONTROLLER ONLY", "PANEL AND CLAUDE REFUSED"),
)

BTN_X0, BTN_X1 = 24, 470            # the mode column; the stop button owns the rest
BTN_Y0, BTN_H, BTN_GAP = 104, 56, 10
TEL_Y0, TEL_Y1 = 302, 356           # battery and board temperature, under the modes
STOP_X0, STOP_X1 = 490, 776
STOP_Y0, STOP_Y1 = 104, 356
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
        self._buttons = []
        for i, (mode, label, _hint) in enumerate(MODES):
            y = BTN_Y0 + i * (BTN_H + BTN_GAP)
            self._buttons.append((mode, ui.Button(BTN_X0, y, BTN_X1, y + BTN_H,
                                                  label, scale=3)))
        self._stop = ui.Button(STOP_X0, STOP_Y0, STOP_X1, STOP_Y1)
        self._pending: str | None = None
        # Last drawn e-stop state. on_touch is handed the touch and the net, not
        # the snapshot, so the tap has to know from the frame before it what the
        # button it just hit was showing — at 30 fps that is a frame old, against
        # a poll that is up to two seconds old anyway.
        self._latched = False
        self._armed_at = 0.0            # monotonic time of the first CLEAR tap

    # ---- input ------------------------------------------------------------
    def on_touch(self, kind: str, x: int, y: int, net) -> None:
        if kind != "down":
            return
        if self._stop.hit(x, y):
            self._on_stop_tap(net)
            return
        for mode, button in self._buttons:
            if button.hit(x, y):
                self._armed_at = 0.0    # a tap elsewhere is not a confirmation
                self._pending = mode    # lit immediately; the poll confirms
                net.post_cart_controller(mode)
                return

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

        self._latched = bool(cart.get("estop"))
        if not self._latched:
            self._armed_at = 0.0            # cleared, by us or by anyone else
        self._draw_stop(frame)

        self._draw_telemetry(frame, cart)

        hint = next((h for m, _l, h in MODES if m == shown), "")
        if hint:
            ui.text(frame, hint, BTN_X0, STATUS_Y, ui.DIM_INK, 2)

        if not reachable:
            ui.text(frame, "NO CART DRIVER ON THIS PI", BTN_X0, STATUS_Y + LINE_H,
                    ui.BAD_INK, 2)
            return
        if self._pending:
            ui.text(frame, "SAVING...", BTN_X0, STATUS_Y + LINE_H, ui.DIM_INK, 2)

        pad = cart.get("controller") or {}
        if not pad.get("connected"):
            line, ink = "CONTROLLER NOT CONNECTED", ui.DIM_INK
        elif pad.get("deadman"):
            # Words, not signed numbers. The font has a "+" now, but "FWD" and
            # "LEFT" are still what you want to read at a glance while watching
            # the base move — a sign is something you decode.
            speed, steer = pad.get("speed", 0.0), pad.get("steer", 0.0)
            way = "FWD" if speed > 0 else ("REV" if speed < 0 else "IDLE")
            turn = "RIGHT" if steer > 0 else ("LEFT" if steer < 0 else "STRAIGHT")
            line, ink = (f"DRIVING - {way} {abs(speed):.2f} {turn} {abs(steer):.2f}",
                         ui.OK_INK)
        else:
            line, ink = "CONTROLLER CONNECTED - R1 NOT HELD", ui.INK
        ui.text(frame, line, BTN_X0, STATUS_Y + LINE_H * 2, ink, 2)

        if self._latched:
            ui.text(frame, "E-STOP LATCHED - NOTHING WILL MOVE", BTN_X0,
                    STATUS_Y + LINE_H * 3, ui.BAD_INK, 2)
        elif cart.get("host_locked"):
            # The deadman was released while the panel or Claude was still
            # commanding, so the host is locked out until it asks again. Said
            # here because the alternative is watching the cart refuse a panel
            # that looks like it is driving.
            ui.text(frame, "DEADMAN RELEASED - HOST MUST COMMAND AGAIN", BTN_X0,
                    STATUS_Y + LINE_H * 3, ui.WARN_INK, 2)
        elif not cart.get("connected"):
            ui.text(frame, "CART PICO NOT PLUGGED IN", BTN_X0, STATUS_Y + LINE_H * 3,
                    ui.DIM_INK, 2)

    def _draw_telemetry(self, frame, cart: dict) -> None:
        """Battery and board temperature, next to the controls that spend them.

        Both were only on the STATUS page, which is the wrong tab to be reading
        while a 350 lb base is moving — this is where your eyes already are.

        **The voltage is shown, not judged, below the top of its band.** This
        pack has read 23% low before (a miscalibration that masqueraded as a
        dead battery, see Project-FRED-Cart's README), so a red "flat" here
        would eventually be a lie, and a panel that cries wolf stops being read.
        Amber at 3.3 V/cell is as far as it goes. Temperature is not coloured at
        all: there is no figure for this board worth calling hot.

        Absent and stale are said in words rather than left as a number that
        looks current. A reading is only true while it is arriving.

        **Drawn bare, with no panel and no border.** Its first cut had both,
        which is exactly how every button on this page is drawn — so it read as
        a fourth control sitting under the three mode buttons, and invited a
        thumb that would do nothing. On a touchscreen the box *is* the
        affordance; a readout has to go without one.
        """
        volts = cart.get("battery_v")
        temp = cart.get("board_temp_c")
        age = cart.get("telemetry_age")
        stale = age is not None and age > 5.0

        if volts is None:
            ui.text(frame, "--.-V", BTN_X0, TEL_Y0 + 8, ui.DIM_INK, 4)
            note = "NO MAINBOARD" if not cart.get("mainboard_seen") else "NO TELEMETRY"
            ui.text(frame, note, BTN_X0 + 138, TEL_Y0 + 15, ui.DIM_INK, 2)
            return

        volts = float(volts)
        if stale:
            ink = ui.DIM_INK
        elif volts < LOW_V:
            ink = ui.WARN_INK
        elif volts < NOMINAL_V:
            ink = ui.INK
        else:
            ink = ui.OK_INK
        # The label column starts after the voltage rather than at a fixed
        # offset: the themes' typefaces are not the same width, and 138px was
        # measured against the old bitmap font. In a wide face the reading ran
        # straight into its own label.
        reading = f"{volts:.1f}V"
        ui.text(frame, reading, BTN_X0, TEL_Y0 + 8, ink, 4)
        label_x = BTN_X0 + max(138, ui.text_width(reading, 4) + 14)
        ui.text(frame, "BATTERY", label_x, TEL_Y0 + 6, ui.DIM_INK, 1)
        second_y = TEL_Y0 + 6 + ui.line_height(1)
        if stale:
            ui.text(frame, "STALE", label_x, second_y, ui.WARN_INK, 2)
        elif temp is not None:
            ui.text(frame, f"BOARD {round(float(temp))}C", label_x, second_y, ui.INK, 2)
        else:
            ui.text(frame, "NO BOARD TEMP", label_x, second_y, ui.DIM_INK, 2)

    def _draw_stop(self, frame) -> None:
        """The e-stop, drawn by hand rather than as a ui.Button.

        Button.draw paints the shared blue face, which is the whole point of it
        and exactly wrong here — this one has to not look like the other four.
        It keeps the Button only for hit-testing, so the geometry is still
        written down once.
        """
        armed = self._latched and self._is_armed()
        if armed:
            face, ink, label, sub = ui.STOP_PANEL_ARM, ui.WARN_INK, "CLEAR", "TAP AGAIN"
        elif self._latched:
            face, ink, label, sub = ui.STOP_PANEL, ui.BAD_INK, "CLEAR", "E-STOP LATCHED"
        else:
            face, ink, label, sub = ui.STOP_PANEL, ui.BAD_INK, "STOP", "LATCHES"

        # Its own colours, the theme's shape, and — via the Button it already
        # keeps for hit-testing — the same press animation as everything else.
        # This is the control most worth acknowledging: a stop that looks inert
        # when you hit it is a stop you hit again, harder.
        ui.draw_face(frame, STOP_X0, STOP_Y0, STOP_X1, STOP_Y1, face, ink,
                     self._stop.phase(), weight=3.0)
        # Big enough to hit without looking at it: the label is scale 8, about a
        # third of the button's height, and the whole face is the target.
        ui.text_centred(frame, label, STOP_X0, STOP_X1, STOP_Y0 + 63, ink, 8)
        ui.text_centred(frame, sub, STOP_X0, STOP_X1, STOP_Y0 + 139, ink, 2)

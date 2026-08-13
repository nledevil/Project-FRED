"""Pick what the chest screen shows, from the chest screen.

Changing the animation meant the web panel, which means the brain, which means a
laptop — for a choice about this display, made by someone standing in front of
it. The list and the process that runs it are both local to this Pi, so this
page needs nothing switched on but the Pi it is drawn on.

Two columns of four rather than a scrolling list, for the same reason the cart
page uses three buttons instead of a dropdown: this is a 7" panel prodded with a
thumb, and everything that fits on one screen should be one tap.

Selecting is optimistic — the tapped entry lights immediately and the poll
confirms a moment later. Restarting an animation child takes a beat, and a
button that does nothing visible for half a second reads as a button that did
not work, which is how you get someone tapping it four times.
"""
from __future__ import annotations

import menu_ui as ui
from font5x7 import text_width

COLS, ROWS = 2, 4
GRID_X0, GRID_X1 = 24, 776
GRID_Y0, GRID_Y1 = 104, 400
GAP = 12
STATUS_Y = 418


class DisplayPage:
    title = "DISPLAY"

    def __init__(self):
        self._buttons: list[tuple[str, ui.Button]] = []
        self._pending: str | None = None
        self._built_for: list[str] = []

    # ---- layout ------------------------------------------------------------
    def _build(self, animations: list[dict]) -> None:
        """Lay the grid out for whatever the daemon offers.

        Built from the list rather than hardcoded: the presets are defined in
        display_control.py, and a page that assumed eight of them would quietly
        hide the ninth.
        """
        ids = [a.get("id", "") for a in animations]
        if ids == self._built_for:
            return
        self._built_for = ids
        self._buttons = []
        cols = COLS if len(animations) > ROWS else 1
        rows = max(1, -(-len(animations) // cols))       # ceil
        w = (GRID_X1 - GRID_X0 - GAP * (cols - 1)) // cols
        h = (GRID_Y1 - GRID_Y0 - GAP * (rows - 1)) // rows
        for i, anim in enumerate(animations):
            col, row = i % cols, i // cols
            x0 = GRID_X0 + col * (w + GAP)
            y0 = GRID_Y0 + row * (h + GAP)
            label = str(anim.get("label") or anim.get("id") or "?")
            # Biggest scale that fits, rather than a fixed one: "Arc Reactor
            # (Copper)" is twice the width of "Off" and a size chosen for the
            # longest would make the short ones look like a mistake.
            scale = next((s for s in (3, 2, 1) if text_width(label, s) <= w - 16), 1)
            self._buttons.append((str(anim.get("id") or ""),
                                  ui.Button(x0, y0, x0 + w, y0 + h, label, scale=scale)))

    # ---- input -------------------------------------------------------------
    def on_touch(self, kind: str, x: int, y: int, net) -> None:
        if kind != "down":
            return
        for anim, button in self._buttons:
            if button.hit(x, y):
                self._pending = anim
                net.post_animation(anim)
                return

    # ---- drawing -----------------------------------------------------------
    def draw(self, frame, snap: dict) -> None:
        animations = net_animations(snap)
        self._build(animations)
        display = (snap.get("chest") or {}).get("display") or {}
        current = display.get("animation")
        if current and current == self._pending:
            self._pending = None                 # the daemon caught up
        shown = self._pending or current

        if not self._buttons:
            ui.text(frame, "NO ANIMATION LIST FROM THIS PI", GRID_X0, GRID_Y0,
                    ui.BAD_INK, 2)
            return

        for anim, button in self._buttons:
            on = (anim == shown)
            # "off" lit is a blank screen on purpose, which is worth not
            # colouring like a healthy running animation.
            ink = ui.INK
            if on:
                ink = ui.DIM_INK if anim == "off" else ui.OK_INK
            button.draw(frame, on=on, ink=ink)

        if self._pending:
            ui.text(frame, "STARTING...", GRID_X0, STATUS_Y, ui.DIM_INK, 2)
        elif display.get("error"):
            ui.text(frame, str(display["error"])[:44], GRID_X0, STATUS_Y,
                    ui.BAD_INK, 2)
        elif not display.get("running"):
            ui.text(frame, "NOTHING RUNNING ON THE SCREEN", GRID_X0, STATUS_Y,
                    ui.WARN_INK, 2)
        else:
            ui.text(frame, str(display.get("label") or "").upper(), GRID_X0,
                    STATUS_Y, ui.DIM_INK, 2)


def net_animations(snap: dict) -> list[dict]:
    """The preset list out of the snapshot, defensively.

    A separate function so the page stays drawable in a test with a hand-made
    snapshot, without a Net at all.
    """
    animations = (snap.get("chest") or {}).get("animations")
    return list(animations) if isinstance(animations, list) else []

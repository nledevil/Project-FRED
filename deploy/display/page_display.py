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
import theme as theme_mod

COLS, ROWS = 2, 4
# Four rows of two is what fits at a size a thumb can hit. Beyond eight presets
# the grid used to divide the same space by more rows and quietly shrink the
# buttons — at twelve they were 33px tall. It pages instead.
PER_PAGE = COLS * ROWS
GRID_X0, GRID_X1 = ui.X0, ui.X1
GRID_Y0, GRID_Y1 = 110, 362
GAP = 12
# The look of this menu is a choice about this display, made in front of it —
# the same argument that put the animation list here rather than in the web
# panel. It is a strip rather than a page of its own because it is three taps
# in a lifetime, and a tab for that would cost a tab on every other screen.
THEME_Y0, THEME_Y1 = 376, 424
STATUS_Y = 438


class DisplayPage:
    title = "DISPLAY"

    def __init__(self):
        self._buttons: list[tuple[str, ui.Button]] = []
        self._pending: str | None = None
        self._built_for: tuple | None = None
        self._snap: dict = {}
        n = len(theme_mod.ORDER)
        w = (GRID_X1 - GRID_X0 - GAP * (n - 1)) // n
        self._themes = [
            (name, ui.Button(GRID_X0 + i * (w + GAP), THEME_Y0,
                             GRID_X0 + i * (w + GAP) + w, THEME_Y1,
                             theme_mod.THEMES[name].label, scale=2))
            for i, name in enumerate(theme_mod.ORDER)]
        # Shares the status line: the pager only draws when it is needed, so on
        # the usual six presets that row is just the status text as before.
        self._pager = ui.Pager(PER_PAGE, STATUS_Y - 6)

    # ---- layout ------------------------------------------------------------
    def _build(self, animations: list[dict]) -> None:
        """Lay the grid out for whatever the daemon offers.

        Built from the list rather than hardcoded: the presets are defined in
        display_control.py, and a page that assumed eight of them would quietly
        hide the ninth.
        """
        # Keyed on the theme as well as the list: the scale each label is drawn
        # at is chosen from how wide that label measures, and every theme has a
        # different typeface. Cached on the ids alone, switching to a wider face
        # kept the scales picked for the narrower one and long labels ran off
        # the ends of their buttons.
        key = ([a.get("id", "") for a in animations],
               ui.THEME.name if ui.THEME else None, self._pager.page)
        if key == self._built_for:
            return
        self._built_for = key
        self._buttons = []
        shown = self._pager.slice(animations)
        # Geometry comes from the whole list, not the visible slice: a last page
        # with four items on it would otherwise trip the one-column rule and the
        # buttons would change size and shape as you paged, which reads as a
        # different screen rather than as more of the same one.
        paged = len(animations) > PER_PAGE
        cols = COLS if len(animations) > ROWS else 1
        rows = ROWS if paged else max(1, -(-len(shown) // cols))
        w = (GRID_X1 - GRID_X0 - GAP * (cols - 1)) // cols
        h = (GRID_Y1 - GRID_Y0 - GAP * (rows - 1)) // rows
        for i, anim in enumerate(shown):
            col, row = i % cols, i // cols
            x0 = GRID_X0 + col * (w + GAP)
            y0 = GRID_Y0 + row * (h + GAP)
            label = str(anim.get("label") or anim.get("id") or "?")
            # Biggest scale that fits, rather than a fixed one: "Arc Reactor
            # (Copper)" is twice the width of "Off" and a size chosen for the
            # longest would make the short ones look like a mistake.
            scale = next((s for s in (3, 2, 1) if ui.text_width(label, s) <= w - 16), 1)
            self._buttons.append((str(anim.get("id") or ""),
                                  ui.Button(x0, y0, x0 + w, y0 + h, label, scale=scale)))

    # ---- input -------------------------------------------------------------
    def on_touch(self, kind: str, x: int, y: int, net) -> None:
        if kind != "down":
            return
        for name, button in self._themes:
            if button.hit(x, y):
                # Applied and saved on the spot: the next frame is already in
                # the new look, which is the only honest preview of a theme.
                ui.apply_theme(name)
                theme_mod.save_name(name)
                return
        if self._pager.on_touch(kind, x, y, len(net_animations(self._snap))):
            return
        for anim, button in self._buttons:
            if button.hit(x, y):
                self._pending = anim
                net.post_animation(anim)
                return

    # ---- drawing -----------------------------------------------------------
    def draw(self, frame, snap: dict) -> None:
        # Kept so on_touch can ask how many pages there are; the touch handler
        # runs before draw() on the frame a tap lands in.
        self._snap = snap
        animations = net_animations(snap)
        self._build(animations)
        display = (snap.get("chest") or {}).get("display") or {}
        current = display.get("animation")
        if current and current == self._pending:
            self._pending = None                 # the daemon caught up
        shown = self._pending or current

        # The theme strip is drawn before the animation list bails out: the look
        # of the menu is a local choice and stays available even when the Pi
        # cannot say what it is running.
        active = ui.THEME.name if ui.THEME else theme_mod.DEFAULT
        for name, button in self._themes:
            button.draw(frame, on=(name == active),
                        ink=ui.OK_INK if name == active else ui.DIM_INK)

        if not self._buttons:
            ui.empty(frame, "NO ANIMATION LIST FROM THIS PI", GRID_Y0)
            return

        for anim, button in self._buttons:
            on = (anim == shown)
            # "off" lit is a blank screen on purpose, which is worth not
            # colouring like a healthy running animation.
            ink = ui.INK
            if on:
                ink = ui.DIM_INK if anim == "off" else ui.OK_INK
            button.draw(frame, on=on, ink=ink)

        self._pager.draw(frame, len(animations))

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

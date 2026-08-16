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

import theme as theme_mod

COLS, ROWS = 2, 4
# Four rows of two is what fits at a size a thumb can hit. Beyond eight presets
# the grid used to divide the same space by more rows and quietly shrink the
# buttons — at twelve they were 33px tall. It pages instead.
PER_PAGE = COLS * ROWS
# The look of this menu is a choice about this display, made in front of it —
# the same argument that put the animation list here rather than in the web
# panel. It is a strip rather than a page of its own because it is three taps
# in a lifetime, and a tab for that would cost a tab on every other screen.
THEME_Y0, THEME_Y1 = 376, 424
STATUS_Y = 438


def net_animations(snap: dict) -> list[dict]:
    """The preset list out of the snapshot, defensively.

    A separate function so the page stays testable with a hand-made snapshot,
    without a Net at all.
    """
    animations = (snap.get("chest") or {}).get("animations")
    return list(animations) if isinstance(animations, list) else []


class DisplayPage:
    title = "DISPLAY"

    def __init__(self):
        self._pending: str | None = None
        self._page = 0
        self._snap: dict = {}

    def view(self, snap: dict) -> dict:
        """The page as data: the animation list, the theme strip, the status.

        Split out of draw() so the Qt panel makes the same calls about what is
        running — including that "off" lit is a blank screen on purpose and
        should not be coloured like a healthy animation.
        """
        self._snap = snap
        animations = net_animations(snap)
        display = (snap.get("chest") or {}).get("display") or {}
        current = display.get("animation")
        if current and current == self._pending:
            self._pending = None                 # the daemon caught up
        shown = self._pending or current
        active = theme_mod.load_name()

        if self._pending:
            status, ink = "STARTING...", "dim"
        elif display.get("error"):
            status, ink = str(display["error"])[:44], "bad"
        elif not display.get("running"):
            status, ink = "NOTHING RUNNING ON THE SCREEN", "warn"
        else:
            status, ink = str(display.get("label") or "").upper(), "dim"

        pages = max(1, -(-len(animations) // PER_PAGE))
        page = min(self._page, pages - 1)
        start = page * PER_PAGE
        return {
            "animations": [{"id": a.get("id"), "label": str(a.get("label") or ""),
                            "on": a.get("id") == shown,
                            "ink": ("dim" if a.get("id") == "off" else "ok")
                                   if a.get("id") == shown else "ink"}
                           for a in animations[start:start + PER_PAGE]],
            "themes": [{"name": n, "label": t.label, "on": n == active}
                       for n, t in theme_mod.THEMES.items()],
            "status": status, "statusInk": ink,
            "page": page, "pages": pages,
            "empty": not animations,
        }

    def pick(self, anim: str, net) -> None:
        """Ask the daemon for an animation, wherever the tap came from."""
        self._pending = anim
        net.post_animation(anim)

    @staticmethod
    def pick_theme(name: str) -> None:
        """Persist the pick. The panel re-execs to wear it — see
        panel.pickTheme, which is the only caller."""
        theme_mod.save_name(name)

    def turn_page(self, delta: int, total: int) -> None:
        pages = max(1, -(-total // PER_PAGE))
        self._page = max(0, min(pages - 1, self._page + delta))

    # ---- drawing -----------------------------------------------------------

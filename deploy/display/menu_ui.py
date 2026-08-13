"""Buttons and panels for the settings menu, drawn straight into a numpy frame.

There is no toolkit on this Pi and this is not the beginning of one — it is the
four shapes the menu actually needs. Anything more belongs in the head's web
panel, which already has a browser to draw it with.

Two things worth knowing before adding a screen:

**Text is upper-cased on the way in** unless you pass ``preserve_case=True``, so
lowercase is safe to pass and a label reads as a label. The font gained lowercase
and the passphrase symbols with keyboard.py, but it is still not all of ASCII —
anything missing renders as a blank with no error, so check ``FONT`` before
drawing text you did not write yourself.

**Drawing is additive, on a dark frame.** ``draw_text`` adds into the frame the
way the animations composite glow, so a filled panel is drawn first and its text
added on top. ``fill`` therefore *sets* rather than adds — it is the one place
that wants a flat background to put text on.
"""
from __future__ import annotations

import numpy as np

from font5x7 import CHAR_H, draw_text, text_width

# The panel palette, shared with metrics_hud.py so the menu looks like the rest
# of the chest screen rather than a different program.
INK = (120, 210, 255)          # ordinary text
DIM_INK = (90, 150, 190)       # labels, secondary text
OK_INK = (90, 255, 150)        # a link that is up
WARN_INK = (255, 180, 60)      # working, but not in the state you probably want
BAD_INK = (230, 120, 90)       # a link that is down
BG = (10, 22, 30)              # the menu's background
PANEL = (18, 40, 54)           # a raised panel / button face
PANEL_ON = (30, 78, 104)       # a button in its "on" state
EDGE = (60, 120, 160)          # panel border

# The stop control is deliberately outside the palette above: everything else on
# this screen is one family of blue, so a red face is not "another button" at a
# glance. Dark enough that BAD_INK/WARN_INK still add to something legible on it.
STOP_PANEL = (60, 16, 12)      # the e-stop's face
STOP_PANEL_ARM = (64, 44, 8)   # ...while it is waiting for a confirming tap


def fill(frame: np.ndarray, x0: int, y0: int, x1: int, y1: int, rgb) -> None:
    """Set a rectangle to a flat colour (not additive — see the module docstring)."""
    H, W = frame.shape[:2]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    if x1 <= x0 or y1 <= y0:
        return
    frame[y0:y1, x0:x1] = np.asarray(rgb, dtype=np.float32)


def border(frame: np.ndarray, x0: int, y0: int, x1: int, y1: int, rgb,
           weight: int = 1) -> None:
    """A rectangle outline, ``weight`` pixels thick."""
    fill(frame, x0, y0, x1, y0 + weight, rgb)
    fill(frame, x0, y1 - weight, x1, y1, rgb)
    fill(frame, x0, y0, x0 + weight, y1, rgb)
    fill(frame, x1 - weight, y0, x1, y1, rgb)


# preserve_case rides through these to draw_text. Everything on this panel is an
# uppercase label except text the user typed, which has to read back exactly —
# see keyboard.py.
def text(frame: np.ndarray, s: str, x: int, y: int, rgb, scale: int = 2,
         preserve_case: bool = False) -> None:
    draw_text(frame, s, x, y, rgb, scale, preserve_case=preserve_case)


def text_right(frame: np.ndarray, s: str, x_right: int, y: int, rgb,
               scale: int = 2, preserve_case: bool = False) -> None:
    """Right-align ``s`` so its last pixel lands on ``x_right``."""
    draw_text(frame, s, x_right - text_width(s, scale), y, rgb, scale,
              preserve_case=preserve_case)


def text_centred(frame: np.ndarray, s: str, x0: int, x1: int, y: int, rgb,
                 scale: int = 2, preserve_case: bool = False) -> None:
    draw_text(frame, s, x0 + (x1 - x0 - text_width(s, scale)) // 2, y, rgb, scale,
              preserve_case=preserve_case)


def line_height(scale: int = 2) -> int:
    return CHAR_H * scale


class Button:
    """A rectangle that knows whether it was tapped.

    Holds its own geometry so a page can declare its buttons once and then just
    draw them and ask ``hit()`` — the alternative is the same four numbers
    written out twice, once to draw and once to test, which is exactly the sort
    of thing that drifts.
    """

    def __init__(self, x0: int, y0: int, x1: int, y1: int, label: str = "",
                 scale: int = 3, preserve_case: bool = False):
        self.rect = (x0, y0, x1, y1)
        self.label = label
        self.scale = scale
        self.preserve_case = preserve_case

    def hit(self, x: int, y: int) -> bool:
        x0, y0, x1, y1 = self.rect
        return x0 <= x < x1 and y0 <= y < y1

    def draw(self, frame: np.ndarray, on: bool = False, ink=None,
             label: str | None = None) -> None:
        x0, y0, x1, y1 = self.rect
        fill(frame, x0, y0, x1, y1, PANEL_ON if on else PANEL)
        border(frame, x0, y0, x1, y1, EDGE)
        s = self.label if label is None else label
        if s:
            text_centred(frame, s, x0, x1,
                         y0 + (y1 - y0 - line_height(self.scale)) // 2,
                         ink or INK, self.scale,
                         preserve_case=self.preserve_case)

"""An on-screen keyboard for the chest panel.

Built for one job: setting the access point's SSID and passphrase without an SSH
session. That is the whole reason the Wireless tab exists, and until now it
could only toggle the AP — the two things most worth changing about it still
meant a laptop and a text editor as root.

**It only offers characters the font can draw.** Anything outside the glyph
table renders as a blank with no error (see font5x7), so a keyboard offering the
full ASCII set would let you set a passphrase you could not read back, on the
only screen that can show it to you. The layout is checked against FONT at
import, so adding a key without a glyph fails loudly here instead of quietly on
the panel.

Text is drawn with preserve_case, which is why the lowercase glyphs exist. A
keyboard that showed you ``MyPass`` while storing ``MYPASS`` would be worse than
no keyboard.

It sits below the tab strip rather than over it. Tabbing away mid-edit is
therefore possible, and abandons nothing — the field keeps its text, because the
page owns it and the keyboard is only a way of changing it.
"""
from __future__ import annotations

import menu_ui as ui
from font5x7 import FONT, text_width

X0, X1 = 24, 776
# The label sits at 104, immediately under the tab strip (which ends at 90).
FIELD_Y0, FIELD_Y1 = 126, 172
ROWS_Y0, ROWS_Y1 = 182, 470
GAP = 6

# label -> column span. A bare character is a one-column key that types itself;
# the capitalised words are actions.
LOWER = (
    ("q", "w", "e", "r", "t", "y", "u", "i", "o", "p"),
    ("a", "s", "d", "f", "g", "h", "j", "k", "l", "-"),
    ("SHIFT", "z", "x", "c", "v", "b", "n", "m", ".", "DEL"),
)
UPPER = (
    ("Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P"),
    ("A", "S", "D", "F", "G", "H", "J", "K", "L", "_"),
    ("shift", "Z", "X", "C", "V", "B", "N", "M", ",", "DEL"),
)
SYMS = (
    ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0"),
    ("!", "?", "@", "#", "$", "%", "&", "*", "+", "="),
    ("abc", "(", ")", "'", ":", "/", "-", "_", ".", "DEL"),
)
# The bottom row is the same on every layer, so the way out never moves.
BOTTOM = (("SYM", 2), ("SPACE", 4), ("CANCEL", 2), ("DONE", 2))

ACTIONS = {"SHIFT", "shift", "DEL", "SYM", "abc", "SPACE", "CANCEL", "DONE"}

# A key with no glyph would type an invisible character. Fail at import.
_missing = sorted({k for layer in (LOWER, UPPER, SYMS) for row in layer for k in row
                   if k not in ACTIONS and k not in FONT})
if _missing:                                    # pragma: no cover - import guard
    raise RuntimeError(f"keyboard keys with no glyph in font5x7: {_missing}")


class Keyboard:
    """Modal text entry. ``result`` is None until DONE or CANCEL is pressed.

    The caller drives it: forward touches while ``active``, draw it instead of
    the page, and collect ``text``/``cancelled`` when it stops being active.
    """

    def __init__(self, label: str, text: str = "", max_len: int = 63,
                 min_len: int = 0):
        self.label = label
        self.text = str(text)
        self.max_len = int(max_len)
        self.min_len = int(min_len)
        self.active = True
        self.cancelled = False
        self._layer = "lower"
        self._keys: list[tuple[str, ui.Button]] = []
        self._build()

    # ---- layout ------------------------------------------------------------
    def _rows(self):
        return {"lower": LOWER, "upper": UPPER, "sym": SYMS}[self._layer]

    def _build(self) -> None:
        self._keys = []
        rows = self._rows()
        n_rows = len(rows) + 1                      # + the fixed bottom row
        h = (ROWS_Y1 - ROWS_Y0 - GAP * (n_rows - 1)) // n_rows
        cols = max(len(r) for r in rows)
        w = (X1 - X0 - GAP * (cols - 1)) // cols
        for r, row in enumerate(rows):
            y0 = ROWS_Y0 + r * (h + GAP)
            for c, key in enumerate(row):
                x0 = X0 + c * (w + GAP)
                self._keys.append((key, ui.Button(
                    x0, y0, x0 + w, y0 + h, key,
                    scale=3 if len(key) == 1 else 2,
                    # A lowercase key drawn upper-cased would lie about what it
                    # types, which is the whole failure this keyboard avoids.
                    preserve_case=True)))
        y0 = ROWS_Y0 + len(rows) * (h + GAP)
        x = X0
        for key, span in BOTTOM:
            width = w * span + GAP * (span - 1)
            self._keys.append((key, ui.Button(x, y0, x + width, y0 + h, key,
                                              scale=2, preserve_case=True)))
            x += width + GAP

    # ---- input -------------------------------------------------------------
    def on_touch(self, kind: str, x: int, y: int) -> None:
        if kind != "down" or not self.active:
            return
        for key, button in self._keys:
            if button.hit(x, y):
                self._press(key)
                return

    def _press(self, key: str) -> None:
        if key in ("SHIFT", "shift"):
            self._layer = "upper" if self._layer != "upper" else "lower"
            self._build()
        elif key in ("SYM", "abc"):
            self._layer = "sym" if self._layer != "sym" else "lower"
            self._build()
        elif key == "DEL":
            self.text = self.text[:-1]
        elif key == "SPACE":
            self._type(" ")
        elif key == "CANCEL":
            self.active, self.cancelled = False, True
        elif key == "DONE":
            if self.ok():
                self.active, self.cancelled = False, False
        else:
            self._type(key)
            if self._layer == "upper":
                # One capital, then back down — the usual shift behaviour, and
                # the one that stops a passphrase becoming SHOUTED by accident.
                self._layer = "lower"
                self._build()

    def _type(self, ch: str) -> None:
        if len(self.text) < self.max_len:
            self.text += ch

    def ok(self) -> bool:
        return self.min_len <= len(self.text) <= self.max_len

    # ---- drawing -----------------------------------------------------------
    def draw(self, frame) -> None:
        ui.text(frame, self.label, X0, FIELD_Y0 - 22, ui.DIM_INK, 2)
        ui.fill(frame, X0, FIELD_Y0, X1, FIELD_Y1, ui.PANEL)
        ui.border(frame, X0, FIELD_Y0, X1, FIELD_Y1, ui.EDGE)

        # Show the tail when it overflows: what you are typing is the end of it,
        # so a field that clipped the right would hide the cursor.
        shown, scale = self.text, 3
        while shown and text_width(shown, scale) > (X1 - X0 - 24):
            shown = shown[1:]
        ui.text(frame, shown, X0 + 12, FIELD_Y0 + 12, ui.INK, scale,
                preserve_case=True)
        if not self.text:
            ui.text(frame, "EMPTY", X0 + 12, FIELD_Y0 + 16, ui.DIM_INK, 2)

        count = f"{len(self.text)}"
        if self.min_len and len(self.text) < self.min_len:
            count += f" - NEEDS {self.min_len}"
        ui.text_right(frame, count, X1 - 12, FIELD_Y0 + 16,
                      ui.BAD_INK if not self.ok() else ui.DIM_INK, 2)

        for key, button in self._keys:
            ink = ui.INK
            if key == "DONE":
                ink = ui.OK_INK if self.ok() else ui.DIM_INK
            elif key == "CANCEL":
                ink = ui.BAD_INK
            elif key in ACTIONS:
                ink = ui.DIM_INK
            lit = ((key in ("SHIFT", "shift") and self._layer == "upper")
                   or (key in ("SYM", "abc") and self._layer == "sym"))
            button.draw(frame, on=lit, ink=ink)

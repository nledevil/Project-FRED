"""Draw anti-aliased text on the chest panel from a baked glyph atlas.

The companion to tools/bake_font.py, which runs on the NUC. Everything that
needs a font engine happened there; this side is numpy indexing and one
alpha blend, so the Pi keeps its stdlib-only rule and still gets real type.

    f = Font.load("fonts/orbitron-34.npz")
    f.draw(frame, "SERVOS", x=40, y=120, rgb=(180, 220, 255))

Coverage is blended, not thresholded — that is the entire difference between
this and font5x7.py, and the reason a label stops looking like a debug readout.
"""
from __future__ import annotations

import pathlib

import numpy as np


class Font:
    def __init__(self, atlas: np.ndarray, cells: np.ndarray, chars: np.ndarray,
                 height: int, ascent: int):
        self.atlas, self.height, self.ascent = atlas, int(height), int(ascent)
        # char code -> row of `cells`. A dict beats searching the array per glyph,
        # and the atlas is small enough that building it at load is free.
        self._by_char = {int(c): cells[i] for i, c in enumerate(chars)}
        self._space = self._by_char.get(ord(" "))

    @classmethod
    def load(cls, path: str | pathlib.Path) -> "Font":
        d = np.load(str(path))
        return cls(d["atlas"], d["cells"], d["chars"], d["height"], d["ascent"])

    def width(self, s: str, tracking: float = 0.0) -> int:
        """Pen advance for a string, so callers can centre without drawing."""
        total = 0.0
        for ch in s:
            cell = self._by_char.get(ord(ch), self._space)
            if cell is not None:
                total += float(cell[5]) + tracking
        return int(round(total))

    def draw(self, frame: np.ndarray, s: str, x: int, y: int, rgb,
             tracking: float = 0.0, alpha: float = 1.0) -> int:
        """Blit `s` with its baseline-relative top at `y`. Returns the end x.

        `tracking` is extra space per glyph — letterspaced caps are most of what
        makes a HUD look like a HUD, and it costs nothing to support here.
        `alpha` fades the whole run, which is what a press or a page transition
        animates.
        """
        colour = np.array(rgb, dtype=np.uint16)
        H, W = frame.shape[:2]
        pen = float(x)
        for ch in s:
            cell = self._by_char.get(ord(ch), self._space)
            if cell is None:
                continue
            ax, gw, gh, bx, by, adv = (int(cell[0]), int(cell[1]), int(cell[2]),
                                       int(cell[3]), int(cell[4]), float(cell[5]))
            if gw and gh:
                dx, dy = int(round(pen)) + bx, y + by
                # Clip against the frame — a label running off the panel edge
                # should be cut, not raise, because page layouts change.
                sx0, sy0 = max(0, -dx), max(0, -dy)
                ex, ey = min(gw, W - dx), min(gh, H - dy)
                if ex > sx0 and ey > sy0:
                    cov = self.atlas[sy0:ey, ax + sx0:ax + ex].astype(np.uint16)
                    if alpha < 1.0:
                        cov = (cov * max(0.0, min(1.0, alpha))).astype(np.uint16)
                    region = frame[dy + sy0:dy + ey, dx + sx0:dx + ex].astype(np.uint16)
                    cov = cov[:, :, None]
                    frame[dy + sy0:dy + ey, dx + sx0:dx + ex] = (
                        (region * (255 - cov) + colour * cov) >> 8).astype(np.uint8)
            pen += adv + tracking
        return int(round(pen))

    def draw_centred(self, frame: np.ndarray, s: str, x0: int, x1: int, y: int,
                     rgb, tracking: float = 0.0, alpha: float = 1.0) -> None:
        w = self.width(s, tracking)
        self.draw(frame, s, x0 + ((x1 - x0) - w) // 2, y, rgb, tracking, alpha)

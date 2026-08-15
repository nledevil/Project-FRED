"""The settings cog, painted into the corner of whatever animation is running.

Same split as metrics_hud.py, and for the same reason: the animation child mmaps
/dev/fb0 and owns it exclusively, so the daemon cannot draw over it. Every
animation therefore paints the cog itself — one call, next to the one it already
makes for the sensor HUD.

The icon is a **bitmap, not geometry**. It could have been circles and trig, but
voice_hud.c is a pixel-for-pixel port of voice_hud.py and tools/verify_voice_hud.py
proves it stays that way; matching floating-point trig across numpy and C is a
promise neither wants to keep. A bitmap is the same 24 rows of data on both
sides, so the two renderers cannot drift and the test cannot start failing for
reasons nobody can see. It was generated once, by eye, and is now just data.

The draw sequence — clip, dim, add, clip — mirrors metrics_draw() in voice_hud.c
exactly, including clipping *before* the dim: the frame reaching an overlay may
have accumulated past 255, and dimming an unclipped value shrinks the wrong
number. Keeping the order identical in both files is what keeps them in step.

``HOTSPOT`` is the touch target, and lives here so the daemon's hit-test and the
drawing can never disagree about where the cog is.
"""
from __future__ import annotations

import numpy as np

import theme

# 24x24, MSB left, generated then hand-checked. Eight teeth and a hole: at
# scale 2 it reads as a cog from across the room, which is all it has to do.
_COG = (
    "000000000000000000000000",
    "000000000011110000000000",
    "000000000111111000000000",
    "000000110111111011000000",
    "000011110111111011110000",
    "000011111111111111110000",
    "000111111111111111111000",
    "000111111111111111111000",
    "000001111111111111100000",
    "001111111100001111111100",
    "011111111000000111111110",
    "011111111000000111111110",
    "011111111000000111111110",
    "011111111000000111111110",
    "001111111100001111111100",
    "000001111111111111100000",
    "000111111111111111111000",
    "000111111111111111111000",
    "000011111111111111110000",
    "000011110111111011110000",
    "000000110111111011000000",
    "000000000111111000000000",
    "000000000011110000000000",
    "000000000000000000000000",
)

ICON = np.array([[c == "1" for c in row] for row in _COG], dtype=bool)
ICON_N = ICON.shape[0]                 # 24

SCALE = 2                              # 48x48 drawn
PAD = 8                                # panel border around the icon
MARGIN = 12                            # gap to the screen edge; matches metrics_hud
DIM = 0.25                             # a power of two, so float32 and float64 agree
COG_RGB = theme.ramp().at(0.33)        # metrics_hud's TITLE_RGB: same furniture

BOX = ICON_N * SCALE + PAD * 2         # 64

# Where the cog sits and what counts as touching it, for an 800x480 panel. The
# hit area is grown up and left of the drawn box — a fingertip is wider than an
# icon, and there is nothing else in that corner to hit by mistake.
_GROW = 16


def box(width: int = 800, height: int = 480) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) of the drawn panel, bottom-right of a width x height screen."""
    x1, y1 = width - MARGIN, height - MARGIN
    return x1 - BOX, y1 - BOX, x1, y1


def hotspot(width: int = 800, height: int = 480) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) of the touch target.

    Grown up and left of the drawn box for fingertips, and run out to the screen
    edge on the other two sides: the margin below and right of the icon is still
    obviously "the cog" to anyone aiming at it, and a tap in the very corner of
    the panel should not be a miss.
    """
    x0, y0, _x1, _y1 = box(width, height)
    return x0 - _GROW, y0 - _GROW, width, height


def hit(x: int, y: int, width: int = 800, height: int = 480) -> bool:
    """Is (x, y) a tap on the cog?"""
    x0, y0, x1, y1 = hotspot(width, height)
    return x0 <= x < x1 and y0 <= y < y1


class CogHud:
    """Paints the cog into an animation's frame. Never raises.

    ``enabled=False`` turns it off entirely, for an animation that wants the
    screen to itself (and for the settings menu, which draws its own chrome).
    """

    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled)
        self._big = np.repeat(np.repeat(ICON, SCALE, axis=0), SCALE, axis=1)
        self._rgb = np.asarray(COG_RGB, dtype=np.float32)

    def draw(self, frame: np.ndarray) -> None:
        """Overlay the cog bottom-right. Call once per frame, before the blit."""
        if not self.enabled:
            return
        try:
            H, W = frame.shape[:2]
            x0, y0, x1, y1 = box(W, H)
            if x0 < 0 or y0 < 0:
                return                      # screen too small for the corner panel
            region = frame[y0:y1, x0:x1]

            # Clip first: see the module docstring. Then knock the animation
            # back so the cog reads as furniture rather than washing out over a
            # bright core, exactly as the sensor panel does.
            np.clip(region, 0, 255, out=region)
            region *= DIM

            ix, iy = x0 + PAD, y0 + PAD
            frame[iy:iy + self._big.shape[0],
                  ix:ix + self._big.shape[1]][self._big] += self._rgb

            np.clip(region, 0, 255, out=region)
        except Exception:
            pass          # a broken overlay must never take the animation down

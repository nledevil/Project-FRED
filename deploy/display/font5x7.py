"""A 5x7 bitmap font, drawn straight into a numpy RGB frame.

There's no Pillow and no system fonts on the display Pi, and a chest panel needs
a handful of short uppercase labels — not a text engine. So: 36 glyphs as bit
rows, rendered by slicing. No dependencies beyond numpy, and reusable by any
animation that wants a label (a boot/POST screen, a status readout).

    draw_text(frame, "SPEAKING", x, y, colour, scale=4)
"""
from __future__ import annotations

import numpy as np

# One line per glyph: 7 rows of 5 bits, MSB left. Readable on purpose — a font
# packed into hex is unverifiable by eye, and this is data you fix by looking.
_GLYPHS = {
    "A": "01110 10001 10001 11111 10001 10001 10001",
    "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110",
    "D": "11110 10001 10001 10001 10001 10001 11110",
    "E": "11111 10000 10000 11110 10000 10000 11111",
    "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111",
    "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "11111 00100 00100 00100 00100 00100 11111",
    "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001",
    "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001",
    "N": "10001 10001 11001 10101 10011 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110",
    "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101",
    "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110",
    "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110",
    "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 11011 10001",
    "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100",
    "Z": "11111 00001 00010 00100 01000 10000 11111",
    "0": "01110 10001 10011 10101 11001 10001 01110",
    "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111",
    "3": "11111 00010 00100 00010 00001 10001 01110",
    "4": "00010 00110 01010 10010 11111 00010 00010",
    "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110",
    "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110",
    "9": "01110 10001 10001 01111 00001 00010 01100",
    " ": "00000 00000 00000 00000 00000 00000 00000",
    ".": "00000 00000 00000 00000 00000 01100 01100",
    ":": "00000 01100 01100 00000 01100 01100 00000",
    "-": "00000 00000 00000 11111 00000 00000 00000",
    "/": "00001 00010 00010 00100 01000 01000 10000",
}

# Parsed once into (7, 5) bool arrays.
FONT = {ch: np.array([[c == "1" for c in row] for row in bits.split()], dtype=bool)
        for ch, bits in _GLYPHS.items()}

CHAR_W, CHAR_H = 5, 7


def text_width(text: str, scale: int = 1, spacing: int = 1) -> int:
    """Pixel width of ``text`` as draw_text would render it."""
    return len(text) * (CHAR_W + spacing) * scale - spacing * scale


def draw_text(frame: np.ndarray, text: str, x: int, y: int,
              colour, scale: int = 1, spacing: int = 1) -> None:
    """Blit ``text`` at (x, y) top-left. Unknown characters render as blanks.

    Adds into the frame (it's a glow-composited image, not a canvas), and clips
    at the edges so a caller can't crash the animation with a long label.
    """
    H, W = frame.shape[:2]
    colour = np.asarray(colour, dtype=np.float32)
    step = (CHAR_W + spacing) * scale
    for i, ch in enumerate(text.upper()):
        glyph = FONT.get(ch)
        if glyph is None or not glyph.any():
            continue
        gx = x + i * step
        if gx >= W or gx + CHAR_W * scale <= 0 or y >= H or y + CHAR_H * scale <= 0:
            continue
        big = np.repeat(np.repeat(glyph, scale, axis=0), scale, axis=1)
        # Clip to the frame so edge cases are a smaller glyph, not an exception.
        y0, x0 = max(y, 0), max(gx, 0)
        y1, x1 = min(y + big.shape[0], H), min(gx + big.shape[1], W)
        sub = big[y0 - y:y1 - y, x0 - gx:x1 - gx]
        frame[y0:y1, x0:x1][sub] += colour

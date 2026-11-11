"""A 5x7 bitmap font, drawn straight into a numpy RGB frame.

There's no Pillow and no system fonts on the display Pi, and a chest panel needs
short labels — not a text engine. So: the glyphs below as bit rows, rendered by
slicing. No dependencies beyond numpy, and reusable by any animation that wants
a label (a boot/POST screen, a status readout).

    draw_text(frame, "SPEAKING", x, y, colour, scale=4)

It was uppercase, digits and `. : - /` for a long time, which is all a status
readout needs. Lowercase and the passphrase symbols arrived with the WiFi
keyboard, where upper-casing the input would have been a way to store a password
nobody typed. Everything still upper-cases by default; see draw_text.

**Anything not in the table renders as a blank, silently.** That is deliberate —
a long label must not crash an animation — but it means a caller showing
arbitrary text should check FONT first, as keyboard.py does.
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

    # Lowercase, added for the WiFi keyboard. Until then every label here was a
    # short uppercase one and draw_text simply upper-cased its input, which is
    # fine for "SPEAKING" and a trap for a passphrase: it would show you MyPass
    # while storing something you never typed. Callers opt in with
    # preserve_case=True, so the existing labels are untouched.
    #
    # Descenders (g j p q y) use the bottom two rows, so they sit low against
    # the baseline the uppercase glyphs share.
    "a": "00000 00000 01110 00001 01111 10001 01111",
    "b": "10000 10000 11110 10001 10001 10001 11110",
    "c": "00000 00000 01111 10000 10000 10000 01111",
    "d": "00001 00001 01111 10001 10001 10001 01111",
    "e": "00000 00000 01110 10001 11111 10000 01110",
    "f": "00110 01001 01000 11100 01000 01000 01000",
    "g": "00000 00000 01111 10001 01111 00001 01110",
    "h": "10000 10000 11110 10001 10001 10001 10001",
    "i": "00100 00000 01100 00100 00100 00100 01110",
    "j": "00010 00000 00110 00010 00010 10010 01100",
    "k": "10000 10000 10010 10100 11000 10100 10010",
    "l": "01100 00100 00100 00100 00100 00100 01110",
    "m": "00000 00000 11010 10101 10101 10101 10101",
    "n": "00000 00000 11110 10001 10001 10001 10001",
    "o": "00000 00000 01110 10001 10001 10001 01110",
    "p": "00000 00000 11110 10001 11110 10000 10000",
    "q": "00000 00000 01111 10001 01111 00001 00001",
    "r": "00000 00000 10110 11001 10000 10000 10000",
    "s": "00000 00000 01111 10000 01110 00001 11110",
    "t": "01000 01000 11110 01000 01000 01001 00110",
    "u": "00000 00000 10001 10001 10001 10011 01101",
    "v": "00000 00000 10001 10001 10001 01010 00100",
    "w": "00000 00000 10001 10001 10101 10101 01010",
    "x": "00000 00000 10001 01010 00100 01010 10001",
    "y": "00000 00000 10001 10001 01111 00001 01110",
    "z": "00000 00000 11111 00010 00100 01000 11111",

    # The symbols a WPA passphrase is likely to contain. Not an exhaustive
    # ASCII set: anything absent renders as a blank, so the keyboard only
    # offers what is here — see keyboard.py, which is checked against FONT.
    "_": "00000 00000 00000 00000 00000 00000 11111",
    "!": "00100 00100 00100 00100 00100 00000 00100",
    "?": "01110 10001 00001 00010 00100 00000 00100",
    "@": "01110 10001 10111 10101 10111 10000 01110",
    "#": "01010 01010 11111 01010 11111 01010 01010",
    "$": "00100 01111 10100 01110 00101 11110 00100",
    "%": "11001 11010 00010 00100 01011 10011 00000",
    "&": "01100 10010 10100 01000 10101 10010 01101",
    "*": "00000 10101 01110 11111 01110 10101 00000",
    "+": "00000 00100 00100 11111 00100 00100 00000",
    "=": "00000 00000 11111 00000 11111 00000 00000",
    "(": "00010 00100 01000 01000 01000 00100 00010",
    ")": "01000 00100 00010 00010 00010 00100 01000",
    ",": "00000 00000 00000 00000 01100 01100 00100",
    "'": "00100 00100 00000 00000 00000 00000 00000",
}

# Parsed once into (7, 5) bool arrays.
FONT = {ch: np.array([[c == "1" for c in row] for row in bits.split()], dtype=bool)
        for ch, bits in _GLYPHS.items()}

CHAR_W, CHAR_H = 5, 7


def text_width(text: str, scale: int = 1, spacing: int = 1) -> int:
    """Pixel width of ``text`` as draw_text would render it."""
    return len(text) * (CHAR_W + spacing) * scale - spacing * scale


def draw_text(frame: np.ndarray, text: str, x: int, y: int,
              colour, scale: int = 1, spacing: int = 1,
              preserve_case: bool = False) -> None:
    """Blit ``text`` at (x, y) top-left. Unknown characters render as blanks.

    Adds into the frame (it's a glow-composited image, not a canvas), and clips
    at the edges so a caller can't crash the animation with a long label.

    Upper-cases by default, which is what every label on this panel wants and
    what all of them relied on before lowercase glyphs existed. ``preserve_case``
    turns that off for the one thing that cannot tolerate it: text the user typed
    and has to be able to read back exactly, i.e. a WiFi passphrase.
    """
    H, W = frame.shape[:2]
    colour = np.asarray(colour, dtype=np.float32)
    step = (CHAR_W + spacing) * scale
    for i, ch in enumerate(text if preserve_case else text.upper()):
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

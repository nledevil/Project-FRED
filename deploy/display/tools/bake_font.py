#!/usr/bin/env python3
"""Bake a TrueType face into an anti-aliased glyph atlas the chest Pi can blit.

Run this on the NUC, ship the .npz. The chest panel has no Pillow and no system
fonts — that is why it draws with a 5x7 bitmap font, and why the menu reads as a
debug readout no matter how nice the buttons around it get. Nothing about that
constraint requires the *letterforms* to be crude, though: rasterising is the
only part that needs a font engine, and it can happen here, once, offline.

So: Pillow renders every glyph to an 8-bit coverage mask here; the Pi loads one
numpy array and alpha-blends from it. The Pi stays stdlib + numpy, exactly as
before, and gains real type with smooth edges.

    python3 tools/bake_font.py Orbitron.ttf 34 --out fonts/orbitron-34.npz

The atlas is a single row of glyph masks, `cells` records where each one sits
plus its advance and bearing, so text can be proportionally spaced rather than
locked to a grid. See font_atlas.py for the reader.
"""
from __future__ import annotations

import argparse
import pathlib

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Printable ASCII. The panel shows labels, readings and a WiFi passphrase; that
# is the whole of it, and a bigger set would only make the atlas wider.
CHARS = "".join(chr(c) for c in range(32, 127))
PAD = 1          # transparent gutter, so a glyph never bleeds into its neighbour


def bake(ttf: pathlib.Path, px: int) -> dict:
    font = ImageFont.truetype(str(ttf), px)
    ascent, descent = font.getmetrics()
    height = ascent + descent

    # Measure first: the atlas is allocated once at the exact width needed.
    metrics = []
    for ch in CHARS:
        # getbbox gives the inked box; getlength gives the pen advance, which is
        # what actually spaces text. They differ, and using the wrong one is how
        # letterspacing ends up wrong on 'j', 'A' and every accented glyph.
        x0, y0, x1, y1 = font.getbbox(ch)
        metrics.append({"ch": ch, "w": max(0, x1 - x0), "h": max(0, y1 - y0),
                        "bx": x0, "by": y0, "adv": font.getlength(ch)})

    total = sum(m["w"] + PAD * 2 for m in metrics)
    # Tall enough for the tallest ink, not for the line box: every cell is read
    # back starting at row 0, so the glyphs are packed flush to the top.
    atlas = Image.new("L", (total, max(m["h"] for m in metrics) + PAD * 2), 0)
    draw = ImageDraw.Draw(atlas)

    cells, x = [], 0
    for m in metrics:
        # Offset by BOTH bearings so the ink lands at the cell's top-left corner.
        # Drawing at y=0 instead would leave it sitting `by` rows down, and the
        # reader — which slices from row 0 — would clip the bottom off every
        # glyph. The bearings are carried in `cells` and re-applied at blit time.
        draw.text((x + PAD - m["bx"], -m["by"]), m["ch"], font=font, fill=255)
        cells.append((x + PAD, m["w"], m["h"], m["bx"], m["by"], m["adv"]))
        x += m["w"] + PAD * 2

    return {
        "atlas": np.asarray(atlas, dtype=np.uint8),
        "cells": np.array(cells, dtype=np.float32),
        "chars": np.array([ord(c) for c in CHARS], dtype=np.int32),
        "height": np.int32(height),
        "ascent": np.int32(ascent),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ttf", type=pathlib.Path)
    ap.add_argument("px", type=int, help="pixel size to rasterise at")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()

    data = bake(args.ttf, args.px)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.out, **data)
    a = data["atlas"]
    print(f"{args.out}: {a.shape[1]}x{a.shape[0]} atlas, "
          f"{len(CHARS)} glyphs, {args.out.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Check the framebuffer packing at both depths, without a framebuffer.

The panel is 16bpp RGB565 today and becomes 32bpp XRGB8888 the moment
vc4-kms-v3d is loaded, so one of these two paths is always the one nobody is
running. That is the whole reason the packing was split out of Framebuffer:
the depth you are not currently plugged into is the one that breaks.

The 16bpp expectations here are the literal expression fb.py shipped with
before it learned about depth, so this also pins that nothing changed for the
panel as it stands.

    python3 tools/test_fb_pack.py
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.dirname(_HERE)]       # repo layout and flattened Pi layout

import numpy as np                                    # noqa: E402

from fb import pack                                   # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    rng = np.random.default_rng(7)
    img = rng.integers(0, 256, size=(37, 53, 3), dtype=np.uint8)

    print("16bpp RGB565 — unchanged from what the panel runs today")
    r = img[..., 0].astype(np.uint16)
    g = img[..., 1].astype(np.uint16)
    b = img[..., 2].astype(np.uint16)
    want = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    got = pack(img, 16)
    check("matches the original expression", np.array_equal(got, want))
    check("is 2 bytes per pixel", got.tobytes().__len__() == img.shape[0] * img.shape[1] * 2,
          str(len(got.tobytes())))
    check("white stays white", int(pack(np.full((1, 1, 3), 255, np.uint8), 16)[0, 0]) == 0xFFFF)
    check("black stays black", int(pack(np.zeros((1, 1, 3), np.uint8), 16)[0, 0]) == 0)

    print("32bpp XRGB8888 — what vc4-kms-v3d gives you")
    got = pack(img, 32)
    check("is 4 bytes per pixel", got.shape == (37, 53, 4), str(got.shape))
    # In memory the bytes are B, G, R, X. Read them back and the original must
    # survive exactly: unlike 565 this format is lossless, so any difference is
    # a channel-order bug, which on a panel looks like a blue robot.
    back = np.dstack([got[..., 2], got[..., 1], got[..., 0]])
    check("round-trips every channel", np.array_equal(back, img))
    check("alpha byte is opaque", bool((got[..., 3] == 0xFF).all()))
    red = pack(np.array([[[255, 0, 0]]], np.uint8), 32)[0, 0]
    check("pure red lands as B,G,R,X = 0,0,255,255",
          list(red) == [0, 0, 255, 255], str(list(red)))

    print("a depth this panel will never have")
    try:
        pack(img, 24)
        check("24bpp is refused", False, "it was accepted")
    except ValueError:
        check("24bpp is refused", True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Does reactor.frag actually reproduce reactor.build_geometry()?

glslangValidator proves the shader compiles. It does not prove it draws the
right thing, and a shader that compiles and draws a slightly different reactor
is the worst outcome of a port — nobody would notice until they put the two
panels side by side.

So this evaluates the shader's arithmetic in numpy, line for line off the .frag,
and diffs it against what reactor.py builds today. It is not a GPU test: it
checks the transliteration, which is the part a human got wrong. Running the
real shader needs a GPU, which needs vc4-kms-v3d.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]
                       / "deploy" / "display"))

import numpy as np                                        # noqa: E402

import reactor                                            # noqa: E402
import theme                                              # noqa: E402

W, H = 800, 480


def shader(w, h, ramp, glow, copper=0.0, half=0.5):
    """reactor.frag, in numpy. Colours in 0..1, as the shader has them."""
    deep = np.array(ramp.deep, np.float64) / 255.0
    accent = np.array(ramp.accent, np.float64) / 255.0

    def lvl(t):
        if t <= 0.5:
            return deep + (accent - deep) * (t * 2.0)
        return accent + (np.ones(3) - accent) * ((t - 0.5) * 2.0)

    def band(d, r, wd):
        x = (d - r) / wd
        return np.exp(-x * x)

    Y, X = np.mgrid[0:h, 0:w].astype(np.float64)
    # The shader samples pixel centres; np.mgrid gives corners. Half a pixel.
    dx, dy = (X + half) - w / 2.0, (Y + half) - h / 2.0
    dist = np.sqrt(dx * dx + dy * dy)
    ang = np.arctan2(dy, dx)
    R = min(w, h) * 0.5 * 0.92

    col = np.zeros((h, w, 3))

    def add(inten, colour, gain):
        col[:] += inten[..., None] * colour[None, None, :] * (1.0 + (glow - 1.0) * gain)

    vign = np.clip(1.0 - dist / (R * 1.6), 0.0, 1.0)
    add(vign * vign * 0.12, lvl(0.0), 0.0)
    add(band(dist, R * 0.95, R * 0.03), lvl(0.34), 0.15)
    add(band(dist, R * 0.78, R * 0.02), lvl(0.41), 0.15)

    ring = band(dist, R * 0.62, R * 0.10)
    seg = (0.5 + 0.5 * np.cos(10.0 * ang)) ** 6
    coil = lvl(0.5) * (1 - copper) + (np.array([210.0, 150.0, 70.0]) / 255.0) * copper
    add(ring * seg, coil, 0.25)
    wire = (0.5 + 0.5 * np.cos(30.0 * ang)) ** 8
    add(ring * wire * 0.5, np.array([255.0, 240.0, 210.0]) / 255.0, 0.2)

    add(band(dist, R * 0.40, R * 0.03), lvl(0.51), 0.3)
    tri = (0.5 + 0.5 * np.cos(3.0 * ang)) ** 3
    add(band(dist, R * 0.26, R * 0.05) * tri, lvl(0.63), 0.5)
    core = dist / (R * 0.22)
    add(np.exp(-core * core), lvl(0.72), 1.0)
    hot = dist / (R * 0.09)
    add(np.exp(-hot * hot), np.ones(3), 1.0)

    return np.clip(col, 0.0, 1.0) * 255.0


def compare(half):
    """Worst and mean difference from reactor.py, over every theme and pulse."""
    worst = mean = 0.0
    for name in theme.ORDER:
        for copper in (0.0, 1.0):
            ramp = theme.THEMES[name].ramp()
            for glow in (0.55, 1.0):
                A, B = reactor.build_geometry(W, H, copper=bool(copper), ramp=ramp)
                cpu = np.clip(A + glow * B, 0, 255).astype(np.float64)
                d = np.abs(cpu - shader(W, H, ramp, glow, copper, half=half))
                worst = max(worst, float(d.max()))
                mean = max(mean, float(d.mean()))
    return worst, mean


def main() -> int:
    # Two comparisons, because they answer different questions.
    same_worst, same_mean = compare(0.0)
    gpu_worst, gpu_mean = compare(0.5)

    print("Is the port faithful?  (sampled on numpy's own grid)")
    ok = same_worst <= 3.0
    print(f"  {'PASS' if ok else 'FAIL'}  worst {same_worst:5.2f}/255   "
          f"mean {same_mean:.3f}   — ramp rounding only")

    print()
    print("What will the GPU actually draw?  (fragments sample pixel centres)")
    print(f"        worst {gpu_worst:5.2f}/255   mean {gpu_mean:.3f}   "
          f"— a half-pixel shift")
    print()
    print("  Those rings are ~4px wide, so half a pixel moves a lot of intensity")
    print("  locally while being invisible to the eye. The consequence is not")
    print("  cosmetic: a GPU renderer cannot be held byte-identical to the CPU")
    print("  one the way tools/verify_voice_hud.py holds voice_hud.c. Moving the")
    print("  animations to shaders means trading an exact check for a tolerance.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

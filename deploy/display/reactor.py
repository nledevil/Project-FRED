#!/usr/bin/env python3
"""Pulsing Iron Man arc reactor for the InMoov chest/face screen.

Pure numpy -> /dev/fb0. Static geometry (rings + coils) is precomputed once;
each frame just re-weights the glow layers by a breathing pulse, so it stays
fast at 800x480.

Usage:
    sudo python3 reactor.py                 # run forever (Ctrl+C to stop)
    sudo python3 reactor.py --seconds 5     # run 5s then quit (for testing)
    sudo python3 reactor.py --copper        # copper/gold coils instead of cyan
"""
import sys
import time
import signal
import argparse
import numpy as np
import theme
from fb import Framebuffer, hide_cursor
from cog_hud import CogHud
from metrics_hud import MetricsHud


def band(dist, r, width):
    """Smooth glowing ring centered at radius r."""
    return np.exp(-(((dist - r) / width) ** 2))


def build_geometry(w, h, copper=False, ramp=None):
    """Precompute the reactor. ``ramp`` is a theme.Ramp; colours are sampled
    from it by level, so the accent moves with the panel's theme while the
    structure — dim rim, bright coils, white core — does not.

    Copper stays copper. It is a second preset in the animation list rather
    than a theme, chosen because someone wanted a copper reactor, so re-tinting
    it to the theme would delete the only thing that distinguishes it.
    """
    ramp = ramp or theme.ramp()
    lvl = lambda t: np.array(ramp.at(t), dtype=np.float32)
    cx, cy = w / 2.0, h / 2.0
    Y, X = np.mgrid[0:h, 0:w].astype(np.float32)
    dx, dy = X - cx, Y - cy
    dist = np.sqrt(dx * dx + dy * dy)
    ang = np.arctan2(dy, dx)

    R = min(w, h) / 2.0 * 0.92        # ~220px on 800x480

    layers = []  # (intensity_map, base_color, pulse_gain)
    # gain = how much this layer breathes with the pulse (0 = static, 1 = full)

    # background radial vignette (very dark blue)
    vign = np.clip(1.0 - dist / (R * 1.6), 0, 1) ** 2
    layers.append((vign * 0.12, lvl(0.0), 0.0))                  # vignette

    # outer steel rings
    layers.append((band(dist, R * 0.95, R * 0.03), lvl(0.34), 0.15))   # outer ring
    layers.append((band(dist, R * 0.78, R * 0.02), lvl(0.41), 0.15))   # inner steel

    # segmented copper coils (the classic 10 wound posts)
    N = 10
    seg = 0.5 + 0.5 * np.cos(N * ang)
    seg = seg ** 6                                   # sharpen into discrete posts
    coil_ring = band(dist, R * 0.62, R * 0.10)
    coil_color = np.array([210, 150, 70], np.float32) if copper else lvl(0.5)
    layers.append((coil_ring * seg, coil_color, 0.25))
    # thin wire highlights across the coils
    wire = (0.5 + 0.5 * np.cos(N * 3 * ang)) ** 8
    layers.append((coil_ring * wire * 0.5, np.array([255, 240, 210], np.float32), 0.2))

    # inner ring
    layers.append((band(dist, R * 0.40, R * 0.03), lvl(0.51), 0.3))    # inner ring

    # triangle core outline (Iron Man's tri-emitter)
    tri = 0.5 + 0.5 * np.cos(3 * ang)
    tri = tri ** 3
    layers.append((band(dist, R * 0.26, R * 0.05) * tri, lvl(0.63), 0.5))  # tri-emitter

    # bright pulsing core
    core = np.exp(-((dist / (R * 0.22)) ** 2))
    layers.append((core, lvl(0.72), 1.0))                        # core
    # hot center
    layers.append((np.exp(-((dist / (R * 0.09)) ** 2)), np.array([255, 255, 255]), 1.0))

    # Every layer is  intensity * color * ((1-gain) + gain*glow), which is linear
    # in the single scalar `glow`. Collapse all layers into two static images so
    # each frame is just  A + glow*B  (one array multiply-add, ~10x faster).
    A = np.zeros((h, w, 3), dtype=np.float32)
    B = np.zeros((h, w, 3), dtype=np.float32)
    for inten, color, gain in layers:
        rgb = inten[..., None] * color[None, None, :].astype(np.float32)
        A += rgb * (1.0 - gain)
        B += rgb * gain
    return A, B


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=0, help="run time (0 = forever)")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--copper", action="store_true", help="copper coils instead of cyan")
    args = ap.parse_args()

    fb = Framebuffer()
    hide_cursor()
    A, B = build_geometry(fb.w, fb.h, copper=args.copper)
    hud = MetricsHud()          # no-op unless the sensor overlay is switched on
    cog = CogHud()              # the settings cog, bottom-right

    running = [True]
    signal.signal(signal.SIGINT, lambda *a: running.__setitem__(0, False))
    signal.signal(signal.SIGTERM, lambda *a: running.__setitem__(0, False))

    start = time.monotonic()
    period = 1.0 / args.fps
    n = 0
    try:
        while running[0]:
            t = time.monotonic() - start
            if args.seconds and t >= args.seconds:
                break

            # breathing pulse (slow) + faint flicker (fast)
            pulse = 0.55 + 0.45 * (0.5 + 0.5 * np.sin(t * 2.2))
            flicker = 1.0 + 0.04 * np.sin(t * 37.0)
            glow = float(pulse * flicker)

            frame = A + glow * B
            np.clip(frame, 0, 255, out=frame)
            hud.draw(frame)
            cog.draw(frame)
            fb.show(frame.astype(np.uint8))

            n += 1
            sleep = period - ((time.monotonic() - start) - n * period)
            if sleep > 0:
                time.sleep(sleep)
    finally:
        fb.clear()
        fb.close()
        dur = time.monotonic() - start
        print(f"{n} frames in {dur:.1f}s = {n / dur:.1f} fps", file=sys.stderr)


if __name__ == "__main__":
    main()

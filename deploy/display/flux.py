#!/usr/bin/env python3
"""Flux capacitor for the InMoov screen.

Three glass tubes in a "Y" meet at a central hub. Bright sparks race down the
tubes toward the hub; when they arrive the hub flashes white.

Speed trick: the moving sparks aren't linear in one scalar, so instead of
recomputing the whole frame we precompute, for each tube, the list of pixels it
covers plus each pixel's position `s` along the tube (0=hub, 1=end) and a
transverse glow weight. Each frame only touches those ~tube pixels.

Usage:
    sudo python3 flux.py               # run forever (Ctrl+C to stop)
    sudo python3 flux.py --seconds 5   # run 5s then quit (for testing)
"""
import sys
import time
import signal
import argparse
import numpy as np
from fb import Framebuffer, hide_cursor
from metrics_hud import MetricsHud


def build(w, h):
    cx, cy = w / 2.0, h * 0.52
    R = min(w, h) * 0.40                      # arm length
    tube_r = min(w, h) * 0.055                 # tube half-width
    bulb_r = tube_r * 1.7

    # Y-shape: two arms up-diagonal, one straight down (fork points up)
    dirs = [np.array([-0.80, -0.60]),
            np.array([0.80, -0.60]),
            np.array([0.0, 1.0])]
    ends = [np.array([cx, cy]) + d * R for d in dirs]
    hub = np.array([cx, cy])

    Y, X = np.mgrid[0:h, 0:w].astype(np.float32)
    P = np.stack([X, Y], axis=-1)             # (h, w, 2)

    base = np.zeros((h, w, 3), dtype=np.float32)
    dim_tube = np.array([25, 55, 80], dtype=np.float32)
    bulb_col = np.array([90, 150, 200], dtype=np.float32)

    arms = []  # per arm: (flat_idx, s_along, glow_weight)
    for end in ends:
        d = end - hub
        L2 = float(d @ d)
        rel = P - hub                          # (h,w,2)
        t = np.clip((rel @ d) / L2, 0.0, 1.0)  # projection param 0..1
        closest = hub + t[..., None] * d
        trans = np.linalg.norm(P - closest, axis=-1)   # transverse distance
        glow = np.exp(-((trans / (tube_r * 0.6)) ** 2))
        mask = trans < (tube_r * 1.6)

        # dim resting glow of the glass tube
        base += (glow * mask)[..., None] * dim_tube

        idx = np.flatnonzero(mask)
        arms.append((idx, t.reshape(-1)[idx].astype(np.float32),
                     glow.reshape(-1)[idx].astype(np.float32)))

        # bright end bulb
        bd = np.linalg.norm(P - end, axis=-1)
        base += np.exp(-((bd / bulb_r) ** 2))[..., None] * bulb_col

    # central hub: ring + core (core flashes on spark arrival)
    hd = np.linalg.norm(P - hub, axis=-1)
    base += np.exp(-(((hd - bulb_r * 1.3) / (tube_r * 0.4)) ** 2))[..., None] * np.array([80, 140, 190])
    hub_flash = np.exp(-((hd / (bulb_r * 1.5)) ** 2)).astype(np.float32)  # (h,w)

    return base, arms, hub_flash


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--speed", type=float, default=1.4, help="spark cycles/sec")
    args = ap.parse_args()

    fb = Framebuffer()
    hide_cursor()
    hud = MetricsHud()          # no-op unless the sensor overlay is switched on
    base, arms, hub_flash = build(fb.w, fb.h)
    spark_col = np.array([210, 240, 255], dtype=np.float32)
    flash_col = np.array([255, 255, 255], dtype=np.float32)
    npix = fb.w * fb.h

    running = [True]
    signal.signal(signal.SIGINT, lambda *a: running.__setitem__(0, False))
    signal.signal(signal.SIGTERM, lambda *a: running.__setitem__(0, False))

    start = time.monotonic()
    period = 1.0 / args.fps
    n = 0
    frame = np.empty((npix, 3), dtype=np.float32)
    base_flat = base.reshape(-1, 3)
    hub_flat = hub_flash.reshape(-1)
    try:
        while running[0]:
            t = time.monotonic() - start
            if args.seconds and t >= args.seconds:
                break

            frame[:] = base_flat
            flash = 0.0
            for i, (idx, s, glow) in enumerate(arms):
                phase = (t * args.speed + i / 3.0) % 1.0     # 0..1, staggered arms
                pos = 1.0 - phase                            # spark travels end->hub
                spark = np.exp(-(((s - pos) / 0.10) ** 2)) * glow
                frame[idx] += spark[:, None] * spark_col
                flash = max(flash, float(np.exp(-((pos / 0.12) ** 2))))

            frame += (flash * hub_flat)[:, None] * flash_col
            np.clip(frame, 0, 255, out=frame)
            # This animation works on a flat (N, 3) buffer; reshape is a view,
            # so the overlay lands in the same memory.
            view = frame.reshape(fb.h, fb.w, 3)
            hud.draw(view)
            fb.show(view.astype(np.uint8))

            n += 1
            sleep = period - ((time.monotonic() - start) - n * period)
            if sleep > 0:
                time.sleep(sleep)
    finally:
        fb.clear()
        fb.close()
        dur = time.monotonic() - start
        print(f"{n} frames in {dur:.1f}s = {n / max(dur, 1e-6):.1f} fps", file=sys.stderr)


if __name__ == "__main__":
    main()

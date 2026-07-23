#!/usr/bin/env python3
"""Animated "face" / HUD for the InMoov screen, driven by FRED's real voice.

Two glowing cyan eyes that blink and glance around, a mouth waveform, and a
Jarvis-style HUD ring. Eyes and mouth are rendered only inside their local
bounding boxes each frame, so it stays smooth at 800x480.

The mouth rides FRED's *actual* speech envelope — the same levels driving the jaw
servo and the web face — pushed here by the head (see voice_state.py). It isn't a
lookalike flutter: when the mouth is wide, real audio is loud. The HUD ring
colours the voice state, so you can see him listening and thinking from across a
room:

    idle       dim cyan, slow breathing
    listening  green, steady
    thinking   amber, restless scan
    speaking   bright cyan, rides the envelope

Usage:
    sudo python3 face.py               # run forever (Ctrl+C to stop)
    sudo python3 face.py --seconds 6   # run 6s then quit (for testing)
    sudo python3 face.py --talk        # fake mouth motion; demo with no audio
"""
import sys
import time
import math
import signal
import argparse
import numpy as np
from fb import Framebuffer, hide_cursor
from voice_state import VoiceFeed

CYAN = np.array([90, 210, 255], dtype=np.float32)
WHITE = np.array([220, 245, 255], dtype=np.float32)
GREEN = np.array([90, 255, 150], dtype=np.float32)
AMBER = np.array([255, 180, 60], dtype=np.float32)

# Ring tint + how urgently it moves, per voice state.
STATE_STYLE = {
    "idle":      (CYAN,  1.6),
    "listening": (GREEN, 2.6),
    "thinking":  (AMBER, 7.0),
    "speaking":  (CYAN,  1.6),
}


def build_hud(w, h):
    """Static + pulsing HUD as two images: frame = A + glow*B."""
    cx, cy = w / 2.0, h / 2.0
    Y, X = np.mgrid[0:h, 0:w].astype(np.float32)
    dist = np.sqrt((X - cx) ** 2 + (Y - cy) ** 2)
    ang = np.arctan2(Y - cy, X - cx)
    R = min(w, h) / 2.0

    A = np.zeros((h, w, 3), dtype=np.float32)
    B = np.zeros((h, w, 3), dtype=np.float32)

    # outer HUD ring with tick marks
    ring = np.exp(-(((dist - R * 0.95) / 2.0) ** 2))
    ticks = (0.5 + 0.5 * np.cos(ang * 60)) ** 8
    A += ring[..., None] * np.array([30, 70, 95])
    B += (ring * (0.4 + ticks))[..., None] * CYAN * 0.5

    # inner arc segments (broken ring)
    arc = np.exp(-(((dist - R * 0.82) / 1.5) ** 2))
    gaps = (0.5 + 0.5 * np.cos(ang * 8)) ** 2
    B += (arc * gaps)[..., None] * np.array([40, 120, 160])
    return A, B


class Region:
    """Precomputed local pixel grid for cheap per-frame rendering."""
    def __init__(self, cx, cy, hw, hh, W, H):
        self.x0, self.x1 = max(0, cx - hw), min(W, cx + hw)
        self.y0, self.y1 = max(0, cy - hh), min(H, cy + hh)
        yy, xx = np.mgrid[self.y0:self.y1, self.x0:self.x1].astype(np.float32)
        self.dx = xx - cx
        self.dy = yy - cy

    def add(self, frame, rgb):
        frame[self.y0:self.y1, self.x0:self.x1] += rgb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--talk", action="store_true", help="animate mouth as speaking")
    args = ap.parse_args()

    fb = Framebuffer()
    hide_cursor()
    W, H = fb.w, fb.h
    A, B = build_hud(W, H)
    feed = VoiceFeed()          # what FRED is doing, published by display_control.py

    # The HUD art is baked cyan, so a state's colour is a channel rescale
    # (tint/CYAN) that keeps the ring's structure. Bake one A/B pair per state up
    # front rather than rescaling 1.15M pixels every frame — the frame budget
    # here is already tight, and this makes a colour change cost exactly nothing.
    # ~5MB per state on a Pi with gigabytes; the cyan states come out identical
    # to the original art (ratio 1.0).
    HUD = {s: ((A * (tint / CYAN)).astype(np.float32),
               (B * (tint / CYAN)).astype(np.float32))
           for s, (tint, _) in STATE_STYLE.items()}

    eye_dx = int(W * 0.17)
    eye_y = int(H * 0.40)
    eye_rx, eye_ry = W * 0.075, H * 0.14
    L = Region(W // 2 - eye_dx, eye_y, int(eye_rx * 1.8), int(eye_ry * 1.8), W, H)
    Rg = Region(W // 2 + eye_dx, eye_y, int(eye_rx * 1.8), int(eye_ry * 1.8), W, H)
    mouth = Region(W // 2, int(H * 0.74), int(W * 0.22), int(H * 0.12), W, H)
    mx = mouth.dx[0] / (W * 0.20)     # normalized x across mouth, one row

    running = [True]
    signal.signal(signal.SIGINT, lambda *a: running.__setitem__(0, False))
    signal.signal(signal.SIGTERM, lambda *a: running.__setitem__(0, False))

    # gaze wander state
    gaze = np.zeros(2)
    gaze_target = np.zeros(2)
    next_gaze = 0.0
    next_blink = 1.5

    start = time.monotonic()
    period = 1.0 / args.fps
    n = 0
    try:
        while running[0]:
            t = time.monotonic() - start
            if args.seconds and t >= args.seconds:
                break

            # --- voice: FRED's real state and, while speaking, his real loudness ---
            state = feed.state()
            _, rate = STATE_STYLE.get(state, STATE_STYLE["idle"])
            level = feed.level()            # 0..1 from the envelope; 0 when silent

            if state == "speaking":
                glow = 0.7 + 0.5 * level    # the ring breathes with the voice itself
            else:
                glow = 0.6 + 0.4 * (0.5 + 0.5 * math.sin(t * rate))

            A_s, B_s = HUD.get(state, HUD["idle"])      # pre-tinted; no per-frame cost
            frame = A_s + glow * B_s

            # --- gaze: pick a new target occasionally, ease toward it ---
            if t >= next_gaze:
                # deterministic-ish wander using sines of t (no RNG needed)
                gaze_target = np.array([math.sin(t * 0.7) * 0.6,
                                        math.sin(t * 0.9 + 1.0) * 0.4])
                next_gaze = t + 1.2
            gaze += (gaze_target - gaze) * 0.15

            # --- blink: quick close roughly every 4s ---
            openness = 1.0
            if t >= next_blink:
                bt = t - next_blink
                if bt < 0.18:
                    openness = abs(math.cos(bt / 0.18 * math.pi))
                else:
                    next_blink = t + 3.5 + (math.sin(t) + 1.0)

            for reg in (L, Rg):
                ry = eye_ry * max(openness, 0.04)
                gx = gaze[0] * eye_rx * 0.5
                gy = gaze[1] * eye_ry * 0.4
                # eye white-glow (almond) + darker pupil that follows gaze
                e = np.exp(-(((reg.dx) / eye_rx) ** 2 + ((reg.dy) / ry) ** 2))
                pupil = np.exp(-((((reg.dx - gx) / (eye_rx * 0.35)) ** 2
                                  + ((reg.dy - gy) / (ry * 0.45)) ** 2)))
                inten = np.clip(e * 1.1 - pupil * 0.9, 0, 1)
                reg.add(frame, inten[..., None] * CYAN * glow)
                reg.add(frame, (pupil * e)[..., None] * WHITE * 0.5)

            # --- mouth: FRED's real speech envelope (or a fake, with --talk) ---
            idle_amp = 0.18 + 0.06 * math.sin(t * 2.0)      # gentle resting ripple
            if args.talk:
                amp = (0.4 + 0.6 * abs(math.sin(t * 6.0))) * (0.6 + 0.4 * math.sin(t * 11.0))
            else:
                # Real loudness on top of the idle ripple: wide mouth == loud audio.
                amp = idle_amp + level * 0.95
            wave = amp * (np.sin(mx * 7 + t * 9) * 0.6 + np.sin(mx * 13 - t * 5) * 0.4)
            wy = wave[None, :] * (H * 0.09)              # target y per column
            line = np.exp(-(((mouth.dy - wy) / 4.0) ** 2))
            mouth.add(frame, line[..., None] * CYAN * (0.7 + 0.3 * glow))

            np.clip(frame, 0, 255, out=frame)
            fb.show(frame.astype(np.uint8))

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

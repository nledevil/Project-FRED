#!/usr/bin/env python3
"""Voice telemetry HUD for the InMoov chest screen.

Shows what FRED's voice is doing, from his real data — the same envelope that
drives the jaw servo, pushed here by the head (see voice_state.py).

The whole envelope for an utterance arrives *before* the first sample is audible,
which buys a trick a streaming meter can't do: draw the entire waveform of what
he's about to say, then sweep a playhead across it as he says it. The bright part
is what he's already said; the dim part is what's coming.

    idle       flat trace, slow breath
    listening  green, baseline pulsing
    thinking   amber, a scanner sweeping the baseline
    speaking   cyan waveform + playhead riding the real audio

Usage:
    sudo python3 voice_hud.py              # run forever (Ctrl+C to stop)
    sudo python3 voice_hud.py --seconds 6  # run 6s then quit (for testing)
"""
import sys
import time
import math
import signal
import argparse
import numpy as np
from fb import Framebuffer, hide_cursor
from metrics_hud import MetricsHud
from voice_state import VoiceFeed
from font5x7 import draw_text, text_width, CHAR_H

CYAN = np.array([90, 210, 255], dtype=np.float32)
GREEN = np.array([90, 255, 150], dtype=np.float32)
AMBER = np.array([255, 180, 60], dtype=np.float32)
WHITE = np.array([225, 245, 255], dtype=np.float32)

STATE_COLOUR = {"idle": CYAN * 0.75, "listening": GREEN,
                "thinking": AMBER, "speaking": CYAN}

# How far the trace is knocked back ahead of the playhead. float32 rather than a
# bare literal so the multiply rounds exactly as the old per-pixel shade did.
AHEAD = np.float32(0.28)


def build_chrome(w, h, wx0, wx1, wy0, wy1):
    """Static HUD furniture: corner brackets + a centre baseline. Drawn once."""
    img = np.zeros((h, w, 3), dtype=np.float32)
    dim = np.array([28, 66, 88], dtype=np.float32)
    cy = (wy0 + wy1) // 2

    img[cy - 1:cy + 1, wx0:wx1] += dim * 0.7            # baseline through the trace
    for x in (wx0, wx1 - 2):                            # end caps
        img[wy0:wy1, x:x + 2] += dim * 0.5

    blen, t = 34, 2                                     # corner brackets
    for (cx, cyy, sx, sy) in ((wx0, wy0, 1, 1), (wx1 - t, wy0, -1, 1),
                              (wx0, wy1 - t, 1, -1), (wx1 - t, wy1 - t, -1, -1)):
        xs = slice(cx, cx + blen) if sx > 0 else slice(cx - blen + t, cx + t)
        ys = slice(cyy, cyy + blen) if sy > 0 else slice(cyy - blen + t, cyy + t)
        img[cyy:cyy + t, xs] += dim
        img[ys, cx:cx + t] += dim
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--fps", type=float, default=30.0)
    args = ap.parse_args()

    fb = Framebuffer()
    hide_cursor()
    W, H = fb.w, fb.h
    feed = VoiceFeed()
    hud = MetricsHud()          # no-op unless the sensor overlay is switched on

    # Trace window: the star of the screen, so give it the middle two thirds.
    wx0, wx1 = int(W * 0.06), int(W * 0.94)
    wy0, wy1 = int(H * 0.30), int(H * 0.86)
    wcy, whh = (wy0 + wy1) // 2, (wy1 - wy0) // 2
    cols = wx1 - wx0

    chrome = build_chrome(W, H, wx0, wx1, wy0, wy1)
    # Column-local grid, precomputed once: per frame we only compare against it.
    dy = (np.arange(wy0, wy1, dtype=np.float32) - wcy)[:, None]
    col_i = np.arange(cols)

    # The state dot's falloff never changes — build it once, not thirty times a
    # second. Same for the meter's backing colour.
    r = 9
    yy, xx = np.mgrid[0:2 * r, 0:2 * r].astype(np.float32)
    DOT = np.exp(-((((xx - r) / (r * 0.55)) ** 2
                    + ((yy - r) / (r * 0.55)) ** 2)))[..., None]
    METER_BG = np.array([22, 52, 70], dtype=np.float32)

    # Everything outside the trace window — label, state dot, level meter — sits in
    # one band across the top. Its geometry is fixed, so work it out once.
    ty = int(H * 0.12)
    ddy0 = ty + (CHAR_H * 4) // 2 - r
    mw, mh = int(W * 0.20), 8
    mx0, my0 = wx1 - mw, int(H * 0.145)

    running = [True]
    signal.signal(signal.SIGINT, lambda *a: running.__setitem__(0, False))
    signal.signal(signal.SIGTERM, lambda *a: running.__setitem__(0, False))

    start = time.monotonic()
    period = 1.0 / args.fps
    n = 0
    try:
        while running[0]:
            now = time.monotonic()
            t = now - start
            if args.seconds and t >= args.seconds:
                break

            d = feed.poll()
            state = feed.state()
            colour = STATE_COLOUR.get(state, CYAN)
            frame = chrome.copy()
            win = frame[wy0:wy1, wx0:wx1]

            levels = d.get("levels")
            play_at, frame_dt = d.get("play_at"), d.get("frame_dt")
            frac = None
            if levels and play_at is not None and frame_dt:
                dur = len(levels) * frame_dt
                frac = (now - play_at) / dur if dur > 0 else None

            if levels and frac is not None and -0.5 <= frac <= 1.25:
                # --- the utterance, whole: waveform + playhead ---
                lv = np.asarray(levels, dtype=np.float32)
                idx = np.clip((col_i / cols * len(lv)).astype(int), 0, len(lv) - 1)
                amp = np.maximum(lv[idx] * (whh * 0.95), 1.5)     # mirrored envelope
                band = (np.abs(dy) <= amp[None, :])

                head = frac * cols
                # Ahead of the playhead sits what he hasn't said yet — dimmer. The
                # shade is a step at the playhead, so it's two column ranges, not a
                # per-pixel weight: write each with a masked add and touch only the
                # ~2% of the window the envelope actually covers.
                # ceil, not int: a column counts as played while head is anywhere
                # past its left edge, which is what `col_i < head` used to say.
                hcol = int(min(max(math.ceil(head), 0), cols))
                if hcol:
                    win[:, :hcol][band[:, :hcol]] += colour
                if hcol < cols:
                    win[:, hcol:][band[:, hcol:]] += colour * AHEAD

                if 0 <= head < cols:                             # the playhead itself
                    hx = wx0 + int(head)
                    frame[wy0:wy1, max(hx - 1, wx0):hx + 2] += WHITE * 0.55
            else:
                # --- no clip: a living baseline that says which state we're in ---
                if state == "listening":
                    amp = 2.0 + 3.5 * (0.5 + 0.5 * math.sin(t * 3.0))
                elif state == "thinking":
                    amp = 2.0
                else:
                    amp = 1.5 + 1.0 * (0.5 + 0.5 * math.sin(t * 1.4))
                # A flat baseline is the same every column, so it's a contiguous run
                # of rows — a slice, not a mask over the whole window.
                rows = np.nonzero(np.abs(dy[:, 0]) <= amp)[0]
                if rows.size:
                    br0, br1 = rows[0], rows[-1] + 1
                    win[br0:br1] += colour * 0.8

                    if state == "thinking":
                        # A scanner sweeping the trace: he's working on it.
                        sx = (0.5 + 0.5 * math.sin(t * 2.4)) * (cols - 1)
                        glow = np.exp(-(((col_i - sx) / 26.0) ** 2)).astype(np.float32)
                        win[br0:br1] += glow[None, :, None] * AMBER * 1.6

            # --- state readout ---
            label = state.upper()
            pulse = 0.75 + 0.25 * (0.5 + 0.5 * math.sin(t * 3.2))
            draw_text(frame, label, wx0 + 28, ty, colour * pulse, scale=4)
            frame[ddy0:ddy0 + 2 * r, wx0:wx0 + 2 * r] += DOT * colour * pulse

            # --- live level meter, bottom right ---
            lvl = feed.level(now)
            frame[my0:my0 + mh, mx0:mx0 + mw] += METER_BG
            fill = int(mw * min(lvl, 1.0))
            if fill > 0:
                frame[my0:my0 + mh, mx0:mx0 + fill] += colour * 0.9

            # Clip the whole frame, not just the rects we drew into: `frame` is
            # contiguous and the sub-views are not, and one pass over contiguous
            # memory measured twice as fast as two passes over strided slices.
            np.clip(frame, 0, 255, out=frame)
            hud.draw(frame)
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

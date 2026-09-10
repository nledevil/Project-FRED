#!/usr/bin/env python3
"""Voice telemetry HUD for the InMoov chest screen — the reference renderer.

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

**This is the definition of the picture, not what runs on the robot.** The
look that ships is ``shaders/voice_hud.frag``, hosted by panel.py like every
other animation, and ``tools/verify_shaders.py`` holds it to this file the
same way it holds the reactor to reactor.py. The two things a fragment shader
cannot draw for itself — the state word, which is a font, and the envelope,
which is data — it takes as textures that ``word_layer`` and ``encode_levels``
below produce, so that half of the picture is not a second implementation at
all. (A native C port used to be what ran, held to this file byte for byte;
it went with the third rendering stack on 2026-09-10.)

Usage, as a renderer in its own right — for the framebuffer, at a cost of
~70% of a core, which is why it is the reference and not the product:

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
from cog_hud import CogHud
from metrics_hud import MetricsHud
from voice_state import VoiceFeed
from font5x7 import draw_text, text_width, CHAR_H

import theme

# The state word is the point of this screen; everything else is texture. 11
# puts LISTENING — the longest of the four — at 583 px across an 800 px panel
# and 77 px tall, which clears the trace window starting at 0.30 H.
STATE_SCALE_MAX = 11
STATE_MARGIN = 48

STATES = ("idle", "listening", "thinking", "speaking")


def palette(name=None):
    """The seven colours, for one theme. Levels live in theme.HUD_LEVELS so
    the shader gets the same numbers from the same place (it reconstructs the
    ramp from the theme's deep and accent colours, as reactor.frag does)."""
    c = theme.hud_colours(name)
    return {k: np.array(v, dtype=np.float32) for k, v in c.items()}


# How far the trace is knocked back ahead of the playhead. float32 rather than a
# bare literal so the multiply rounds exactly as the old per-pixel shade did.
AHEAD = np.float32(0.28)

# Static furniture and the meter's backing colour. Not themed, and never were.
DIM = np.array([28, 66, 88], dtype=np.float32)
METER_BG = np.array([22, 52, 70], dtype=np.float32)
DOT_R = 9


def build_chrome(w, h, wx0, wx1, wy0, wy1):
    """Static HUD furniture: corner brackets + a centre baseline. Drawn once."""
    img = np.zeros((h, w, 3), dtype=np.float32)
    dim = DIM
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


def state_scale(label: str, w: int) -> int:
    """The largest font scale at which ``label`` fits the panel's width."""
    scale = STATE_SCALE_MAX
    while scale > 4 and text_width(label, scale) > w - 2 * STATE_MARGIN:
        scale -= 1
    return scale


def encode_levels(levels) -> np.ndarray:
    """The envelope as a (1, N, 4) RGBA8 row: 16 bits of amplitude per sample.

    High byte in R, low byte in G, so the shader reads
    ``(R*256 + G) / 65535``. Eight bits would have done for the eye, but the
    verifier compares against this file's own arithmetic, and at 8 bits a
    half-pixel of band edge moved on a few dozen columns per frame.
    """
    lv = np.clip(np.asarray(levels, dtype=np.float64), 0.0, 1.0)
    q = np.round(lv * 65535.0).astype(np.uint32)
    out = np.zeros((1, max(len(q), 1), 4), dtype=np.uint8)
    out[0, :len(q), 0] = (q >> 8) & 255
    out[0, :len(q), 1] = q & 255
    out[..., 3] = 255
    return out


def decode_levels(rgba: np.ndarray) -> np.ndarray:
    """The inverse of encode_levels, as the shader computes it."""
    r = rgba[0, :, 0].astype(np.float64)
    g = rgba[0, :, 1].astype(np.float64)
    return (r * 256.0 + g) / 65535.0


class Hud:
    """The picture, as a function of time and the voice feed's document.

    Geometry is fixed by the panel size, so it is worked out once here;
    ``render`` is what the old main loop did per frame, minus the overlays and
    the clip, which the caller applies (main below, or the verifier). Nothing in
    it reads a clock or a file: everything it needs arrives as an argument, which
    is what lets the verifier ask for the same instant twice.
    """

    def __init__(self, w: int = 800, h: int = 480, theme_name=None):
        self.w, self.h = w, h
        p = palette(theme_name)
        self.cyan, self.green, self.amber, self.white = (
            p["base"], p["green"], p["amber"], p["white"])
        self.state_colour = {"idle": self.cyan * 0.75, "listening": self.green,
                             "thinking": self.amber, "speaking": self.cyan}

        # Trace window: the star of the screen, so give it the middle two thirds.
        self.wx0, self.wx1 = int(w * 0.06), int(w * 0.94)
        self.wy0, self.wy1 = int(h * 0.30), int(h * 0.86)
        self.wcy, self.whh = (self.wy0 + self.wy1) // 2, (self.wy1 - self.wy0) // 2
        self.cols = self.wx1 - self.wx0

        self.chrome = build_chrome(w, h, self.wx0, self.wx1, self.wy0, self.wy1)
        # Column-local grid, precomputed once: per frame we only compare against it.
        self.dy = (np.arange(self.wy0, self.wy1, dtype=np.float32) - self.wcy)[:, None]
        self.col_i = np.arange(self.cols)

        # The state dot's falloff never changes — build it once, not thirty
        # times a second.
        r = DOT_R
        yy, xx = np.mgrid[0:2 * r, 0:2 * r].astype(np.float32)
        self.dot = np.exp(-((((xx - r) / (r * 0.55)) ** 2
                             + ((yy - r) / (r * 0.55)) ** 2)))[..., None]

        # Everything outside the trace window — label, state dot, level meter —
        # sits in one band across the top. Its geometry is fixed.
        self.ty = int(h * 0.12)
        self.ddy0 = self.ty + (CHAR_H * 4) // 2 - r
        self.mw, self.mh = int(w * 0.20), 8
        self.mx0, self.my0 = self.wx1 - self.mw, int(h * 0.145)

    # ---- what the shader takes as textures -----------------------------------
    def word_layer(self, state: str) -> np.ndarray:
        """The state word and its dot, as an (H, W) intensity map, 0..1.

        Exactly the pixels ``render`` lights for them, before colour and pulse:
        the shader multiplies this by ``colour * pulse`` and adds it, which is
        what render does per pixel. Drawn by the same draw_text, so the glyphs
        cannot drift between the two.
        """
        layer = np.zeros((self.h, self.w, 3), dtype=np.float32)
        label = state.upper()
        scale = state_scale(label, self.w)
        tx = (self.w - text_width(label, scale)) // 2
        draw_text(layer, label, tx, self.ty, (1.0, 1.0, 1.0), scale=scale)
        r = DOT_R
        dx = tx - 4 * r
        layer[self.ddy0:self.ddy0 + 2 * r, dx:dx + 2 * r] += self.dot
        return np.clip(layer[..., 0], 0.0, 1.0)

    # ---- the frame -------------------------------------------------------------
    @staticmethod
    def clip_fraction(d: dict, now: float):
        """Where the playhead is through the clip, or None when there isn't one."""
        levels = d.get("levels")
        play_at, frame_dt = d.get("play_at"), d.get("frame_dt")
        if not (levels and play_at is not None and frame_dt):
            return None
        dur = len(levels) * frame_dt
        return (now - play_at) / dur if dur > 0 else None

    @classmethod
    def clip_showing(cls, d: dict, now: float) -> bool:
        frac = cls.clip_fraction(d, now)
        return bool(d.get("levels")) and frac is not None and -0.5 <= frac <= 1.25

    def render(self, t: float, now: float, d: dict, state: str, lvl: float) -> np.ndarray:
        """One frame as float RGB, unclipped and without the overlays.

        ``t`` is seconds since the animation started (the pulses); ``now`` is
        the clock the feed's play_at is on (the playhead); ``d`` is the feed's
        document; ``lvl`` the live level for the meter.
        """
        colour = self.state_colour.get(state, self.cyan)
        frame = self.chrome.copy()
        wy0, wy1, wx0 = self.wy0, self.wy1, self.wx0
        cols, whh, dy, col_i = self.cols, self.whh, self.dy, self.col_i
        win = frame[wy0:wy1, wx0:self.wx1]

        frac = self.clip_fraction(d, now)
        levels = d.get("levels")
        if levels and frac is not None and -0.5 <= frac <= 1.25:
            # --- the utterance, whole: waveform + playhead ---
            lv = np.asarray(levels, dtype=np.float32)
            idx = np.clip((col_i / cols * len(lv)).astype(int), 0, len(lv) - 1)
            amp = np.maximum(lv[idx] * (whh * 0.95), 1.5)     # mirrored envelope
            band = (np.abs(dy) <= amp[None, :])

            head = frac * cols
            # Ahead of the playhead sits what he hasn't said yet — dimmer. The
            # shade is a step at the playhead, so it's two column ranges, not a
            # per-pixel weight. ceil, not int: a column counts as played while
            # head is anywhere past its left edge.
            hcol = int(min(max(math.ceil(head), 0), cols))
            if hcol:
                win[:, :hcol][band[:, :hcol]] += colour
            if hcol < cols:
                win[:, hcol:][band[:, hcol:]] += colour * AHEAD

            if 0 <= head < cols:                             # the playhead itself
                hx = wx0 + int(head)
                frame[wy0:wy1, max(hx - 1, wx0):hx + 2] += self.white * 0.55
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
                    win[br0:br1] += glow[None, :, None] * self.amber * 1.6

        # --- state readout ---
        # Big, centred, and the largest thing on the panel — this screen is at
        # a child's eyeline and its job in a crowd is turn-taking, which a 28px
        # word in the corner could not do from the back of a queue.
        pulse = 0.75 + 0.25 * (0.5 + 0.5 * math.sin(t * 3.2))
        label = state.upper()
        scale = state_scale(label, self.w)
        tx = (self.w - text_width(label, scale)) // 2
        draw_text(frame, label, tx, self.ty, colour * pulse, scale=scale)
        # The dot still says the panel is live mid-word; it just moves out of
        # the way of the text it used to sit in front of.
        r = DOT_R
        dx = tx - 4 * r
        frame[self.ddy0:self.ddy0 + 2 * r, dx:dx + 2 * r] += self.dot * colour * pulse

        # --- live level meter, top right ---
        mx0, my0, mw, mh = self.mx0, self.my0, self.mw, self.mh
        frame[my0:my0 + mh, mx0:mx0 + mw] += METER_BG
        fill = int(mw * min(lvl, 1.0))
        if fill > 0:
            frame[my0:my0 + mh, mx0:mx0 + fill] += colour * 0.9
        return frame


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
    cog = CogHud()              # the settings cog, bottom-right
    pic = Hud(W, H)

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
            frame = pic.render(t, now, d, feed.state(), feed.level(now))

            # Clip the whole frame, not just the rects we drew into: `frame` is
            # contiguous and the sub-views are not, and one pass over contiguous
            # memory measured twice as fast as two passes over strided slices.
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
        print(f"{n} frames in {dur:.1f}s = {n / max(dur, 1e-6):.1f} fps", file=sys.stderr)


if __name__ == "__main__":
    main()

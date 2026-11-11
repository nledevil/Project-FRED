#!/usr/bin/env python3
"""Render every animation in every theme, to .npy frames, without a panel.

The companion to render_pages.py, and it exists for the same reason: the only
thing that had ever drawn a themed reactor was the robot. Colours are resolved
when a module is imported — reactor, flux, face, metrics_hud and cog_hud all ask
theme.ramp() once — so each frame is rendered in its own process with the theme
forced before the first import. That is also the honest way to test it, because
it is exactly how the daemon runs them: one process per animation, started after
the theme was chosen.

    python3 tools/render_anims.py --out /tmp
    # then, on a machine with Pillow:
    #   Image.fromarray(np.load("/tmp/an_neon_reactor.npy")).save("x.png")

Safe to run with the display daemon up: nothing here opens /dev/fb0.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DISPLAY = os.path.dirname(_HERE)
sys.path[:0] = [_HERE, _DISPLAY]                     # repo layout and flattened Pi layout

import theme                                          # noqa: E402

# module, extra argv, and how many frames in to grab — animations that breathe
# look wrong at t=0, where every pulse is at the same phase.
ANIMS = [
    ("reactor", [], 20),
    ("reactor", ["--copper"], 20),
    ("flux", [], 30),
    ("face", [], 20),
    ("voice_hud", [], 20),
]

# Rendered in a child so the theme is set before the first import. Frames come
# back through the FakeFB rather than a pipe: one frame, not a stream.
CHILD = r'''
import sys, numpy as np, time as _time
sys.path.insert(0, {display!r})

import theme
theme.load_name = lambda *a, **k: {theme_name!r}

FRAME = [None]
N = {grab!r}
PERIOD = 1.0 / 30.0
seen = [0]
_time.monotonic = lambda: seen[0] * PERIOD      # frame index as the clock
_time.sleep = lambda *a: None

class FakeFB:
    def __init__(self, dev="/dev/fb0"):
        self.w, self.h = 800, 480
    def show(self, frame):
        seen[0] += 1
        if seen[0] >= N and FRAME[0] is None:
            FRAME[0] = frame.copy()
            raise SystemExit(0)
    def clear(self): pass
    def close(self): pass

import {module} as anim
anim.Framebuffer = FakeFB
anim.hide_cursor = lambda: None
if hasattr(anim, "VoiceFeed"):
    # A fixed state, so the frame is the same every run: the voice HUD is the
    # one animation whose colours depend on what FRED is doing.
    class Feed:
        def __init__(self, *a, **k): pass
        def state(self, *a, **k): return {state!r}
        def level(self, *a, **k): return 0.4
        def poll(self, *a, **k):
            # A whole utterance mid-playback: the frame worth looking at is the
            # one with a waveform and a playhead in it, not an idle window.
            n = 96
            lv = [0.25 + 0.7 * abs(np.sin(i / 7.0)) for i in range(n)]
            return {{"levels": lv, "play_at": 0.0, "frame_dt": PERIOD}}
        def __getattr__(self, n): return lambda *a, **k: None
    anim.VoiceFeed = Feed

sys.argv = ["{module}.py"] + {extra!r}
try:
    anim.main()
except SystemExit:
    pass
if FRAME[0] is None:
    sys.exit("no frame captured")
np.save({out!r}, np.clip(FRAME[0], 0, 255).astype(np.uint8))
'''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="/tmp")
    ap.add_argument("--state", default="speaking",
                    help="voice state for the voice HUD frame")
    args = ap.parse_args()

    # Create it rather than failing 15 times over at the last line.
    os.makedirs(args.out, exist_ok=True)
    bad = 0
    for theme_name in theme.ORDER:
        for module, extra, grab in ANIMS:
            tag = module + ("-copper" if "--copper" in extra else "")
            out = os.path.join(args.out, f"an_{theme_name}_{tag}.npy")
            script = CHILD.format(display=_DISPLAY, theme_name=theme_name,
                                  module=module, extra=extra, grab=grab,
                                  out=out, state=args.state)
            r = subprocess.run([sys.executable, "-c", script],
                               capture_output=True, text=True)
            if r.returncode != 0 or not os.path.exists(out):
                print(f"  {theme_name}/{tag}: FAILED\n{r.stderr.strip()[-400:]}")
                bad += 1
    print("rendered" if not bad else f"{bad} animation(s) failed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

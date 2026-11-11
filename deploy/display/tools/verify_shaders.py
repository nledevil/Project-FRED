#!/usr/bin/env python3
"""Check each GPU animation against the numpy one it replaced.

The native C voice HUD used to be held to voice_hud.py byte for byte, 720
frames at a time. That technique does not survive the move to a GPU:
fragments sample pixel centres and np.mgrid samples corners, and on rings four
pixels wide that half-pixel moves up to 31 levels out of 255 locally. It is
invisible to the eye and fatal to an equality check, so this compares with a
tolerance instead and says so.

The voice HUD is the exception that proves it: its geometry is integer
rectangles, so the shader reproduces it almost exactly — but a band edge that
does move by a pixel moves by the whole colour, so its tolerance is a *count*
of pixels off, not a worst value. It is grabbed through panel.py rather than
gpu_anim.py, because the two textures it reads (the word, the envelope) are
made there; a fixed voice document is handed to both sides so the playhead
sits in the same place.

What it does *not* do is compare the shader against a numpy transliteration of
the shader — that would only prove the transliteration matches, which is the
easy half. It runs the real shader on the real GPU, grabs the frame, and diffs
it against reactor.py/flux.py/face.py/voice_hud.py at the same instant.

That means it takes the panel. Stop the animation first:

    curl -sX POST -H 'Content-Type: application/json' \\
         -d '{"animation":"off"}' http://10.0.0.11:8081/api/animation
    sudo python3 tools/verify_shaders.py

Needs root for the DRM device, and Pillow is not on this Pi — frames come back
as PNG from Qt and are read with QImage, which is already here.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
# Two layouts: tools/ is a subdirectory in the repo and everything is flat on
# the chest Pi. Find the host rather than assume which one we are in — assuming
# resolved to /home/dietpi and the whole run failed nine times identically.
_DISPLAY = next((d for d in (os.path.dirname(_HERE), _HERE)
                 if os.path.isfile(os.path.join(d, "gpu_anim.py"))),
                os.path.dirname(_HERE))
sys.path[:0] = [_HERE, _DISPLAY]

import json                                           # noqa: E402
from pathlib import Path                              # noqa: E402

import numpy as np                                    # noqa: E402

import theme                                          # noqa: E402
import voice_hud                                      # noqa: E402
from voice_state import VoiceFeed                     # noqa: E402

W, H = 800, 480

# How far a GPU frame may sit from the numpy one before it is a bug rather than
# a sampling difference. Measured: the half-pixel offset alone accounts for ~31
# levels on the reactor's thinnest rings, and nothing legitimate exceeds it by
# much. The mean is the more informative number — a real error moves it.
WORST_MAX = 40.0
MEAN_MAX = 3.0
# The voice HUD's hard edges: a pixel is either in the band or not, so a
# half-pixel disagreement is a whole colour, and the fair question is how many
# pixels disagree. One in a thousand is a few dozen edge pixels on a frame of
# 384,000; a wrong formula lights thousands.
OFF_FRACTION_MAX = 0.001

AT = 1.7            # the instant to compare at; nothing special, just not zero

# The voice HUD is a function of the feed's document as well as the clock, so
# the same four documents go to the shader and the reference. play_at is on
# the frozen clock (--at), so at AT the speaking clip's playhead is a third of
# the way across — bright behind it, dim ahead, the case that matters.
def _clip_levels(n: int = 240) -> list[float]:
    i = np.arange(n)
    env = 0.55 + 0.45 * np.sin(i / 7.0) * np.cos(i / 23.0)
    env *= np.exp(-((i - n / 2) / (n / 1.6)) ** 2)
    return [float(v) for v in np.clip(env, 0.0, 1.0)]


VOICE_CASES = {
    "idle":      {"state": "idle"},
    "listening": {"state": "listening"},
    "thinking":  {"state": "thinking"},
    "speaking":  {"state": "speaking", "levels": _clip_levels(),
                  "play_at": 0.0, "frame_dt": 0.02},
    # The playhead in its last column: the reference paints one column past
    # the window's edge there, which the first shader missed (found in
    # review) and the count tolerance alone could not see.
    "speaking_end": {"state": "speaking", "levels": _clip_levels(),
                     "play_at": AT - 240 * 0.02 * 0.9995, "frame_dt": 0.02},
}


def numpy_reference(anim: str, theme_name: str, t: float,
                    voice_doc: str = "") -> np.ndarray:
    """What the CPU animation draws at time ``t``, as float RGB."""
    ramp = theme.THEMES[theme_name].ramp()
    if anim == "voice-hud":
        # The same document, through the same feed class the panel reads it
        # with, at the same frozen instant. clip() is what main() does before
        # the overlays; the overlays are off on both sides.
        feed = VoiceFeed(Path(voice_doc))
        d = feed.poll()
        hud = voice_hud.Hud(W, H, theme_name)
        return np.clip(hud.render(t, t, d, feed.state(), feed.level(t)), 0, 255)
    if anim in ("reactor", "reactor-copper"):
        import reactor
        A, B = reactor.build_geometry(W, H, copper=anim.endswith("copper"), ramp=ramp)
        pulse = 0.55 + 0.45 * (0.5 + 0.5 * np.sin(t * 2.2))
        glow = float(pulse * (1.0 + 0.04 * np.sin(t * 37.0)))
        return np.clip(A + glow * B, 0, 255)
    if anim == "flux":
        # The base *and* the spark. Comparing against the resting tubes alone
        # scored a worst of 199 with a mean of 1.6 — a handful of very bright
        # pixels, which is exactly what a missing spark looks like, and it was
        # the reference that was wrong rather than the shader.
        import flux
        base, arms, hub_flash = flux.build(W, H, ramp=ramp)
        frame = base.reshape(-1, 3).copy()
        spark_col = np.array(ramp.at(0.78), np.float32)
        flash = 0.0
        for i, (idx, along, glow) in enumerate(arms):
            phase = (t * 1.4 + i / 3.0) % 1.0        # flux.py's --speed default
            pos = 1.0 - phase                        # the spark runs end -> hub
            spark = np.exp(-(((along - pos) / 0.10) ** 2)) * glow
            frame[idx] += spark[:, None] * spark_col
            flash = max(flash, float(np.exp(-((pos / 0.12) ** 2))))
        frame += (flash * hub_flash.reshape(-1))[:, None] * np.float32(255.0)
        return np.clip(frame.reshape(H, W, 3), 0, 255)
    if anim == "face":
        # Only the HUD ring and arcs. The eyes and mouth are driven by gaze,
        # blink and microphone state that evolve frame to frame, so comparing
        # them means making face.py's per-frame drawing callable from here
        # rather than copying its formulas into this file — which is the same
        # duplication that has bitten this repo three times this week. Until
        # then this checks the two-thirds of the face that is a pure function
        # of time, and says so rather than implying more.
        import face
        A, B = face.build_hud(W, H)
        glow = 0.6 + 0.4 * (0.5 + 0.5 * np.sin(t * 1.6))    # idle rate
        return np.clip(A + glow * B, 0, 255)
    raise SystemExit(f"no reference for {anim}")


def gpu_frame(anim: str, theme_name: str, t: float, tmp: str,
              voice_doc: str = "", tag: str = "") -> np.ndarray | None:
    """Render one frame on the GPU and read it back."""
    png = os.path.join(tmp, f"{anim}_{theme_name}{tag}.png")
    if anim == "voice-hud":
        argv = [sys.executable, os.path.join(_DISPLAY, "panel.py"),
                "--anim", "voice-hud", "--grab", png, "--at", str(t),
                "--theme", theme_name, "--no-overlay", "--voice-json", voice_doc,
                "--grab-after", "2.5"]
    else:
        argv = [sys.executable, os.path.join(_DISPLAY, "gpu_anim.py"),
                anim.replace("-copper", ""), "--grab", png, "--at", str(t),
                "--theme", theme_name, "--no-overlay"]
    if anim.endswith("-copper"):
        argv.append("--copper")
    r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if not os.path.exists(png):
        print(f"    gpu render failed: {r.stderr.strip()[-300:]}")
        return None
    from PySide6.QtGui import QImage
    img = QImage(png)
    if img.isNull():
        print(f"    could not read {png}")
        return None
    img = img.convertToFormat(QImage.Format_RGB888)
    ptr = img.constBits()
    arr = np.frombuffer(ptr, np.uint8, count=img.sizeInBytes())
    return arr.reshape(img.height(), img.bytesPerLine() // 3, 3)[:, :img.width()].astype(np.float64)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anim", action="append", default=[],
                    help="restrict to one animation (repeatable)")
    args = ap.parse_args()

    anims = args.anim or ["reactor", "reactor-copper", "flux", "voice-hud"]
    tmp = tempfile.mkdtemp(prefix="verify-shaders-")
    bad = 0
    print(f"comparing the GPU against numpy at t={AT}s, tolerance "
          f"worst<={WORST_MAX:.0f} mean<={MEAN_MAX:.1f} of 255 "
          f"(voice-hud: off<={OFF_FRACTION_MAX:.1%} of pixels, mean<={MEAN_MAX:.1f})\n")
    for anim in anims:
        cases = list(VOICE_CASES.items()) if anim == "voice-hud" else [("", None)]
        for case, doc in cases:
            voice_doc = ""
            if doc is not None:
                voice_doc = os.path.join(tmp, f"voice_{case}.json")
                with open(voice_doc, "w") as f:
                    json.dump(doc, f)
            for theme_name in theme.ORDER:
                label = f"{anim}/{theme_name}" + (f"/{case}" if case else "")
                ref = numpy_reference(anim, theme_name, AT, voice_doc)
                got = gpu_frame(anim, theme_name, AT, tmp, voice_doc, f"_{case}" if case else "")
                if got is None:
                    bad += 1
                    continue
                if got.shape != ref.shape:
                    print(f"  FAIL  {label}: {got.shape} vs {ref.shape}")
                    bad += 1
                    continue
                diff = np.abs(got - ref)
                worst, mean = float(diff.max()), float(diff.mean())
                off = float((diff.max(axis=2) > WORST_MAX).mean())
                if anim == "voice-hud":
                    ok = off <= OFF_FRACTION_MAX and mean <= MEAN_MAX
                else:
                    ok = worst <= WORST_MAX and mean <= MEAN_MAX
                bad += 0 if ok else 1
                print(f"  {'PASS' if ok else 'FAIL'}  {label:34} "
                      f"worst {worst:6.2f}  mean {mean:5.3f}  off {off:.3%}")
    print()
    print("the GPU animations match the ones they replaced" if not bad
          else f"{bad} comparison(s) failed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

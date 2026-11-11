#!/usr/bin/env python3
"""The voice HUD is one renderer now, and the shader's inputs are the reference's.

The chest had three rendering stacks: the numpy animations, the Qt panel with
its shaders, and a native C port of the voice HUD — 1,166 lines, a cross-compile
on the other Pi, a pixel verifier and a generated colour header, all because
the menu could not live inside it. The menu is a panel scene now, so the HUD
became a shader hosted by the same panel as every other look, and the C stack
went. This pins what that rests on:

**The shader's textures are the reference's pixels.** The state word comes to
the GPU as an intensity map that voice_hud.Hud.word_layer draws with the same
draw_text the reference uses; this checks the band above the trace window in a
rendered frame is exactly chrome + word * colour * pulse + meter — i.e. that
multiplying the layer by colour and pulse, which is what the shader does, is
what render() does. The envelope goes as 16 bits across two channels; this
checks the round trip.

**When a clip is on screen is one decision.** Hud.clip_showing / clip_fraction
are what the panel feeds the shader (haveClip, head); a build with a fake feed
must see a clip exactly when the reference would draw one.

**The daemon launches one renderer.** Every preset is panel.py or nothing, the
retired id resolves to the shader rather than to an error or the boot default,
and the cog rule leaves the panel to its own cog for the HUD too.

Hardware-free: the panel is built with the poller, PIN material and shader
compiler stubbed, as test_panel_views.py does.

    python3 deploy/display/tools/test_voice_hud_shader.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Two layouts: tools/ is a subdirectory in the repo and everything is flat on
# the chest Pi. Find the panel rather than assume which one we are in.
DISPLAY = next((d for d in (os.path.dirname(HERE), HERE)
                if os.path.isfile(os.path.join(d, "panel.py"))), os.path.dirname(HERE))
sys.path.insert(0, DISPLAY)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np                                           # noqa: E402

import display_control as dc                                 # noqa: E402
import panel                                                 # noqa: E402
import voice_hud                                             # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeNet:
    snap: dict = {"at": 1.0, "age": 0.2, "nuc": None, "whoami": None}

    def start(self):
        pass

    def snapshot(self):
        return dict(FakeNet.snap)

    def scan_state(self):
        return {"networks": [], "busy": False, "at": 0.0}


class FakeFeed:
    """A voice document the test controls, on the same clock as the panel."""

    doc: dict = {"state": "idle"}

    def poll(self):
        return FakeFeed.doc

    def state(self):
        return FakeFeed.doc.get("state", "idle")

    def level(self, now=None):
        return 0.0


def main() -> int:
    hud = voice_hud.Hud(800, 480)

    print("the word layer is exactly what render() draws above the trace window")
    t = 1.7
    pulse = 0.75 + 0.25 * (0.5 + 0.5 * math.sin(t * 3.2))
    for state in voice_hud.STATES:
        frame = hud.render(t, t, {"state": state}, state, 0.37)
        layer = hud.word_layer(state)
        colour = hud.state_colour[state]
        expect = hud.chrome.copy()
        expect += layer[..., None] * colour * pulse
        my0, mh, mx0, mw = hud.my0, hud.mh, hud.mx0, hud.mw
        expect[my0:my0 + mh, mx0:mx0 + mw] += voice_hud.METER_BG
        fill = int(mw * 0.37)
        expect[my0:my0 + mh, mx0:mx0 + fill] += colour * 0.9
        band = slice(0, hud.wy0)
        worst = float(np.abs(frame[band] - expect[band]).max())
        check(f"{state:9} band above the window == chrome + word*colour*pulse + meter",
              worst < 1e-3, f"worst {worst:.4f}")
        lit = int((layer > 0).sum())
        check(f"{state:9} the word and dot are there", lit > 500, f"{lit} px")
    check("the layer is an intensity map, 0..1",
          float(hud.word_layer("listening").max()) <= 1.0 + 1e-6)

    print("the envelope survives its trip through two 8-bit channels")
    levels = [0.0, 1.0, 0.5, 0.123456, 0.999, 0.00001, 0.7]
    back = voice_hud.decode_levels(voice_hud.encode_levels(levels))
    err = float(np.abs(back - np.array(levels)).max())
    check("round trip within one part in 65535", err <= 1.0 / 65535 + 1e-9, f"{err:.2e}")
    check("an empty envelope still makes a one-texel texture",
          voice_hud.encode_levels([]).shape == (1, 1, 4))
    long = [0.1] * 9000
    long[4444] = 0.9                                     # one peak, mid-clip
    enc = voice_hud.encode_levels(long)
    back = voice_hud.decode_levels(enc)
    check("a clip longer than the GPU's widest texture is bucketed to fit",
          enc.shape[1] == voice_hud.ENVELOPE_MAX, str(enc.shape))
    check("...without losing its peak", abs(float(back.max()) - 0.9) < 1e-4, str(back.max()))

    print("when a clip is on screen is one decision, shared with the panel")
    doc = {"state": "speaking", "levels": [0.2] * 100, "play_at": 10.0, "frame_dt": 0.02}
    check("no levels: no clip", hud.clip_fraction({"state": "idle"}, 5.0) is None)
    check("before it starts (frac -0.5..0): showing, so the whole waveform is dim",
          hud.clip_showing(doc, 9.5) and not hud.clip_showing(doc, 8.9))
    check("mid-clip: showing at the right fraction",
          abs(hud.clip_fraction(doc, 11.0) - 0.5) < 1e-9)
    check("long after: gone", not hud.clip_showing(doc, 13.0))

    panel.Net = FakeNet
    panel.pin_gate.material = lambda nuc="": {}
    panel.qsb_for = lambda name: ""
    p = panel.Panel("voice-hud", "anim")
    p.set_feed(FakeFeed())
    p.freeze(11.0)
    FakeFeed.doc = doc
    p.tick()
    check("panel: a clip on screen -> haveClip 1, head at the reference's column",
          p.haveClip == 1.0 and abs(p.head - 0.5 * hud.cols) < 1e-6,
          f"haveClip={p.haveClip} head={p.head}")
    check("panel: envLen is the clip's sample count", p.envLen == 100.0, str(p.envLen))
    check("panel: the word is the state", p.voiceWord == "speaking", p.voiceWord)
    gen = p.envGen
    for _ in range(5):
        p.tick()
    check("panel: the same clip is encoded once, not every tick", p.envGen == gen)
    FakeFeed.doc = {"state": "listening"}
    p.tick()
    check("panel: no clip -> haveClip 0, word LISTENING",
          p.haveClip == 0.0 and p.voiceWord == "listening", f"{p.haveClip} {p.voiceWord}")
    w, m = p.win, p.meter
    check("panel: the window and meter uniforms are the reference's geometry",
          (w.x(), w.y(), w.z(), w.w()) == (hud.wx0, hud.wy0, hud.wx1, hud.wy1)
          and (m.x(), m.y(), m.z(), m.w()) == (hud.mx0, hud.my0, hud.mw, hud.mh))
    img = p.word.requestImage("listening", None, None)
    check("the word provider serves a panel-sized image",
          img is not None and (img.width(), img.height()) == (800, 480))
    check("the shader is a preset the panel knows", panel.SHADERS.get("voice-hud") == "voice_hud")
    # Two layouts: shaders/ in the repo, flat on the chest Pi.
    check("...and the shader source exists",
          any(os.path.isfile(os.path.join(DISPLAY, *parts))
              for parts in (("shaders", "voice_hud.frag"), ("voice_hud.frag",))))

    print("the daemon launches one renderer")
    others = [q["id"] for q in dc.PRESETS if q["argv"] not in (None, ["panel.py"], ["panel.py", "--menu"])]
    check("every preset is panel.py or nothing", not others, str(others))
    check("voice-hud is the panel", dc.PRESET_BY_ID["voice-hud"]["argv"] == ["panel.py"])
    check("the retired native id is gone from the table", "voice-hud-c" not in dc.PRESET_BY_ID)
    check("...but still resolves to the shader", dc.LEGACY_PRESETS.get("voice-hud-c") == "voice-hud")
    check("the cog on the HUD is the panel's own business",
          not dc.opens_menu("voice-hud", "down", 770, 450))
    for gone in ("voice_hud.c", "Makefile", "theme_colors.h",
                 os.path.join("tools", "verify_voice_hud.py"),
                 os.path.join("tools", "gen_theme_colors.py")):
        check(f"{gone} is gone from the tree", not os.path.exists(os.path.join(DISPLAY, gone)))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

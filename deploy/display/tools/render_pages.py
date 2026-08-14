#!/usr/bin/env python3
"""Render every settings page in every theme, to .npy frames, without a panel.

Layout on this screen is hand-placed, and the themes do not share text metrics —
a label that fits in one typeface overflows in another. This draws each page
against a hand-made snapshot so that breakage is visible before somebody walks
over to the robot and taps through eight tabs.

It has already earned its keep twice: it caught long animation labels running
off their buttons in the HUD theme (the page cached its layout on the animation
list alone, so switching typeface kept the old sizes), and the status rows
putting their sub-labels through the bottom edge (fixed offsets from when every
scale was the same 7px grid).

    python3 tools/render_pages.py --out /tmp
    # then convert on a machine that has Pillow:
    #   Image.fromarray(np.load("/tmp/pg_soft_status.npy")).save("x.png")

Run it on the chest Pi — it imports the pages, which import numpy and each
other. It never opens the framebuffer, so it is safe to run with the display
daemon up.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.dirname(_HERE)]       # repo layout and flattened Pi layout

import menu_ui as ui                                 # noqa: E402
import theme as theme_mod                            # noqa: E402

W, H = 800, 480

# Enough of a snapshot to exercise the drawing. Pages are written to be
# defensive about missing keys, so the gaps here are themselves a test.
SNAP = {
    "chest": {
        "animations": [{"id": i, "label": l} for i, l in (
            ("off", "Off"), ("reactor", "Arc Reactor"), ("flux", "Flux"),
            ("face", "Face"), ("metrics", "Metrics"), ("voice", "Voice HUD"),
            ("copper", "Arc Reactor (Copper)"), ("pulse", "Pulse"))],
        "display": {"animation": "reactor", "running": True, "label": "Arc Reactor"},
        "cart": {"connected": True, "battery_v": 39.2, "estop": False},
    },
    "voice": {"enabled": True, "state": "listening"},
    "hotspot": {"enabled": False, "ssid": "fred"},
    "servos": {"count": 14},
}


def pages():
    import page_cart, page_display, page_info, page_servos, page_status, page_voice, page_wireless
    return [("display", page_display.DisplayPage()), ("status", page_status.StatusPage()),
            ("wireless", page_wireless.WirelessPage()), ("servos", page_servos.ServosPage()),
            ("info", page_info.InfoPage()), ("voice", page_voice.VoicePage()),
            ("cart", page_cart.CartPage())]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="/tmp")
    args = ap.parse_args()

    bad = 0
    for name in theme_mod.ORDER:
        ui.apply_theme(name)
        # Rebuilt per theme on purpose: a page that caches its layout across a
        # theme switch is the bug this harness exists to catch, and sharing one
        # instance would hide it behind the cache instead of showing it.
        for page_name, page in pages():
            frame = np.zeros((H, W, 3), np.float32)
            frame[:] = ui.BG
            try:
                page.draw(frame, SNAP)
            except Exception as exc:                 # noqa: BLE001
                print(f"  {name}/{page_name}: RAISED {type(exc).__name__}: {exc}")
                bad += 1
                continue
            out = os.path.join(args.out, f"pg_{name}_{page_name}.npy")
            np.save(out, np.clip(frame, 0, 255).astype(np.uint8))
    print("rendered" if not bad else f"{bad} page(s) raised")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

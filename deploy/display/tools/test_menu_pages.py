#!/usr/bin/env python3
"""Exercise the two new settings pages and the keyboard, with no screen.

The keyboard is the part worth testing rather than eyeballing. It is the first
thing on this panel that shows text a person typed, and the font upper-cases by
default — so the failure it has to not have is silent: a passphrase displayed as
MYPASS while MyPass is what gets stored, on the only screen that could have told
you. Several checks below exist only to pin that down.

The display picker is simpler, but it builds its grid from whatever the daemon
offers, so it is checked against lists it was not designed around.

    python3 tools/test_menu_pages.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import numpy as np                                      # noqa: E402

import keyboard as kbmod                                # noqa: E402
from font5x7 import FONT                                # noqa: E402
from page_display import DisplayPage                    # noqa: E402
from page_info import InfoPage                          # noqa: E402
from page_wireless import WirelessPage                  # noqa: E402

FAILURES: list[str] = []
W, H = 800, 480


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeNet:
    def __init__(self, snap=None):
        self.calls = []
        self._snap = snap or {}

    def snapshot(self):
        return self._snap

    def post_animation(self, a):
        self.calls.append(("animation", a))

    def post_hotspot(self, on):
        self.calls.append(("hotspot", on))

    def post_hotspot_config(self, ssid, psk):
        self.calls.append(("config", ssid, psk))


def centre(rect):
    x0, y0, x1, y1 = rect
    return ((x0 + x1) // 2, (y0 + y1) // 2)


def press(kb, keys):
    """Tap these keys, switching layers to find them like a person would.

    Digits and symbols live on the SYM layer and capitals behind SHIFT, so a
    helper that only looked at the current layer could not type a realistic
    passphrase — which is exactly what these tests need to do.
    """
    for k in keys:
        for _ in range(4):
            lookup = {k2: b for k2, b in kb._keys}
            if k in lookup:
                kb.on_touch("down", *centre(lookup[k].rect))
                break
            if len(k) == 1 and k.isalpha() and k.isupper():
                switch = "SHIFT"
            else:
                switch = "abc" if kb._layer == "sym" else "SYM"
            kb.on_touch("down", *centre(lookup[switch].rect))
        else:
            raise AssertionError(f"no key {k!r} on any layer")


ANIMS = [{"id": "reactor", "label": "Arc Reactor"},
         {"id": "flux", "label": "Flux Capacitor"},
         {"id": "face", "label": "Face (live voice)"},
         {"id": "off", "label": "Off (blank screen)"}]


def main() -> int:
    frame = np.zeros((H, W, 3), dtype=np.float32)

    print("the keyboard types what it shows")
    kb = kbmod.Keyboard("TEST", "")
    press(kb, list("fred"))
    check("lowercase keys type lowercase", kb.text == "fred", repr(kb.text))
    press(kb, ["SHIFT"] + ["A"])
    check("shift types one capital", kb.text == "fredA", repr(kb.text))
    check("...then falls back to lowercase", kb._layer == "lower")
    press(kb, ["DEL"])
    check("DEL removes the last character", kb.text == "fred", repr(kb.text))
    press(kb, ["SYM"])
    check("SYM switches layer", kb._layer == "sym")
    press(kb, ["!"])
    check("symbols type themselves", kb.text == "fred!", repr(kb.text))

    print("every key can actually be drawn")
    # The whole reason the keyboard is limited to this charset: a glyph the font
    # lacks renders as a blank with no error, so an unrenderable key would type
    # a character you could never read back.
    unrenderable = sorted({k for layer in (kbmod.LOWER, kbmod.UPPER, kbmod.SYMS)
                           for row in layer for k in row
                           if k not in kbmod.ACTIONS and k not in FONT})
    check("no key lacks a glyph", not unrenderable, str(unrenderable))
    check("lowercase glyphs differ from their capitals",
          not np.array_equal(FONT["a"], FONT["A"]))
    check("...for every letter",
          all(not np.array_equal(FONT[c], FONT[c.upper()])
              for c in "abcdefghijklmnopqrstuvwxyz"))

    print("length rules")
    kb = kbmod.Keyboard("PSK", "", min_len=8, max_len=12)
    press(kb, list("short"))
    check("DONE is refused below the minimum", not kb.ok(), f"{len(kb.text)} chars")
    press(kb, ["DONE"])
    check("...and pressing it does nothing", kb.active)
    press(kb, list("enoughmore"))        # 5 + 10 would be 15, capped at 12
    check("the maximum is enforced while typing", len(kb.text) == 12,
          f"{len(kb.text)} chars: {kb.text!r}")
    check("...and it is now valid", kb.ok())
    press(kb, ["DONE"])
    check("DONE closes it", not kb.active and not kb.cancelled)

    kb = kbmod.Keyboard("PSK", "whatever")
    press(kb, ["CANCEL"])
    check("CANCEL closes it as cancelled", not kb.active and kb.cancelled)

    print("the wireless page saves both fields together")
    ap = {"hotspot": {"configured": True, "enabled": True, "ssid": "fred",
                      "address": "192.168.50.1", "clients": 0}}
    page, net = WirelessPage(), FakeNet(ap)
    page.draw(frame, ap)
    page.on_touch("down", *centre(page._ssid_btn.rect), net)
    check("tapping NAME opens a keyboard", page._kb is not None)
    check("...seeded with the current SSID", page._kb.text == "fred", repr(page._kb.text))
    press(page._kb, ["DEL"] * 4 + list("venue"))
    page.on_touch("down", *centre({k: b for k, b in page._kb._keys}["DONE"].rect), net)
    check("a name alone does not save", net.calls == [], f"{net.calls}")
    check("...and says why", "PASSWORD" in page._note, page._note)

    page.on_touch("down", *centre(page._psk_btn.rect), net)
    check("the password field starts empty, never prefilled",
          page._kb.text == "", repr(page._kb.text))
    press(page._kb, list("hunter2hunter2"))
    page.on_touch("down", *centre({k: b for k, b in page._kb._keys}["DONE"].rect), net)
    check("both together do save", net.calls == [("config", "venue", "hunter2hunter2")],
          f"{net.calls}")
    check("the passphrase is not kept after saving", page._pending_psk == "",
          repr(page._pending_psk))

    print("...and refuses to edit with no brain")
    page, net = WirelessPage(), FakeNet({})
    page.draw(frame, {})
    page.on_touch("down", *centre(page._ssid_btn.rect), net)
    check("no keyboard opens without the brain", page._kb is None)
    check("...and it says so", "NO LINK" in page._note, page._note)

    print("the display picker")
    page, net = DisplayPage(), FakeNet()
    snap = {"chest": {"animations": ANIMS,
                      "display": {"animation": "flux", "label": "Flux Capacitor",
                                  "running": True}}}
    page.draw(frame, snap)
    check("a button per animation", len(page._buttons) == len(ANIMS),
          f"{len(page._buttons)}")
    check("buttons stay on screen",
          all(0 <= b.rect[0] < b.rect[2] <= W and 0 <= b.rect[1] < b.rect[3] <= H
              for _a, b in page._buttons))
    overlap = [(a1, a2) for i, (a1, b1) in enumerate(page._buttons)
               for a2, b2 in page._buttons[i + 1:]
               if not (b1.rect[2] <= b2.rect[0] or b1.rect[0] >= b2.rect[2]
                       or b1.rect[3] <= b2.rect[1] or b1.rect[1] >= b2.rect[3])]
    check("no two buttons overlap", not overlap, str(overlap))
    page.on_touch("down", *centre(page._buttons[0][1].rect), net)
    check("tapping selects that animation", net.calls == [("animation", "reactor")],
          f"{net.calls}")
    check("...and lights immediately", page._pending == "reactor")

    # Built from the list, so a daemon that grows or loses a preset still works.
    for n in (1, 3, 8, 11):
        p2 = DisplayPage()
        p2.draw(frame, {"chest": {"animations": [{"id": f"a{i}", "label": f"Anim {i}"}
                                                 for i in range(n)]}})
        check(f"lays out {n} animation(s)", len(p2._buttons) == n,
              f"{len(p2._buttons)}")
        check(f"...{n} stays on screen",
              all(b.rect[3] <= H for _a, b in p2._buttons))

    print("the info page")
    who = {"hostname": "fred", "uptime_s": 7800,
           "addresses": [{"interface": "br0", "address": "10.0.0.1", "up": True}],
           "revision": {"commit": "abc1234", "branch": "nuc-brain", "dirty": False},
           "inference": {"library": "Vulkan", "name": "Vulkan0", "total": "22.7 GiB"},
           "brain": {"active": "claude", "backend": "auto", "model": "qwen2.5:3b"}}
    frame[:] = 0.0
    InfoPage().draw(frame, {"whoami": who})
    check("the info page draws", bool(np.isfinite(frame).all()))
    check("...and paints something", float(frame.max()) > 0.0)

    # The line the page exists for: a CPU fallback is the silent 25x slowdown,
    # so it has to look different from a healthy GPU, not just say a word.
    def band(w):
        f = np.zeros((H, W, 3), dtype=np.float32)
        InfoPage().draw(f, {"whoami": {**who, "inference": w}})
        return f[130:150, 300:700].copy()
    gpu = band({"library": "Vulkan", "name": "Vulkan0", "total": "22.7 GiB"})
    cpu = band({"library": "cpu"})
    check("a CPU fallback is drawn differently from the GPU",
          not np.array_equal(gpu, cpu))
    check("...and it is not the same colour",
          not np.array_equal(gpu.reshape(-1, 3).max(axis=0),
                             cpu.reshape(-1, 3).max(axis=0)),
          f"{tuple(gpu.reshape(-1,3).max(axis=0))} vs {tuple(cpu.reshape(-1,3).max(axis=0))}")

    frame[:] = 0.0
    InfoPage().draw(frame, {})
    check("with no brain it says so rather than drawing blanks",
          bool(np.isfinite(frame).all()) and float(frame.max()) > 0.0)

    print("the tab strip still fits")
    # Seven tabs at 100px; the reason WIRELESS became WIFI. A label that
    # overflows its box spills onto its neighbour, silently.
    from font5x7 import text_width as tw
    titles = ["STATUS", "VOICE", "SERVOS", "CART", "DISPLAY", "WIFI", "INFO"]
    tab_w = (776 - 24 - 8 * (len(titles) - 1)) // len(titles)
    for t in titles:
        check(f"tab {t!r} fits its box", tw(t, 2) <= tab_w - 8,
              f"{tw(t, 2)} <= {tab_w - 8}px")

    print("drawing")
    for name, page_, snap_ in (
            ("display", DisplayPage(), snap),
            ("display with no list", DisplayPage(), {}),
            ("wireless", WirelessPage(), ap),
            ("wireless with no brain", WirelessPage(), {})):
        frame[:] = 0.0
        try:
            page_.draw(frame, snap_)
            drew = True
        except Exception as exc:                        # noqa: BLE001 — report it
            drew, name = False, f"{name}: {exc!r}"
        check(f"draws {name}", drew)
        check(f"...{name} stays finite", bool(np.isfinite(frame).all()))

    frame[:] = 0.0
    kb = kbmod.Keyboard("PSK", "MiXeD-case-42!")
    kb.draw(frame)
    check("the keyboard draws", bool(np.isfinite(frame).all()))
    check("...and paints something", float(frame.max()) > 0.0)

    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

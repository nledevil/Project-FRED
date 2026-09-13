#!/usr/bin/env python3
"""Something to touch, and a screen that sleeps when nobody is there (T1, T2).

Outside the menu the chest screen answered a finger with nothing, or with a
PIN pad. Now a tap cycles the looks and a swipe up opens a read-only card, and
an "attract" preset turns the looks over by itself and blanks the screen when
the motion sensor has seen nobody for a while. This pins the judgements those
rest on — the parts a screenshot cannot show:

**The tap goes somewhere sensible.** The next look in the daemon's own order,
skipping blank, the menu and attract itself; the first look when the current
one is not in the list; nothing at all when the list has not arrived; and in
event mode no change — the chest is the turn-taking signal there, so the tap
says how to talk to him instead. In attract mode the tap advances the cycle
without leaving the mode.

**The card closes itself**, after ABOUT_IDLE_S without a touch, and never
opens over a sleeping screen.

**Sleep needs evidence.** Asleep only when the motion sensor is fresh *and*
quiet for SLEEP_AFTER_S with no touch; a stale or missing sensor doc means
awake, so an unplugged node can never look like a crash. Motion or a touch
wakes it, and the menu is never put to sleep.

**The daemon's cycle is a ring**, and picking attract starts it from a real
look at once rather than a minute later.

Hardware-free, with the panel's poller, PIN material and shader compiler
stubbed as the other panel tests do.

    python3 deploy/display/tools/test_visitor_attract.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
DISPLAY = os.path.dirname(HERE)
sys.path.insert(0, DISPLAY)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import display_control as dc                                 # noqa: E402
import panel                                                 # noqa: E402
import theme                                                 # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


ANIMS = [{"id": "reactor", "label": "Arc Reactor"}, {"id": "flux", "label": "Flux Capacitor"},
         {"id": "voice-hud", "label": "Voice HUD"}, {"id": "attract", "label": "Attract"},
         {"id": "off", "label": "Off"}]


class FakeNet:
    snap: dict = {"at": 1.0, "age": 0.2, "nuc": None, "whoami": None,
                  "chest": {"animations": ANIMS}}
    posted: list = []

    def start(self):
        pass

    def snapshot(self):
        return dict(FakeNet.snap)

    def scan_state(self):
        return {"networks": [], "busy": False, "at": 0.0}

    def post_animation(self, anim):
        FakeNet.posted.append(anim)


class FakeMetrics:
    doc: dict = {}

    def poll(self):
        return FakeMetrics.doc


def make(scene="anim"):
    panel.Net = FakeNet
    panel.pin_gate.material = lambda nuc="": {}
    panel.qsb_for = lambda name: ""
    p = panel.Panel(None, scene)
    p._metrics = FakeMetrics()
    toasts = []
    p.toast.connect(toasts.append)
    return p, toasts


def main() -> int:
    print("the tap goes somewhere sensible")
    check("next after reactor is flux", panel.next_look("reactor", ANIMS) == "flux")
    check("the ring wraps, skipping attract and off",
          panel.next_look("voice-hud", ANIMS) == "reactor")
    check("an unknown current lands on the first look",
          panel.next_look("settings", ANIMS) == "reactor")
    check("no list, no pick", panel.next_look("reactor", []) is None)

    p, toasts = make()
    FakeNet.posted.clear()
    p.tick()
    p.visitorTap()
    check("a tap asks the daemon for the next look", FakeNet.posted == ["flux"], str(FakeNet.posted))
    check("...and says which", toasts and toasts[-1] == "FLUX CAPACITOR", str(toasts))
    FakeNet.snap = {**FakeNet.snap, "nuc": {"event": {"enabled": True}}}
    p.tick()
    p.visitorTap()
    check("event mode: no change, a hint instead",
          FakeNet.posted == ["flux"] and "FRED" in toasts[-1], str(toasts[-1]))
    FakeNet.snap = {**FakeNet.snap, "nuc": None}
    p.tick()
    p._attract = True
    p.visitorTap()
    check("attract: the tap advances the look locally and stays in the mode",
          FakeNet.posted == ["flux"] and p._anim == "flux" and p._attract,
          f"posted={FakeNet.posted} anim={p._anim}")

    print("the card closes itself")
    p, _ = make()
    p.openAbout()
    check("swipe up opens it", p.about)
    p._last_touch = time.monotonic() - panel.ABOUT_IDLE_S + 5
    p.tick()
    check("still up inside the idle window", p.about)
    p._last_touch = time.monotonic() - panel.ABOUT_IDLE_S - 1
    p.tick()
    check("gone after ABOUT_IDLE_S without a touch", not p.about)

    print("sleep needs evidence")
    p, _ = make()
    now = time.monotonic()
    quiet = now - panel.SLEEP_AFTER_S - 10
    p._attract = True
    p._last_touch = quiet
    p._motion_at = quiet
    FakeMetrics.doc = {}
    p.tick()
    check("no sensor doc at all: awake", not p.asleep)
    FakeMetrics.doc = {"t": now - panel.SENSOR_FRESH_S - 5,
                       "readings": {"pir": {"type": "motion", "active": False}}}
    p.tick()
    check("a stale sensor doc: awake", not p.asleep)
    FakeMetrics.doc = {"t": now, "readings": {"pir": {"type": "motion", "active": False}}}
    p.tick()
    check("fresh sensor, nobody for SLEEP_AFTER_S, no touch: asleep", p.asleep)
    check("the card will not open over a sleeping screen", (p.openAbout(), not p.about)[1])
    FakeMetrics.doc = {"t": now, "readings": {"pir": {"type": "motion", "active": True}}}
    p.tick()
    check("motion wakes it", not p.asleep)
    FakeMetrics.doc = {"t": now, "readings": {"pir": {"type": "motion", "active": False}}}
    p._motion_at = quiet
    p.tick()
    check("...and it sleeps again once the motion is old", p.asleep)
    p._last_touch = time.monotonic()
    p.tick()
    check("a touch wakes it", not p.asleep)
    p._last_touch = quiet
    p._no_gate = True                   # keep the menu's own idle close out of this
    p.scene = "menu"
    p.tick()
    check("the menu is never put to sleep", not p.asleep and p.scene == "menu")
    p._no_gate = False
    p.scene = "anim"
    p._attract = False
    p.tick()
    check("not in attract: never asleep, whatever the sensor says", not p.asleep)

    print("the panel follows the daemon's cycle")
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "state.json"
        keep = theme.STATE_PATH
        theme.STATE_PATH = state
        try:
            p, _ = make()
            state.write_text(json.dumps({"animation": "attract", "showing": "flux"}))
            p.follow_state()
            check("animation=attract: the panel shows 'showing'",
                  p._attract and p._anim == "flux", f"{p._attract} {p._anim}")
            state.write_text(json.dumps({"animation": "attract", "showing": "voice-hud"}))
            p.follow_state()
            check("...and moves when it changes", p._anim == "voice-hud", p._anim)
            state.write_text(json.dumps({"animation": "reactor", "showing": "voice-hud"}))
            p.follow_state()
            check("a plain look leaves the mode", not p._attract and p._anim == "reactor")
        finally:
            theme.STATE_PATH = keep

    print("the daemon's cycle is a ring")
    check("attract is a preset, and a panel one",
          dc.PRESET_BY_ID.get("attract", {}).get("argv") == ["panel.py"])
    check("the ring is every panel look but attract",
          "attract" not in dc.LOOKS and "off" not in dc.LOOKS and "voice-hud" in dc.LOOKS,
          str(dc.LOOKS))
    ring = [dc.LOOKS[0]]
    for _ in dc.LOOKS:
        ring.append(dc.next_look(ring[-1]))
    check("next_look walks every look and wraps", ring[:-1] == list(dc.LOOKS) and ring[-1] == dc.LOOKS[0],
          str(ring))
    check("a non-look starts at the first", dc.next_look("off") == dc.LOOKS[0])
    check("attract's cadence is about a minute", 30 <= dc.ATTRACT_EVERY_S <= 120)

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

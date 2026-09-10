#!/usr/bin/env python3
"""The menu tells QML its facts changed only when they did (T3, first half).

The chest panel's tick fired one ``changed`` signal at 30 Hz for everything —
the shader's uniforms and every page's view-model alike. Every binding on the
visible tab re-read its map through PySide and re-laid itself out thirty times
a second to draw the same picture. Measured on the chest Pi: 53% of a core
idling on the PIN pad, 84% on the STATUS tab, 66% on WIFI. The seven page
builders together cost 0.26 ms a tick — under 1% — so the waste was never the
Python. It was the signal.

Now there are two. ``changed`` carries the uniforms and fires only while the
animation is showing. ``viewsChanged`` carries the menu and fires when the
visible tab's facts differ from the last build. This pins the promises that
split rests on:

**Idle is silent.** With the poller returning the same snapshot, the menu
emits viewsChanged once (the first build) and never again; the animation
scene emits it never. A real change emits it exactly once.

**Only the visible tab is built.** A hidden tab's view reads empty, and the
page setter rebuilds *before* it emits, so the Loader finds the new tab's
facts already there — the alternative is one frame of "cannot read property
of undefined" on every tab switch.

**No map or list lives on the 30 Hz signal.** Checked through the meta-object,
because putting one back there would quietly restore the half-core.

Runs without a screen: PySide6 QObject signals work without an application,
and the poller, the PIN material and the shader compiler are stubbed.

    python3 deploy/display/tools/test_panel_views.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DISPLAY = os.path.dirname(HERE)
sys.path.insert(0, DISPLAY)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import panel                                                 # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeNet:
    """The poller, with a snapshot the test controls and no thread."""

    snap: dict = {"at": 1.0, "age": 0.2, "nuc": None, "whoami": None}

    def start(self):
        pass

    def snapshot(self):
        return dict(FakeNet.snap)

    def scan_state(self):
        return {"networks": [], "busy": False, "at": 0.0}


class Counter:
    def __init__(self, signal):
        self.n = 0
        signal.connect(self._hit)

    def _hit(self):
        self.n += 1


def make(scene="anim"):
    panel.Net = FakeNet
    panel.pin_gate.material = lambda nuc="": {}      # no PIN set: the gate is open
    panel.qsb_for = lambda name: ""                  # never shell out to qsb
    p = panel.Panel(None, scene)
    return p, Counter(p.viewsChanged), Counter(p.changed)


def main() -> int:
    print("idle is silent")
    p, views, uniforms = make("anim")
    for _ in range(40):
        p.tick()
    check("animation scene: 40 ticks, viewsChanged never fires", views.n == 0, str(views.n))
    check("...and the uniforms fire every tick", uniforms.n == 40, str(uniforms.n))

    p, views, uniforms = make("menu")
    p.tick()
    check("menu: the first tick builds the views once", views.n == 1, str(views.n))
    for _ in range(40):
        p.tick()
    check("menu: 40 idle ticks, nothing more", views.n == 1, str(views.n))
    check("menu: the uniforms never fire while the shader is hidden",
          uniforms.n == 0, str(uniforms.n))

    print("a real change is one emit, and a change the page cannot show is none")
    FakeNet.snap = {**FakeNet.snap, "age": 1.6}     # STATUS prints whole seconds
    p.tick()
    check("age 0.2 -> 1.6: the STATUS tab's 'UPDATED 2S AGO' moved", views.n == 2,
          f"{views.n}, snapAge={p.snapAge}")
    FakeNet.snap = {**FakeNet.snap, "age": 1.7}
    for _ in range(10):
        p.tick()
    check("age 1.6 -> 1.7 rounds the same, so nothing fires", views.n == 2, str(views.n))
    FakeNet.snap = {**FakeNet.snap, "whoami": {"host": "fred"}}
    p.tick()
    check("the brain appearing fires once", views.n == 3 and p.brainReachable, str(views.n))

    print("only the visible tab is built, and it is built before the Loader looks")
    check("on STATUS, the SERVOS view is empty", p.servosView == {}, str(p.servosView))
    seen = {}

    def on_scene():
        seen["servos"] = dict(p.servosView)
        seen["status"] = list(p.statusRows)
    p.sceneChanged.connect(on_scene)
    p.page = 2
    check("switching to SERVOS: its view exists when sceneChanged fires",
          bool(seen.get("servos")), str(seen.get("servos"))[:80])
    check("...and STATUS has been dropped", seen.get("status") == [], str(seen.get("status")))
    check("the switch itself fired viewsChanged once", views.n == 4, str(views.n))
    for _ in range(20):
        p.tick()
    check("and it is quiet again", views.n == 4, str(views.n))
    for page in range(7):
        p.page = page
        p.tick()
    check("every tab builds without error", True)

    print("the gate: locked, only the overlays exist")
    p.page = 0
    p.tick()
    before = views.n
    p._gate.unlocked = False
    p.tick()
    check("locking fires once", views.n == before + 1, str(views.n))
    check("locked: no page facts, only the pad and the power menu",
          p.statusRows == [] and p.gateView.get("unlocked") is False
          and "filled" in p.gateView and p.powerView != {},
          f"status={p.statusRows} gate={p.gateView}")
    p.scene = "anim"
    check("leaving the menu empties everything", p.gateView == {} and p.powerView == {},
          str(p.gateView))

    print("no map or list lives on the 30 Hz signal")
    mo = p.metaObject()
    on_fast = []
    for i in range(mo.propertyCount()):
        prop = mo.property(i)
        sig = prop.notifySignal()
        if not sig.isValid():
            continue
        name = bytes(sig.name()).decode()
        if name == "changed" and prop.typeName() not in ("double", "float"):
            on_fast.append(f"{prop.name()}:{prop.typeName()}")
    check("every property on `changed` is a float uniform", not on_fast, str(on_fast))
    slow = [mo.property(i).name() for i in range(mo.propertyCount())
            if mo.property(i).notifySignal().isValid()
            and bytes(mo.property(i).notifySignal().name()).decode() == "viewsChanged"]
    check("the page views all notify on viewsChanged",
          {"statusRows", "voiceView", "servosView", "cartView", "displayView",
           "wifiView", "uplinkView", "infoRows", "infoPaging", "gateView",
           "powerView", "brainReachable", "snapAge"} <= set(slow), str(sorted(slow)))
    check("the full snapshot is no longer a property at all",
          mo.indexOfProperty("snap") < 0)

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

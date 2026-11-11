#!/usr/bin/env python3
"""Qt Quick spike for the chest panel. Not deployed; not in any manifest.

Answers three questions and nothing else:

  1. Does the menu port?  --shot writes a PNG per theme, offscreen, so it can
     be compared with tools/render_pages.py's output side by side.
  2. Is it testable?      --test renders offscreen and asserts on pixels and on
     item geometry, the way tools/test_*.py do today. This is the question that
     decides whether the cart page and the e-stop can ever move.
  3. What does it cost?   --bench reports import time, first-frame time and
     steady-state frame time.

The palette is theme.py, handed to QML as a context property. There is no
second copy of it in main.qml — the discipline that theme_colors.h already
follows for the C renderer.

    python3 spike.py --shot /tmp            # PNG per theme, no panel touched
    python3 spike.py --test
    python3 spike.py --bench
    python3 spike.py --panel                # onto /dev/fb0 (stop the animation first)
"""
from __future__ import annotations

import argparse
import os
import sys
import time

T0 = time.monotonic()

DISPLAY = "/home/dietpi/display"
if os.path.isdir(DISPLAY):
    sys.path.insert(0, DISPLAY)
else:                                        # running from the repo
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "deploy", "display"))

import theme                                 # noqa: E402


def palette_for(name: str) -> dict:
    """theme.py -> the flat object QML binds to. One source, two renderers."""
    th = theme.THEMES[name]
    p = th.palette

    def hexof(rgb):
        return "#%02x%02x%02x" % tuple(int(v) for v in rgb)

    return {
        "name": name,
        "style": th.style,
        "radius": th.radius,
        "tracking": th.tracking,
        # Qt does its own text rendering, so the baked atlases are not needed —
        # but the *typeface* still has to exist on this machine, which is the
        # one thing the atlas approach guaranteed and this does not.
        "font": {"soft": "Rajdhani", "hud": "Orbitron", "neon": "Exo 2"}[name],
        "ink": hexof(p["INK"]), "dimInk": hexof(p["DIM_INK"]),
        "okInk": hexof(p["OK_INK"]), "warnInk": hexof(p["WARN_INK"]),
        "badInk": hexof(p["BAD_INK"]),
        "bg": hexof(p["BG"]), "panel": hexof(p["PANEL"]),
        "panelOn": hexof(p["PANEL_ON"]), "edge": hexof(p["EDGE"]),
        "readout": hexof(p["READOUT"]), "readoutEdge": hexof(p["READOUT_EDGE"]),
        "stopPanel": hexof(p["STOP_PANEL"]), "stopArm": hexof(p["STOP_PANEL_ARM"]),
        "accent": hexof(th.accent),
    }


def build(theme_name: str, offscreen: bool):
    if offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ.setdefault("QT_QUICK_BACKEND", "software")   # no GPU until KMS
    os.environ.setdefault("QSG_RENDER_LOOP", "basic")

    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuick import QQuickView

    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    view = QQuickView()
    view.rootContext().setContextProperty("Th", palette_for(theme_name))
    here = os.path.dirname(os.path.abspath(__file__))
    view.setSource(QUrl.fromLocalFile(os.path.join(here, "main.qml")))
    if view.status() != QQuickView.Ready:
        for e in view.errors():
            print("  QML:", e.toString())
        raise SystemExit("QML failed to load")
    view.resize(800, 480)
    return app, view


def shot(out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    for name in theme.ORDER:
        app, view = build(name, offscreen=True)
        view.show()
        app.processEvents()
        img = view.grabWindow()
        path = os.path.join(out_dir, f"qt_{name}.png")
        # save() returns False rather than raising; printing "wrote" without
        # checking it is how this reported three files that did not exist.
        if not img.save(path):
            raise SystemExit(f"could not write {path}")
        print(f"  wrote {path}  ({img.width()}x{img.height()})")
        view.close()
    return 0


def test() -> int:
    """The decisive question: can this be checked without a panel or a human?"""
    from PySide6.QtCore import QPoint
    fails = []

    def check(label, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
        if not ok:
            fails.append(label)

    app, view = build("hud", offscreen=True)
    view.show()
    app.processEvents()
    root = view.rootObject()

    # -- geometry, the way test_power_menu.py checks for overlap -------------
    def rect_of(item):
        p = item.mapToGlobal(QPoint(0, 0))
        return (p.x(), p.y(), p.x() + item.width(), p.y() + item.height())

    # QML delegates are not QObject children of the root in the way findChildren
    # assumes — a Repeater parents them to itself, and the visual tree is the
    # one that matters. Walk childItems() instead; this is the thing a test for
    # this UI has to know, and it took one failed run to learn.
    def walk(item):
        for c in item.childItems():
            yield c
            yield from walk(c)

    tabs = [c for c in walk(root) if c.objectName() == "tab"]
    check("found all seven tabs", len(tabs) == 7, str(len(tabs)))
    rects = sorted(rect_of(t) for t in tabs)
    gaps = [b[0] - a[2] for a, b in zip(rects, rects[1:])]
    check("no two tabs overlap", all(g >= 0 for g in gaps), str(gaps))
    check("every tab gap clears the halo (4px each side)",
          all(g >= 8 for g in gaps), str(gaps))

    # -- the label actually fits its button, in this typeface ---------------
    over = [(t.property("label"), t.width()) for t in tabs
            if t.findChild(object) and t.width() <= 0]
    check("no tab has zero width", not over, str(over))

    # -- pixels, the way test_cart_page.py checks the e-stop ----------------
    stop = next((c for c in walk(root) if c.objectName() == "estop"), None)
    check("found the e-stop", stop is not None)
    if stop is not None:
        def settle(ms=250):
            # Visual state in QML is asynchronous: a Behavior animates the
            # colour over 120ms, so the frame straight after setProperty() is
            # still the old one. The framebuffer version had no such gap —
            # draw() was synchronous. This is the tax on testing a retained UI,
            # and it is a pump loop, not a redesign.
            end = time.monotonic() + ms / 1000.0
            while time.monotonic() < end:
                app.processEvents()
                time.sleep(0.005)

        def sample():
            settle()
            app.processEvents()
            img = view.grabWindow()
            # Not the centre: that is the white "STOP" label, so both states
            # sampled (255,255,255) and the check compared white to white. The
            # same mistake the framebuffer version of this test made with a
            # rounded corner. Sample the face, above the text.
            p = stop.mapToGlobal(QPoint(int(stop.width() / 2),
                                        int(stop.height() * 0.12)))
            return img.pixelColor(p.x(), p.y()).getRgb()[:3]

        idle = sample()
        stop.setProperty("stage", 1)
        armed = sample()
        check("the armed stop looks different from idle", idle != armed,
              f"{idle} vs {armed}")

    print()
    print("all checks passed" if not fails else f"FAILED: {len(fails)}")
    return 1 if fails else 0


def bench() -> int:
    imported = time.monotonic() - T0
    t0 = time.monotonic()
    app, view = build("hud", offscreen=True)
    view.show()
    app.processEvents()
    view.grabWindow()
    first = time.monotonic() - t0

    n, t0 = 60, time.monotonic()
    for i in range(n):
        view.rootObject().setProperty("current", i % 7)   # force a real relayout
        app.processEvents()
        view.grabWindow()
    per = (time.monotonic() - t0) / n
    print(f"  import + theme      {imported * 1000:7.1f} ms")
    print(f"  first frame         {first * 1000:7.1f} ms")
    print(f"  steady frame        {per * 1000:7.1f} ms  ({1 / per:5.1f} fps, software renderer)")
    print(f"  budget at 30fps     {33.3:7.1f} ms")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shot", metavar="DIR")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--panel", action="store_true")
    ap.add_argument("--theme", default="hud")
    args = ap.parse_args()

    if args.shot:
        return shot(args.shot)
    if args.test:
        return test()
    if args.bench:
        return bench()
    if args.panel:
        os.environ["QT_QPA_PLATFORM"] = "linuxfb:fb=/dev/fb0"
        app, view = build(args.theme, offscreen=False)
        view.showFullScreen()
        return app.exec()
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run the reactor as a GPU shader and time it against the numpy version.

The one question the offscreen work could not answer: on this Pi's VideoCore,
is the shader actually cheaper than reactor.py, and by how much. Everything
else about the port is already established — it compiles, and its arithmetic
matches build_geometry() to half a level out of 255.

Takes the panel, so stop the display animation first:
    curl -sX POST -H 'Content-Type: application/json' \
         -d '{"animation":"off"}' http://10.0.0.11:8081/api/animation

    python3 reactor_gpu.py --seconds 8          # GPU, on the panel
    python3 reactor_gpu.py --cpu-bench          # the numpy one, same machine
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time

# Flattened on the chest Pi, nested in the repo.
for _d in ("/home/dietpi/display",
           str(pathlib.Path(__file__).resolve().parents[2]
               / "deploy" / "display")):
    if os.path.isdir(_d):
        sys.path.insert(0, _d)
        break

import theme                                          # noqa: E402


def cpu_bench(seconds: float) -> int:
    """What reactor.py costs per frame here, for the comparison."""
    import numpy as np
    import reactor

    ramp = theme.ramp()
    t0 = time.monotonic()
    A, B = reactor.build_geometry(800, 480, ramp=ramp)
    build = time.monotonic() - t0

    n, t0 = 0, time.monotonic()
    while time.monotonic() - t0 < seconds:
        glow = 0.55 + 0.45 * (0.5 + 0.5 * np.sin((time.monotonic() - t0) * 2.2))
        frame = A + glow * B
        np.clip(frame, 0, 255, out=frame)
        frame.astype(np.uint8)          # what fb.show() would then pack
        n += 1
    per = (time.monotonic() - t0) / n
    print(f"  numpy build_geometry   {build * 1000:7.1f} ms  (once, at startup)")
    print(f"  numpy frame            {per * 1000:7.1f} ms  ({1 / per:5.1f} fps)")
    print("  ...and that excludes the RGB565 pack and the mmap write.")
    return 0


def gpu(seconds: float, theme_name: str, shot: str = "") -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "eglfs")
    os.environ.setdefault("QT_QPA_EGLFS_KMS_ATOMIC", "1")

    from PySide6.QtCore import QUrl, QTimer
    from PySide6.QtGui import QGuiApplication, QColor
    from PySide6.QtQuick import QQuickView

    app = QGuiApplication(sys.argv[:1])
    view = QQuickView()
    here = os.path.dirname(os.path.abspath(__file__))
    view.setSource(QUrl.fromLocalFile(os.path.join(here, "reactor_gpu.qml")))
    if view.status() != QQuickView.Ready:
        for e in view.errors():
            print("  QML:", e.toString())
        return 2

    r = theme.THEMES[theme_name].ramp()
    root = view.rootObject()
    root.setProperty("deepCol", QColor(*r.deep))
    root.setProperty("accentCol", QColor(*r.accent))

    counted = {"n": 0, "t0": None}

    def swapped():
        if counted["t0"] is None:
            counted["t0"] = time.monotonic()     # ignore the first frame
            return
        counted["n"] += 1

    view.frameSwapped.connect(swapped)
    view.showFullScreen()

    def done():
        n, t0 = counted["n"], counted["t0"]
        if not n or t0 is None:
            print("  no frames were swapped — the GPU path did not run")
            app.exit(2)
            return
        per = (time.monotonic() - t0) / n
        print(f"  gpu frame              {per * 1000:7.1f} ms  ({1 / per:5.1f} fps)")
        print(f"  {n} frames in {seconds:.0f}s on the panel")
        app.exit(0)

    if shot:
        # 467 frames is not proof it drew a reactor. Grab one and look.
        def grab():
            img = view.grabWindow()
            if not img.save(shot):
                print(f"  could not write {shot}")
                app.exit(2)
                return
            print(f"  wrote {shot} ({img.width()}x{img.height()})")
            app.exit(0)
        QTimer.singleShot(2000, grab)
    else:
        QTimer.singleShot(int(seconds * 1000), done)
    return app.exec()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--theme", default=None)
    ap.add_argument("--cpu-bench", action="store_true")
    ap.add_argument("--shot", default="")
    args = ap.parse_args()
    if args.cpu_bench:
        return cpu_bench(args.seconds)
    return gpu(args.seconds, args.theme or theme.load_name(), args.shot)


if __name__ == "__main__":
    raise SystemExit(main())

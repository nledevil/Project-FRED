#!/usr/bin/env python3
"""Run one of the panel's animations on the GPU.

The reactor, the flux capacitor and the face are all the same kind of thing: a
value computed per pixel from its distance and angle from a centre. numpy does
that on the CPU 384,000 times a frame, which measured at 77%, 100% and 100% of
a core on this Pi — two of them saturated, which is why they were not reaching
30fps. A fragment shader is what that computation is for.

    python3 gpu_anim.py reactor
    python3 gpu_anim.py reactor --copper
    python3 gpu_anim.py flux
    python3 gpu_anim.py face

**The numpy versions are still the source of truth for what these look like.**
They are not launched any more, but they are not dead code either: the shaders
are checked against them by tools/verify_shaders.py, which is the same
arrangement voice_hud.py has with voice_hud.c — a readable reference and a fast
renderer, held together by a harness.

The cog and the sensor overlay are still drawn by cog_hud and metrics_hud, into
a numpy image that is composited over the shader as a texture. They are text
and a bitmap icon rather than per-pixel maths, so the CPU was never the problem
for them, and drawing them any other way would mean a second implementation of
the panel's look. They refresh a few times a second; the shader runs at 60.

Takes the screen through DRM, so the daemon must have stopped whatever had it.
"""
from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import numpy as np                                      # noqa: E402

import cog_hud                                          # noqa: E402
import metrics_hud                                      # noqa: E402
import theme                                            # noqa: E402
from voice_state import VoiceFeed                       # noqa: E402

W, H = 800, 480

# Two layouts, as everywhere else here: shaders/ beside the source in the repo,
# and flat beside it on the chest Pi, whose manifest flattens the tree.
SHADER_DIRS = (os.path.join(_HERE, "shaders"), _HERE)

# How often the numpy overlay is redrawn. The metrics text changes when a sensor
# reading lands, a few times a second at most, and the cog never changes at all.
OVERLAY_HZ = 4.0


from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Property, Qt   # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage                 # noqa: E402
from PySide6.QtQuick import QQuickImageProvider, QQuickView               # noqa: E402


class Overlay(QQuickImageProvider):
    """Serves the numpy-drawn cog + metrics as a texture."""

    def __init__(self):
        super().__init__(QQuickImageProvider.Image)
        self._cog = cog_hud.CogHud()
        self._hud = metrics_hud.MetricsHud()
        self._buf = np.zeros((H, W, 3), np.float32)
        self._img = None
        self._stamp = -1.0
        self.refresh()

    def stale(self) -> bool:
        """Has anything the overlay draws actually changed?

        The cog never changes and the sensor readings change a few times a
        second at most, so rebuilding an 800x480 RGBA image and re-uploading
        1.5MB of texture on a timer was paying full price for a picture that is
        usually identical to the one already on the GPU.
        """
        try:
            stamp = metrics_hud.METRICS_PATH.stat().st_mtime
        except OSError:
            stamp = 0.0
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        return True

    def refresh(self):
        rgba = overlay_image(self._cog, self._hud, self._buf)
        self._rgba = rgba                      # keep the buffer alive for Qt
        self._img = QImage(rgba.data, W, H, 4 * W, QImage.Format_RGBA8888)

    def requestImage(self, _id, _size, _requested):
        return self._img

class State(QObject):
    """What the shader needs from the rest of FRED, per frame."""

    changed = Signal()

    def __init__(self, anim: str, frozen_at: float | None):
        super().__init__()
        # frozen_at is set by --grab: the clock stops so the frame is
        # reproducible and can be diffed against the numpy reference.
        self._frozen = frozen_at
        self._t = 0.0
        self._level = 0.0
        self._state = 0.0
        self._feed = VoiceFeed() if anim == "face" else None
        self._start = time.monotonic()
        # The face's evolving bits, kept here rather than in the shader because
        # they are not functions of time: the gaze drifts to a new target every
        # few seconds and the blink timer accumulates. Six floats at 30Hz costs
        # nothing; it is the 384,000 pixels they drive that had to move.
        self._gaze = [0.0, 0.0]
        self._target = [0.0, 0.0]
        self._next_gaze = 0.0
        self._next_blink = 1.5
        self._openness = 1.0
        self._glow = 1.0

    def tick(self):
        if self._frozen is not None:
            self._t = self._frozen
            self.changed.emit()
            return
        self._t = t = time.monotonic() - self._start
        if self._feed is not None:
            self._feed.poll()
            # The ring takes its colour and urgency from the voice state, so the
            # shader is told which of the four it is.
            order = {"idle": 0.0, "listening": 1.0, "thinking": 2.0,
                     "speaking": 3.0}
            self._state = order.get(self._feed.state(), 0.0)
            self._level = float(self._feed.level() or 0.0)
            # Exactly face.py's: only while speaking does the ring breathe with
            # the voice; otherwise it breathes at the state's own rate. Getting
            # this wrong is invisible in a screenshot and obvious in the room.
            name = self._feed.state()
            rate = {"idle": 1.6, "listening": 2.6,
                    "thinking": 7.0, "speaking": 1.6}.get(name, 1.6)
            self._glow = (0.7 + 0.5 * self._level if name == "speaking"
                          else 0.6 + 0.4 * (0.5 + 0.5 * math.sin(t * rate)))

            # gaze: a new target every 1.2s, eased towards. Deterministic sines
            # rather than an RNG, so the shader and the reference agree.
            if t >= self._next_gaze:
                self._target = [math.sin(t * 0.7) * 0.6,
                                math.sin(t * 0.9 + 1.0) * 0.4]
                self._next_gaze = t + 1.2
            for i in (0, 1):
                self._gaze[i] += (self._target[i] - self._gaze[i]) * 0.15

            # blink: a quick close roughly every four seconds
            self._openness = 1.0
            if t >= self._next_blink:
                bt = t - self._next_blink
                if bt < 0.18:
                    self._openness = abs(math.cos(bt / 0.18 * math.pi))
                else:
                    self._next_blink = t + 3.5 + (math.sin(t) + 1.0)
        self.changed.emit()

    @Property(float, notify=changed)
    def t(self):
        return self._t

    @Property(float, notify=changed)
    def level(self):
        return self._level

    @Property(float, notify=changed)
    def voiceState(self):
        return self._state

    @Property(float, notify=changed)
    def gazeX(self):
        return self._gaze[0]

    @Property(float, notify=changed)
    def gazeY(self):
        return self._gaze[1]

    @Property(float, notify=changed)
    def openness(self):
        return self._openness

    @Property(float, notify=changed)
    def glow(self):
        return self._glow


def qsb_path(name: str) -> str:
    """Compile <name>.frag to .qsb if it is missing or stale, and return it.

    Baked here rather than by a build step because this Pi has qsb and the
    alternative is shipping a binary artifact that has to be rebuilt on another
    machine — which is exactly the arrangement that makes voice_hud awkward.
    """
    src = next((os.path.join(d, f"{name}.frag") for d in SHADER_DIRS
                if os.path.isfile(os.path.join(d, f"{name}.frag"))), "")
    if not src:
        raise SystemExit(f"no shader for '{name}' in " + " or ".join(SHADER_DIRS))
    out = src + ".qsb"
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
        return out
    for qsb in ("/usr/lib/qt6/bin/qsb", "qsb"):
        try:
            subprocess.run([qsb, "--qt6", "-o", out, src], check=True,
                           capture_output=True)
            print(f"baked {os.path.basename(out)}", flush=True)
            return out
        except (OSError, subprocess.CalledProcessError) as exc:
            last = exc
    raise SystemExit(f"could not bake {src}: {last}")


def overlay_image(cog, hud, frame_buf):
    """The cog and sensor panel, as an RGBA image to composite over the shader.

    cog_hud and metrics_hud both *dim* what is behind them before drawing, which
    is a blend against the animation and cannot be expressed as a plain overlay.
    So they are given a black frame here and the result is used as premultiplied
    colour with alpha taken from how much was drawn — over a dark animation the
    two are indistinguishable, and it keeps one implementation of the look.
    """
    frame_buf[:] = 0.0
    hud.draw(frame_buf)
    cog.draw(frame_buf)
    rgb = np.clip(frame_buf, 0, 255).astype(np.uint8)
    alpha = rgb.max(axis=2)
    return np.dstack([rgb, alpha]).copy()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("anim", help="reactor | flux | face")
    ap.add_argument("--copper", action="store_true", help="the copper reactor")
    ap.add_argument("--talk", action="store_true",
                    help="the face's demo mouth, ignoring the microphone")
    ap.add_argument("--seconds", type=float, default=0.0, help="quit after N seconds")
    ap.add_argument("--fps-report", action="store_true", help="print frames/s on exit")
    # For tools/verify_shaders.py: render one frame at a fixed time, in a named
    # theme, and write it out. A fixed clock is what makes the comparison
    # against the numpy reference reproducible.
    ap.add_argument("--grab", metavar="PNG", default="")
    ap.add_argument("--at", type=float, default=1.0, help="freeze the clock here")
    ap.add_argument("--theme", default=None)
    ap.add_argument("--no-overlay", action="store_true",
                    help="shader only, so a grab can be diffed against numpy")
    args = ap.parse_args()

    shader = qsb_path(args.anim)

    os.environ.setdefault("QT_QPA_PLATFORM", "eglfs")
    os.environ.setdefault("QT_QPA_EGLFS_KMS_ATOMIC", "1")
    # No mouse pointer. eglfs draws one as soon as it finds a pointing device,
    # and the touchscreen presents itself as mouse0 as well as event4 — so a
    # panel nobody has ever pointed a mouse at grew an arrow in the corner the
    # moment the animations became Qt. The framebuffer renderers never had one.
    os.environ.setdefault("QT_QPA_EGLFS_HIDECURSOR", "1")
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")

    ramp = theme.ramp(args.theme)

    app = QGuiApplication(sys.argv[:1])
    overlay = Overlay()
    state = State(args.anim, args.at if args.grab else None)

    view = QQuickView()
    view.engine().addImageProvider("overlay", overlay)
    ctx = view.rootContext()
    ctx.setContextProperty("Sh", QUrl.fromLocalFile(shader))
    ctx.setContextProperty("St", state)
    ctx.setContextProperty("Deep", QColor(*ramp.deep))
    ctx.setContextProperty("Accent", QColor(*ramp.accent))
    ctx.setContextProperty("OkCol", QColor(*ramp.ok))
    ctx.setContextProperty("WarnCol", QColor(*ramp.warn))
    ctx.setContextProperty("Copper", 1.0 if args.copper else 0.0)
    ctx.setContextProperty("Talk", 1.0 if args.talk else 0.0)
    # Every one of these has to be set before setSource: QML evaluates its
    # bindings as the scene loads, and a context property added afterwards
    # is simply undefined by then.
    ctx.setContextProperty("HideOverlay", bool(args.no_overlay))
    ctx.setContextProperty("FrozenT", args.at if args.grab else -1.0)
    view.setSource(QUrl.fromLocalFile(os.path.join(_HERE, "anim.qml")))
    if view.status() != QQuickView.Ready:
        for e in view.errors():
            print("  QML:", e.toString(), file=sys.stderr)
        return 2

    frames = [0]
    view.frameSwapped.connect(lambda: frames.__setitem__(0, frames[0] + 1))
    view.setColor(Qt.black)
    view.showFullScreen()

    # No per-frame timer: QML interpolates the shader's clock itself. Pushing
    # `t` from Python 60 times a second cost about a quarter of a core and held
    # the panel to 40fps. Python is left with the voice state, which changes at
    # human speed.
    ticker = QTimer()
    ticker.timeout.connect(state.tick)
    ticker.start(33 if args.anim == "face" else 200)

    refresher = QTimer()

    def redraw_overlay():
        if not overlay.stale():
            return
        overlay.refresh()
        root = view.rootObject()
        if root is not None:
            root.setProperty("overlayGeneration", root.property("overlayGeneration") + 1)

    refresher.timeout.connect(redraw_overlay)
    refresher.start(int(1000 / OVERLAY_HZ))

    if args.grab:
        def grab():
            img = view.grabWindow()
            if not img.save(args.grab):
                print(f"could not write {args.grab}", file=sys.stderr)
                app.exit(2)
                return
            app.quit()
        QTimer.singleShot(600, grab)

    started = time.monotonic()
    if args.seconds:
        QTimer.singleShot(int(args.seconds * 1000), app.quit)
    rc = app.exec()
    # Drop the scene while the objects its bindings point at are still alive.
    # Without this, Python collects State on the way out and every binding that
    # reads it logs "cannot read property of null" — teardown noise that reads
    # exactly like a real fault in a log somebody scans later.
    view.setSource(QUrl())
    if args.fps_report:
        dur = time.monotonic() - started
        print(f"{frames[0]} frames in {dur:.1f}s = {frames[0] / max(dur, 1e-6):.1f} fps",
              file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

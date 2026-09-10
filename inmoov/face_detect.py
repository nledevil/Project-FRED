"""Face detection, behind one interface, so the detector can change without the
callers changing.

FRED shipped on Haar cascades — the 2001 Viola-Jones detector OpenCV bundles.
They find near-frontal faces and little else, which is the wrong tool for the
wide spotter in particular: its whole job is to notice people who are *not yet
facing him* — walking in from the side, a three-quarter view — exactly the poses
Haar structurally misses, and it false-fires on patterned walls besides.

YuNet (``cv2.FaceDetectorYN``) is a small CNN that ships inside the OpenCV 5.0
this robot already runs — no new dependency, ~230 KB of weights in the repo. On
this robot's own saved frames it was strictly better: it found faces on three
off-angle/partly-occluded frames Haar missed, and did not fire on two frames
where Haar had a false positive on the background.

The interface is deliberately the shape ``detectMultiScale`` already returned —
a list of ``(x, y, w, h)`` int tuples — so a caller swaps its detector and
changes nothing else about how it reads a box.

Degrades the way everything else here does: no OpenCV, or a missing/!unloadable
ONNX, and ``make("yunet")`` falls back to Haar with a printed reason rather than
leaving a consumer with no detector at all. ``available()`` is False only when
even Haar cannot load.
"""
from __future__ import annotations

from pathlib import Path

try:
    import cv2
    import numpy as np
    _CV_ERR = None
except Exception as exc:  # noqa: BLE001 - no OpenCV means no vision, handled upstream
    cv2 = None
    np = None
    _CV_ERR = exc

# Debian's python3-opencv ships the cascade here; cv2.data isn't packaged, hence
# the absolute path (the same one face_tracker has always used).
HAAR_PATH = "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"
YUNET_PATH = str(Path(__file__).resolve().parent.parent
                 / "models" / "yunet" / "face_detection_yunet_2023mar.onnx")

# Below this YuNet score a detection is more likely wall than face. 0.6 matched
# the robot's own frames — it kept every real face and dropped the background
# hits Haar would have taken. Raise it in a busy hall if false wakes appear.
YUNET_SCORE = 0.6


class _HaarDetector:
    name = "haar"

    def __init__(self, path: str | None = None):
        path = path or HAAR_PATH
        self._casc = None
        if cv2 is not None:
            c = cv2.CascadeClassifier(path)
            self._casc = None if c.empty() else c
        self.error = None if self._casc is not None else f"cascade not loaded: {path}"

    def available(self) -> bool:
        return self._casc is not None

    def detect(self, bgr, min_px: int = 24) -> list:
        """Boxes as (x, y, w, h). Equalised grayscale, as the callers always did
        — the panorama's edges are dimmer than its centre."""
        gray = cv2.equalizeHist(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
        found = self._casc.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=5,
                                            minSize=(min_px, min_px))
        return [(int(x), int(y), int(w), int(h)) for (x, y, w, h) in found]


class _YuNetDetector:
    name = "yunet"

    def __init__(self, path: str | None = None, score: float = YUNET_SCORE):
        # Resolve the path at call time, not from a def-time default, so a
        # config override or a test that repoints YUNET_PATH is honoured.
        path = path or YUNET_PATH
        self._det = None
        self._size = (0, 0)
        self.error = None
        if cv2 is None:
            self.error = f"no OpenCV ({_CV_ERR})"
            return
        if not Path(path).is_file():
            self.error = f"model missing: {path}"
            return
        try:
            # Input size is set per-frame in detect(); (320,320) is a placeholder.
            self._det = cv2.FaceDetectorYN.create(path, "", (320, 320),
                                                  score_threshold=float(score))
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"

    def available(self) -> bool:
        return self._det is not None

    def detect(self, bgr, min_px: int = 24) -> list:
        """Boxes as (x, y, w, h), same shape as Haar, filtered by min_px on the
        short side so a caller's minSize still means what it meant."""
        h, w = bgr.shape[:2]
        if (w, h) != self._size:
            self._det.setInputSize((w, h))    # must match the frame or it throws
            self._size = (w, h)
        n, faces = self._det.detect(bgr)
        if faces is None:
            return []
        out = []
        for f in faces:
            x, y, bw, bh = (int(v) for v in f[:4])
            if bw >= min_px and bh >= min_px:
                out.append((max(0, x), max(0, y), bw, bh))
        return out


def make(prefer: str = "yunet"):
    """A detector by name, falling back to Haar rather than to nothing.

    ``prefer="yunet"`` returns YuNet when its model and OpenCV are both present,
    else a Haar detector with a printed reason — a consumer always gets *a*
    detector if faces can be found at all. ``prefer="haar"`` returns Haar
    directly, for the tracker's PD loop until its gains are re-tuned against
    YuNet's tighter boxes on the bench.
    """
    if prefer == "yunet":
        d = _YuNetDetector()
        if d.available():
            return d
        print(f"[face_detect] YuNet unavailable ({d.error}); using Haar")
    return _HaarDetector()

"""Wide-angle face spotter — the Jabra PanaCast as a bearing source.

The head camera rides the head, so it only ever sees where FRED is already
looking; the moment he turns away from someone, they cease to exist. The
PanaCast sits on top of the chest touchscreen looking out at ~180°, and it does
not move, so it can answer the one question the head camera structurally cannot:
*is there anybody over there?*

This produces exactly the signal ``FaceTracker`` already accepts — a
``bearing_cb`` returning -1..+1, or ``None`` for "no opinion" — so wiring it in
is a swap, not a redesign. It replaces (and falls back to) the chest ultrasonics,
which could only say "something is nearer on the left"; two range-finders cannot
tell a person from a chair, and cannot give an angle.

**Still open-loop, for the same reason the ultrasonics are.** This camera is
bolted to the body. Turning the neck does not change what it sees, so the "error"
never shrinks and a proportional loop would walk the neck into its end stop and
sit there. The tracker's one-shot-on-change behaviour is what makes that safe, and
nothing here should tempt it into a loop. A face in the *head* camera still wins
whenever there is one — that one does move with the head, and closes properly.

**Why grab/retrieve rather than read.** The panorama arrives at ~15-22 fps and
the device ignores requests to slow down, but decoding 3840x1080 MJPG costs ~25%
of a core, and acquisition does not need 15 Hz — a person walking into the room
is a human-timescale event. ``grab()`` pulls a frame off the device without
decoding it (measured: free), so the loop drains at native rate to keep frames
fresh and only calls ``retrieve()`` when it actually intends to detect. That is
the difference between ~25% and ~7% of one core.

Degrades to silence: if OpenCV, the cascade, or the camera are missing,
``available()`` is False and ``bearing()`` returns None forever, which the
tracker already treats as "no opinion".
"""
from __future__ import annotations

import threading
import time

try:
    import cv2
    _CV2_ERR = None
except Exception as exc:                # noqa: BLE001 - no OpenCV just means no spotter
    cv2 = None
    _CV2_ERR = exc

from inmoov.face_tracker import CASCADE_PATH

# The panorama is the only mode that actually spans ~180 degrees; every other
# resolution this device offers is a 16:9 crop. If we silently got a crop, the
# bearing scale would be wrong (a face at the edge of a 90-degree crop is not at
# the edge of the world), so the real aspect is checked and reported.
PANORAMA = (3840, 1080)
_PANORAMA_MIN_ASPECT = 3.0


class WideSpotter:
    """Watches a fixed wide-angle camera and reports a bearing to the most
    prominent face, as -1..+1 (negative = image left), or None.

    Parameters
    ----------
    device : int
        V4L2 index. The PanaCast enumerates two nodes; only the first streams.
    detect_hz : float
        How often to actually decode and detect. Acquisition is a human-scale
        event; 3-5 Hz is plenty and keeps the decode cost down.
    detect_width : int
        Frames are downscaled to this width before detection. 1920 keeps faces
        big enough to find across a 180 degree span while costing ~10 ms.
    stale_after : float
        A bearing older than this is withdrawn (``bearing()`` returns None)
        rather than left standing as a stale opinion.
    """

    def __init__(self, device: int = 0, *, detect_hz: float = 4.0,
                 detect_width: int = 1920, cascade_path: str = CASCADE_PATH,
                 min_face_px: int = 24, stale_after: float = 2.0,
                 size: tuple[int, int] = PANORAMA):
        self.device = int(device)
        self.detect_hz = float(detect_hz)
        self.detect_width = int(detect_width)
        self.min_face_px = int(min_face_px)
        self.stale_after = float(stale_after)
        self.size = size
        self.last_error: str | None = None if cv2 is not None else str(_CV2_ERR)

        self._cap = None
        self._cascade = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._bearing: float | None = None
        self._bearing_at = 0.0
        self._faces = 0
        self._frame_wh: tuple[int, int] | None = None
        self._detects = 0
        self._detect_ms = 0.0

        if cv2 is not None:
            casc = cv2.CascadeClassifier(cascade_path)
            self._cascade = None if casc.empty() else casc
            if self._cascade is None:
                self.last_error = f"cascade not loaded: {cascade_path}"

    # ---- lifecycle --------------------------------------------------------
    def available(self) -> bool:
        return cv2 is not None and self._cascade is not None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if not self.available() or self.is_running():
            return False
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            self.last_error = f"cannot open /dev/video{self.device}"
            return False
        # MJPG first: the panorama is not offered as raw YUV at this size.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            self.last_error = f"/dev/video{self.device} opened but delivered no frame"
            return False
        h, w = frame.shape[:2]
        self._frame_wh = (w, h)
        if w / max(h, 1) < _PANORAMA_MIN_ASPECT:
            # Not fatal — a crop still spots faces — but the bearing then spans a
            # much narrower arc than 180 degrees, so say so rather than pretend.
            self.last_error = (f"got {w}x{h} (aspect {w/h:.2f}), not the "
                               f"panorama — bearing covers a narrower arc")
        self._cap = cap
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wide-spotter",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:           # noqa: BLE001
                pass
            self._cap = None
        with self._lock:
            self._bearing, self._faces = None, 0

    # ---- the loop ---------------------------------------------------------
    def _run(self) -> None:
        interval = 1.0 / max(self.detect_hz, 0.1)
        next_detect = 0.0
        while not self._stop.is_set():
            # Cheap: pulls the frame off the device without decoding it, which
            # is what keeps the buffer from going stale between detections.
            if not self._cap.grab():
                time.sleep(0.05)
                continue
            now = time.monotonic()
            if now < next_detect:
                continue
            next_detect = now + interval
            ok, frame = self._cap.retrieve()
            if not ok or frame is None:
                continue
            self._detect(frame)

    def _detect(self, frame) -> None:
        h, w = frame.shape[:2]
        self._frame_wh = (w, h)
        scale = self.detect_width / float(w)
        if scale < 1.0:
            frame = cv2.resize(frame, (self.detect_width, max(int(h * scale), 1)))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)   # the panorama's edges are dimmer than its centre
        t0 = time.monotonic()
        faces = self._cascade.detectMultiScale(
            gray, scaleFactor=1.2, minNeighbors=5,
            minSize=(self.min_face_px, self.min_face_px))
        took = (time.monotonic() - t0) * 1000.0

        with self._lock:
            self._detects += 1
            self._detect_ms = took
            self._faces = len(faces)
            if len(faces) == 0:
                # Do NOT write a bearing of 0.0 here — that would read as
                # "straight ahead" instead of "no opinion". Let it go stale.
                return
            # Largest face wins: nearest person, and the least likely to be a
            # false positive on a patterned wall.
            x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
            cx = x + fw / 2.0
            self._bearing = (cx / float(gray.shape[1])) * 2.0 - 1.0
            self._bearing_at = time.monotonic()

    # ---- what the tracker consumes ---------------------------------------
    def bearing(self) -> float | None:
        """-1..+1 (negative = image left), or None for "no opinion".

        None is returned when nothing has been seen recently, matching
        ``Sensors.bearing()``'s contract — the tracker treats None as "say
        nothing", which is very different from "aim straight ahead".
        """
        with self._lock:
            if self._bearing is None:
                return None
            if (time.monotonic() - self._bearing_at) > self.stale_after:
                return None
            return self._bearing

    def status(self) -> dict:
        with self._lock:
            fresh = (self._bearing is not None
                     and (time.monotonic() - self._bearing_at) <= self.stale_after)
            return {"available": self.available(), "running": self.is_running(),
                    "faces": self._faces,
                    "bearing": self._bearing if fresh else None,
                    "size": list(self._frame_wh) if self._frame_wh else None,
                    "detect_hz": self.detect_hz,
                    "detect_ms": round(self._detect_ms, 1),
                    "error": self.last_error}

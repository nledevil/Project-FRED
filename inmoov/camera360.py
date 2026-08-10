"""360° surround camera for FRED — an Insta360 X5 on a mast above the head.

The X5 rides the *cart*, not the head, so unlike the head camera it never moves
when the neck does: its frame is a fixed, full-circle view of FRED's
surroundings. Three consumers hang off it:

  * the **web panel** shows the live panorama (and a steerable dewarped view);
  * **SurroundVision** (``surround.py``) runs person/motion detection on the
    frames and turns them into bearings and per-sector occupancy;
  * the **brain** grabs dewarped snapshots so Claude can literally look in a
    direction and describe what's there.

The camera is consumed as a video source, not via Insta360's SDK: put the X5 in
**webcam mode** over USB-C and it enumerates as a standard UVC device
(``/dev/video*``) that OpenCV opens like any webcam, delivering stitched
equirectangular frames. No proprietary code, no pairing — the same path also
accepts a network URL (RTSP/RTMP) should the camera end up streaming over WiFi
instead. See ``docs/INSTA360.md`` for the bring-up checklist.

Frames are treated as **equirectangular**: the full width spans 360° of yaw and
the height spans 180° of pitch. ``view()`` reprojects any (yaw, pitch, fov)
window into an ordinary rectilinear image — that's what makes the pano usable:
raw equirect pixels are stretched everywhere except the horizon, but a dewarped
view looks like a normal photo, which is what both the Haar detectors and
Claude's vision expect.

Yaw is given in the **robot frame**: 0° = cart-forward, positive = clockwise
(to FRED's right), range -180..180. ``front_yaw`` calibrates where
cart-forward sits in the camera's own image — set it once after mounting (spin
the dewarped view until it looks straight down the cart) and every consumer
agrees on directions from then on.

With no camera attached the module runs in **mock mode**: a synthetic
equirectangular scene with labelled compass bearings and a person-sized blob
that wanders around FRED, so the dewarp, the detector plumbing, the panel and
the Claude tools can all be developed and demoed with no hardware present —
same pattern as every other hardware wrapper here.
"""
from __future__ import annotations

import math
import threading
import time

try:
    import cv2
    import numpy as np
    _CV_ERR = None
except Exception as exc:  # noqa: BLE001 - no OpenCV just means "no 360 camera"
    cv2 = None
    np = None
    _CV_ERR = exc

# Default capture geometry. The X5's webcam mode offers a few equirect sizes;
# 1920x960 is plenty for detection and panel viewing while staying light on a
# Pi 4 (an 8K stitch would swamp it). Confirm the real mode list on bring-up.
DEFAULT_SIZE = (1920, 960)
DEFAULT_FPS = 15.0

# Bounds for view(); fov outside this either wastes pixels or fish-eyes again.
FOV_MIN, FOV_MAX = 20.0, 120.0


def _wrap_deg(a: float) -> float:
    """Wrap an angle to -180..180."""
    return (float(a) + 180.0) % 360.0 - 180.0


class _FrameSlot:
    """Latest equirect frame + a condition consumers wait on (same idea as the
    head camera's broker, but ndarray-valued: JPEG encoding happens per-consumer
    because each wants a different projection)."""

    def __init__(self):
        self.frame = None          # BGR ndarray, or None before first capture
        self.seq = 0
        self.cond = threading.Condition()

    def publish(self, frame) -> None:
        with self.cond:
            self.frame = frame
            self.seq += 1
            self.cond.notify_all()

    def latest(self):
        with self.cond:
            return self.frame, self.seq

    def wait_next(self, last_seq: int, timeout: float = 5.0):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq, timeout=timeout)
            return self.frame, self.seq


class Camera360:
    """Lazily-started 360° source. Thread-safe; shared by panel/detector/brain."""

    def __init__(self, source: str = "auto", size=DEFAULT_SIZE, fps: float = DEFAULT_FPS,
                 front_yaw: float = 0.0, device: str = "/dev/video8"):
        # source: "auto" = open `device` (UVC webcam mode), "mock" = synthetic
        # scene, anything with "://" = network stream URL. "auto" quietly falls
        # back to mock when the device can't be opened, so a bench machine (or
        # the Pi before the X5 arrives) still exercises the whole path.
        self._source = str(source or "auto")
        self._device = str(device)
        self._size = (int(size[0]), int(size[1]))
        self._fps = max(1.0, float(fps))
        self.front_yaw = float(front_yaw)      # cam yaw of cart-forward, degrees
        self._slot = _FrameSlot()
        self._holds = 0                        # acquire()/release() consumers
        self._viewers = 0                      # MJPEG stream clients
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()          # guards holds/viewers/thread
        self._mode = "off"                     # off | uvc | url | mock (live value)
        self._error = ""                       # last open failure, for the panel
        # remap-map cache: dewarp maps depend only on the view params and the
        # equirect size, so each (yaw,pitch,fov,size) pair is built once. Small
        # and bounded — the panel + brain use a handful of fixed views.
        self._maps: dict[tuple, tuple] = {}
        self._maps_lock = threading.Lock()

    # ---- capability --------------------------------------------------------
    def available(self) -> bool:
        """OpenCV present. Mock keeps this True on camera-less machines: the
        rest of the stack treats mock frames as real (that's the point)."""
        return cv2 is not None

    def status(self) -> dict:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
        _f, seq = self._slot.latest()
        return {"available": self.available(), "running": running,
                "mode": self._mode, "source": self._source, "device": self._device,
                "size": list(self._size), "fps": self._fps,
                "front_yaw": self.front_yaw, "frames": seq, "error": self._error}

    # ---- lifecycle ---------------------------------------------------------
    def acquire(self) -> None:
        """Keep the capture loop running for a frame consumer (detector, brain
        snapshot). Pair with release(). Same contract as the head Camera."""
        with self._lock:
            self._holds += 1
            self._ensure_running_locked()

    def release(self) -> None:
        with self._lock:
            if self._holds > 0:
                self._holds -= 1
            self._maybe_stop_locked()

    def _ensure_running_locked(self) -> None:
        if not self.available():
            return
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="camera360",
                                            daemon=True)
            self._thread.start()

    def _maybe_stop_locked(self) -> None:
        if self._viewers == 0 and self._holds == 0:
            self._stop.set()           # loop notices and exits; thread reaped lazily

    def set_source(self, source: str | None = None, device: str | None = None) -> None:
        """Repoint at a different source/device. Takes effect the next time the
        capture loop (re)opens — an active loop keeps its current handle."""
        if source is not None:
            self._source = str(source).strip() or "auto"
        if device is not None:
            self._device = str(device).strip()

    def set_front_yaw(self, deg: float) -> None:
        """Re-zero cart-forward in the camera image (mount calibration). Live —
        the map cache is keyed on it, so old maps are simply left behind."""
        self.front_yaw = _wrap_deg(deg)
        with self._maps_lock:
            self._maps.clear()

    # ---- capture loop ------------------------------------------------------
    def _open_capture(self):
        """Try to open the configured real source. Returns a VideoCapture or
        None (mock and open-failure both end up as None)."""
        if self._source == "mock" or cv2 is None:
            return None
        target = self._source if "://" in self._source else self._device
        try:
            cap = cv2.VideoCapture(target)
            if not cap.isOpened():
                cap.release()
                self._error = f"could not open {target}"
                return None
            # UVC: ask for MJPEG at the configured geometry — raw YUY2 at
            # equirect sizes exceeds USB2 bandwidth and silently drops to 5fps.
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._size[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._size[1])
            cap.set(cv2.CAP_PROP_FPS, self._fps)
            self._error = ""
            return cap
        except Exception as exc:  # noqa: BLE001 - any failure = fall to mock
            self._error = str(exc)
            return None

    def _run(self) -> None:
        cap = self._open_capture()
        self._mode = ("mock" if cap is None else
                      ("url" if "://" in self._source else "uvc"))
        if cap is None and self._source not in ("mock", "auto"):
            # An explicitly-configured real source that won't open is an error
            # worth surfacing; "auto" degrading to mock is expected on a bench.
            print(f"[Camera360] {self._error or 'open failed'} — running mock scene")
        period = 1.0 / self._fps
        t0 = time.monotonic()
        misses = 0
        try:
            while not self._stop.is_set():
                tick = time.monotonic()
                if cap is not None:
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        misses += 1
                        if misses >= 30:           # ~2 s of nothing: reopen (unplug?)
                            cap.release()
                            cap = self._open_capture()
                            misses = 0
                        self._stop.wait(0.1)
                        continue
                    misses = 0
                    if (frame.shape[1], frame.shape[0]) != self._size:
                        frame = cv2.resize(frame, self._size)
                else:
                    frame = self._mock_frame(tick - t0)
                self._slot.publish(frame)
                dt = time.monotonic() - tick
                if dt < period:
                    self._stop.wait(period - dt)
        finally:
            if cap is not None:
                cap.release()
            self._mode = "off"
            with self._lock:
                self._thread = None

    # ---- the mock scene ----------------------------------------------------
    def _mock_frame(self, t: float):
        """A synthetic equirect: sky/floor bands, compass labels at the four
        cardinal robot yaws, and a person-sized blob orbiting FRED at ~20°/s.
        Enough structure for the dewarp, the panel, motion detection and the
        sector map to all be exercised for real."""
        w, h = self._size
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[: h // 2] = (60, 40, 20)           # "sky" (BGR: dark blue-ish)
        img[h // 2:] = (30, 60, 90)            # "floor"
        cv2.line(img, (0, h // 2), (w, h // 2), (160, 160, 160), 2)
        for robot_yaw, label in ((0, "FWD"), (90, "RIGHT"), (180, "BACK"), (-90, "LEFT")):
            x = int(((_wrap_deg(robot_yaw + self.front_yaw)) / 360.0 + 0.5) * w) % w
            cv2.line(img, (x, 0), (x, h), (90, 90, 90), 1)
            cv2.putText(img, label, (max(4, x - 40), h // 2 - 18),
                        cv2.FONT_HERSHEY_SIMPLEX, h / 700.0, (240, 240, 240), 2)
        # The wandering "person": orbit yaw at 20°/s with a little sway.
        yaw = _wrap_deg(20.0 * t)
        px = int(((_wrap_deg(yaw + self.front_yaw)) / 360.0 + 0.5) * w) % w
        ph = int(h * 0.30)                     # apparent height ~ a few metres away
        top = h // 2 - int(ph * 0.85)          # head just above the horizon
        cv2.ellipse(img, (px, top + ph // 2), (ph // 6, ph // 2), 0, 0, 360,
                    (40, 40, 40), -1)          # body
        cv2.circle(img, (px, top), ph // 8, (140, 160, 200), -1)   # head
        cv2.putText(img, f"mock person @ {yaw:+.0f} deg", (10, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
        return img

    # ---- frame access ------------------------------------------------------
    def frame(self):
        """Latest equirect BGR frame (no copy — treat as read-only), or None."""
        f, _seq = self._slot.latest()
        return f

    def wait_frame(self, last_seq: int, timeout: float = 5.0):
        """Block until a frame newer than ``last_seq`` lands. -> (frame, seq)."""
        return self._slot.wait_next(last_seq, timeout)

    def snapshot(self, timeout: float = 3.0):
        """One fresh equirect frame, starting the camera if idle. May block up
        to ``timeout`` for the source to open and deliver. None on failure."""
        self.acquire()
        try:
            deadline = time.monotonic() + timeout
            f, seq = self._slot.latest()
            while f is None and time.monotonic() < deadline:
                f, seq = self._slot.wait_next(seq, timeout=0.25)
            return None if f is None else f.copy()
        finally:
            self.release()

    # ---- projection --------------------------------------------------------
    def _view_maps(self, eq_w: int, eq_h: int, yaw: float, pitch: float,
                   fov: float, out_w: int, out_h: int):
        """cv2.remap maps projecting an equirect onto a virtual pinhole camera
        aimed at (yaw, pitch) robot-frame with horizontal ``fov``. Cached."""
        key = (eq_w, eq_h, round(yaw, 1), round(pitch, 1), round(fov, 1),
               out_w, out_h, round(self.front_yaw, 1))
        with self._maps_lock:
            hit = self._maps.get(key)
        if hit is not None:
            return hit
        fov_r = math.radians(min(FOV_MAX, max(FOV_MIN, fov)))
        fpx = (out_w / 2.0) / math.tan(fov_r / 2.0)
        xs = (np.arange(out_w, dtype=np.float32) - out_w / 2.0 + 0.5) / fpx
        ys = (np.arange(out_h, dtype=np.float32) - out_h / 2.0 + 0.5) / fpx
        u, vd = np.meshgrid(xs, ys)            # u right, vd down (image coords)
        v = -vd                                # up-positive for the math
        norm = np.sqrt(u * u + v * v + 1.0)
        u, v, wf = u / norm, v / norm, 1.0 / norm
        # Pitch the camera up by ``pitch`` (about the right axis)...
        th = math.radians(pitch)
        v2 = v * math.cos(th) + wf * math.sin(th)
        w2 = -v * math.sin(th) + wf * math.cos(th)
        # ...then yaw it right by ``yaw`` (about the vertical axis).
        ps = math.radians(yaw)
        u3 = u * math.cos(ps) + w2 * math.sin(ps)
        w3 = -u * math.sin(ps) + w2 * math.cos(ps)
        ray_yaw = np.degrees(np.arctan2(u3, w3))          # robot frame
        ray_pitch = np.degrees(np.arcsin(np.clip(v2, -1.0, 1.0)))
        map_x = (((ray_yaw + self.front_yaw) / 360.0 + 0.5) % 1.0) * eq_w
        map_y = (0.5 - ray_pitch / 180.0) * eq_h
        maps = (map_x.astype(np.float32),
                np.clip(map_y, 0, eq_h - 1).astype(np.float32))
        with self._maps_lock:
            if len(self._maps) > 32:          # bounded: stale front_yaw keys etc.
                self._maps.clear()
            self._maps[key] = maps
        return maps

    def view(self, yaw: float = 0.0, pitch: float = 0.0, fov: float = 90.0,
             out_size=(640, 480), frame=None):
        """Dewarped rectilinear view toward robot-frame (yaw, pitch). Uses the
        latest frame unless one is passed in (the detector passes its own so a
        burst of tile views all come from the same instant). None if no frame."""
        if frame is None:
            frame = self.frame()
        if frame is None or cv2 is None:
            return None
        eq_h, eq_w = frame.shape[:2]
        out_w, out_h = int(out_size[0]), int(out_size[1])
        map_x, map_y = self._view_maps(eq_w, eq_h, _wrap_deg(yaw), float(pitch),
                                       float(fov), out_w, out_h)
        # WRAP so a view straddling the seam (yaw ±180 in camera space) is
        # continuous instead of showing a hard edge.
        return cv2.remap(frame, map_x, map_y, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_WRAP)

    # ---- JPEG products -----------------------------------------------------
    @staticmethod
    def _jpeg(img, quality: int = 80):
        if img is None or cv2 is None:
            return None
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return bytes(buf) if ok else None

    def jpeg_pano(self, max_width: int = 1280, fresh: bool = False):
        """The full equirect as JPEG (downscaled for the panel), or None."""
        f = self.snapshot() if fresh else self.frame()
        if f is None:
            f = self.snapshot()
        if f is None:
            return None
        if f.shape[1] > max_width:
            scale = max_width / f.shape[1]
            f = cv2.resize(f, (max_width, max(1, int(f.shape[0] * scale))))
        return self._jpeg(f)

    def jpeg_view(self, yaw: float = 0.0, pitch: float = 0.0, fov: float = 90.0,
                  out_size=(640, 480), quality: int = 80):
        """A dewarped view as JPEG (grabs a fresh frame if none is flowing)."""
        f = self.frame()
        if f is None:
            f = self.snapshot()
        return self._jpeg(self.view(yaw, pitch, fov, out_size, frame=f), quality)

    # ---- MJPEG streaming ---------------------------------------------------
    def frames_pano(self, max_width: int = 1280):
        """Yield pano JPEGs until the client disconnects (panel stream)."""
        yield from self._stream(lambda f: self._shrink_jpeg(f, max_width))

    def frames_view(self, yaw: float = 0.0, pitch: float = 0.0, fov: float = 90.0,
                    out_size=(640, 480)):
        """Yield dewarped-view JPEGs (the panel's steerable window)."""
        yield from self._stream(
            lambda f: self._jpeg(self.view(yaw, pitch, fov, out_size, frame=f)))

    def _shrink_jpeg(self, f, max_width: int):
        if f.shape[1] > max_width:
            scale = max_width / f.shape[1]
            f = cv2.resize(f, (max_width, max(1, int(f.shape[0] * scale))))
        return self._jpeg(f)

    def _stream(self, encode):
        if not self.available():
            raise RuntimeError("no 360 camera available (OpenCV missing)")
        with self._lock:
            self._viewers += 1
            self._ensure_running_locked()
        seq = 0
        try:
            while True:
                frame, seq = self._slot.wait_next(seq)
                if frame is None:
                    continue                    # source still opening — keep-alive
                jpg = encode(frame)
                if jpg:
                    yield jpg
        finally:
            with self._lock:
                self._viewers -= 1
                self._maybe_stop_locked()

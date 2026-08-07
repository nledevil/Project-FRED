"""Live MJPEG streaming for the InMoov head camera.

Wraps a camera behind a tiny interface the web app uses to serve a
multipart/x-mixed-replace stream. The camera is started lazily on the first
viewer and released when the last viewer disconnects, so it draws no power or
CPU while nobody is watching. Degrades gracefully (``available()`` -> False) on
machines with no camera, so the rest of the app still runs.

Frames flow through a persistent broker (frame + condition) that outlives any
single capture session, so focus/flip changes can stop and restart the source
*underneath* active viewers without breaking their streams.

Three interchangeable backends sit behind that interface, because the brain
moved off the head Pi and the camera did not follow it:

``picamera2``
    The original: a Pi Camera Module 3 (imx708) on the local CSI bus, using the
    Pi's hardware MJPEG encoder. Only works on a Pi.
``mjpeg``
    Consume another machine's MJPEG-over-HTTP stream — specifically the head
    Pi's ``deploy/camera_stream.py``. This is how the NUC sees the head camera
    now that the head is a device server. JPEG bytes are passed through to
    viewers untouched (no decode/re-encode), so a browser watching the panel
    costs almost nothing; frames are only decoded when the face tracker asks
    for grayscale.
``v4l2``
    A local USB/UVC camera via OpenCV — the Jabra PanaCast on the NUC. Frames
    arrive decoded, so this one encodes JPEG for viewers.

The 180 flip (rotate_180, both axes) defaults OFF; toggle it from the UI if the
camera is mounted inverted and the image comes in upside-down. Focus control is
a picamera2-only feature — ``settings()`` reports ``focus_supported`` so the UI
can hide the control rather than offer a button that does nothing.
"""
from __future__ import annotations

import io
import os
import socket
import threading
import time
from urllib.parse import urlparse

try:
    from picamera2 import Picamera2
    from picamera2.encoders import MJPEGEncoder
    from picamera2.outputs import FileOutput
    from libcamera import Transform
    _IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001 - any import failure just means "no picamera2 here"
    Picamera2 = None
    _IMPORT_ERROR = exc

try:
    import cv2
    import numpy as np
except Exception:  # noqa: BLE001 - no OpenCV: the mjpeg/v4l2 backends are unavailable
    cv2 = None
    np = None

# AfMode / AfTrigger values (libcamera): match the enum ints so we needn't import them.
AF_MANUAL = 0
AF_AUTO = 1
AF_CONTINUOUS = 2
AF_TRIGGER_START = 0
LENS_MIN = 0.0     # dioptres: 0.0 = focus at infinity
LENS_MAX = 15.0    # fallback only; the real max is read from the sensor on start
                   # (mode-dependent: ~15 in video, ~32 in still — higher = nearer)

# How long a backend thread waits before retrying a dead source. The head Pi can
# reboot underneath us; reconnecting quietly beats reporting a permanently dead
# camera, so a restart on either end heals itself without touching the panel.
_RECONNECT_DELAY = 2.0


class _FrameBroker:
    """Latest JPEG frame + a condition viewers wait on. Outlives the source."""

    def __init__(self):
        self.frame: bytes | None = None
        self.seq = 0
        self.cond = threading.Condition()

    def publish(self, buf) -> None:
        with self.cond:
            self.frame = bytes(buf)
            self.seq += 1
            self.cond.notify_all()


class _Output(io.BufferedIOBase):
    """File-like sink the MJPEG encoder writes each finished frame to."""

    def __init__(self, broker: _FrameBroker):
        self._broker = broker

    def writable(self) -> bool:
        return True

    def write(self, buf) -> int:
        self._broker.publish(buf)
        return len(buf)


def _to_gray(bgr, lores_size, rotate_180: bool):
    """BGR frame -> small grayscale ndarray for the face tracker.

    Downscaled to ``lores_size`` to match what the picamera2 backend hands over,
    which keeps the cascade's cost the same whichever camera is feeding it.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if rotate_180:
        gray = cv2.rotate(gray, cv2.ROTATE_180)
    w, h = lores_size
    if gray.shape[1] != w or gray.shape[0] != h:
        gray = cv2.resize(gray, (w, h), interpolation=cv2.INTER_AREA)
    return gray


class _Picamera2Backend:
    """Pi Camera Module via picamera2 + the Pi's hardware MJPEG encoder."""

    name = "picamera2"
    focus_supported = True

    def __init__(self, size, lores_size):
        self._size = size
        self._lores_size = lores_size
        self._picam = None
        self.lens_min = LENS_MIN
        self.lens_max = LENS_MAX

    @staticmethod
    def detect() -> bool:
        if Picamera2 is None:
            return False
        try:
            return len(Picamera2.global_camera_info()) > 0
        except Exception:  # noqa: BLE001
            return False

    def start(self, broker: _FrameBroker, rotate_180: bool,
              af_mode: int, lens_position: float) -> float:
        """Start the sensor. Returns the (possibly re-clamped) lens position."""
        self._picam = Picamera2()
        transform = Transform(hflip=1, vflip=1) if rotate_180 else Transform()
        cfg = self._picam.create_video_configuration(
            main={"size": self._size},
            lores={"size": self._lores_size, "format": "YUV420"},
            transform=transform)
        self._picam.configure(cfg)
        # The lens range is only known once configured, and it's mode-dependent, so
        # read the real limits and re-clamp our position to them.
        lp = self._picam.camera_controls.get("LensPosition")
        if lp and lp[0] is not None and lp[1] is not None:
            self.lens_min, self.lens_max = float(lp[0]), float(lp[1])
            lens_position = max(self.lens_min, min(self.lens_max, lens_position))
        self._picam.start_recording(MJPEGEncoder(), FileOutput(_Output(broker)))
        self.apply_focus(af_mode, lens_position)
        return lens_position

    def stop(self) -> None:
        try:
            self._picam.stop_recording()
        finally:
            self._picam.close()
            self._picam = None

    def running(self) -> bool:
        return self._picam is not None

    def capture_gray(self):
        """The lores stream's YUV420 Y-plane is luma = grayscale. Cheap: it
        doesn't disturb the MJPEG encoder on the main stream."""
        picam = self._picam
        if picam is None:
            return None
        try:
            yuv = picam.capture_array("lores")     # shape (h*3/2, w), uint8
        except Exception:      # noqa: BLE001 - sensor may be mid-restart; caller retries
            return None
        w, h = self._lores_size
        return yuv[:h, :w]                          # top h rows = Y (luma) plane

    def apply_focus(self, af_mode: int, lens_position: float) -> None:
        if self._picam is None:
            return
        ctrls = {"AfMode": af_mode}
        if af_mode == AF_MANUAL:
            ctrls["LensPosition"] = lens_position
        self._picam.set_controls(ctrls)

    def autofocus(self) -> None:
        if self._picam is not None:
            self._picam.set_controls({"AfMode": AF_AUTO, "AfTrigger": AF_TRIGGER_START})


class _ThreadedBackend:
    """Shared machinery for backends that pull frames on a worker thread.

    Both the network and USB sources are pull-based and can die independently of
    us, so both want the same loop: run until told to stop, publish JPEG to the
    broker, keep the newest frame for the tracker, and reconnect on failure.
    """

    focus_supported = False

    def __init__(self, lores_size):
        self._lores_size = lores_size
        self._thread = None
        self._stop = threading.Event()
        self._broker = None
        self._rotate_180 = False
        self._gray = None            # newest grayscale frame, for the tracker
        self._gray_lock = threading.Lock()
        self.last_error = None       # surfaced in settings() so the UI can explain silence

    def start(self, broker: _FrameBroker, rotate_180: bool,
              af_mode: int, lens_position: float) -> float:
        self._broker = broker
        self._rotate_180 = rotate_180
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"cam-{self.name}",
                                        daemon=True)
        self._thread.start()
        return lens_position

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=3.0)
        with self._gray_lock:
            self._gray = None

    def running(self) -> bool:
        return self._thread is not None

    def capture_gray(self):
        with self._gray_lock:
            return self._gray

    def _publish_gray(self, gray) -> None:
        with self._gray_lock:
            self._gray = gray

    def apply_focus(self, af_mode: int, lens_position: float) -> None:
        return          # no focus control on these sources

    def autofocus(self) -> None:
        return

    def _run(self) -> None:
        raise NotImplementedError


class _MjpegBackend(_ThreadedBackend):
    """Consume an MJPEG-over-HTTP stream from another machine (the head Pi).

    JPEG bytes go to viewers untouched — no decode/re-encode — so serving the
    panel costs nothing beyond the relay. Frames are decoded to grayscale only
    when the face tracker actually asks, and at most once per frame.
    """

    name = "mjpeg"

    def __init__(self, url: str, lores_size, timeout: float = 5.0):
        super().__init__(lores_size)
        self.url = url
        self._timeout = timeout

    @staticmethod
    def detect(url: str, timeout: float = 1.0) -> bool:
        """A quick TCP probe. Deliberately NOT used to gate availability: the
        head Pi may be rebooting while the NUC boots, and a camera that is
        merely asleep must not be reported as absent forever."""
        try:
            p = urlparse(url)
            with socket.create_connection((p.hostname, p.port or 80), timeout):
                return True
        except Exception:  # noqa: BLE001
            return False

    def _run(self) -> None:
        import urllib.request
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(self.url, headers={"User-Agent": "inmoov"})
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    self.last_error = None
                    self._pump(resp)
            except Exception as exc:  # noqa: BLE001 - source down: report, wait, retry
                self.last_error = f"{type(exc).__name__}: {exc}"
            if self._stop.wait(_RECONNECT_DELAY):
                break

    def _pump(self, resp) -> None:
        """Scan the multipart body for JPEG frames.

        Frames are found by SOI/EOI markers rather than by parsing multipart
        boundaries: it is shorter, and it does not care what boundary string or
        part headers the far end chose.
        """
        buf = b""
        while not self._stop.is_set():
            chunk = resp.read(8192)
            if not chunk:
                return                       # stream ended; _run reconnects
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")            # JPEG SOI
                if start < 0:
                    if len(buf) > 1 << 20:               # no SOI in 1 MB: junk, drop it
                        buf = b""
                    break
                end = buf.find(b"\xff\xd9", start + 2)   # JPEG EOI
                if end < 0:
                    if start:
                        buf = buf[start:]                # discard preamble, keep partial
                    break
                jpeg = buf[start:end + 2]
                buf = buf[end + 2:]
                self._broker.publish(jpeg)
                self._decode_gray(jpeg)

    def _decode_gray(self, jpeg: bytes) -> None:
        if cv2 is None:
            return
        try:
            bgr = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if bgr is not None:
                self._publish_gray(_to_gray(bgr, self._lores_size, self._rotate_180))
        except Exception:  # noqa: BLE001 - a torn frame must not kill the pump
            pass


class _V4L2Backend(_ThreadedBackend):
    """A local USB/UVC camera through OpenCV (the Jabra PanaCast on the NUC).

    Frames arrive decoded, so this backend owns the JPEG encode that the
    picamera2 and mjpeg paths get for free.
    """

    name = "v4l2"

    def __init__(self, device, size, lores_size, fps: int = 15, quality: int = 80):
        super().__init__(lores_size)
        self.device = device
        self._size = size
        self._fps = fps
        self._quality = int(quality)

    @staticmethod
    def detect(device) -> bool:
        if cv2 is None:
            return False
        if isinstance(device, str) and device.startswith("/dev/"):
            return os.path.exists(device)
        return os.path.exists(f"/dev/video{int(device)}")

    def _open(self):
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise RuntimeError(f"cannot open {self.device!r}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._size[1])
        cap.set(cv2.CAP_PROP_FPS, self._fps)
        # A 1-frame buffer keeps the tracker looking at *now* rather than at a
        # queue of stale frames — latency matters more than smoothness here.
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:  # noqa: BLE001 - not all drivers expose it
            pass
        return cap

    def _run(self) -> None:
        params = [int(cv2.IMWRITE_JPEG_QUALITY), self._quality]
        while not self._stop.is_set():
            cap = None
            try:
                cap = self._open()
                self.last_error = None
                while not self._stop.is_set():
                    ok, bgr = cap.read()
                    if not ok:
                        raise RuntimeError("frame grab failed")
                    if self._rotate_180:
                        bgr = cv2.rotate(bgr, cv2.ROTATE_180)
                    ok, enc = cv2.imencode(".jpg", bgr, params)
                    if ok:
                        self._broker.publish(enc.tobytes())
                    # Rotation is already applied above, so don't rotate twice.
                    self._publish_gray(_to_gray(bgr, self._lores_size, False))
            except Exception as exc:  # noqa: BLE001 - camera unplugged: report, retry
                self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                if cap is not None:
                    cap.release()
            if self._stop.wait(_RECONNECT_DELAY):
                break


def _build_backend(backend: str, source, size, lores_size):
    """Pick a backend. ``backend='auto'`` prefers a real local sensor, then a
    remote stream, then a USB camera — the order a machine is most likely to
    want: the Pi keeps its CSI camera, the NUC falls back to whatever it is
    given. Returns (backend, resolved_name) or (None, reason)."""
    if backend == "auto":
        if _Picamera2Backend.detect():
            backend = "picamera2"
        elif isinstance(source, str) and source.startswith(("http://", "https://")):
            backend = "mjpeg"
        elif _V4L2Backend.detect(source if source not in (None, "") else 0):
            backend = "v4l2"
        else:
            return None, "no camera found (no picamera2, no stream URL, no /dev/video*)"

    if backend == "picamera2":
        if not _Picamera2Backend.detect():
            return None, f"picamera2 unavailable ({_IMPORT_ERROR or 'no camera detected'})"
        return _Picamera2Backend(size, lores_size), "picamera2"
    if backend == "mjpeg":
        if cv2 is None:
            return None, "mjpeg backend needs OpenCV (opencv-python-headless)"
        if not isinstance(source, str) or not source.startswith(("http://", "https://")):
            return None, f"mjpeg backend needs a stream URL, got {source!r}"
        return _MjpegBackend(source, lores_size), "mjpeg"
    if backend == "v4l2":
        if cv2 is None:
            return None, "v4l2 backend needs OpenCV (opencv-python-headless)"
        dev = source if source not in (None, "") else 0
        if isinstance(dev, str) and dev.isdigit():
            dev = int(dev)
        if not _V4L2Backend.detect(dev):
            return None, f"no such video device: {dev!r}"
        return _V4L2Backend(dev, size, lores_size), "v4l2"
    return None, f"unknown camera backend {backend!r}"


class Camera:
    """Lazily-started MJPEG source. Thread-safe; shared by all stream clients.

    Parameters
    ----------
    backend : str
        ``"auto"`` (default), ``"picamera2"``, ``"mjpeg"`` or ``"v4l2"``.
    source : str | int | None
        Stream URL for ``mjpeg``; device index or ``/dev/videoN`` for ``v4l2``.
        Ignored by ``picamera2``.
    """

    def __init__(self, size=(640, 480), rotate_180: bool = False, indicator=None,
                 af_mode: int = AF_MANUAL, lens_position: float = 2.0,
                 lores_size=(320, 240), backend: str = "auto", source=None):
        self._size = size
        self._lores_size = lores_size       # small frame for the face tracker
        self._rotate_180 = rotate_180
        # Optional status LED (inmoov.led.Led): lit while the camera is streaming.
        # Driven on the first-viewer / last-viewer transitions, NOT on the sensor
        # start/stop, so a flip/focus restart doesn't blink it. None = no LED.
        self._indicator = indicator
        # Starting focus (overridable from persisted settings). Continuous AF hunts
        # and fails to lock on close/low-contrast scenes on this rig, so the default
        # is reliable manual focus. The real lens range is read from the sensor on
        # start (it's mode-dependent), these are just fallbacks.
        self._af_mode = af_mode if af_mode in (AF_MANUAL, AF_AUTO, AF_CONTINUOUS) else AF_MANUAL
        self._lens_position = float(lens_position)   # dioptres (~0.5 m); clamped to real range
        self._broker = _FrameBroker()       # persistent across restarts
        self._viewers = 0                   # MJPEG stream clients
        self._holds = 0                     # non-streaming consumers (e.g. face tracker)
        # Suspended = the sensor has been handed off to another owner (e.g.
        # MyRobotLab). While suspended available() is False, the sensor is
        # force-stopped, and nothing restarts it until resume().
        self._suspended = False
        self._lock = threading.Lock()       # guards backend/_viewers/_holds/settings

        self._backend, reason = _build_backend(backend, source, size, lores_size)
        self.backend_name = self._backend.name if self._backend else None
        self.backend_error = None if self._backend else reason
        self._source = source
        # A remote stream that is merely asleep must not read as "no camera" for
        # the rest of the process's life, so mjpeg counts as detected whenever it
        # is configured; start() will reconnect when the far end comes back.
        self.detected = self._backend is not None
        if self._backend is None:
            print(f"[Camera] disabled — {reason}")
        else:
            print(f"[Camera] backend={self.backend_name}"
                  + (f" source={source!r}" if source not in (None, "") else ""))

    def available(self) -> bool:
        return self.detected and not self._suspended

    def is_streaming(self) -> bool:
        """True while the source is running (a viewer or a hold)."""
        with self._lock:
            return self._backend is not None and self._backend.running()

    # ---- non-streaming consumers (face tracker) ---------------------------
    def acquire(self) -> None:
        """Keep the source running for a consumer that reads frames directly via
        ``capture_gray()`` (rather than the MJPEG stream). Pair with release().
        Does not light the status LED — that tracks MJPEG viewers only."""
        with self._lock:
            self._holds += 1
            if self._can_start_locked():
                self._start_locked()

    def release(self) -> None:
        """Drop a hold taken by acquire(); stops the source if nobody's left."""
        with self._lock:
            if self._holds > 0:
                self._holds -= 1
            if self._viewers == 0 and self._holds == 0 and self._running_locked():
                self._stop_locked()

    # ---- hardware handoff -------------------------------------------------
    def is_suspended(self) -> bool:
        return self._suspended

    def suspend(self) -> None:
        """Release the camera so another process (e.g. MyRobotLab) can use it.
        Force-stops it even if viewers/holds remain, and reports unavailable()
        until resume(); active MJPEG streams simply stop receiving frames.
        Idempotent."""
        with self._lock:
            if self._suspended:
                return
            self._suspended = True
            if self._running_locked():
                self._stop_locked()
            if self._indicator is not None:
                self._indicator.notify_camera(False)

    def resume(self) -> None:
        """Take the camera back. Doesn't restart it — the next viewer/hold does,
        lazily. Idempotent."""
        with self._lock:
            self._suspended = False

    def capture_gray(self):
        """Grab the current frame as a grayscale ndarray, or None if not running
        / no frame yet. Cheap on every backend: picamera2 reads the lores YUV
        luma plane directly, and the threaded backends hand back a frame that
        was decoded once as it arrived."""
        with self._lock:
            backend = self._backend
        if backend is None:
            return None
        return backend.capture_gray()

    def settings(self) -> dict:
        lens_min = getattr(self._backend, "lens_min", LENS_MIN)
        lens_max = getattr(self._backend, "lens_max", LENS_MAX)
        out = {"flip": self._rotate_180, "af_mode": self._af_mode,
               "lens_position": round(self._lens_position, 2),
               "lens_min": round(lens_min, 2), "lens_max": round(lens_max, 2),
               "backend": self.backend_name,
               "focus_supported": bool(getattr(self._backend, "focus_supported", False))}
        if self._source not in (None, ""):
            out["source"] = self._source
        err = self.backend_error or getattr(self._backend, "last_error", None)
        if err:
            out["error"] = err
        return out

    # ---- lifecycle (callers must hold self._lock) -------------------------
    def _running_locked(self) -> bool:
        return self._backend is not None and self._backend.running()

    def _can_start_locked(self) -> bool:
        return (self._backend is not None and not self._backend.running()
                and not self._suspended)

    def _start_locked(self) -> None:
        self._lens_position = self._backend.start(
            self._broker, self._rotate_180, self._af_mode, self._lens_position)

    def _stop_locked(self) -> None:
        self._backend.stop()

    # ---- runtime adjustments ----------------------------------------------
    def set_rotation(self, flip: bool) -> dict:
        """Flip the image 180 (both axes) or not. Restarts the source underneath
        any active viewers, who keep streaming via the broker."""
        with self._lock:
            if bool(flip) != self._rotate_180:
                self._rotate_180 = bool(flip)
                if self._running_locked():
                    self._stop_locked()
                    self._start_locked()
        return self.settings()

    def set_focus(self, mode: int | None = None,
                  lens_position: float | None = None) -> dict:
        """Set autofocus mode (AF_MANUAL / AF_CONTINUOUS) and/or manual lens
        position in dioptres. Applied live if running, remembered otherwise.
        A no-op on backends without focus control (see ``focus_supported``)."""
        with self._lock:
            if mode is not None:
                m = int(mode)
                self._af_mode = m if m in (AF_MANUAL, AF_AUTO, AF_CONTINUOUS) else AF_CONTINUOUS
            if lens_position is not None:
                lens_min = getattr(self._backend, "lens_min", LENS_MIN)
                lens_max = getattr(self._backend, "lens_max", LENS_MAX)
                self._lens_position = max(lens_min, min(lens_max, float(lens_position)))
            if self._backend is not None:
                self._backend.apply_focus(self._af_mode, self._lens_position)
        return self.settings()

    def autofocus(self) -> dict:
        """Run a single autofocus scan and hold wherever it lands (Auto + trigger).

        Useful when continuous AF would keep hunting — one press, one attempt.
        A no-op on backends without focus control.
        """
        with self._lock:
            if getattr(self._backend, "focus_supported", False):
                self._af_mode = AF_AUTO
                self._backend.autofocus()
        return self.settings()

    # ---- streaming --------------------------------------------------------
    def frames(self):
        """Yield JPEG frames until the client disconnects. Starts the camera for
        the first viewer and stops it when the last one leaves."""
        if not self.available():
            raise RuntimeError("no camera available")
        with self._lock:
            self._viewers += 1
            if self._can_start_locked():              # tracker may already hold it open
                self._start_locked()
            if self._viewers == 1 and self._indicator is not None:
                self._indicator.notify_camera(True)   # first viewer -> LED on (if enabled)
        broker = self._broker
        last = 0
        try:
            while True:
                with broker.cond:
                    if not broker.cond.wait_for(lambda: broker.seq != last, timeout=5.0):
                        continue                      # keep-alive across a restart gap
                    last = broker.seq
                    frame = broker.frame
                if frame:
                    yield frame
        finally:
            with self._lock:
                self._viewers -= 1
                if self._viewers == 0:
                    if self._indicator is not None:
                        self._indicator.notify_camera(False)  # last viewer gone -> LED off (if enabled)
                    if self._holds == 0 and self._running_locked():
                        self._stop_locked()               # nobody left (no viewers, no holds)

    def snapshot(self, timeout: float = 5.0) -> bytes | None:
        """Grab a single JPEG frame (starts/stops the camera if idle).

        The threaded backends need a moment to connect and deliver their first
        frame, so unlike the picamera2 path this waits rather than assuming a
        frame is already there.
        """
        if not self.available():
            return None
        self.acquire()
        try:
            broker = self._broker
            deadline = time.monotonic() + timeout
            with broker.cond:
                if broker.frame is None:
                    broker.cond.wait_for(lambda: broker.frame is not None,
                                         timeout=max(0.0, deadline - time.monotonic()))
                return broker.frame
        finally:
            self.release()

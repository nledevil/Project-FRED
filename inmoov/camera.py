"""Live MJPEG streaming for the InMoov head camera (Raspberry Pi Camera Module 3).

Wraps picamera2 + the Pi's hardware MJPEG encoder behind a tiny interface the web
app uses to serve a multipart/x-mixed-replace stream. The camera is started lazily
on the first viewer and released when the last viewer disconnects, so it draws no
power or CPU while nobody is watching. Degrades gracefully (``available()`` -> False)
on machines with no picamera2 or no camera, so the rest of the app still runs.

Frames flow through a persistent broker (frame + condition) that outlives any single
picamera2 instance, so focus/flip changes can stop and restart the sensor *underneath*
active viewers without breaking their streams.

The 180 flip (rotate_180, both axes) defaults OFF; toggle it from the UI if the
camera is mounted inverted and the image comes in upside-down.
"""
from __future__ import annotations

import io
import threading

try:
    from picamera2 import Picamera2
    from picamera2.encoders import MJPEGEncoder
    from picamera2.outputs import FileOutput
    from libcamera import Transform
    _IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001 - any import failure just means "no camera here"
    Picamera2 = None
    _IMPORT_ERROR = exc

# AfMode / AfTrigger values (libcamera): match the enum ints so we needn't import them.
AF_MANUAL = 0
AF_AUTO = 1
AF_CONTINUOUS = 2
AF_TRIGGER_START = 0
LENS_MIN = 0.0     # dioptres: 0.0 = focus at infinity
LENS_MAX = 15.0    # fallback only; the real max is read from the sensor on start
                   # (mode-dependent: ~15 in video, ~32 in still — higher = nearer)


class _FrameBroker:
    """Latest JPEG frame + a condition viewers wait on. Outlives the picam."""

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


class Camera:
    """Lazily-started MJPEG source. Thread-safe; shared by all stream clients."""

    def __init__(self, size=(640, 480), rotate_180: bool = False, indicator=None,
                 af_mode: int = AF_MANUAL, lens_position: float = 2.0,
                 lores_size=(320, 240)):
        self._size = size
        self._lores_size = lores_size       # small YUV stream for the face tracker
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
        self._lens_min = LENS_MIN
        self._lens_max = LENS_MAX
        self._picam = None
        self._broker = _FrameBroker()       # persistent across restarts
        self._viewers = 0                   # MJPEG stream clients
        self._holds = 0                     # non-streaming consumers (e.g. face tracker)
        # Suspended = the sensor has been handed off to another owner (e.g.
        # MyRobotLab). While suspended available() is False, the sensor is
        # force-stopped, and nothing restarts it until resume().
        self._suspended = False
        self._lock = threading.Lock()       # guards _picam/_viewers/_holds/settings

        # Detect once at construction so /api/state stays cheap.
        self.detected = False
        if Picamera2 is not None:
            try:
                self.detected = len(Picamera2.global_camera_info()) > 0
            except Exception:
                self.detected = False

    def available(self) -> bool:
        return self.detected and not self._suspended

    def is_streaming(self) -> bool:
        """True while the sensor is running (a viewer or a hold)."""
        with self._lock:
            return self._picam is not None

    # ---- non-streaming consumers (face tracker) ---------------------------
    def acquire(self) -> None:
        """Keep the sensor running for a consumer that reads frames directly via
        ``capture_gray()`` (rather than the MJPEG stream). Pair with release().
        Does not light the status LED — that tracks MJPEG viewers only."""
        with self._lock:
            self._holds += 1
            if self._picam is None and not self._suspended:
                self._start_locked()

    def release(self) -> None:
        """Drop a hold taken by acquire(); stops the sensor if nobody's left."""
        with self._lock:
            if self._holds > 0:
                self._holds -= 1
            if self._viewers == 0 and self._holds == 0 and self._picam is not None:
                self._stop_locked()

    # ---- hardware handoff -------------------------------------------------
    def is_suspended(self) -> bool:
        return self._suspended

    def suspend(self) -> None:
        """Release the camera sensor so another process (e.g. MyRobotLab) can use
        it. Force-stops the sensor even if viewers/holds remain, and reports
        unavailable() until resume(); active MJPEG streams simply stop receiving
        frames. Idempotent."""
        with self._lock:
            if self._suspended:
                return
            self._suspended = True
            if self._picam is not None:
                self._stop_locked()
            if self._indicator is not None:
                self._indicator.notify_camera(False)

    def resume(self) -> None:
        """Take the sensor back. Doesn't restart it — the next viewer/hold does,
        lazily. Idempotent."""
        with self._lock:
            self._suspended = False

    def capture_gray(self):
        """Grab the current frame as a grayscale ndarray from the lores stream
        (its YUV420 Y-plane is luma = grayscale), or None if not running / error.
        Cheap: it doesn't disturb the MJPEG encoder on the main stream."""
        with self._lock:
            picam = self._picam
        if picam is None:
            return None
        try:
            yuv = picam.capture_array("lores")     # shape (h*3/2, w), uint8
        except Exception:      # noqa: BLE001 - sensor may be mid-restart; caller retries
            return None
        w, h = self._lores_size
        return yuv[:h, :w]                          # top h rows = Y (luma) plane

    def settings(self) -> dict:
        return {"flip": self._rotate_180, "af_mode": self._af_mode,
                "lens_position": round(self._lens_position, 2),
                "lens_min": round(self._lens_min, 2), "lens_max": round(self._lens_max, 2)}

    # ---- lifecycle (callers must hold self._lock) -------------------------
    def _start_locked(self) -> None:
        self._picam = Picamera2()
        transform = Transform(hflip=1, vflip=1) if self._rotate_180 else Transform()
        cfg = self._picam.create_video_configuration(
            main={"size": self._size},
            lores={"size": self._lores_size, "format": "YUV420"},
            transform=transform)
        self._picam.configure(cfg)
        # The lens range is only known once configured, and it's mode-dependent, so
        # read the real limits and re-clamp our position to them.
        lp = self._picam.camera_controls.get("LensPosition")
        if lp and lp[0] is not None and lp[1] is not None:
            self._lens_min, self._lens_max = float(lp[0]), float(lp[1])
            self._lens_position = max(self._lens_min, min(self._lens_max, self._lens_position))
        self._picam.start_recording(MJPEGEncoder(), FileOutput(_Output(self._broker)))
        self._apply_focus_locked()

    def _stop_locked(self) -> None:
        try:
            self._picam.stop_recording()
        finally:
            self._picam.close()
            self._picam = None

    def _apply_focus_locked(self) -> None:
        if self._picam is None:
            return
        ctrls = {"AfMode": self._af_mode}
        if self._af_mode == AF_MANUAL:
            ctrls["LensPosition"] = self._lens_position
        self._picam.set_controls(ctrls)

    # ---- runtime adjustments ----------------------------------------------
    def set_rotation(self, flip: bool) -> dict:
        """Flip the sensor image 180 (both axes) or not. Restarts the sensor
        underneath any active viewers, who keep streaming via the broker."""
        with self._lock:
            if bool(flip) != self._rotate_180:
                self._rotate_180 = bool(flip)
                if self._picam is not None:
                    self._stop_locked()
                    self._start_locked()
        return self.settings()

    def set_focus(self, mode: int | None = None,
                  lens_position: float | None = None) -> dict:
        """Set autofocus mode (AF_MANUAL / AF_CONTINUOUS) and/or manual lens
        position in dioptres. Applied live if running, remembered otherwise."""
        with self._lock:
            if mode is not None:
                m = int(mode)
                self._af_mode = m if m in (AF_MANUAL, AF_AUTO, AF_CONTINUOUS) else AF_CONTINUOUS
            if lens_position is not None:
                self._lens_position = max(self._lens_min,
                                          min(self._lens_max, float(lens_position)))
            self._apply_focus_locked()
        return self.settings()

    def autofocus(self) -> dict:
        """Run a single autofocus scan and hold wherever it lands (Auto + trigger).

        Useful when continuous AF would keep hunting — one press, one attempt.
        """
        with self._lock:
            self._af_mode = AF_AUTO
            if self._picam is not None:
                self._picam.set_controls({"AfMode": AF_AUTO, "AfTrigger": AF_TRIGGER_START})
        return self.settings()

    # ---- streaming --------------------------------------------------------
    def frames(self):
        """Yield JPEG frames until the client disconnects. Starts the camera for
        the first viewer and stops it when the last one leaves."""
        if not self.available():
            raise RuntimeError("no camera available")
        with self._lock:
            self._viewers += 1
            if self._picam is None:                       # tracker may already hold it open
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
                    if self._holds == 0 and self._picam is not None:
                        self._stop_locked()               # nobody left (no viewers, no holds)

    def snapshot(self) -> bytes | None:
        """Grab a single JPEG frame (starts/stops the camera if idle)."""
        for frame in self.frames():
            return frame          # closing the generator runs frames()' cleanup
        return None

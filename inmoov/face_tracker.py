"""Face tracking for the InMoov head — move the eyes (and neck/head tilt) to
keep a detected face centred in the camera view.

A background thread pulls low-res grayscale frames from the shared ``Camera``
(its lores stream, so the MJPEG viewers are undisturbed), runs an OpenCV Haar
cascade, picks the largest face, and runs a PD-controller that nudges
``eye_x``/``eye_y`` toward it. This is a *feedback* loop: the camera rides the
moving head, so as the eyes turn toward the face the error shrinks and the
motion settles. The derivative (``damp_*``) term eases the eye off as it closes
on the face so a laggy servo doesn't sail past centre and ring — keep the
proportional (``gain_*``) term modest for the same reason. When an eye saturates
near its travel limit the head follows in that direction and the eye eases back
toward centre to regain range: ``neck`` handles the horizontal axis, and
``head_tilt_fb`` (forward/back) does the same job vertically, so the head looks
up at someone tall or down at a child instead of rolling its eyes at them. The
tilt assist is optional — heads without that servo still track on eyes + neck.

When the face then leaves the frame entirely, the tracker doesn't just freeze:
for a short window (``lost_hold_frames``) it keeps rotating the neck toward the
direction the face was last seen heading (``seek_gain``), trying to bring a
person who walked out of shot back into view before giving up. This seek only
kicks in when the face was near a frame edge (``seek_edge``) and has been gone
for a few frames (``seek_start_misses``), so ordinary detection dropouts during
following don't nudge the head past a subject who's still there.

When there is no face to go on at all — nobody has been in frame, or one
vanished from the middle of it — the chest ultrasonics provide a coarse
left/right bearing instead (``bearing_cb``): whichever side reads nearer is the
side to turn toward. It sees a much wider arc than the lens, so it covers the
case the camera cannot, someone walking up from the side.

That assist is **open-loop and one-shot on purpose**. The ultrasonics are in the
chest and do not move with the head, so turning does not reduce the differential
the way it reduces a camera error; run as a proportional loop it would drive the
neck into its stop and hold it there. It therefore re-aims only when the reading
itself changes (``bearing_change``), and a face — when there is one — always
wins, because the camera is far more accurate than two range-finders.

Degrades gracefully: ``available()`` is False (and ``start()`` a no-op) when
OpenCV, the cascade, or the camera aren't present, so the rest of the app runs.

The direction signs (``invert_*``) and gains almost certainly need a little
bench tuning the first time real servos move — they're live-adjustable via
``configure()`` (wired to ``POST /api/track``) so you can dial them in without a
restart. If an axis runs *away* from the face instead of toward it, flip that
axis's invert.
"""
from __future__ import annotations

import threading
import time

try:
    import cv2
    _CV_ERR = None
except Exception as exc:  # noqa: BLE001 - no OpenCV just means "no tracking"
    cv2 = None
    _CV_ERR = exc

# Debian's python3-opencv doesn't ship the cv2.data helper, so use the abs path.
CASCADE_PATH = "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"

# Tuning fields the UI/endpoint may adjust live via configure(). Public because
# the app persists exactly this set to settings.json ("track") and filters what
# it loads back through it, so a stale key in the file can't break construction.
TUNABLE = ("gain_x", "gain_y", "damp_x", "damp_y", "invert_x", "invert_y",
           "invert_neck", "invert_tilt", "deadzone", "neck_gain", "tilt_gain",
           "sat_margin", "eye_recenter", "fps", "seek_gain", "seek_edge",
           "bearing_gain", "invert_bearing", "bearing_change")


class FaceTracker:
    """Background face-follow controller for eye_x/eye_y (+ neck, head tilt)."""

    def __init__(self, camera, controller, *, cascade_path: str = CASCADE_PATH,
                 eye_x: str = "eye_x", eye_y: str = "eye_y", neck: str = "neck",
                 tilt: str = "head_tilt_fb",
                 gain_x: float = 3.5, gain_y: float = 3.0,
                 damp_x: float = 3.0, damp_y: float = 2.5,
                 invert_x: bool = False, invert_y: bool = False,
                 invert_neck: bool = False, invert_tilt: bool = False,
                 deadzone: float = 0.06,
                 fps: float = 15.0, neck_gain: float = 2.5, tilt_gain: float = 1.8,
                 sat_margin: float = 5.0, eye_recenter: float = 0.15,
                 seek_gain: float = 0.7, seek_edge: float = 0.35,
                 seek_start_misses: int = 3, lost_hold_frames: int = 31,
                 bearing_gain: float = 0.6, invert_bearing: bool = False,
                 bearing_change: float = 0.15,
                 event_cb=None, bearing_cb=None):
        self._cam = camera
        self._ctrl = controller
        self._event_cb = event_cb                # called with a string on notable events
        self._names = {"x": eye_x, "y": eye_y, "neck": neck}
        # Head tilt is optional and deliberately kept out of _names: available()
        # requires every name there, so listing it would turn "no tilt servo" into
        # "no face tracking at all" on a head that never had one. Resolved to None
        # when it isn't configured, which just disables the vertical assist.
        servos = getattr(controller, "servos", {}) or {}
        self._tilt = tilt if tilt in servos else None

        # --- tunables (all live-adjustable) ---
        self.gain_x, self.gain_y = float(gain_x), float(gain_y)
        # Derivative (damping) gains: oppose the *rate* the error is changing, so
        # the eye eases off as it closes on the face instead of charging past it.
        # This is what turns the pure-P loop into a PD loop and kills the overshoot
        # /ringing that a laggy servo + camera-on-a-moving-head otherwise produces.
        self.damp_x, self.damp_y = float(damp_x), float(damp_y)
        self.invert_x, self.invert_y = bool(invert_x), bool(invert_y)
        self.invert_neck = bool(invert_neck)
        self.invert_tilt = bool(invert_tilt)
        self.deadzone = float(deadzone)
        self.neck_gain = float(neck_gain)
        # Lower than neck_gain by default: the tilt servo carries the head's
        # weight over a much shorter travel (head_tilt_fb spans ~79° vs the
        # neck's ~155°), so the same step size goes a lot further.
        self.tilt_gain = float(tilt_gain)
        self.sat_margin = float(sat_margin)
        self.eye_recenter = float(eye_recenter)
        self.seek_gain = float(seek_gain)        # blind-follow speed after a face is lost
        self.seek_edge = float(seek_edge)        # only chase if the face was this far off-centre (0..1) when lost
        self.seek_start_misses = int(seek_start_misses)  # consecutive misses before seeking (ignore brief dropouts)
        # Paced to the frame *source*, not to the CPU. 12.0 was the head Pi 4B's
        # detection budget; on an x86 brain a JPEG decode plus a full-frame Haar
        # pass costs ~12 ms (~80/s), so the frames themselves are the ceiling
        # now. Asking for more than the camera delivers only re-runs the detector
        # on a frame capture_gray() already handed us — the backends publish the
        # newest frame and never block — and a repeat measures de/dt = 0, which
        # quietly drops the damping term for that tick. Match the source instead:
        # the head's camera_stream.py serves CAM_STREAM_FPS (15), a local
        # picamera2 lores stream runs at 30.
        self._fps = max(1.0, float(fps))
        # Counted in frames, so this window shortens as fps rises: 31 @ 15 fps is
        # the ~2.1 s hold it was tuned to at 12. Giving up on someone who walked
        # out of shot is a human-scale timeout — move it with fps. seek_start_misses
        # is genuinely frame-shaped (ignore a few dropped detections), so it isn't.
        self.lost_hold_frames = int(lost_hold_frames)
        # Last horizontal error while a face was visible (+ = face to the right).
        # Its sign says which way to keep turning if the face then leaves frame;
        # its magnitude gates whether the face was near an edge (worth chasing)
        # vs. a centred detection that merely flickered out (hold, don't chase).
        self._last_ex = 0.0
        self._seek_active = False
        # Chest-sensor bearing: a coarse "someone is over there" for when the
        # camera has nothing at all. bearing_cb() -> float in -1..+1 (+ = image
        # right) or None for "no opinion".
        self._bearing_cb = bearing_cb
        self.bearing_gain = float(bearing_gain)      # 0 disables the assist entirely
        self.invert_bearing = bool(invert_bearing)   # if the head turns the wrong way
        self.bearing_change = float(bearing_change)  # re-aim only on a change this big
        self._bearing_applied = None                 # last hint we actually acted on
        # Previous per-axis error + smoothed error-rate, for the derivative term.
        # ``None`` = no valid history yet (just acquired / after a gap), so the
        # first frame contributes no damping kick from a stale reference.
        self._prev_ex = self._prev_ey = None
        self._dex = self._dey = 0.0

        # Load the cascade once (None if OpenCV/file missing or file invalid).
        self._cascade = None
        if cv2 is not None:
            try:
                c = cv2.CascadeClassifier(cascade_path)
                self._cascade = c if not c.empty() else None
            except Exception:  # noqa: BLE001
                self._cascade = None

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._status = {"face": False, "faces": 0, "error": None,
                        "center": None, "size": None, "loop_fps": 0.0,
                        "seeking": False}

    # ---- capability / introspection ---------------------------------------
    def available(self) -> bool:
        return (cv2 is not None and self._cascade is not None
                and self._cam is not None and self._cam.available()
                and self._ctrl is not None
                and all(n in self._ctrl.servos for n in self._names.values()))

    def is_tracking(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def tuning(self) -> dict:
        return {k: getattr(self, "_fps" if k == "fps" else k) for k in TUNABLE}

    def status(self) -> dict:
        with self._lock:
            s = dict(self._status)
        s["available"] = self.available()
        s["tracking"] = self.is_tracking()
        s["tuning"] = self.tuning()
        s["tilt"] = self._tilt          # servo name, or None if this head has no tilt
        if not s["available"] and _CV_ERR is not None:
            s["reason"] = "OpenCV not available"
        return s

    def configure(self, **kw) -> None:
        """Update tunables live. Unknown keys ignored; bad values skipped."""
        for k in TUNABLE:
            if k not in kw or kw[k] is None:
                continue
            try:
                if k in ("invert_x", "invert_y", "invert_neck", "invert_tilt"):
                    setattr(self, k, bool(kw[k]))
                elif k == "fps":
                    self._fps = max(1.0, float(kw[k]))
                else:
                    setattr(self, k, float(kw[k]))
            except (TypeError, ValueError):
                pass

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        if not self.available():
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="face-tracker",
                                            daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._last_ex = 0.0
        self._seek_active = False
        self._reset_derivative()
        with self._lock:
            self._thread = None
            self._status.update(face=False, error=None, center=None, size=None,
                                seeking=False)

    # ---- the loop ---------------------------------------------------------
    def _run(self) -> None:
        self._cam.acquire()                      # keep the sensor running while we track
        self._emit("▶ Face tracking started")
        misses = 0
        had_face = False                         # for acquired/lost transition events
        ema_cycle = 1.0 / self._fps              # actual loop period (incl. pacing), smoothed
        prev = None
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                if prev is not None:             # measure the true tick rate, not just work time
                    ema_cycle = 0.8 * ema_cycle + 0.2 * max(t0 - prev, 1e-3)
                    with self._lock:
                        self._status["loop_fps"] = round(1.0 / ema_cycle, 1)
                prev = t0

                gray = self._cam.capture_gray()
                if gray is None:
                    self._stop.wait(0.1)
                    continue
                face = self._detect(gray)
                if face is not None:
                    misses = 0
                    if not had_face:                      # just acquired a face
                        had_face = True
                        self._emit("👁 Detected a face")
                    self._bearing_applied = None           # re-aim afresh if lost again
                    if self._seek_active:                 # re-acquired mid-seek
                        self._seek_active = False
                        self._reset_derivative()          # position jumped; drop stale rate
                        with self._lock:
                            self._status["seeking"] = False
                    self._track_face(gray, face)
                else:
                    misses += 1
                    # Only chase a face that was clearly heading out an edge, and
                    # only after a few misses — so brief detection dropouts during
                    # normal following don't nudge the head past the subject.
                    if (had_face and self.seek_start_misses <= misses < self.lost_hold_frames
                            and abs(self._last_ex) >= self.seek_edge):
                        self._seek_lost(misses)
                    elif misses >= self.seek_start_misses:
                        # No usable last-seen direction: either no face has ever
                        # been in frame, or it vanished from the middle of it.
                        # The chest sensors are then the only bearing available.
                        self._seek_bearing()
                    if misses >= self.lost_hold_frames:    # give up: hold, note "lost"
                        if had_face:
                            had_face = False
                            self._seek_active = False
                            self._last_ex = 0.0
                            self._reset_derivative()
                            self._emit("👁 Lost the face")
                        with self._lock:
                            self._status.update(face=False, error=None, center=None,
                                                size=None, seeking=False)

                dt = time.monotonic() - t0
                period = 1.0 / self._fps
                if dt < period:
                    self._stop.wait(period - dt)          # interruptible pacing
        finally:
            self._cam.release()
            self._emit("⏹ Face tracking stopped")

    def _emit(self, text: str) -> None:
        if self._event_cb is not None:
            try:
                self._event_cb(text)
            except Exception:      # noqa: BLE001 - logging must never break tracking
                pass

    def _detect(self, gray):
        try:
            g = cv2.equalizeHist(gray)                     # lift low-contrast faces
            faces = self._cascade.detectMultiScale(
                g, scaleFactor=1.2, minNeighbors=5, minSize=(40, 40))
        except Exception:  # noqa: BLE001 - a bad frame shouldn't kill the loop
            faces = []
        n = len(faces)
        with self._lock:
            self._status["faces"] = n
        if n == 0:
            return None
        return max(faces, key=lambda f: int(f[2]) * int(f[3]))   # largest face

    def _track_face(self, gray, face) -> None:
        h, w = gray.shape[:2]
        x, y, fw, fh = (float(v) for v in face)
        fx, fy = x + fw / 2.0, y + fh / 2.0
        # Normalised error from frame centre: + ex = face right, + ey = face low.
        ex = (fx - w / 2.0) / (w / 2.0)
        ey = (fy - h / 2.0) / (h / 2.0)
        cmd_x = 0.0 if abs(ex) < self.deadzone else ex
        cmd_y = 0.0 if abs(ey) < self.deadzone else ey
        # Remember the horizontal error so, if the face then leaves the frame, we
        # know which way it went and whether it was near an edge (see _seek_lost).
        self._last_ex = ex

        # Error-rate for the derivative term, smoothed so a jittery Haar box
        # doesn't inject a phantom velocity. No history (just acquired / post-gap)
        # => zero rate this frame, so damping never kicks off a stale reference.
        if self._prev_ex is None:
            dex = dey = 0.0
        else:
            dex, dey = ex - self._prev_ex, ey - self._prev_ey
        self._prev_ex, self._prev_ey = ex, ey
        self._dex = 0.6 * self._dex + 0.4 * dex
        self._dey = 0.6 * self._dey + 0.4 * dey

        ctrl = self._ctrl
        nx, ny, nn = self._names["x"], self._names["y"], self._names["neck"]
        sx, sy = ctrl.servos[nx], ctrl.servos[ny]
        cur_x = ctrl.get_angle(nx)
        cur_y = ctrl.get_angle(ny)
        if cur_x is None:
            cur_x = sx["rest_angle"]
        if cur_y is None:
            cur_y = sy["rest_angle"]

        dir_x = -1.0 if self.invert_x else 1.0
        dir_y = -1.0 if self.invert_y else 1.0
        # PD step: proportional pull toward the face + damping that eases off as
        # the error shrinks (dex/dey < 0 while closing). Damping applies only when
        # actually correcting, so it never drifts the eye around inside deadzone.
        step_x = 0.0 if cmd_x == 0.0 else self.gain_x * cmd_x + self.damp_x * self._dex
        step_y = 0.0 if cmd_y == 0.0 else self.gain_y * cmd_y + self.damp_y * self._dey
        act_x = ctrl.set_angle(nx, cur_x + dir_x * step_x)
        act_y = ctrl.set_angle(ny, cur_y + dir_y * step_y)

        # Neck follows when the eye is pinned at a limit but the face is still
        # off-centre in that direction; then ease the eye back toward rest.
        if cmd_x != 0.0:
            push_up = (dir_x * cmd_x) > 0            # command drives the eye angle up
            pinned_hi = act_x >= sx["max_angle"] - self.sat_margin
            pinned_lo = act_x <= sx["min_angle"] + self.sat_margin
            if (pinned_hi and push_up) or (pinned_lo and not push_up):
                dir_n = -1.0 if self.invert_neck else 1.0
                cur_n = ctrl.get_angle(nn)
                if cur_n is None:
                    cur_n = ctrl.servos[nn]["rest_angle"]
                ctrl.set_angle(nn, cur_n + dir_n * self.neck_gain * cmd_x)
                rest_x = sx["rest_angle"]
                ctrl.set_angle(nx, act_x + (rest_x - act_x) * self.eye_recenter)

        # Head tilt does the same thing vertically: once the eye can't look any
        # further up/down, tip the head that way and ease the eye back toward
        # rest. Without this the eyes just pin at their limit and a face above or
        # below the head's eyeline is tracked with a fixed stare.
        if cmd_y != 0.0 and self._tilt is not None:
            push_up = (dir_y * cmd_y) > 0            # command drives the eye angle up
            pinned_hi = act_y >= sy["max_angle"] - self.sat_margin
            pinned_lo = act_y <= sy["min_angle"] + self.sat_margin
            if (pinned_hi and push_up) or (pinned_lo and not push_up):
                dir_t = -1.0 if self.invert_tilt else 1.0
                cur_t = ctrl.get_angle(self._tilt)
                if cur_t is None:
                    cur_t = ctrl.servos[self._tilt]["rest_angle"]
                ctrl.set_angle(self._tilt, cur_t + dir_t * self.tilt_gain * cmd_y)
                rest_y = sy["rest_angle"]
                ctrl.set_angle(ny, act_y + (rest_y - act_y) * self.eye_recenter)

        with self._lock:
            self._status.update(face=True, error=[round(ex, 3), round(ey, 3)],
                                center=[round(fx / w, 3), round(fy / h, 3)],
                                size=[round(fw / w, 3), round(fh / h, 3)])

    def _reset_derivative(self) -> None:
        """Drop the derivative history so the next tracked frame starts damping
        from a clean slate (no phantom velocity across a detection gap)."""
        self._prev_ex = self._prev_ey = None
        self._dex = self._dey = 0.0

    def _seek_bearing(self) -> None:
        """Aim the neck at whichever side the chest sensors say someone is on.

        This is the only bearing that exists when no face has been seen at all —
        someone walking up from the side, never yet in frame — which is exactly
        the case the camera cannot help with.

        Deliberately **open-loop and one-shot**. The ultrasonics are mounted in
        the chest and do not move with the head, so turning does *not* reduce the
        differential the way turning reduces a camera error. Run as a
        proportional loop this would drive the neck into its end stop and hold it
        there, since the "error" never goes away. So it re-aims only when the
        reading itself changes by ``bearing_change``, and otherwise does nothing.
        """
        if self.bearing_gain <= 0.0 or self._bearing_cb is None:
            return
        try:
            hint = self._bearing_cb()
        except Exception:                       # a sensor hiccup must not stop tracking
            return
        if hint is None:                        # "no opinion" — not "straight ahead"
            return
        prev = self._bearing_applied
        if prev is not None and abs(hint - prev) < self.bearing_change:
            return                              # nobody has moved enough to re-aim
        self._bearing_applied = hint
        ctrl = self._ctrl
        nn = self._names["neck"]
        sn = ctrl.servos.get(nn)
        if not sn:
            return
        step = hint * (-1.0 if self.invert_bearing else 1.0)
        dir_n = -1.0 if self.invert_neck else 1.0
        cur = ctrl.get_angle(nn)
        if cur is None:
            cur = sn["rest_angle"]
        ctrl.set_angle(nn, cur + dir_n * self.neck_gain * self.bearing_gain * step)
        if prev is None:                        # first aim of this search only
            self._emit("📡 Turning toward the chest sensors")

    def _seek_lost(self, misses: int) -> None:
        """The face just left the frame — keep rotating the neck toward where it
        was last seen (and ease the eyes back to centre for a fresh field of
        view) so a person walking out of shot is chased rather than dropped.

        Runs each frame the face is missing (once the loop's edge/miss gate is
        satisfied), until it's re-acquired, the neck hits its travel limit, or
        the loop gives up after ``lost_hold_frames``. The step *decays* toward
        zero as the miss count climbs: we commit hard right after the face slips
        the edge (when the last-seen direction is still trustworthy) and back off
        as confidence fades, so a wrong guess can't crank the neck far off. No-op
        if ``seek_gain`` = 0."""
        if self.seek_gain <= 0.0 or self._last_ex == 0.0:
            return
        d = 1.0 if self._last_ex > 0.0 else -1.0
        # Linear decay 1 -> 0 across the seek window [seek_start_misses, lost_hold].
        span = max(1, self.lost_hold_frames - self.seek_start_misses)
        decay = max(0.0, 1.0 - (misses - self.seek_start_misses) / span)
        ctrl = self._ctrl
        nn, nx = self._names["neck"], self._names["x"]
        sn, sx = ctrl.servos[nn], ctrl.servos[nx]
        dir_n = -1.0 if self.invert_neck else 1.0
        cur_n = ctrl.get_angle(nn)
        if cur_n is None:
            cur_n = sn["rest_angle"]
        ctrl.set_angle(nn, cur_n + dir_n * self.neck_gain * self.seek_gain * decay * d)
        # Recentre the eyes so they regain range for when the face reappears.
        cur_x = ctrl.get_angle(nx)
        if cur_x is None:
            cur_x = sx["rest_angle"]
        ctrl.set_angle(nx, cur_x + (sx["rest_angle"] - cur_x) * self.eye_recenter)
        if not self._seek_active:
            self._seek_active = True
            self._emit("🔎 Turning to keep the face in view")
        with self._lock:
            self._status["seeking"] = True

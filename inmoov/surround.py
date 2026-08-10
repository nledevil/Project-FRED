"""SurroundVision — turn the 360° camera into bearings and a sector map.

Sits between ``Camera360`` and everything that wants to know *where people
are*: the face tracker (a bearing to turn the neck toward), the cart's safety
governor (is anyone in the path?), the brain ("who's around me?"), and the web
panel (a live radar). One background thread, three layers, cheapest first:

  1. **Motion sectors.** The equirect is downscaled and differenced against a
     slow exponential background; the energy is summed into N yaw sectors
     (robot frame). This is nearly free, sees all 360° every tick, and catches
     what a face detector never will — someone walking past behind FRED, a back
     of a head, a kid sprinting by.
  2. **Targeted person detection.** Each tick, the busiest sectors (plus one
     round-robin sweep sector, so a motionless person is still found within a
     few seconds) get a *dewarped* rectilinear view aimed at them, and OpenCV
     Haar cascades (frontal face + upper body) run on that. Detection runs on
     dewarped tiles rather than the raw equirect because Haar is trained on
     ordinary photos — equirect distortion away from the horizon breaks it.
  3. **Track merging.** Detections and sustained motion are merged into a small
     list of "presences" by yaw proximity, smoothed, aged out after a few
     seconds. Face/body hits carry a rough distance from apparent size; motion
     only carries a direction — real but blind to range, and callers see
     ``dist_m: None`` and treat it accordingly.

Distance from a Haar box is genuinely rough (±30% — faces differ, and the tile
projection stretches near its edges): good enough to rank "close" against
"far" for the cart governor, nowhere near good enough to navigate by. The
ultrasonics stay the authority on actual range up close.

Everything degrades: no OpenCV → ``available()`` False; no cascades → motion
sectors still work; camera in mock mode → the mock's wandering blob exercises
the whole path (motion sees it; Haar ignores a painted ellipse, which is
itself the honest test of the fallback layer).

Robot-frame yaw throughout: 0° = cart-forward, positive = FRED's right.
"""
from __future__ import annotations

import math
import threading
import time

try:
    import cv2
    import numpy as np
    _CV_ERR = None
except Exception as exc:  # noqa: BLE001
    cv2 = None
    np = None
    _CV_ERR = exc

# Debian apt path first (the Pi), then the pip wheel's helper (dev machines).
_CASCADE_DIRS = ["/usr/share/opencv4/haarcascades/"]
if cv2 is not None and hasattr(cv2, "data"):
    _CASCADE_DIRS.append(cv2.data.haarcascades)

# Assumed real-world heights behind the size→distance estimate.
_FACE_M = 0.24          # chin→crown incl. hair, roughly
_BODY_M = 0.80          # head + torso as the upperbody cascade frames it

# Live-adjustable fields, persisted to settings.json ("surround") the same way
# the face tracker's TUNABLE set is.
TUNABLE = ("fps", "motion_threshold", "presence_ttl", "sweep", "min_face",
           "stop_m", "slow_m", "bearing_max_age")


def _wrap_deg(a: float) -> float:
    return (float(a) + 180.0) % 360.0 - 180.0


def _load_cascade(name: str):
    if cv2 is None:
        return None
    for d in _CASCADE_DIRS:
        try:
            c = cv2.CascadeClassifier(d + name)
            if not c.empty():
                return c
        except Exception:  # noqa: BLE001
            pass
    return None


class SurroundVision:
    """Background 360° person/motion awareness over a Camera360."""

    def __init__(self, camera, *, sectors: int = 12, fps: float = 6.0,
                 motion_threshold: float = 30.0, presence_ttl: float = 4.0,
                 sweep: bool = True, min_face: int = 28,
                 stop_m: float = 1.0, slow_m: float = 2.5,
                 bearing_max_age: float = 2.0, event_cb=None):
        self._cam = camera
        self._event_cb = event_cb
        self.sectors = max(4, int(sectors))
        self._fps = max(1.0, float(fps))
        # Sector energy = mean |diff| (0..255) of the sector's top 5% most-
        # changed pixels — so a person-sized target scores the same whether the
        # sector is narrow or wide, while broad sensor noise stays low (noise
        # raises *all* pixels a little; a mover raises *some* pixels a lot).
        # This is the threshold that energy must exceed; live-tunable because
        # venues differ.
        self.motion_threshold = float(motion_threshold)
        self.presence_ttl = float(presence_ttl)       # seconds before a track ages out
        self.sweep = bool(sweep)                      # round-robin detect on quiet sectors
        self.min_face = int(min_face)                 # px in the 320-wide tile
        self.stop_m = float(stop_m)                   # governor: full stop inside this
        self.slow_m = float(slow_m)                   # governor: scale down inside this
        self.bearing_max_age = float(bearing_max_age)

        self._face = _load_cascade("haarcascade_frontalface_default.xml")
        self._body = _load_cascade("haarcascade_upperbody.xml")

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._bg = None                               # float32 EMA background (small gray)
        self._sweep_at = 0                            # next sweep sector index
        self._motion = [0.0] * self.sectors           # robot-frame sector energies
        self._presences: list[dict] = []              # merged tracks
        self._loop_fps = 0.0

    # ---- capability / status ----------------------------------------------
    def available(self) -> bool:
        return cv2 is not None and self._cam is not None and self._cam.available()

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def tuning(self) -> dict:
        return {k: getattr(self, "_fps" if k == "fps" else k) for k in TUNABLE}

    def configure(self, **kw) -> None:
        """Update tunables live. Unknown keys ignored; bad values skipped."""
        for k in TUNABLE:
            if k not in kw or kw[k] is None:
                continue
            try:
                if k == "fps":
                    self._fps = max(1.0, float(kw[k]))
                elif k == "sweep":
                    self.sweep = bool(kw[k])
                elif k == "min_face":
                    self.min_face = int(kw[k])
                else:
                    setattr(self, k, float(kw[k]))
            except (TypeError, ValueError):
                pass

    def status(self) -> dict:
        with self._lock:
            motion = list(self._motion)
            pres = [dict(p) for p in self._presences]
            fps = self._loop_fps
        now = time.monotonic()
        span = 360.0 / self.sectors
        return {
            "available": self.available(),
            "running": self.is_running(),
            "detectors": {"face": self._face is not None,
                          "body": self._body is not None},
            "loop_fps": round(fps, 1),
            "sector_span": span,
            # sector i covers robot yaw [-180 + i*span, -180 + (i+1)*span)
            "motion": [round(m, 1) for m in motion],
            "presences": [{
                "yaw": round(p["yaw"], 1),
                "kind": p["kind"],
                "dist_m": None if p.get("dist_m") is None else round(p["dist_m"], 2),
                "age": round(now - p["seen"], 1),
            } for p in pres],
            "tuning": self.tuning(),
        }

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> bool:
        if not self.available():
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="surround",
                                            daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        with self._lock:
            self._thread = None
            self._presences = []
            self._motion = [0.0] * self.sectors
            self._bg = None

    # ---- consumers ---------------------------------------------------------
    def bearing(self):
        """Face-tracker hint: -1..+1, positive = turn right, or None.

        Chooses the freshest presence (faces beat motion-only at equal age),
        and maps its robot yaw through ±90° onto the hint range — the neck
        can't reach behind the cart anyway, so anything further sideways just
        saturates the hint. Same contract as SensorHub.bearing(): None means
        "no opinion", never "straight ahead".
        """
        now = time.monotonic()
        with self._lock:
            fresh = [p for p in self._presences
                     if now - p["seen"] <= self.bearing_max_age]
        if not fresh:
            return None
        fresh.sort(key=lambda p: (p["kind"] == "motion", now - p["seen"]))
        return max(-1.0, min(1.0, fresh[0]["yaw"] / 90.0))

    def nav_assess(self, speed: int) -> dict:
        """Speed gate for the cart governor: how fast is it safe to go?

        ``speed`` sign picks the direction of travel (+ forward = yaw 0,
        - reverse = yaw 180). Any presence within ±45° of that heading and
        inside ``stop_m`` → factor 0; inside ``slow_m`` → linear scale-down.
        A motion-only presence (no range) in the path caps the factor at 0.5:
        we know someone is there but not how far, so creep, don't cruise.
        """
        heading = 0.0 if speed >= 0 else 180.0
        factor, why = 1.0, ""
        now = time.monotonic()
        with self._lock:
            pres = [dict(p) for p in self._presences]
        for p in pres:
            if now - p["seen"] > self.presence_ttl:
                continue
            off = abs(_wrap_deg(p["yaw"] - heading))
            if off > 45.0:
                continue
            d = p.get("dist_m")
            if d is None:
                if factor > 0.5:
                    factor, why = 0.5, f"movement in path at {p['yaw']:+.0f} deg"
                continue
            if d <= self.stop_m:
                return {"factor": 0.0,
                        "why": f"person {d:.1f} m ahead at {p['yaw']:+.0f} deg"}
            if d <= self.slow_m:
                f = (d - self.stop_m) / max(0.1, self.slow_m - self.stop_m)
                if f < factor:
                    factor, why = f, f"person {d:.1f} m at {p['yaw']:+.0f} deg"
        return {"factor": round(factor, 2), "why": why}

    def spoken_summary(self) -> str:
        """What the 360 camera sees, phrased to be spoken aloud (brain tool)."""
        if not self.is_running():
            return "My surround camera isn't watching right now."
        now = time.monotonic()
        with self._lock:
            pres = [p for p in self._presences
                    if now - p["seen"] <= self.presence_ttl]
        if not pres:
            return "I don't see anyone around me right now."
        parts = []
        for p in sorted(pres, key=lambda q: abs(q["yaw"])):
            where = self._spoken_direction(p["yaw"])
            if p.get("dist_m") is not None:
                phrase = f"someone about {self._spoken_m(p['dist_m'])} {where}"
            else:
                phrase = f"movement {where}"
            # Two tracks of the same thing in the same rough direction (a fast
            # mover briefly splits into neighbouring tracks) would read as
            # "movement in front of me, and movement in front of me".
            if phrase not in parts:
                parts.append(phrase)
        listing = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f", and {parts[-1]}"
        return f"I can see {listing}."

    @staticmethod
    def _spoken_direction(yaw: float) -> str:
        y = _wrap_deg(yaw)
        if abs(y) <= 25:
            return "in front of me"
        if abs(y) >= 155:
            return "behind me"
        side = "right" if y > 0 else "left"
        return f"to my {side}" if abs(y) <= 65 else f"behind me to the {side}"

    @staticmethod
    def _spoken_m(m: float) -> str:
        if m < 1.2:
            return "a metre away"
        return f"{m:.0f} metres away"

    # ---- the loop -----------------------------------------------------------
    def _run(self) -> None:
        self._cam.acquire()
        self._emit("◎ Surround vision started")
        seq = 0
        prev = None
        ema_cycle = 1.0 / self._fps
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                if prev is not None:
                    ema_cycle = 0.8 * ema_cycle + 0.2 * max(t0 - prev, 1e-3)
                    with self._lock:
                        self._loop_fps = 1.0 / ema_cycle
                prev = t0

                frame, seq = self._cam.wait_frame(seq, timeout=1.0)
                if frame is None:
                    continue
                try:
                    active = self._motion_pass(frame)
                    self._detect_pass(frame, active)
                    self._age_out()
                except Exception as exc:  # noqa: BLE001 - one bad frame must not kill the thread
                    print(f"[Surround] frame skipped: {exc}")

                dt = time.monotonic() - t0
                period = 1.0 / self._fps
                if dt < period:
                    self._stop.wait(period - dt)
        finally:
            self._cam.release()
            self._emit("◎ Surround vision stopped")

    def _emit(self, text: str) -> None:
        if self._event_cb is not None:
            try:
                self._event_cb(text)
            except Exception:  # noqa: BLE001
                pass

    # ---- layer 1: motion sectors -------------------------------------------
    def _motion_pass(self, frame) -> list[int]:
        """Update per-sector motion energy; return sector indices worth a look."""
        small = cv2.resize(frame, (self.sectors * 24, 120))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if self._bg is None or self._bg.shape != gray.shape:
            self._bg = gray.copy()
            return []
        diff = np.abs(gray - self._bg)
        # Slow background so a person who stops moving fades in over ~5 s
        # rather than instantly becoming furniture.
        self._bg = 0.95 * self._bg + 0.05 * gray

        # Pixel diffs → robot-frame sectors. Column 0 is camera yaw -180;
        # rolling by front_yaw's column offset re-zeros the array on
        # cart-forward so sector maths never has to think about the mount.
        w = diff.shape[1]
        shift = int(round((getattr(self._cam, "front_yaw", 0.0) / 360.0) * w))
        diff = np.roll(diff, -shift, axis=1)
        per = w // self.sectors
        energies = []
        for i in range(self.sectors):
            band = diff[:, i * per:(i + 1) * per].ravel()
            k = max(1, band.size // 20)              # top 5% most-changed pixels
            top = np.partition(band, band.size - k)[band.size - k:]
            energies.append(float(top.mean()))
        with self._lock:
            self._motion = energies

        active = [i for i, e in enumerate(energies) if e >= self.motion_threshold]
        # Busiest first, capped: each gets a Haar pass, and Haar is the budget.
        active.sort(key=lambda i: -energies[i])
        active = active[:3]
        # Sustained motion with no detector hit still counts as a presence —
        # it's how someone walking behind FRED (no face to find) is tracked.
        now = time.monotonic()
        for i in active:
            self._merge_presence(self._sector_yaw(i), None, "motion", now)
        if self.sweep:
            self._sweep_at = (self._sweep_at + 1) % self.sectors
            if self._sweep_at not in active:
                active = active + [self._sweep_at]
        return active

    def _sector_yaw(self, i: int) -> float:
        span = 360.0 / self.sectors
        return _wrap_deg(-180.0 + (i + 0.5) * span)

    # ---- layer 2: targeted detection ---------------------------------------
    def _detect_pass(self, frame, sector_ids: list[int]) -> None:
        if self._face is None and self._body is None:
            return
        tile_w, tile_h, fov = 320, 240, 70.0
        fpx = (tile_w / 2.0) / math.tan(math.radians(fov / 2.0))
        now = time.monotonic()
        for i in sector_ids:
            yaw0 = self._sector_yaw(i)
            tile = self._cam.view(yaw=yaw0, pitch=0.0, fov=fov,
                                  out_size=(tile_w, tile_h), frame=frame)
            if tile is None:
                return
            gray = cv2.equalizeHist(cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY))
            for cascade, kind, real_m in ((self._face, "face", _FACE_M),
                                          (self._body, "body", _BODY_M)):
                if cascade is None:
                    continue
                try:
                    hits = cascade.detectMultiScale(
                        gray, scaleFactor=1.2, minNeighbors=5,
                        minSize=(self.min_face, self.min_face))
                except Exception:  # noqa: BLE001
                    hits = []
                for (x, y, hw, hh) in hits:
                    cx = x + hw / 2.0
                    yaw = _wrap_deg(yaw0 + math.degrees(
                        math.atan2(cx - tile_w / 2.0, fpx)))
                    dist = real_m * fpx / float(hh)      # pinhole: h_px = f*H/d
                    self._merge_presence(yaw, dist, kind, now)

    # ---- layer 3: track maintenance ----------------------------------------
    def _merge_presence(self, yaw: float, dist_m, kind: str, now: float) -> None:
        """Fold one observation into the track list (nearest-in-yaw within 20°)."""
        with self._lock:
            best, best_off = None, 20.0
            for p in self._presences:
                off = abs(_wrap_deg(p["yaw"] - yaw))
                if off < best_off:
                    best, best_off = p, off
            if best is None:
                self._presences.append(
                    {"yaw": yaw, "dist_m": dist_m, "kind": kind, "seen": now,
                     "born": now})
                fresh = True
            else:
                # Smooth the yaw through the wrap (nudge by the wrapped delta).
                best["yaw"] = _wrap_deg(best["yaw"] + 0.4 * _wrap_deg(yaw - best["yaw"]))
                if dist_m is not None:
                    best["dist_m"] = (dist_m if best.get("dist_m") is None
                                      else 0.5 * best["dist_m"] + 0.5 * dist_m)
                # A detector hit upgrades a motion-only track, never the reverse.
                if kind != "motion":
                    best["kind"] = kind
                best["seen"] = now
                fresh = False
        if fresh and kind != "motion":
            self._emit(f"◎ Spotted someone {self._spoken_direction(yaw)}")

    def _age_out(self) -> None:
        cutoff = time.monotonic() - self.presence_ttl
        with self._lock:
            self._presences = [p for p in self._presences if p["seen"] >= cutoff]

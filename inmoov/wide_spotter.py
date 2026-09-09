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

**Watching it in the browser goes through here too, and has to.** A V4L2 node
streams to one opener; this class holds it, so a second ``VideoCapture`` for the
panel would simply fail — and stopping the spotter to look through it would
switch off the thing that finds people in order to watch for people. So the
loop publishes JPEGs itself, and ``frames()`` hands them to the panel.

That is opt-in and viewer-counted for the reason in the paragraph above: a
retrieve plus a JPEG encode is exactly the cost the grab/retrieve split exists
to avoid, so with nobody watching, not one extra frame is decoded and the idle
cost is unchanged. When somebody is watching, the decode is shared — a frame
retrieved for detection is published as well, rather than pulled twice.

Degrades to silence: if OpenCV, the cascade, or the camera are missing,
``available()`` is False and ``bearing()`` returns None forever, which the
tracker already treats as "no opinion".
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
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
# When every grab() has failed for this long, the camera is gone — unplugged,
# renumbered, or wedged — and the loop moves from "one dropped frame" to
# reopening the device. Failed grabs return immediately, so this is elapsed
# time, not frame count.
LOST_AFTER = 2.0
# How often to retry the reopen while the camera stays gone.
REOPEN_DELAY = 2.0

PANORAMA = (3840, 1080)
_PANORAMA_MIN_ASPECT = 3.0

# What a browser viewer gets. 3840 wide is four times what any panel shows it in
# and would cost more to encode than to detect on, so frames are downscaled;
# 1280x360 still resolves a face across the whole arc. 10 Hz because this is a
# room view, not a driving feed — and every one of these frames is a decode the
# idle spotter does not do, which is why nothing here runs without a viewer.
VIEW_HZ = 10.0
VIEW_WIDTH = 1280
VIEW_QUALITY = 70


V4L_SYSFS = "/sys/class/video4linux"


def _video_nodes(root: str = V4L_SYSFS) -> list[str]:
    """Every /dev/videoN the kernel currently has, as "videoN: name" strings."""
    out = []
    for path in sorted(Path(root).glob("video*")):
        try:
            out.append(f"{path.name}: {(path / 'name').read_text().strip()}")
        except OSError:
            continue
    return out


def _hub_port(node: int) -> tuple[str, str] | None:
    """(hub location, port) for /dev/videoN, as uhubctl names them.

    Read out of the sysfs path rather than parsed from uhubctl's output: the
    kernel already knows, and "4-1.4" means port 4 of hub 4-1 by construction.
    A device straight on a root hub is "4-2" — bus 4, port 2 — and has no dot.
    """
    try:
        dev = Path(f"{V4L_SYSFS}/video{node}/device").resolve()
    except OSError:
        return None
    for parent in [dev, *dev.parents][:5]:
        name = parent.name
        if not re.fullmatch(r"\d+-\d+(\.\d+)*", name):
            continue
        if "." in name:
            hub, _, port = name.rpartition(".")
            return hub, port
        bus, _, port = name.partition("-")
        return bus, port
    return None


def power_cycle_camera(node: int, delay: float = 2.0, log=None) -> str:
    """Cut power to the camera's USB port and bring it back. "" on success.

    The software equivalent of pulling the lead out, which is the one thing that
    fixed a PanaCast that opened and then delivered nothing. Neither of the
    obvious alternatives does this: toggling sysfs ``authorized`` is a logical
    disconnect and reloading uvcvideo re-binds the driver, and both leave the
    device powered the whole time.

    Needs a hub with per-port power switching, which is not universal — it is
    reported as "ppps" by uhubctl and this one has it. On a hub without it there
    is no software answer and the lead really does have to come out.

    ``-e`` matters: without it uhubctl pairs the USB2 and USB3 halves of a hub
    on purpose, and cycling one port here also cycled the matching port on the
    other hub. That was harmless because it was empty, and would not have been.
    """
    where = _hub_port(node)
    if where is None:
        return "couldn't work out which USB port the camera is on"
    hub, port = where
    argv = ["sudo", "-n", "uhubctl", "-l", hub, "-p", port, "-e",
            "-a", "cycle", "-d", str(delay)]
    if log:
        log(f"[spotter] power-cycling USB {hub} port {port}")
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                           check=False)
    except FileNotFoundError:
        return "uhubctl is not installed (apt install uhubctl)"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    if r.returncode != 0:
        return ((r.stderr or r.stdout or "").strip().splitlines() or
                [f"uhubctl exited {r.returncode}"])[-1]
    # It comes back in a USB3 low-power state and refuses to open for a few
    # seconds. Measured: an immediate retry failed, eight seconds later it
    # streamed. Waiting here rather than in the caller keeps "it is back" and
    # "it is ready" the same event.
    time.sleep(6.0)
    return ""


def _resolve_device(device, root: str = V4L_SYSFS) -> int | None:
    """Turn a configured device into a /dev/videoN number, or None.

    An integer is taken literally, which is what it always did. A *string* is
    matched against the card names in sysfs — and that is the one worth using,
    because a V4L2 index is not a stable name for a camera. This one moved on
    its own: the PanaCast came up as video1/video2 after the audio hardware
    changed, and a config pinned to index 0 pointed at a node that no longer
    existed. The panel showed the camera greyed out and the only clue was one
    line at startup.

    A camera can present several nodes and only one of them streams — the
    PanaCast offers two — so among matches, the one the *device* calls its own
    index 0 is taken. That is the kernel's own answer to "which is the real
    one", and it does not move when the numbering does.
    """
    if isinstance(device, int) or (isinstance(device, str) and device.isdigit()):
        return int(device)
    want = str(device).strip().lower()
    if not want:
        return None
    best: tuple[int, int] | None = None       # (device's own index, node number)
    for path in sorted(Path(root).glob("video*")):
        try:
            name = (path / "name").read_text().strip().lower()
        except OSError:
            continue
        if want not in name:
            continue
        try:
            own = int((path / "index").read_text().strip())
        except (OSError, ValueError):
            own = 0
        node = int(path.name.removeprefix("video"))
        if best is None or (own, node) < best:
            best = (own, node)
    return best[1] if best else None


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
    view_hz, view_width, view_quality :
        The browser feed served by ``frames()``. Costs nothing until somebody
        actually watches.
    """

    def __init__(self, device: int = 0, *, detect_hz: float = 4.0,
                 detect_width: int = 1920, cascade_path: str = CASCADE_PATH,
                 min_face_px: int = 24, stale_after: float = 2.0,
                 size: tuple[int, int] = PANORAMA,
                 view_hz: float = VIEW_HZ, view_width: int = VIEW_WIDTH,
                 view_quality: int = VIEW_QUALITY, log=None):
        # An index or a name. See _resolve_device: a V4L2 index is not a stable
        # way to name a camera, and this one moved without anybody touching it.
        self.device = device
        self.detect_hz = float(detect_hz)
        self.detect_width = int(detect_width)
        self.min_face_px = int(min_face_px)
        self.stale_after = float(stale_after)
        self.size = size
        self.view_hz = float(view_hz)
        self.view_width = int(view_width)
        self.view_quality = int(view_quality)
        self.last_error: str | None = None if cv2 is not None else str(_CV2_ERR)
        # What to *do* about last_error, when there is something worth saying.
        # Separate from the error so the UI can show the diagnosis and the
        # remedy differently, and so a message meant for a person does not have
        # to be squeezed into a log line.
        self.last_hint: str | None = None
        self._log = log

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

        # The browser feed, on its own condition rather than _lock: a viewer
        # blocks here for up to seconds waiting for the next frame, and holding
        # the lock the tracker reads bearings through while doing so would stall
        # the one consumer that must never wait.
        self._view = threading.Condition()
        self._viewers = 0
        self._frame: bytes | None = None
        self._seq = 0

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

    def _open(self):
        """Resolve, open, and verify the camera. Returns a delivering capture,
        or None with last_error/last_hint saying why. Shared by start() and the
        reopen path in _run(), because the device can die *after* start too —
        and when it does, the node may have been renumbered on the way back, so
        every open re-runs the name resolution from scratch.
        """
        self.last_hint = None
        node = _resolve_device(self.device)
        if node is None:
            self.last_error = (f"no camera matching {self.device!r} "
                               f"(have: {', '.join(_video_nodes()) or 'none'})")
            return None
        cap = cv2.VideoCapture(node, cv2.CAP_V4L2)
        if not cap.isOpened():
            self.last_error = f"cannot open /dev/video{node}"
            return None
        # MJPG first: the panorama is not offered as raw YUV at this size.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            # The one failure worth spelling out, because it looks like a dead
            # camera and is not. The device enumerates, the driver binds, the
            # node opens — and select() times out on every format at every
            # resolution, as root, with nothing logged by USB or UVC.
            #
            # What fixes it is cutting power. Toggling sysfs "authorized" and
            # reloading uvcvideo both re-enumerate and neither does that, and
            # neither worked; pulling the lead did. recover() now does the same
            # thing without the lead, on a hub that can switch port power.
            #
            # Link speed is deliberately *not* mentioned as a clue. It looked
            # like one — the camera reads 5 Gbps on a 10 Gbps hub — but it
            # streams perfectly happily at 5, so saying so would only send the
            # next person after the wrong thing.
            self.last_error = f"/dev/video{node} opened but delivered no frame"
            self.last_hint = (
                "The camera is plugged in and answering, but sending no video. "
                "Power-cycling its USB port fixes this; use the button, and if "
                "that fails, unplug the lead and plug it back in. Restarting "
                "FRED will not help — the camera needs its power cut, and "
                "nothing in software does that except the port switch.")
            return None
        h, w = frame.shape[:2]
        self._frame_wh = (w, h)
        if w / max(h, 1) < _PANORAMA_MIN_ASPECT:
            # Not fatal — a crop still spots faces — but the bearing then spans a
            # much narrower arc than 180 degrees, so say so rather than pretend.
            self.last_error = (f"got {w}x{h} (aspect {w/h:.2f}), not the "
                               f"panorama — bearing covers a narrower arc")
        return cap

    def start(self) -> bool:
        if not self.available() or self.is_running():
            return False
        cap = self._open()
        if cap is None:
            return False
        self._cap = cap
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wide-spotter",
                                        daemon=True)
        self._thread.start()
        return True

    def recover(self) -> dict:
        """Power-cycle the camera's USB port and try to start again.

        The whole point of finding this out once: a camera that opens and
        delivers nothing is fixed by cutting its power, and now that does not
        need anybody to be standing next to the robot with a cable.
        """
        was_running = self.is_running()
        self.stop()
        node = _resolve_device(self.device)
        if node is None:
            return {"ok": False, "error": f"no camera matching {self.device!r}"}
        problem = power_cycle_camera(node, log=self._log)
        if problem:
            return {"ok": False, "error": problem}
        started = self.start()
        return {"ok": started, "running": started, "restarted": was_running,
                "error": None if started else self.last_error,
                "hint": None if started else self.last_hint}

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
        with self._view:
            self._frame = None       # do not hand a stopped camera's last frame
            self._view.notify_all()  # to the next viewer; wake the ones waiting

    # ---- the loop ---------------------------------------------------------
    def _reopen(self) -> bool:
        """The camera died mid-run: drop it, say so, and try to get it back.

        This exists because the old loop spun on a failed grab() forever — no
        reopen, no error, status still saying running: true — so an unplugged
        or renumbered PanaCast (this project's known failure class) read as a
        healthy spotter that mysteriously saw nobody. The stale bearing is
        dropped immediately rather than left to age out: readings from a
        camera that is gone are not merely old, they are unowned.

        One reopen attempt per call; the loop paces the retries. True when the
        camera is delivering again.
        """
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:               # noqa: BLE001 - it's already gone
                pass
            self._cap = None
        with self._lock:
            self._bearing, self._faces = None, 0
        if self.last_error is None:
            self.last_error = "camera stopped delivering frames — reopening"
            if self._log:
                self._log(f"[spotter] {self.last_error}")
        cap = self._open()                  # re-resolves the node by name too
        if cap is None:
            return False
        if self._stop.is_set():             # stop() ran while we were opening
            cap.release()
            return False
        self._cap = cap
        self.last_error = None
        if self._log:
            self._log("[spotter] camera back — resuming")
        return True

    def _run(self) -> None:
        detect_interval = 1.0 / max(self.detect_hz, 0.1)
        view_interval = 1.0 / max(self.view_hz, 0.1)
        next_detect = 0.0
        next_view = 0.0
        failing_since = None                # monotonic start of a grab-failure run
        while not self._stop.is_set():
            if self._cap is None:           # lost earlier; retry on a slow clock
                if not self._reopen():
                    self._stop.wait(REOPEN_DELAY)
                continue
            # Cheap: pulls the frame off the device without decoding it, which
            # is what keeps the buffer from going stale between detections.
            if not self._cap.grab():
                now = time.monotonic()
                if failing_since is None:
                    failing_since = now
                if now - failing_since >= LOST_AFTER:
                    failing_since = None
                    if not self._reopen():
                        self._stop.wait(REOPEN_DELAY)
                else:
                    time.sleep(0.05)
                continue
            failing_since = None
            now = time.monotonic()
            want_detect = now >= next_detect
            with self._view:
                want_view = self._viewers > 0 and now >= next_view
            if not (want_detect or want_view):
                continue
            # ONE retrieve serves both. Decoding is the expensive half, so when
            # somebody is watching, detection rides along on a frame that was
            # going to be decoded anyway instead of paying for its own.
            ok, frame = self._cap.retrieve()
            if not ok or frame is None:
                continue
            if want_detect:
                next_detect = now + detect_interval
                self._detect(frame)
            if want_view:
                next_view = now + view_interval
                self._publish(frame)

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

    # ---- what the panel consumes -----------------------------------------
    def _publish(self, frame) -> None:
        """Downscale + JPEG-encode one frame for whoever is watching."""
        h, w = frame.shape[:2]
        scale = self.view_width / float(w)
        if scale < 1.0:
            frame = cv2.resize(frame, (self.view_width, max(int(h * scale), 1)))
        ok, buf = cv2.imencode(".jpg", frame,
                               [int(cv2.IMWRITE_JPEG_QUALITY), self.view_quality])
        if not ok:
            return
        with self._view:
            self._frame = buf.tobytes()
            self._seq += 1
            self._view.notify_all()

    def viewers(self) -> int:
        with self._view:
            return self._viewers

    def frames(self):
        """Yield JPEG frames until the caller stops consuming. Mirrors
        ``Camera.frames()`` so the panel's MJPEG route treats both the same.

        Does NOT start the spotter. The camera being off is a decision made
        elsewhere and quietly
        retaking a device another process now owns is how you get a fight over
        it. If it is not running, say so.
        """
        if not self.is_running():
            raise RuntimeError(self.last_error or "wide camera is not running")
        with self._view:
            self._viewers += 1
            last = self._seq       # only frames from now on: publishing stops
        try:                       # with the last viewer, so _frame may be old
            while not self._stop.is_set():
                with self._view:
                    if not self._view.wait_for(lambda: self._seq != last,
                                               timeout=5.0):
                        continue           # keep-alive rather than hang up
                    last = self._seq
                    frame = self._frame
                if frame:
                    yield frame
        finally:
            with self._view:
                self._viewers = max(0, self._viewers - 1)

    def snapshot(self, timeout: float = 3.0) -> bytes | None:
        """One fresh JPEG. Registers as a viewer for as long as it takes, since
        the loop publishes nothing when nobody is watching."""
        if not self.is_running():
            return None
        with self._view:
            self._viewers += 1
            last = self._seq
        try:
            with self._view:
                if not self._view.wait_for(lambda: self._seq != last,
                                           timeout=timeout):
                    return None
                return self._frame
        finally:
            with self._view:
                self._viewers = max(0, self._viewers - 1)

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
        viewers = self.viewers()        # before _lock, not inside it: the only
        with self._lock:                # place the two locks would ever nest
            fresh = (self._bearing is not None
                     and (time.monotonic() - self._bearing_at) <= self.stale_after)
            return {"available": self.available(), "running": self.is_running(),
                    "faces": self._faces,
                    "bearing": self._bearing if fresh else None,
                    "size": list(self._frame_wh) if self._frame_wh else None,
                    "detect_hz": self.detect_hz,
                    "detect_ms": round(self._detect_ms, 1),
                    "viewers": viewers,
                    "error": self.last_error,
                    "hint": self.last_hint}

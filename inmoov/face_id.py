"""Session-scoped face recall — "weren't you just asking me about servos?"

FRED is taken to public STEM events, mostly full of children, so this had one
hard requirement before it had any features: **nothing biometric may ever touch
disk**. There is no file in this module. Nothing is written, cached, pickled or
logged; the only thing that leaves it is a small integer and whatever the
visitor themselves said out loud. Everything here dies when the process does,
and — more to the point — it dies long before that, on the same idle timeout the
conversation memory uses (``Brain.HISTORY_IDLE_SECS``). When FRED forgets what
the last visitor was talking about, he forgets their face in the same breath.
That is not a coincidence of implementation: the whole point of the feature is
to hold a *conversation* together, so it should live exactly as long as the
conversation does and not one second longer.

What is actually stored per visitor is a **HOG descriptor** — a 1764-float
histogram of gradient directions over a 64x64 crop. It is not a picture and it
is not an identity: it cannot be looked up anywhere, it is never compared
against anything outside this process, and there is no name attached to it, only
"visitor 3". It is worth being straight about the one caveat: HOG is not a
one-way hash, and academic work (Vondrick's HOGgles) can invert one into a
blurry smudge that resembles the original crop. That is why the rule is
memory-only rather than "it's only a descriptor, it's fine".

Why OpenCV's own tools and not a real face-recognition network
--------------------------------------------------------------
Because dlib/face_recognition are not installed and a 37 MB SFace ONNX file is
still a model on disk. What *is* here is opencv-contrib, so the candidates were
raw pixels, DCT, LBP (including ``cv2.face.LBPHFaceRecognizer``) and HOG, and
all of them are weak by 2015 standards, let alone today's. Measured on ORL/AT&T
(40 people, 10 shots each, put through a jitter that emulates a Haar box on a
live stream) at a 1%-false-accept operating point, **a single frame gets you
nothing worth shipping** — 15% true-accept for raw pixels, 24% for LBP, 28% for
HOG. That is the honest headline: one look at one frame cannot tell two children
apart.

What makes it work is that FRED does not get one frame. Somebody talking to him
stands in front of a camera that is already running, so the unit of recognition
here is a *burst* of frames, and a burst is a different problem entirely. Same
data, same descriptor, scoring a burst against a gallery (see ``_match``):

    frames per burst      1      3      6     10     15
    true-accept @1% FAR   .34    .62    .83    .93    .94   (ORL + jitter)

The other half of the gain was preprocessing. Cropping to the inner 72% of the
Haar box (throwing away hair and background, which are what a person changes by
turning round) and running Tan-Triggs illumination normalisation instead of a
plain histogram equalisation took the hardest test — LFW, where a person's shots
come from different photographs, years and lighting — from .66 to .85 at 10
frames. It also did something more useful than raising a score: it put the two
datasets on the *same distance scale* (impostor median .163 on ORL vs .158 on
LFW), which is what makes a fixed threshold shippable at all. Without it, a
number tuned on one kind of room means nothing in another.

So the defaults below are set from measurement, at a deliberately timid
operating point (``MATCH_DIST`` = 0.10, ten frames a burst):

    ORL, session-like (same room, minutes apart)   74% recall, 0.14% false-merge
    LFW, different photo/year/lighting             20% recall, 0.00% false-merge

Both halves of that are the design. The recall we want is the first row: a
visitor who comes back to the same spot two minutes later. The second row is the
one we deliberately do *not* want to be good at, and it lines up with the
privacy rule — this could not recognise somebody tomorrow even if the memory
survived, which it does not.

The asymmetry is the reason for the timidity. A miss is invisible: FRED just
greets someone normally. A false merge means telling a child he remembers a
conversation they never had, in front of their parents. So the threshold is set
where false merges are rare rather than where recall is best, a match must also
beat the runner-up by ``MATCH_MARGIN``, and a burst must be several frames deep
before it is allowed to claim anything at all.

Degrades gracefully, the same way ``FaceTracker`` does: ``available()`` is False
when OpenCV or the cascade are missing, ``observe()`` returns None forever, and
nothing else in the robot notices.

``tools/test_face_id.py`` checks the promises (including the no-disk one, which
it enforces rather than trusts), re-measures the numbers above against any
folder of faces, and will run the whole thing live off FRED's own camera.
"""
from __future__ import annotations

import threading
import time

try:
    import cv2
    import numpy as np
    _CV_ERR = None
except Exception as exc:  # noqa: BLE001 - no OpenCV just means "no face recall"
    cv2 = None
    np = None
    _CV_ERR = exc

from .face_tracker import CASCADE_PATH

# How long a visitor survives with nobody looking at them. Mirrors
# Brain.HISTORY_IDLE_SECS, and Brain passes its own value in — this default only
# matters to a FaceId built on its own (the test tool). The two must not drift:
# a face outliving the conversation it belongs to is the failure this feature is
# most likely to be blamed for.
IDLE_SECS = 180.0

# Cosine distance between two bursts, below which they are the same person. See
# the module docstring for where 0.10 comes from and what it buys.
MATCH_DIST = 0.10
# ...and how far the best match must beat the second-best. With several people
# in memory the per-comparison false-accept rate is paid once per visitor, so
# this is what stops a face that is vaguely close to two records picking one.
# Worth knowing what it does *not* buy: raising it to 0.04 changed nothing in
# the end-to-end runs, because the wrong matches that survive are not the
# hesitant ones. Two people who genuinely look alike produce a confident wrong
# answer with the runner-up far behind, and no decision rule downstream of the
# descriptor can see the difference. This guards ambiguity, not resemblance.
MATCH_MARGIN = 0.02

# Frames in the burst further than this from the newest one are not of the
# person standing there now, so they stop voting. Without it, the burst spans
# the changing of the guard: when one child steps aside and the next steps up,
# for a few seconds the burst holds both, and the *previous* visitor's frames
# are enough to match the previous visitor — a wrong greeting delivered by
# arithmetic. Measured on same-visit frame pairs, a 0.16 radius keeps 80% of a
# person's own frames and throws out 93% of somebody else's, which is the right
# trade for a filter whose job is to be roughly right immediately.
BURST_COHERE = 0.16
# Below this, two descriptors are the same *frame*, not two frames of the same
# person. The camera publishes its newest frame and never blocks, so a stalled
# feed hands the sampler the same picture over and over — and a burst of five
# copies of one frame would satisfy every "five separate frames agreed" rule in
# here while being a single observation. Sensor noise alone puts two genuinely
# different frames of somebody sitting still three orders of magnitude above
# this, so it only ever catches a literal repeat.
DUP_EPS = 1e-5
# ...and a gap this long with no face at all ends the burst outright. Somebody
# walked away; whoever appears next is a new question, not a continuation.
BURST_GAP = 3.0

# A burst this deep can be matched against the gallery; NEW_SAMPLES (deeper) is
# what it takes to open a *new* record. Asymmetric on purpose: recognising is
# cheap to get wrong in one direction only, but a record created from two stray
# detections is a phantom visitor that then competes for every later match.
MIN_SAMPLES = 5
NEW_SAMPLES = 6
# Frames older than this are not part of "who is in front of me now". Long
# enough to accumulate a burst while someone asks a question, short enough that
# the person who walked off ten seconds ago isn't still voting.
BURST_SECS = 20.0
# A gap this long before seeing the same face again counts as them coming *back*
# rather than still being here — which is what turns a match into "weren't you
# just asking me about servos?". Comfortably longer than one answer takes.
SIGHTING_GAP = 30.0

# Per-visitor descriptors kept. Twelve 1764-float vectors is 85 KB a head, so
# this is a coverage limit (enough poses to recognise from), not a memory one.
GALLERY_MAX = 12
# A new descriptor only joins the gallery if it is at least this far from
# everything already in it — otherwise thirty near-identical frames of someone
# holding still would crowd out the one shot of them turned slightly away.
GALLERY_NOVELTY = 0.02
# Visitors held before the least-recently-seen is dropped. A queue at an event
# is a rolling handful of people; anyone older than the last dozen has left.
MAX_VISITORS = 12

# Smaller than this (in the frame we are given) and the crop is too coarse to
# describe. Measured on LFW rescaled until the detected box was ~54 px: the
# genuine/impostor distributions still separate, so 48 is the floor rather than
# a comfortable working size.
MIN_FACE_PX = 48

# The face crop the descriptor sees, and how much of the Haar box it keeps.
# 0.72 throws away hair, ears and background — the parts that change when a
# person turns, and the parts that describe the room rather than the face.
CROP = 64
CROP_FRAC = 0.72

# How often the attend loop grabs a frame. A Haar pass over a 320x240 lores
# frame costs 5.5 ms here, so 2 Hz is ~1% of one core, and ten frames — the
# depth where recognition actually works — takes five seconds of somebody
# standing there talking. Faster buys little: consecutive frames of a person
# holding still are nearly the same descriptor (see GALLERY_NOVELTY).
SAMPLE_HZ = 2.0
# How long one attend() keeps the sampler awake. Refreshed on every utterance,
# so it runs through a conversation and stops itself shortly after one ends.
ATTEND_SECS = 25.0

# What a visitor is allowed to have said, and how much of it we keep. This is
# the conversational half of the feature ("you asked me about servos") and it is
# under exactly the same memory-only rule as the descriptors.
NOTES_MAX = 3
NOTE_CHARS = 80


class Visitor:
    """One person FRED has seen during this session. No name, just a number."""

    __slots__ = ("id", "first_seen", "last_seen", "sightings", "_gallery", "_notes")

    def __init__(self, vid: int, now: float):
        self.id = vid
        self.first_seen = now
        self.last_seen = now
        self.sightings = 1          # bursts, not frames: one per time they show up
        self._gallery: list = []
        self._notes: list[str] = []

    @property
    def notes(self) -> list[str]:
        return list(self._notes)

    def remember(self, text: str) -> None:
        """Keep a line this visitor said, so FRED can refer back to it."""
        text = " ".join((text or "").split())[:NOTE_CHARS]
        if not text or text in self._notes:
            return
        self._notes.append(text)
        del self._notes[:-NOTES_MAX]

    def _add(self, desc) -> None:
        """Add a descriptor if it shows the face from a usefully new angle."""
        if self._gallery:
            g = np.stack(self._gallery)
            if float(1.0 - (g @ desc).max()) < GALLERY_NOVELTY:
                return
        self._gallery.append(desc)
        if len(self._gallery) > GALLERY_MAX:
            # Drop the oldest rather than the closest: the early frames are of
            # the pose they walked up in, and by the time there are thirteen the
            # gallery has better coverage of where they are standing now.
            self._gallery.pop(0)

    def _wipe(self) -> None:
        """Zero the descriptors before letting go of them.

        Belt and braces — the process boundary is what actually guarantees this,
        and Python may well have copied them anyway. But leaving a face
        descriptor sitting in a freed buffer for the allocator to hand out is a
        bad look for a feature whose entire premise is that it forgets.
        """
        for d in self._gallery:
            d.fill(0)
        self._gallery.clear()
        self._notes.clear()

    def info(self) -> dict:
        return {"id": self.id, "sightings": self.sightings,
                "known_for": round(self.last_seen - self.first_seen, 1),
                "notes": self.notes}


class FaceId:
    """Remembers faces for as long as one conversation lasts, and no longer."""

    def __init__(self, *, idle_secs: float = IDLE_SECS,
                 threshold: float = MATCH_DIST, margin: float = MATCH_MARGIN,
                 cascade_path: str = CASCADE_PATH):
        self.idle_secs = float(idle_secs)
        self.threshold = float(threshold)
        self.margin = float(margin)
        self._lock = threading.RLock()
        # A second, narrower lock for the OpenCV objects. Two threads reach
        # _describe — the sampler, and the vision tool handing over the frame it
        # just took — and a CascadeClassifier is not documented as safe to call
        # from both at once. Separate from _lock so a 5 ms detection doesn't
        # block status().
        self._cv_lock = threading.Lock()
        self._visitors: list[Visitor] = []
        self._next_id = 1
        # The frames of "right now", each (monotonic, descriptor). Unattributed
        # until there are enough of them to be worth matching.
        self._burst: list[tuple[float, object]] = []
        self._current: Visitor | None = None
        self._faces_seen = 0            # frames a face was found in, this session
        self._frames = 0                # frames looked at, this session

        self._cascade = None
        self._hog = None
        if cv2 is not None:
            try:
                c = cv2.CascadeClassifier(cascade_path)
                self._cascade = c if not c.empty() else None
            except Exception:  # noqa: BLE001
                self._cascade = None
            # 64x64 window, 16x16 blocks on an 8-px stride, 8x8 cells, 9 bins:
            # 1764 floats. The defaults from the pedestrian detector, which is
            # fine — nothing here is using its trained SVM, only its features.
            self._hog = cv2.HOGDescriptor((CROP, CROP), (16, 16), (8, 8), (8, 8), 9)

        # attend() loop
        self._source = None
        self._hold = None
        self._until = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- capability / introspection ---------------------------------------
    def available(self) -> bool:
        return cv2 is not None and self._cascade is not None

    def status(self) -> dict:
        with self._lock:
            self._prune(time.monotonic())
            out = {"available": self.available(),
                   "visitors": [v.info() for v in self._visitors],
                   "current": self._current.id if self._current else None,
                   "burst": len(self._burst),
                   "frames": self._frames, "faces": self._faces_seen,
                   "sampling": self._thread is not None and self._thread.is_alive(),
                   "idle_seconds": self.idle_secs, "threshold": self.threshold}
        if not out["available"] and _CV_ERR is not None:
            out["reason"] = "OpenCV not available"
        return out

    # ---- the one thing that matters ---------------------------------------
    def forget_all(self) -> None:
        """Forget every face and everything anyone said. The whole point."""
        with self._lock:
            for v in self._visitors:
                v._wipe()
            self._visitors.clear()
            for _, d in self._burst:            # the unattributed frames too
                d.fill(0)
            self._burst.clear()
            self._current = None

    def _prune(self, now: float) -> None:
        """Drop anyone nobody has seen for ``idle_secs``. Caller holds the lock.

        Wall-clock, not conversation-driven: a visitor has to expire even if
        nobody ever speaks to FRED again, because "he forgets after three
        minutes" has to be true unconditionally to be worth saying out loud.
        """
        stale = [v for v in self._visitors if now - v.last_seen > self.idle_secs]
        for v in stale:
            v._wipe()
            self._visitors.remove(v)
            if self._current is v:
                self._current = None
        cut = now - BURST_SECS
        self._burst = [(t, d) for (t, d) in self._burst if t >= cut]
        if not self._burst:
            self._current = None

    def _recohere(self, now: float, desc) -> None:
        """Keep only the frames that still look like whoever is there *now*.

        Two ways the burst goes stale, and both end with FRED greeting the
        wrong child: a gap where nobody was in frame (they left), and a swap
        where the next person walked up before the old frames aged out.
        Caller holds the lock.
        """
        if not self._burst:
            return
        if now - self._burst[-1][0] > BURST_GAP:
            self._burst.clear()
            self._current = None
            return
        keep = [(t, d) for (t, d) in self._burst
                if 1.0 - float(d @ desc) <= BURST_COHERE]
        if len(keep) != len(self._burst):
            self._burst = keep
            if not keep:
                self._current = None

    # ---- looking ----------------------------------------------------------
    def observe(self, gray) -> Visitor | None:
        """Take one grayscale frame; return whoever is in front of the camera.

        Returns None when there is no face, when the burst is still too thin to
        say anything, or when this capability isn't available at all. A None is
        never "I don't recognise them" — it is "no opinion", and the caller must
        treat it as such, because the one unacceptable outcome is FRED claiming
        to remember somebody he has never seen.
        """
        if not self.available() or gray is None:
            return None
        now = time.monotonic()
        with self._lock:
            self._frames += 1
        desc = self._describe(gray)
        if desc is None:
            return None
        with self._lock:
            self._faces_seen += 1
            self._prune(now)
            if self._burst and 1.0 - float(self._burst[-1][1] @ desc) < DUP_EPS:
                return self._current       # the feed stalled; this is not new evidence
            self._recohere(now, desc)
            self._burst.append((now, desc))
            del self._burst[:-GALLERY_MAX]      # only the newest ones vote
            if len(self._burst) < MIN_SAMPLES:
                return None
            burst = np.stack([d for _, d in self._burst])
            who = self._match(burst)
            if who is None:
                if len(self._burst) < NEW_SAMPLES:
                    return None
                if len(self._visitors) >= MAX_VISITORS and not self._make_room(now):
                    return None
                who = Visitor(self._next_id, now)
                self._next_id += 1
                self._visitors.append(who)
                # Seed the record from the whole burst, not just this frame: a
                # new visitor whose gallery is one picture is a visitor nobody
                # can match against until they have stood there long enough to
                # fill it, which is most of the window they were going to be
                # here for. _add's novelty filter drops the near-repeats.
                for _, d in self._burst:
                    who._add(d)
            elif now - who.last_seen > SIGHTING_GAP:
                # They went away and came back. Measured as a gap rather than as
                # "not the one we were tracking", because the burst also empties
                # whenever the camera stops streaming, and a dropped feed is not
                # somebody leaving the room.
                who.sightings += 1
            who.last_seen = now
            who._add(desc)
            self._current = who
            return who

    def observe_jpeg(self, jpeg: bytes) -> Visitor | None:
        """``observe`` for a frame that arrived as JPEG (the vision tool's)."""
        if not self.available() or not jpeg:
            return None
        try:
            gray = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
        except Exception:  # noqa: BLE001 - a truncated frame is not an error here
            return None
        return self.observe(gray)

    def current(self) -> Visitor | None:
        """Who is in front of the camera right now, if the burst is fresh."""
        with self._lock:
            self._prune(time.monotonic())
            return self._current

    def remember(self, text: str) -> Visitor | None:
        """Attach a line to whoever FRED is looking at. No face, no note."""
        with self._lock:
            self._prune(time.monotonic())
            if self._current is None:
                return None
            self._current.remember(text)
            return self._current

    # ---- descriptor -------------------------------------------------------
    def _describe(self, gray):
        """Largest face in the frame -> one L2-normalised HOG descriptor.

        The largest face, like FaceTracker, because the person talking to FRED
        is the one standing closest. Everything about this crop is measured; see
        the module docstring for what each step was worth.
        """
        with self._cv_lock:
            try:
                g = cv2.equalizeHist(gray)      # for the detector only
                faces = self._cascade.detectMultiScale(
                    g, scaleFactor=1.2, minNeighbors=5,
                    minSize=(MIN_FACE_PX, MIN_FACE_PX))
            except Exception:  # noqa: BLE001 - a bad frame is not worth a traceback
                return None
            if len(faces) == 0:
                return None
            x, y, w, h = (int(v) for v in
                          max(faces, key=lambda f: int(f[2]) * int(f[3])))
            dx, dy = int(w * (1 - CROP_FRAC) / 2), int(h * (1 - CROP_FRAC) / 2)
            crop = gray[y + dy:y + h - dy, x + dx:x + w - dx]
            if crop.size == 0:
                return None
            crop = cv2.resize(crop, (CROP, CROP), interpolation=cv2.INTER_AREA)
            v = self._hog.compute(_tan_triggs(crop)).ravel().astype(np.float32)
        n = float(np.linalg.norm(v))
        if n < 1e-9:
            return None
        return v / n                            # so cosine distance is 1 - a·b

    def _match(self, burst):
        """Best visitor for this burst, or None for "nobody I know".

        Score each burst frame by its distance to the *nearest* gallery entry,
        then take the mean of the three smallest of those — so three separate
        frames have to agree. Scoring the three smallest cross-*pairs* instead
        reads a shade better on paper (78% vs 74% recall at this threshold) and
        is worse where it counts: one contaminating frame that survived
        _recohere can supply all three pairs on its own, and claim a visitor
        by itself. Neither the plain minimum (one lucky pair wins) nor the plain
        mean (a visitor turning their head drags it up) is usable.
        """
        best = second = 1e9
        who = None
        for v in self._visitors:
            if not v._gallery:
                continue
            d = 1.0 - burst @ np.stack(v._gallery).T     # (burst, gallery)
            per_frame = np.sort(d.min(axis=1))
            score = float(per_frame[:min(3, per_frame.size)].mean())
            if score < best:
                best, second, who = score, best, v
            elif score < second:
                second = score
        if who is None or best > self.threshold:
            return None
        if second - best < self.margin:
            # Close between two records: say nothing. This is the case where a
            # wrong answer is most likely and least defensible.
            return None
        return who

    def _make_room(self, now: float) -> bool:
        """Evict the least-recently-seen visitor. Caller holds the lock."""
        if not self._visitors:
            return True
        oldest = min(self._visitors, key=lambda v: v.last_seen)
        if oldest is self._current:
            return False
        oldest._wipe()
        self._visitors.remove(oldest)
        return True

    # ---- the sampler ------------------------------------------------------
    def attend(self, source, hold=None) -> bool:
        """"Somebody is talking to me" — sample faces for the next while.

        ``source`` is a callable returning a grayscale frame or None. Driven
        from the conversation rather than started once and left running,
        because that is when a face is worth having: FRED is being spoken to,
        so somebody is standing in front of him, and the loop stops itself
        shortly after they stop. Cheap enough that this is a nicety rather than
        a necessity (5.5 ms a frame at 2 Hz), but a camera loop that runs all
        day looking at people is exactly the thing this feature promised not to
        be.

        By default this does *not* start the camera. The frame source is
        expected to hand back None unless the sensor is already streaming for
        some other reason (face tracking, someone watching the panel), so face
        recall costs one Haar pass on frames that were being produced anyway,
        and contributes nothing to the decision of whether the camera runs at
        all. The cost of that choice is that on a robot with face tracking
        switched off it never sees anybody, and quietly does nothing.

        ``hold`` is the other option: a ``(acquire, release)`` pair — Camera's,
        in practice — held for the length of the attend window. Cheap in CPU
        (the mjpeg backend's decode is 0.75 ms a frame, ~1% of a core at 15 fps)
        but not free in the way that matters: it makes FRED's camera run
        because somebody spoke to him, and ``Camera.acquire`` deliberately does
        not light the viewer LED. That is the owner's call to make, not this
        module's, so the default is off.
        """
        if not self.available() or source is None:
            return False
        with self._lock:
            self._source = source
            self._hold = hold
            self._until = time.monotonic() + ATTEND_SECS
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="face-id",
                                            daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        """Stop sampling. Does not forget anything — that is forget_all()."""
        with self._lock:
            self._stop.set()
            t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        with self._lock:
            self._thread = None

    def _run(self) -> None:
        period = 1.0 / SAMPLE_HZ
        with self._lock:
            hold = self._hold
        if hold is not None:
            hold[0]()
        try:
            while not self._stop.is_set():
                with self._lock:
                    source, until = self._source, self._until
                if time.monotonic() >= until:
                    break
                try:
                    frame = source()
                except Exception:  # noqa: BLE001 - a camera hiccup must not kill the loop
                    frame = None
                if frame is not None:
                    try:
                        self.observe(frame)
                    except Exception:  # noqa: BLE001
                        pass
                self._stop.wait(period)
        finally:
            if hold is not None:
                hold[1]()               # a held camera must be given back
            with self._lock:
                self._thread = None


def _tan_triggs(g):
    """Tan & Triggs illumination normalisation: gamma, difference-of-Gaussians,
    then a two-pass contrast equalisation.

    This is the step that made a fixed threshold possible. With equalizeHist
    alone the two test sets sat on different distance scales — ORL (a dark
    studio background) ran genuine .060 / impostor .132, LFW (real rooms) ran
    .113 / .154, so a threshold tuned on either was wrong for the other. With
    this, the impostor medians line up at .163 and .158, which is the difference
    between a constant and a knob nobody can tune without a dataset.
    """
    f = np.power(g.astype(np.float32) / 255.0, 0.2)
    f = cv2.GaussianBlur(f, (0, 0), 1.0) - cv2.GaussianBlur(f, (0, 0), 2.0)
    f = f / np.power(np.mean(np.abs(f) ** 0.1), 10.0)
    f = f / np.power(np.mean(np.minimum(np.abs(f), 10.0) ** 0.1), 10.0)
    f = np.tanh(f / 10.0) * 10.0
    return cv2.normalize(f, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

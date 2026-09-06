"""Direction of arrival from the mic array — "someone is talking over there".

The reSpeaker Flex (XVF3800) beamforms toward whoever is speaking, and it will
tell you which way it steered. That is a bearing the camera cannot produce: it
works in the dark, behind the head, and while FRED is looking somewhere else.
This turns it into the same ``-1..+1 or None`` a face detector produces, so the
face tracker consumes it through the assist it already has.

**Third source, not a new mechanism.** ``FaceTracker`` already takes a coarse
bearing from the chest ultrasonics through ``bearing_cb``, deliberately
open-loop and one-shot — see its docstring. A voice slots into that same path
between the two sources that were already there:

  * the wide spotter first, because an actual face detector can tell a person
    from the furniture and is far more accurate;
  * this next, because a voice *is* a person, and a heard one is worth more than
    a shape at a range;
  * the ultrasonics last, which see something without knowing what it is.

Each returns None for "no opinion", which the tracker distinguishes from "aim
straight ahead", so the fall-through costs nothing.

**Which register.** The array will answer "which way was that" in four
different places and only one of them is the question we are asking. This cost
two wrong turns, so the whole map is written down:

``DOA_VALUE`` (resource 20, command 18)
    An integer angle and a speech byte. It lives in the **LED** resource — it is
    what drives the ring's direction mode — and it behaves like something meant
    to light an LED: on firmware 1.0.0 it returned a single value across 394
    reads and 22 separate detections before jumping. 1.0.3 unfroze it, but it
    still latches between a couple of coarse positions. Never a control signal.

``AEC_AZIMUTH_VALUES`` (resource 33, command 75)
    Four azimuths, and the command map names them: *beam 1, beam 2,
    free-running beam, auto-select beam*. They are per-beam, not per-microphone
    — one omnidirectional capsule cannot produce a direction at all, since
    direction only exists in the difference between capsules.

    Beams 1 and 2 are the **fixed** beams and sit still. The **free-running**
    beam is the trap: it has by far the widest spread, which looks like tracking
    and is not. Measured against ``AEC_SPENERGY_VALUES``, it wanders across a
    hundred degrees while the speech energy on every beam is exactly zero. It is
    chasing room noise. Choosing it by spread is choosing the noisiest signal.

``AEC_SPENERGY_VALUES`` (resource 33, command 80)
    Per-beam speech energy, and the honest gate. Above zero means speech on that
    beam; it reads tens of thousands during a sentence and exactly 0.0 between.

``AUDIO_MGR_SELECTED_AZIMUTHS`` (resource 35, command 11) — **what we use**
    Index 0 is the firmware's own considered answer: the command map calls it
    the processed DoA, and says it "uses speech energy to select from the DoA on
    each fixed beam to provide the direction of a speaker", returning NaN when
    neither fixed beam has speech in it. That is the combination across beams,
    already computed on the device, with the speech gate built in — so NaN *is*
    the "no opinion", and no separate flag is needed.

**The cost of that choice** is resolution: the processed DoA selects among the
fixed beams, so it is as coarse as they are placed. For pointing a head that is
the right trade — a stable, speech-gated left/ahead/right beats a continuous
angle that is mostly noise. ``AEC_FIXEDBEAMSAZIMUTH_VALUES`` (33, 81) is
read-write, so the fixed beams can be steered if finer is ever wanted.

``source`` selects between this and a raw beam, and ``tools/doa_probe.py`` shows
all of them side by side against speech energy, which is the only way to tell
tracking from noise.

**Open-loop like the ultrasonics, and for the same reason** — as long as the
array is on the *body*. It does not turn with the head, so turning does not
reduce the angle it reports, and a proportional loop would drive the neck into
its stop and hold it there. If the array is ever moved into the head, this
becomes a closed-loop error to null instead, and this module is the wrong shape
for it; see ``bearing()``.

**Degrades to nothing.** ``available()`` is False when pyusb is missing, the
device is absent, or udev has not granted access to the control interface, and
``start()`` is then a no-op. FRED runs exactly as before.

Protocol per Seeed's ``python_control/respeaker_get_doa.py`` — a vendor control
transfer on endpoint 0, no kernel driver and no vendor binary. The device keeps
streaming audio to ALSA throughout; this is a different interface on the same
device and the two do not contend.
"""
from __future__ import annotations

import collections
import math
import struct
import threading
import time

try:
    import usb.core
    import usb.util
    _USB_ERR = None
except Exception as exc:  # noqa: BLE001 - no pyusb just means "no DOA"
    usb = None
    _USB_ERR = exc

VENDOR_ID = 0x2886                 # Seeed
# Left as None so any Flex firmware variant is found: the product id changes
# with the channel/rate build (C16K6Ch is 0x001e) and pinning it would turn a
# firmware update into "the head stopped turning" with no error anywhere.
PRODUCT_ID = None

# Register: resource 20, command 18, four bytes back plus a status byte.
# Named rather than inlined because the numbers are meaningless on their own and
# appear nowhere else in this codebase to compare against.
_DOA_RESID, _DOA_CMDID, _DOA_LEN = 20, 18, 4
# Four little-endian floats, radians: one azimuth per beam — beam 1, beam 2,
# free-running, auto-select.
_AZ_RESID, _AZ_CMDID, _AZ_LEN = 33, 75, 16
_BEAMS = 4
# Two floats: [processed DoA, auto-select beam]. Index 0 is the one worth having
# — speech-energy-weighted across the fixed beams, NaN when nobody is talking.
_SEL_RESID, _SEL_CMDID, _SEL_LEN = 35, 11, 8
# Four floats: per-beam speech energy. 0.0 between sentences, tens of thousands
# during one. The gate for the raw-beam sources, which have none of their own.
_EN_RESID, _EN_CMDID, _EN_LEN = 33, 80, 16
_ENERGY_FLOOR = 1.0                # anything above 0 is speech, per the vendor

SOURCE_PROCESSED = "processed"     # AUDIO_MGR_SELECTED_AZIMUTHS[0]
SOURCE_BEAM = "beam"               # AEC_AZIMUTH_VALUES[beam], gated on energy
SOURCES = (SOURCE_PROCESSED, SOURCE_BEAM)
_VERSION_RESID, _VERSION_CMDID, _VERSION_LEN = 48, 0, 3
_CTRL_TIMEOUT_MS = 500             # not the vendor's 100s: this runs in a poll loop

# The device answers a read it is not ready for with this status rather than
# failing, and expects to be asked again. Treating it as an error throws away a
# perfectly good reading — measured here, AEC_AZIMUTH_VALUES answers 64 on 71%
# of first attempts, so most of the direction data was being discarded and the
# error line filled with "status 64".
#
# The backoff matters more than the attempt count. Retrying flat out does not
# work: 200 reads hammered with no sleep needed ~40 attempts each and still
# failed 47% of the time, because the servicer never gets to finish. A 10ms
# sleep between attempts succeeded 200/200 in two or three, for the same 16ms of
# wall time and a fifth of the USB traffic. So: wait, do not spin.
#
# Five attempts is 50ms worst case, comfortably inside the 100ms poll period at
# the default rate, against a worst case of three ever observed.
_RETRY_STATUS = 64
_RETRY_BACKOFF_S = 0.010
_MAX_ATTEMPTS = 5

# How far off-axis maps to a full-deflection hint. 90 degrees means someone at
# the robot's shoulder produces the same ±1 the wide spotter produces at the
# edge of its frame, which is what makes the two sources interchangeable to the
# tracker. Beyond it the value clamps rather than wrapping — see _to_bearing.
DEFAULT_SPAN_DEG = 90.0
DEFAULT_POLL_HZ = 10.0
# Longer than the spotter's 2.0s. A face is present continuously while someone
# stands there, but speech is bursty — the gaps between sentences are seconds
# long, and expiring inside one would make the head give up mid-conversation.
DEFAULT_STALE_AFTER = 4.0

# Which of the four azimuths to believe. See the module docstring: the array
# parks its idle beams, and this is the one measured to track a moving talker.
DEFAULT_BEAM = 2

# Speech-bearing samples to take the median of before believing a direction.
# At the default 10 Hz this costs half a second of lag, against a head turn that
# is a human-scale event anyway — and it is what stops one stray frame counting
# as somebody having moved. 1 disables the filter.
DEFAULT_SMOOTH_N = 5

TUNABLE = ("enabled", "mount_offset", "span_deg", "invert", "stale_after",
           "poll_hz", "beam", "smooth_n", "source")


def _wrap180(deg: float) -> float:
    """Fold a 0..360 compass angle into -180..+180, where 0 is straight ahead."""
    return (float(deg) + 180.0) % 360.0 - 180.0


def _circular_median(angles: list[float]) -> float:
    """The angle with the least total angular distance to the others.

    A plain median cannot be used on a compass: sorting [350, 355, 5] puts the
    middle at 350 when the answer is 355, and near the array's zero — which
    ``capture_forward`` deliberately puts *straight ahead* — that is the worst
    place to be wrong.

    A medoid also throws away a lone outlier rather than averaging it in, which
    is the point. Measured here, the tracking beam sits in the fifties and then
    reports a single sample at 117: one reflection, or one other noise in the
    room winning for a frame. Averaged, that drags the head; taken as a median,
    it disappears. The list is five long, so this is 25 comparisons and not
    worth being cleverer about.
    """
    return min(angles, key=lambda a: sum(abs(_wrap180(a - b)) for b in angles))


class MicDoa:
    """Polls the array's steer angle and presents it as a tracker bearing.

    Parameters
    ----------
    mount_offset : float
        Which raw angle, in degrees, is the robot's forward. Depends entirely on
        how the board is screwed in, so it is calibration rather than
        configuration — see ``capture_forward``, which measures it instead of
        asking anyone to work it out.
    span_deg : float
        Degrees off-axis that map to a full ±1 hint.
    invert : bool
        Flip left and right, for a board mounted upside down or facing back.
    stale_after : float
        Seconds a speech-detected reading stays worth acting on.
    beam : int
        Which of the four azimuths to believe. See the module docstring on why
        this is a setting and not a constant.
    """

    def __init__(self, *, enabled: bool = True, mount_offset: float = 0.0,
                 span_deg: float = DEFAULT_SPAN_DEG, invert: bool = False,
                 stale_after: float = DEFAULT_STALE_AFTER,
                 poll_hz: float = DEFAULT_POLL_HZ, beam: int = DEFAULT_BEAM,
                 smooth_n: int = DEFAULT_SMOOTH_N,
                 source: str = SOURCE_PROCESSED, log=None):
        self.enabled = bool(enabled)
        self.mount_offset = float(mount_offset)
        self.span_deg = max(1.0, float(span_deg))
        self.invert = bool(invert)
        self.stale_after = max(0.0, float(stale_after))
        self.poll_hz = max(0.5, float(poll_hz))
        self.beam = min(max(0, int(beam)), _BEAMS - 1)
        self.smooth_n = max(1, int(smooth_n))
        self.source = source if source in SOURCES else SOURCE_PROCESSED
        self._log = log
        # Only angles that arrived with speech on them: the beam wanders while
        # the room is merely noisy, and filtering that in would smooth toward
        # whatever the extractor fan is doing.
        self._recent: collections.deque[float] = collections.deque(
            maxlen=self.smooth_n)

        self._dev = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        # Last reading of any kind, so the calibration page can show the angle
        # live while somebody talks — including the angles that were too stale
        # or too quiet to act on, which is exactly what you need to see when
        # working out whether the mapping is right.
        self._angle: float | None = None    # raw, 0..360, as the device reports
        self._speech = False
        # All four, so the calibration page and doa_probe can show which beam is
        # actually moving rather than making anyone take `beam` on trust.
        self._beams: list[float] | None = None
        self._energy: list[float] | None = None
        self._processed: float | None = None    # NaN from the device -> None here
        self._heard_angle: float | None = None  # last angle with speech on it
        self._heard_at = 0.0                # monotonic
        self._reads = 0
        self._retries = 0            # extra attempts spent waiting on the servicer
        self._giveups = 0            # polls dropped because it stayed busy
        self._error = ""

    # -- device ------------------------------------------------------------
    def _find(self):
        if usb is None:
            return None
        kw = {"idVendor": VENDOR_ID}
        if PRODUCT_ID is not None:
            kw["idProduct"] = PRODUCT_ID
        try:
            return usb.core.find(**kw)
        except Exception:  # noqa: BLE001 - a broken libusb is "no device"
            return None

    def available(self) -> bool:
        """True if there is something to talk to. Does not mean it is running."""
        return self._find() is not None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _read(self, resid: int, cmdid: int, length: int) -> list[int] | None:
        """One vendor control read, retried while the device says "not yet".

        Returns the payload without its status byte, or None. Byte 0 is the
        device's own status: 0 is success, _RETRY_STATUS means it understood the
        request but was busy, and anything else is a genuine refusal worth
        reporting (a wrong length answers 66, for instance).
        """
        dev = self._dev
        if dev is None:
            return None
        for attempt in range(_MAX_ATTEMPTS):
            if attempt:
                time.sleep(_RETRY_BACKOFF_S)
            try:
                raw = dev.ctrl_transfer(
                    usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR
                    | usb.util.CTRL_RECIPIENT_DEVICE,
                    0, 0x80 | cmdid, resid, length + 1, _CTRL_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001
                self._note_error(f"{type(exc).__name__}: {exc}")
                return None
            data = raw.tolist()
            if not data:
                return None
            if data[0] == 0:
                if attempt:
                    with self._lock:
                        self._retries += attempt
                return data[1:]
            if data[0] != _RETRY_STATUS:
                self._note_error(f"device returned status {data[0]}")
                return None
        # Busy for every attempt. Not worth an error line — it is a dropped
        # sample on a source that is polled continuously, and the next one is
        # 100ms away — but it is counted, because a rising count means the
        # backoff above has stopped being enough.
        with self._lock:
            self._giveups += 1
        return None

    def firmware(self) -> str | None:
        """Version string, and the cheapest proof the control path works."""
        v = self._read(_VERSION_RESID, _VERSION_CMDID, _VERSION_LEN)
        return ".".join(str(b) for b in v) if v else None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> bool:
        if self.is_running():
            return True
        dev = self._find()
        if dev is None:
            self._note_error("no reSpeaker on the bus"
                             if usb is not None else f"pyusb missing ({_USB_ERR})")
            return False
        self._dev = dev
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mic-doa",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._thread = None
        dev, self._dev = self._dev, None
        if dev is not None and usb is not None:
            try:
                usb.util.dispose_resources(dev)
            except Exception:  # noqa: BLE001 - releasing a gone device is fine
                pass

    def _note_error(self, msg: str) -> None:
        with self._lock:
            if msg == self._error:
                return                      # once per transition, not per poll
            self._error = msg
        if self._log is not None:
            self._log(f"[mic-doa] {msg}")

    def _azimuths(self) -> list[float] | None:
        """The four beam azimuths, in degrees 0..360."""
        data = self._read(_AZ_RESID, _AZ_CMDID, _AZ_LEN)
        if data is None or len(data) < _AZ_LEN:
            return None
        rads = struct.unpack("<" + "f" * _BEAMS, bytes(data[:_AZ_LEN]))
        return [math.degrees(r) % 360.0 for r in rads]

    def _floats(self, resid: int, cmdid: int, nbytes: int) -> list[float] | None:
        data = self._read(resid, cmdid, nbytes)
        if data is None or len(data) < nbytes:
            return None
        return list(struct.unpack("<" + "f" * (nbytes // 4), bytes(data[:nbytes])))

    def _run(self) -> None:
        while not self._stop.is_set():
            beams = self._azimuths()
            sel = self._floats(_SEL_RESID, _SEL_CMDID, _SEL_LEN)
            energy = self._floats(_EN_RESID, _EN_CMDID, _EN_LEN)
            if beams is not None and sel is not None and energy is not None:
                # The processed DoA is NaN whenever no fixed beam holds speech.
                # That is the device saying "nobody is talking", and it is a
                # better gate than any flag because it is the same computation
                # that produced the angle.
                proc = sel[0] if not math.isnan(sel[0]) else None
                if self.source == SOURCE_PROCESSED:
                    angle = (math.degrees(proc) % 360.0) if proc is not None else None
                    speech = proc is not None
                else:
                    # A raw beam has no gate of its own — the free-running one
                    # wanders a hundred degrees through a silent room — so it
                    # borrows the per-beam speech energy.
                    speech = max(energy) > _ENERGY_FLOOR
                    angle = beams[self.beam] if speech else None
                now = time.monotonic()
                with self._lock:
                    self._speech, self._beams, self._energy = speech, beams, energy
                    self._processed = (math.degrees(proc) % 360.0
                                       if proc is not None else None)
                    self._reads += 1
                    self._error = ""
                    if angle is not None:
                        self._angle = angle
                        self._recent.append(angle)
                        self._heard_angle = _circular_median(list(self._recent))
                        self._heard_at = now
            self._stop.wait(1.0 / self.poll_hz)

    # -- the mapping -------------------------------------------------------
    def _to_bearing(self, angle: float) -> float:
        """Raw device angle -> -1..+1, with + meaning "turn right".

        Clamped rather than wrapped past ``span_deg``. Someone directly behind
        the robot is 180 degrees off either way, and wrapping would make the two
        sides swap at the back — a person walking around him would send the head
        snapping the wrong way as they crossed. Clamping just says "hard right"
        and lets the neck's own limits end it.
        """
        rel = _wrap180(angle - self.mount_offset)
        if self.invert:
            rel = -rel
        return max(-1.0, min(1.0, rel / self.span_deg))

    def bearing(self) -> float | None:
        """-1..+1 (+ = turn right), or None for "no opinion".

        None whenever nobody has been heard recently — see the module docstring
        on why the raw angle alone is a memory rather than an observation.
        """
        if not self.enabled:
            return None
        with self._lock:
            angle, heard_at = self._heard_angle, self._heard_at
        if angle is None or (time.monotonic() - heard_at) > self.stale_after:
            return None
        return self._to_bearing(angle)

    # -- calibration -------------------------------------------------------
    def capture_forward(self) -> dict:
        """Set ``mount_offset`` from whoever is talking *now*.

        The whole calibration: stand in front of him, say something, press the
        button. Whatever angle the array reports at that moment becomes the
        definition of straight ahead, which is a measurement rather than an
        exercise in working out which way the board got screwed in.

        Refuses on a stale reading rather than silently calibrating to a ghost.
        """
        with self._lock:
            angle, heard_at = self._heard_angle, self._heard_at
        if angle is None:
            return {"ok": False, "error": "nothing heard yet — say something "
                                          "while standing in front of him"}
        age = time.monotonic() - heard_at
        if age > self.stale_after:
            return {"ok": False,
                    "error": f"last voice was {age:.1f}s ago; talk and try again"}
        self.mount_offset = float(angle)
        return {"ok": True, "mount_offset": self.mount_offset, "raw_angle": angle,
                "age": round(age, 2)}

    def tuning(self) -> dict:
        return {"enabled": self.enabled, "mount_offset": self.mount_offset,
                "span_deg": self.span_deg, "invert": self.invert,
                "stale_after": self.stale_after, "poll_hz": self.poll_hz,
                "beam": self.beam, "smooth_n": self.smooth_n,
                "source": self.source}

    def configure(self, **kw) -> dict:
        """Apply any subset of TUNABLE. Unknown and None values are ignored, so
        the whole request body can be handed straight in."""
        for key in TUNABLE:
            val = kw.get(key)
            if val is None:
                continue
            if key in ("enabled", "invert"):
                setattr(self, key, bool(val))
            elif key == "span_deg":
                self.span_deg = max(1.0, float(val))
            elif key == "poll_hz":
                self.poll_hz = max(0.5, float(val))
            elif key == "stale_after":
                self.stale_after = max(0.0, float(val))
            elif key == "beam":
                self.beam = min(max(0, int(val)), _BEAMS - 1)
            elif key == "source":
                if val in SOURCES:
                    self.source = val
            elif key == "smooth_n":
                self.smooth_n = max(1, int(val))
                with self._lock:           # resize keeps whatever still fits
                    self._recent = collections.deque(self._recent,
                                                     maxlen=self.smooth_n)
            else:
                self.mount_offset = _wrap180(float(val)) % 360.0
        return self.tuning()

    def status(self) -> dict:
        with self._lock:
            angle, speech, beams = self._angle, self._speech, self._beams
            energy, processed = self._energy, self._processed
            heard_angle, heard_at = self._heard_angle, self._heard_at
            reads, error = self._reads, self._error
            retries, giveups = self._retries, self._giveups
        age = (time.monotonic() - heard_at) if heard_angle is not None else None
        fresh = age is not None and age <= self.stale_after
        return {
            "available": self._dev is not None or self.available(),
            "running": self.is_running(),
            # Raw, straight off the device, so the calibration page can show
            # what it actually said before any of the mapping is applied.
            "angle": round(angle, 1) if angle is not None else None,
            "speech": speech,
            # Every beam, so "am I reading the right one" is answerable from the
            # panel — a parked beam holds one number while the tracking one moves.
            "beams": [round(b, 1) for b in beams] if beams else None,
            # Per-beam speech energy and the device's own processed answer, so
            # the panel can show why a beam is or is not being believed.
            "energy": [round(e, 1) for e in energy] if energy else None,
            "processed": round(processed, 1) if processed is not None else None,
            "heard_angle": round(heard_angle, 1) if heard_angle is not None else None,
            "heard_age": round(age, 2) if age is not None else None,
            # What the tracker would get right now — None when stale, which is
            # the state you want to be able to see rather than infer.
            "bearing": (round(self._to_bearing(heard_angle), 3)
                        if fresh and heard_angle is not None else None),
            # The same angle relative to forward, in degrees, because that is
            # the number a human can check against where they are standing.
            "relative_deg": (round(_wrap180(heard_angle - self.mount_offset), 1)
                             if heard_angle is not None else None),
            "reads": reads,
            "retries": retries,
            "giveups": giveups,
            "error": error,
            **self.tuning(),
        }

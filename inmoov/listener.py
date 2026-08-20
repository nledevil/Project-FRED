"""Always-on wake-word listener for FRED.

Runs ``arecord`` on the USB mic and feeds the raw 16 kHz PCM to a Vosk
recogniser (offline, on-device). When a final transcript contains the wake word
— his name, "Fred", anywhere in the utterance — the rest of that utterance is
treated as a command; if nothing followed it, FRED says "Yes?" and the *next*
utterance is the command. A leading "hey" or "ok" is not required and never was:
_strip_wake scans for the name and keeps whatever comes after it, so "Fred, turn
your head" and "hey Fred, turn your head" are the same sentence to him.

The wake word is how a conversation *starts*, not a toll on every sentence: when
FRED himself ends a turn on a question, ``arm()`` holds the mic open so the
answer to "which one did you mean?" is just the answer. See Assistant.converse.

Design mirrors the other hardware wrappers: ``available()`` is False (and the
thread never starts) when Vosk or the model or the mic are missing, so the app
still runs without voice.

The recogniser is muted while FRED is speaking (``pause``/``resume``) so a reply
cannot answer itself. The microphone itself keeps running throughout — the
PowerConf is full duplex and cancels its own output from its capture; see
``Listener.pause`` for what was measured.
"""
from __future__ import annotations

import collections
import json
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

try:
    from vosk import Model, KaldiRecognizer, SetLogLevel
    SetLogLevel(-1)
    _VOSK_ERR = None
except Exception as exc:  # noqa: BLE001 - no vosk just means "no voice input"
    Model = KaldiRecognizer = None
    _VOSK_ERR = exc

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "vosk-model-small-en-us-0.15"

# Vosk's small model hears the name a few different ways — accept the near ones,
# but keep the set tight (no "red"/"ed") so ordinary speech doesn't false-trigger.
FULL_SCALE = 32767              # int16, so a peak is a fraction of this

# How much level history to keep for the panel's meter. Capture is 4000-byte
# chunks of 16 kHz mono int16, i.e. 125 ms each, so 48 of them is six seconds.
#
# It has to be a history rather than a single number because of the rates
# involved: the panel polls voice status every 1.2 s and would otherwise be
# shown one 125 ms chunk out of every ten, sampling a syllable at random. You
# would talk and watch a meter twitch. Six seconds is enough to say a sentence
# and see the whole of it.
LEVEL_HISTORY = 48

# His name, plus what the recogniser actually produces when someone says it.
# Every one of these was observed in logs/heard.jsonl rather than guessed:
# "alfred" is "hey Fred" run together into a single token, and "fraud"/"bread"
# are what a general lexicon reaches for when a short name competes with the
# whole English vocabulary. Words that are *ordinary* English stay out however
# close they sound — "are" and "from" also turned up, and either would make him
# answer half the conversations in the room.
# "fread" is deliberately absent: it is not in this model's vocabulary, so no
# recogniser can ever emit it and listing it only makes Vosk log a warning per
# grammar it builds. Checked, not assumed — the other six are all in there.
WAKE_WORDS = ("fred", "friend", "frayed", "alfred", "fraud", "bread")
ARM_WINDOW = 6.0     # seconds to wait for the command after a bare "Fred"
# Seconds the mic stays open after FRED has asked a question. Longer than the
# window above because answering a question takes a beat more thought than
# issuing a command after "Yes?" — and because it is measured from the moment he
# stops talking, not from the moment he decided what to say.
FOLLOWUP_WINDOW = 9.0
# Interrupting him takes his name. Any-speech-interrupts was tried first and is
# unusable in a real room: with a television on, four lines of dialogue in seven
# seconds each cut him off *and* were taken as commands, so he started an answer,
# lost it, started another, and the panel filled with replies nobody heard. The
# room is full of speech that isn't for him; his name is the one signal that is.
#
# The cost is that a bare "stop" won't do it — "Fred, stop" will. That is the
# same bargain the wake word already makes everywhere else.
BARGE_NEEDS_WAKE_WORD = True
# Barge-in listens with a *restricted* grammar: the only things it can output
# are his name and "[unk]". A short name competing against the full lexicon is
# the whole problem — measured on this model, "Fred, stop talking" came back as
# "fresh start talking" and "hey Fred" as "alfred", so nothing matched and he
# talked straight over the person. Given only the name to find, it finds it.
#
# Needs a model built with a dynamic graph (the "-lgraph" suffix). If the model
# can't take a grammar, _run falls back to watching the ordinary recogniser and
# says so once.
BARGE_GRAMMAR = json.dumps(sorted(WAKE_WORDS) + ["[unk]"])
# Consecutive partial results that must carry his name before it counts.
#
# A grammar recogniser can only ever output the words it was given, so while it
# is deciding, ordinary speech briefly gets mapped onto one of them before the
# decoder settles on "[unk]". Acting on a single partial made him answer a room
# nobody had addressed. Its *finals* are reliable — nine seconds of television
# produced "[unk]" every time — but waiting for a final means waiting for the
# speaker to stop, which is the whole thing barge-in exists to avoid.
#
# Measured over that same television: a stray hypothesis survives at most two
# consecutive chunks, while someone really saying his name holds for sixteen.
# Four is comfortably clear of the noise and still reacts within half a second
# (a chunk is 4000 bytes = 125 ms).
NAME_MIN_PARTIALS = 4


class Listener:
    """Background wake-word + speech-to-text loop.

    Parameters
    ----------
    on_command : callable(str)   -- called with the recognised command text.
    on_wake : callable()         -- called on a bare "Fred" (say "Yes?").
    device : str                 -- ALSA capture device for arecord.
    gain : float                 -- software mic boost applied to the raw PCM
                                    before Vosk sees it. The USB mic's analog
                                    capture is already maxed (+16 dB), so this is
                                    the only remaining sensitivity knob. 1.0 = off;
                                    ~2-3 helps quiet/distant speech. Too high just
                                    amplifies room noise and clips loud words,
                                    which hurts recognition, so keep it modest.

    Call ``pause()``/``resume()`` around playback: this *stops* the arecord
    capture (not just ignores it) so FRED never captures and plays at the same
    time. The USB codec on this rig wedges under simultaneous capture+playback,
    so the two must never overlap.
    """

    def __init__(self, on_command, on_wake=None, on_barge=None,
                 device: str = "plughw:0,0", model_path: str | Path = MODEL_PATH,
                 gain: float = 1.0, barge_in: bool = True):
        self._on_command = on_command
        self._on_wake = on_wake or (lambda: None)
        # Called the moment somebody starts talking over him, so the reply can be
        # cut short. Separate from on_command because it fires on a *partial* —
        # the point is to stop within a word, not to wait out their sentence.
        self._on_barge = on_barge or (lambda: None)
        # Whether to listen at all while he speaks. Off = the old behaviour, where
        # audio during a reply is read and dropped.
        self.barge_in = bool(barge_in)
        self.device = device
        self.gain = max(1.0, float(gain))       # never attenuate below the captured level
        self.model_path = Path(model_path)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()      # set = capture suspended (during playback)
        self._proc = None                     # the live arecord subprocess (or None)
        self._lock = threading.Lock()
        self._proc_lock = threading.Lock()    # guards the _proc claim/release swap
        self._model = None                    # loaded lazily on first start()
        # Signal level, so "FRED can't hear me" is answerable without guessing.
        # See _note_level() for why digital silence is worth its own flag.
        self._peak = 0                        # loudest sample in the last chunk
        self._levels = collections.deque(maxlen=LEVEL_HISTORY)   # recent chunk peaks
        # Monotonic deadline: while now < this, an utterance is taken as speech
        # meant for FRED with no wake word in front of it. Set by the bare-wake
        # branch in _run and by arm() from the Assistant, hence an attribute
        # rather than a local — a plain float, written and read as one word.
        self._armed_until = 0.0
        self._warned_grammar = False           # only say "no grammar support" once
        self._name_streak = 0                  # consecutive partials naming him
        self._heard_at = 0.0                  # monotonic, last chunk that wasn't silence
        self._captured_at = 0.0               # monotonic, last chunk of any kind
        # When the current unbroken run of digital silence began. Tracked as its
        # own clock rather than derived from _heard_at, because the case that
        # matters most is a mic that has been muted since before we started and
        # so has *never* set _heard_at at all.
        self._silent_since = 0.0

    def pause(self) -> None:
        """Stop feeding the recogniser while FRED speaks. The mic stays open.

        This used to reap arecord and hand the card back, on the grounds that the
        USB codec wedged if capture and playback overlapped even briefly. Measured
        on the Anker PowerConf (2026-08-19) that is not true of this device:

        * Capture and playback are separate USB interfaces with separate
          endpoints (2 OUT / 3 IN, ``/proc/asound/card0/stream0``). Both report
          ``Running`` together indefinitely, a capture taken straight through six
          seconds of continuous playback came back full-length and gap-free, and
          the two streams together use about a fifth of a full-speed bus.
        * The device cancels its own output out of the mic. Playing a phrase of
          nine words no room would produce ("banana helicopter Tuesday...") and
          transcribing the capture — with ``gain`` applied, as below — returned
          not one of them, three times over. Correlation puts the echo near
          -43 dB, roughly 33 dB under the noise floor of an ordinary room; the
          mic actually goes *quieter* while it plays, so it ducks rather than
          merely subtracting.

          It cancels that well because the speaker and the microphones are one
          sealed unit. The A3301 carries a six-mic array around a single driver,
          so the path from driver to capsules is fixed, known to the firmware and
          unchanging, and the array beamforms toward whoever is talking and away
          from its own speaker. (It is Zoom-certified, which is a duplex
          conformance bar, not just a logo.) That is what makes this a property of
          the device rather than of where it happened to be sitting on the day —
          so moving the robot cannot quietly undo it. A separate speaker and a
          single microphone would have neither guarantee, which is very likely
          the arrangement the original "the codec wedges" note was written for.

        So the stream stays up and this only stops the *recogniser* from being
        fed. That matters beyond tidiness: closing meant a device reopen and a
        fresh recogniser on every single reply, and it put arecord's restart in a
        race with aplay for the card — which is the failure ``play_file``'s retry
        and its longer first-clip probe exist to survive.

        Frames are still read while paused (see ``_run``) and simply dropped: stop
        reading and arecord's pipe fills, which really would wedge it.

        Safe to call from the listener thread itself (the usual path: a
        recognised command calls back into Assistant.speak).
        """
        self._paused.set()

    def resume(self) -> None:
        """Start feeding the recogniser again once FRED has stopped talking."""
        self._paused.clear()

    def arm(self, seconds: float = FOLLOWUP_WINDOW) -> None:
        """Accept the next utterance without a wake word, for ``seconds``.

        Call this *after* FRED has finished speaking, not when the reply was
        decided: nothing said while he talks is listened to, so a window opened
        before he starts is mostly spent by the time anyone can answer.

        Cheap to call when already armed — it extends rather than stacks.
        """
        self._armed_until = time.monotonic() + max(0.0, float(seconds))

    def disarm(self) -> None:
        """Close the window early. The wake word is required again."""
        self._armed_until = 0.0

    def is_armed(self) -> bool:
        return time.monotonic() < self._armed_until

    def available(self) -> bool:
        return (Model is not None and self.model_path.is_dir()
                and Path("/dev/snd").exists())

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _note_level(self, data: bytes) -> None:
        """Record how loud the last chunk was, and when we last heard anything.

        This exists because of a failure that is invisible from every other
        angle: the USB speakerphone has its own mute button, and when it is on
        the host sees nothing wrong. ALSA still reports the capture control at
        100% and unmuted, arecord still runs and still returns data — the data
        is just *exact zeros*. FRED sits there looking like he is listening and
        never hears a word, and nothing in any log says so.

        **A zero run is not proof of a mute, on this hardware.** The PowerConf
        does its own noise suppression and gates a quiet room to exact zeros
        too: measured here, 30 s of unbroken zeros with the mic live and
        unmuted, then a peak of 2 when someone moved. A cheap analogue capture
        would give a noise floor to read; this one does not, so silence is
        reported as the fact it is — no signal for N seconds — and the *reason*
        is left to whoever is standing there. Calling it "muted" would be a
        guess, and a guess in red trains people to ignore the panel.
        """
        try:
            samples = np.frombuffer(data, dtype=np.int16)
            peak = int(np.abs(samples).max()) if samples.size else 0
        except ValueError:                    # odd-length chunk; skip this one
            return
        now = time.monotonic()
        with self._lock:
            self._peak = peak
            self._levels.append(peak)
            self._captured_at = now
            if peak > 0:
                self._heard_at = now
                self._silent_since = 0.0      # the run of silence ends here
            elif not self._silent_since:
                self._silent_since = now      # ...and this is where it began

    def status(self) -> dict:
        """What the microphone is actually doing, for the panels.

        ``silent_for`` is seconds of unbroken digital silence while capturing —
        None when we aren't capturing, so a paused or stopped listener is never
        mistaken for a muted one.

        ``levels`` is the recent run of per-chunk peaks, oldest first, so the
        panel can draw what the microphone has actually been doing instead of
        being handed a threshold and a verdict. That is the whole point: this
        hardware gates a quiet room to exact zeros, so no number here can be
        turned into "muted" honestly — but a person watching a meter while they
        talk can settle it in one second.

        Raw sample values rather than anything pre-scaled: ``full_scale`` says
        what they are out of, and how to draw them is the panel's business.
        Levels keep flowing while paused, so the meter shows the last six
        seconds rather than blanking every time FRED speaks; ``capturing`` is
        there for the panel to dim it.
        """
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            paused = self._paused.is_set()
            peak, captured_at = self._peak, self._captured_at
            silent_since = self._silent_since
            levels = list(self._levels)
        now = time.monotonic()
        capturing = running and not paused
        silent_for = None
        if capturing and captured_at:
            silent_for = round(now - silent_since, 1) if silent_since else 0.0
        return {"device": self.device, "running": running, "paused": paused,
                "capturing": capturing, "peak": peak, "silent_for": silent_for,
                "levels": levels, "peak_recent": max(levels) if levels else 0,
                "full_scale": FULL_SCALE,
                "capture_age": round(now - captured_at, 1) if captured_at else None,
                # Why a sentence with no wake word in it was answered. Without
                # this the panel's transcript looks like FRED replied to nothing.
                "barge_in": self.barge_in,
                "armed": now < self._armed_until,
                "armed_for": (round(self._armed_until - now, 1)
                              if now < self._armed_until else None)}

    def start(self) -> bool:
        if not self.available():
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="listener", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            t = self._thread
        if t is not None:
            t.join(timeout=3.0)
        with self._lock:
            self._thread = None

    # ---- the loop ---------------------------------------------------------
    def _run(self) -> None:
        if self._model is None:                # ~2s load; do it off the boot path
            self._model = Model(str(self.model_path))
        rec = KaldiRecognizer(self._model, 16000)
        brec = self._new_barge_rec()
        try:
            dropped = False       # audio was discarded -> restart the recogniser
            barged = False        # already cut this reply short
            name_seen = False     # the detector heard his name in this utterance
            while not self._stop.is_set():
                proc = self._proc
                if proc is None:               # first start, or reopen after an EOF
                    proc = subprocess.Popen(
                        ["arecord", "-q", "-D", self.device, "-f", "S16_LE",
                         "-r", "16000", "-c", "1", "-t", "raw"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    # Publish only if nobody paused us while arecord was starting;
                    # otherwise this capture would run *during* playback, which is
                    # exactly what pause() exists to prevent.
                    with self._proc_lock:
                        claimed = not (self._paused.is_set() or self._stop.is_set())
                        if claimed:
                            self._proc = proc
                    if not claimed:
                        _reap(proc)
                        continue
                    rec = KaldiRecognizer(self._model, 16000)   # fresh recogniser after a gap
                    brec = self._new_barge_rec()
                    with self._lock:
                        # Silence is measured per capture session, and a session
                        # now spans FRED's replies rather than restarting after
                        # each one — so this only fires on a real EOF (arecord
                        # died, or the card went away), where a silence run
                        # measured across the gap would be meaningless.
                        self._silent_since = 0.0
                try:
                    data = proc.stdout.read(4000)
                except (ValueError, OSError):  # pause() closed the pipe under us
                    data = b""
                if not data:                   # arecord ended (killed by pause, or died)
                    self._close_proc()
                    continue
                # Measured *before* gain: this is a question about the hardware,
                # and scaling silence by 8 is still silence.
                self._note_level(data)
                if self.gain > 1.0:
                    data = _amplify(data, self.gain)
                if self._paused.is_set():
                    with self._lock:
                        # Don't let his turn count as the microphone going quiet:
                        # the canceller ducks capture while he speaks, and that
                        # is not the mic failing.
                        self._silent_since = 0.0
                    if not self.barge_in:
                        # Read and drop. Leaving frames in arecord's pipe is what
                        # would actually wedge it; the level meter still sees them,
                        # so the panel keeps moving while he talks.
                        dropped = True
                        continue
                    # Listening *through* his own reply. Safe because the device
                    # cancels its own output out of its capture — see pause() —
                    # so what arrives here is the room, not him.
                    #
                    # Two recognisers, deliberately. brec only knows his name and
                    # decides whether to stop him; rec knows English and produces
                    # the sentence they actually said. Asking one recogniser to do
                    # both is what failed: given the whole lexicon, "Fred, stop
                    # talking" decodes as "fresh start talking" and nothing fires.
                    if not barged and self._detect_name(brec, data):
                        barged = True
                        self._barge()
                    if rec.AcceptWaveform(data):
                        text = json.loads(rec.Result()).get("text", "").strip()
                        if text:
                            if not barged and _wants_him(text):
                                # Nothing caught it earlier — a whole utterance
                                # landing in one chunk. Stop him now.
                                barged = True
                                self._barge()
                            if barged:
                                # He was interrupted, so this was aimed at him.
                                # If his name survived into this transcript let
                                # the normal path strip it (and answer a bare
                                # "Fred" with "Yes?"); if it came out as "fresh"
                                # or "alfred", take the sentence whole rather
                                # than demand a name the recogniser just lost.
                                try:
                                    if _strip_wake(text) is None:
                                        self._dispatch(text, time.monotonic(), armed=True)
                                    else:
                                        self._dispatch(text, time.monotonic())
                                except Exception as exc:  # noqa: BLE001
                                    print(f"[Listener] handler error: {exc}")
                        barged = False
                        name_seen = False
                        self._reset_rec(brec)  # next utterance starts clean
                    continue
                if dropped:
                    # Audio was thrown away, so the recogniser is mid-utterance on
                    # a gap it never saw the far side of. Start it clean. Cheap —
                    # this is the object, not the model.
                    rec = KaldiRecognizer(self._model, 16000)
                    self._reset_rec(brec)
                    dropped = False
                barged = False
                # The same two-recogniser split as barge-in, for the same reason:
                # deciding whether he was addressed and transcribing what was said
                # are different jobs, and the first one loses badly when his name
                # has to out-compete the whole lexicon. "hey Fred" comes back from
                # the general model as the single token "alfred", which no amount
                # of splitting on whitespace will match.
                #
                # Latched rather than checked at the end: brec finishes utterances
                # on its own schedule, so the name can be found and gone again
                # before rec has finished the sentence it belongs to.
                if not name_seen and self._detect_name(brec, data):
                    name_seen = True
                if not rec.AcceptWaveform(data):
                    continue
                text = json.loads(rec.Result()).get("text", "").strip()
                heard_name, name_seen = name_seen, False
                self._reset_rec(brec)          # next utterance starts clean
                if not text:
                    continue
                try:                           # a handler crash must not stop listening
                    # armed only when the detector heard his name and the sentence
                    # itself doesn't carry it — i.e. the name was mangled on its
                    # way through the general model. When it did survive, the
                    # normal path strips it and still answers a bare name with
                    # "Yes?".
                    self._dispatch(text, time.monotonic(),
                                   armed=heard_name and _strip_wake(text) is None)
                except Exception as exc:  # noqa: BLE001
                    print(f"[Listener] handler error: {exc}")
        except Exception as exc:  # noqa: BLE001 - log a crash instead of dying silently
            print(f"[Listener] loop error: {exc}")
        finally:
            self._close_proc()

    def _reset_rec(self, rec) -> None:
        """Clear the name detector between utterances, tolerating one that can't.

        Also drops the partial streak — it belongs to the utterance just ended.

        Guarded because this is the microphone loop: an AttributeError here
        propagates out of _run and FRED goes deaf for the rest of the session,
        which is a wildly disproportionate outcome for a detector that is only
        an optimisation over rebuilding the object.
        """
        self._name_streak = 0
        if rec is None:
            return
        try:
            rec.Reset()
        except Exception as exc:  # noqa: BLE001  (see the docstring)
            print(f"[Listener] could not reset the name detector: {exc}")

    def _new_barge_rec(self):
        """A recogniser that can only hear his name, for deciding to interrupt.

        Returns None when the model has no dynamic graph, in which case _run
        watches the ordinary recogniser instead — worse, but not broken.
        """
        if self._model is None:
            return None
        try:
            return KaldiRecognizer(self._model, 16000, BARGE_GRAMMAR)
        except Exception as exc:  # noqa: BLE001
            if not self._warned_grammar:
                self._warned_grammar = True
                print(f"[Listener] no grammar support in this model ({exc}); "
                      "barge-in will be less reliable at hearing his name")
            return None

    def _detect_name(self, brec, data) -> bool:
        """Feed the name detector one chunk. True when it has really heard him.

        A *final* carrying the name is taken at once; a *partial* has to persist
        for NAME_MIN_PARTIALS chunks, for the reason recorded there.
        """
        if brec is None:
            return False
        if brec.AcceptWaveform(data):
            self._name_streak = 0
            return _wants_him(json.loads(brec.Result()).get("text", ""))
        if _wants_him(json.loads(brec.PartialResult()).get("partial", "")):
            self._name_streak += 1
        else:
            self._name_streak = 0
        return self._name_streak >= NAME_MIN_PARTIALS

    def _barge(self) -> None:
        """Tell whoever is speaking to stop. Never lets a handler kill the loop."""
        try:
            self._on_barge()
        except Exception as exc:  # noqa: BLE001
            print(f"[Listener] barge handler error: {exc}")

    def _dispatch(self, text: str, now: float, armed: bool = False) -> None:
        """Route one final transcript: a command, a wake prompt, or nothing.

        Split out of _run so the barge-in path can use it too. ``armed=True``
        skips the wake word outright — someone talking over him has already made
        it clear who they are talking to.
        """
        if armed or now < self._armed_until:
            # Speech inside an open window: the command after a bare "Fred", the
            # answer to a question he just asked, or an interruption. Consumed
            # *before* the handler runs, so a reply ending on another question
            # can re-arm.
            self._armed_until = 0.0
            self._on_command(text)
            return
        cmd = _strip_wake(text)
        if cmd is None:
            return                             # not addressed to him -> ignore
        if cmd:
            self._on_command(cmd)
        else:                                  # bare "Fred" -> prompt & arm
            self._armed_until = now + ARM_WINDOW
            self._on_wake()

    def _close_proc(self) -> None:
        """Terminate the arecord subprocess and release the capture device.

        Callable from any thread: stop() ends the thread that owns it, and the
        loop calls it on EOF. pause() no longer does — the stream stays up while
        FRED speaks, see pause(). The claim/release of ``_proc`` is atomic so a
        concurrent close + loop-EOF can't double-reap. We reap the
        child (so ALSA frees the device) *and* close the pipe (so we don't leak an
        fd per pause) — the loop's read() tolerates the pipe vanishing underneath
        it, which is what makes closing from another thread safe.
        """
        with self._proc_lock:
            p, self._proc = self._proc, None
        _reap(p)


def _reap(p) -> None:
    """Terminate an arecord process, wait for ALSA to free the device, and close
    its pipe. Tolerates a process that's already dead."""
    if p is None:
        return
    try:
        p.terminate()
        p.wait(timeout=1)
    except Exception:          # noqa: BLE001
        try:
            p.kill()
            p.wait(timeout=1)
        except Exception:      # noqa: BLE001
            pass
    try:
        p.stdout.close()
    except Exception:          # noqa: BLE001
        pass


def _amplify(data: bytes, gain: float) -> bytes:
    """Scale S16_LE mono PCM by ``gain``, clipping hard at the int16 rails so an
    over-driven sample wraps to a click rather than to the opposite polarity."""
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) * gain
    np.clip(samples, -32768, 32767, out=samples)
    return samples.astype(np.int16).tobytes()


def _wants_him(text: str) -> bool:
    """Is this speech addressed to FRED — i.e. does his name appear in it?

    The test for interrupting him. See BARGE_NEEDS_WAKE_WORD for why a room's
    ordinary conversation must not qualify.
    """
    if not BARGE_NEEDS_WAKE_WORD:
        return bool(text.strip())
    return _strip_wake(text) is not None


def _strip_wake(text: str):
    """If ``text`` contains a wake word, return everything after it (may be '');
    return None if no wake word is present."""
    tokens = text.lower().split()
    for i, tok in enumerate(tokens):
        if tok in WAKE_WORDS:
            return " ".join(tokens[i + 1:]).strip()
    return None

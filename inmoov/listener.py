"""Always-on wake-word listener for FRED.

Runs ``arecord`` on the USB mic and feeds the raw 16 kHz PCM to a Vosk
recogniser (offline, on-device). When a final transcript contains the wake word
("Hey FRED"), the rest of that utterance is treated as a command; if nothing
followed the wake word, FRED says "Yes?" and the *next* utterance is the command.

Design mirrors the other hardware wrappers: ``available()`` is False (and the
thread never starts) when Vosk or the model or the mic are missing, so the app
still runs without voice.

The recogniser is muted while FRED is speaking (via the ``is_muted`` callback)
so he doesn't hear and transcribe his own voice.
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

WAKE_WORDS = ("fred", "friend", "fread", "frayed")
_ARM_WINDOW = 6.0    # seconds to wait for the command after a bare "Hey FRED"


class Listener:
    """Background wake-word + speech-to-text loop.

    Parameters
    ----------
    on_command : callable(str)   -- called with the recognised command text.
    on_wake : callable()         -- called on a bare "Hey FRED" (say "Yes?").
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

    def __init__(self, on_command, on_wake=None,
                 device: str = "plughw:0,0", model_path: str | Path = MODEL_PATH,
                 gain: float = 1.0):
        self._on_command = on_command
        self._on_wake = on_wake or (lambda: None)
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
        self._heard_at = 0.0                  # monotonic, last chunk that wasn't silence
        self._captured_at = 0.0               # monotonic, last chunk of any kind
        # When the current unbroken run of digital silence began. Tracked as its
        # own clock rather than derived from _heard_at, because the case that
        # matters most is a mic that has been muted since before we started and
        # so has *never* set _heard_at at all.
        self._silent_since = 0.0

    def pause(self) -> None:
        """Suspend mic capture so playback runs alone, and *block until the card
        is actually free*.

        This used to only send SIGTERM and return, leaving arecord to die on its
        own; the caller got away with it because rendering the TTS took a couple
        of seconds, which was plenty of time. Now that speech is rendered by a
        warm piper daemon (~0.1 s on a cache hit) that accidental grace period is
        gone, so we reap the process here — the USB codec on this rig wedges if
        capture and playback overlap even briefly.

        Safe to call from the listener thread itself (the usual path: a
        recognised command calls back into Assistant.speak).
        """
        self._paused.set()
        self._close_proc()

    def resume(self) -> None:
        """Resume mic capture after playback."""
        self._paused.clear()

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
                "capture_age": round(now - captured_at, 1) if captured_at else None}

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
        armed_until = 0.0
        try:
            while not self._stop.is_set():
                if self._paused.is_set():      # playback in progress -> capture off (no duplex)
                    self._close_proc()
                    self._stop.wait(0.05)
                    continue
                proc = self._proc
                if proc is None:               # (re)open capture after a pause or EOF
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
                    with self._lock:
                        # Silence is measured per capture session: the gap while
                        # FRED was speaking is not the microphone's fault.
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
                if not rec.AcceptWaveform(data):
                    continue
                text = json.loads(rec.Result()).get("text", "").strip()
                if not text:
                    continue
                now = time.monotonic()
                try:                           # a handler crash must not stop listening
                    if now < armed_until:      # command following a bare wake word
                        armed_until = 0.0
                        self._on_command(text)
                    else:
                        cmd = _strip_wake(text)
                        if cmd is None:
                            continue           # no wake word -> ignore
                        if cmd:
                            self._on_command(cmd)
                        else:                  # bare "Hey FRED" -> prompt & arm
                            armed_until = now + _ARM_WINDOW
                            self._on_wake()
                except Exception as exc:  # noqa: BLE001
                    print(f"[Listener] handler error: {exc}")
        except Exception as exc:  # noqa: BLE001 - log a crash instead of dying silently
            print(f"[Listener] loop error: {exc}")
        finally:
            self._close_proc()

    def _close_proc(self) -> None:
        """Terminate the arecord subprocess and release the capture device.

        Callable from any thread: pause() calls it to hand the USB card to
        playback, and the loop calls it on EOF. The claim/release of ``_proc`` is
        atomic so a concurrent pause() + loop-EOF can't double-reap. We reap the
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


def _strip_wake(text: str):
    """If ``text`` contains a wake word, return everything after it (may be '');
    return None if no wake word is present."""
    tokens = text.lower().split()
    for i, tok in enumerate(tokens):
        if tok in WAKE_WORDS:
            return " ".join(tokens[i + 1:]).strip()
    return None

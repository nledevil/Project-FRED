"""A second opinion on what was said — Whisper, for the sentence after his name.

FRED hears with Vosk, and Vosk stays: its grammar API is what makes the wake
detector and barge-in possible, and Whisper has no such thing. But the
sentence *after* "Fred" is a free transcription, and that is where the heard
log shows Vosk losing: "terminator mode" as "terminate or mode", his name as
"read", a child's phrasing as a paragraph of the room. Whisper is markedly
better on noisy and unusual speech, at two costs this module is shaped
around:

**Latency.** Whisper works on a whole utterance, so it runs when Vosk's
endpointing says the sentence is over, and its time is added straight to what
the person waits before FRED starts thinking. faster-whisper on this CPU, int8,
a few threads: tools/bench_transcribers.py measures it. The listener runs it
on a worker thread so the microphone keeps being read meanwhile, and if the
worker is still busy when the next sentence ends, that sentence goes out on
Vosk's words at once rather than queueing — a late answer to the wrong
question is worse than Vosk.

**Hallucination on noise.** Whisper will confidently transcribe silence or
babble as "Thank you." The listener only asks for a second opinion when Vosk
already heard *words* in the utterance, so the endpointed audio is speech;
and an empty answer falls back to Vosk's. Its text is normalised to the shape
the matcher and the wake-word strip expect from Vosk — lower case, no
punctuation, apostrophes kept — so nothing downstream can tell which engine
spoke.

Off unless ``voice.transcriber`` is ``"whisper"``. Needs ``faster-whisper`` in
the venv (``venv/bin/pip install faster-whisper``); the model downloads to the
Hugging Face cache on first load, so the first boot after enabling it needs
the internet once. Loading happens on a background thread at start, exactly
like the local brain's warm-up, and until it is ready Vosk's words are used.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import numpy as np

RATE = 16000
DEFAULT_MODEL = "base.en"
DEFAULT_THREADS = 4
# The longest utterance worth a second opinion. Whisper's time grows with
# the audio; past this the listener has heard a speech, not a question.
MAX_SECONDS = 20.0

# What Vosk hands the rest of the robot: lower case words, apostrophes, no
# punctuation. Whisper writes prose; this makes it Vosk-shaped.
_KEEP = re.compile(r"[^a-z0-9' ]+")


def normalise(text: str) -> str:
    t = (text or "").lower().replace("’", "'")
    t = _KEEP.sub(" ", t)
    return " ".join(t.split())


class WhisperTranscriber:
    """faster-whisper, loaded once in the background, transcribing PCM16 bytes."""

    name = "whisper"

    def __init__(self, model: str = DEFAULT_MODEL, threads: int = DEFAULT_THREADS,
                 log=print):
        self.model_name = model
        self.threads = max(1, int(threads))
        self._log = log
        self._model = None
        self._error = ""
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._count = 0            # transcriptions done
        self._secs = 0.0           # wall time spent in them
        self._thread = threading.Thread(target=self._load, name="whisper-load",
                                        daemon=True)

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if not self._thread.is_alive() and self._model is None and not self._error:
            self._thread.start()

    def _load(self) -> None:
        t0 = time.monotonic()
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                 cpu_threads=self.threads, num_workers=1)
            # One short pass so the first real sentence does not pay the
            # graph's own warm-up — the same reason the local brain warms.
            list(model.transcribe(np.zeros(RATE // 2, np.float32), language="en",
                                  beam_size=1, without_timestamps=True)[0])
        except Exception as exc:  # noqa: BLE001 - no whisper just means Vosk's words
            self._error = f"{type(exc).__name__}: {exc}"
            self._log(f"[Transcriber] whisper unavailable: {self._error}")
            return
        with self._lock:
            self._model = model
            self._loaded_at = time.monotonic()
        self._log(f"[Transcriber] whisper {self.model_name} ready in "
                  f"{time.monotonic() - t0:.1f}s on {self.threads} threads")

    def ready(self) -> bool:
        return self._model is not None

    def status(self) -> dict:
        with self._lock:
            n, secs = self._count, self._secs
        return {"engine": self.name, "model": self.model_name, "ready": self.ready(),
                "error": self._error, "transcribed": n,
                "avg_seconds": round(secs / n, 2) if n else None}

    # ---- the work --------------------------------------------------------
    def transcribe(self, pcm16: bytes) -> str:
        """Vosk-shaped text for the utterance, or "" (use Vosk's words)."""
        model = self._model
        if model is None or not pcm16:
            return ""
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        limit = int(MAX_SECONDS * RATE)
        if len(audio) > limit:
            audio = audio[-limit:]
        t0 = time.monotonic()
        try:
            segments, _ = model.transcribe(
                audio, language="en", beam_size=1, best_of=1, temperature=0.0,
                condition_on_previous_text=False, vad_filter=False,
                without_timestamps=True)
            text = " ".join(s.text.strip() for s in segments)
        except Exception as exc:  # noqa: BLE001 - a bad frame must not lose the sentence
            self._log(f"[Transcriber] whisper failed: {exc}")
            return ""
        with self._lock:
            self._count += 1
            self._secs += time.monotonic() - t0
        return normalise(text)


def make(cfg: dict, log=print):
    """The transcriber ``voice`` settings ask for, or None for Vosk alone."""
    kind = str((cfg or {}).get("transcriber") or "vosk").lower()
    if kind != "whisper":
        return None
    return WhisperTranscriber(model=str(cfg.get("whisper_model") or DEFAULT_MODEL),
                              threads=int(cfg.get("whisper_threads") or DEFAULT_THREADS),
                              log=log)


def save_wav(path: Path, pcm16: bytes) -> None:
    """A debugging convenience: the bytes the transcriber was given, as a file."""
    import wave
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(pcm16)

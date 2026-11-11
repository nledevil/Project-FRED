"""Opt-in capture of the utterances FRED was actually spoken to — the real audio
the ASR-model decision has been waiting on, and cannot be synthesised.

The speech model was picked on cost plus one win on clean synthetic speech; the
case that fails is a child at three feet in a hall of four hundred, and no bench
produces that. tools/bench_asr.py is written and waiting; what it lacks is real
recordings of the failing case. This collects them — but only within the privacy
line heardlog.py already draws: **only what passed the wake gate is ever kept.**
The listener feeds every hot chunk in; a recording is written only when an
utterance actually reached him (a wake, or speech inside an open window). The
room he was not addressed with is fed to a bounded rolling buffer and continually
overwritten, never to disk.

**Off by default.** Nothing is written until voice.capture.enabled is set, and
when it is off feed()/commit() are immediate returns, so the hearing loop is
untouched in ordinary operation. When on, each WAV is named by the same
timestamp heardlog uses, so a recording pairs with its transcript row, and the
directory is bounded by file count — the oldest go first — so a long fair cannot
fill the disk.

The audio is exactly what the recogniser received: one channel, 16 kHz, 16-bit,
gain applied — the beamformed output on the array today. That is the point: to
benchmark a replacement against what the shipped path actually hears, warts and
all, not against a cleaner signal it will never get.
"""
from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

# 16 kHz mono int16 — the format arecord is asked for and Vosk is fed. Written
# into every WAV header; the listener must feed audio of this shape.
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2                      # bytes; S16_LE
_BYTES_PER_SEC = SAMPLE_RATE * SAMPLE_WIDTH

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "logs" / "utterances"
DEFAULT_SECONDS = 12.0               # rolling window: long enough for "Fred, <command>"
DEFAULT_MAX_FILES = 300              # a bounded fair's worth; oldest dropped past this


class UtterCapture:
    def __init__(self, directory: Path = DEFAULT_DIR, *, enabled: bool = False,
                 seconds: float = DEFAULT_SECONDS, max_files: int = DEFAULT_MAX_FILES):
        self._dir = Path(directory)
        self._enabled = bool(enabled)
        self._max_bytes = int(max(1.0, float(seconds)) * _BYTES_PER_SEC)
        self._max_files = int(max(1, max_files))
        self._buf = bytearray()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def configure(self, *, enabled: bool | None = None, directory=None,
                  seconds: float | None = None, max_files: int | None = None) -> None:
        """Set from settings at boot (or when the operator toggles it)."""
        with self._lock:
            if enabled is not None:
                self._enabled = bool(enabled)
                if not self._enabled:
                    self._buf.clear()       # stop holding the room the moment it's off
            if directory is not None:
                self._dir = Path(directory)
            if seconds is not None:
                self._max_bytes = int(max(1.0, float(seconds)) * _BYTES_PER_SEC)
            if max_files is not None:
                self._max_files = int(max(1, max_files))

    def feed(self, pcm: bytes) -> None:
        """One hot chunk (the same bytes fed to the recogniser). A cheap return
        when disabled, so the hearing loop pays nothing in normal operation. The
        buffer is a bounded rolling window: what was never committed is
        overwritten, never written down."""
        if not self._enabled or not pcm:
            return
        with self._lock:
            self._buf.extend(pcm)
            if len(self._buf) > self._max_bytes:
                del self._buf[:len(self._buf) - self._max_bytes]

    def commit(self, stamp: str | None = None) -> str | None:
        """He was addressed — freeze the rolling buffer to a WAV and clear it.

        ``stamp`` is heardlog's timestamp, so the recording pairs with its row;
        one is generated if not given. Returns the path written, or None (off,
        empty, or a write that failed — a full disk must never stop dispatch)."""
        if not self._enabled:
            return None
        with self._lock:
            if not self._buf:
                return None
            data = bytes(self._buf)
            self._buf.clear()
        stamp = stamp or time.strftime("%Y-%m-%dT%H:%M:%S")
        name = stamp.replace(":", "").replace("-", "") + ".wav"
        path = self._dir / name
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(SAMPLE_WIDTH)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(data)
            self._prune()
            return str(path)
        except OSError:
            return None

    def discard(self) -> None:
        """Drop the buffer without writing — a capture gap, or an utterance that
        turned out not to be for him."""
        with self._lock:
            self._buf.clear()

    def _prune(self) -> None:
        """Keep at most max_files WAVs; oldest first. Best-effort, never raises."""
        try:
            wavs = sorted(self._dir.glob("*.wav"))
            for old in wavs[:max(0, len(wavs) - self._max_files)]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except OSError:
            pass


# One capture per process, shared by the listener that feeds it and the app that
# configures it from settings — module-level like the heard log and wake tally.
_CAP = UtterCapture()


def cap() -> UtterCapture:
    return _CAP


def configure(**kw) -> None:
    _CAP.configure(**kw)

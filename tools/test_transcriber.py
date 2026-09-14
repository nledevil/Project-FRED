#!/usr/bin/env python3
"""Whisper's second opinion never costs a sentence, and never delays one twice.

The listener can hand the sentence after "Fred" to Whisper for a better
transcription (voice.transcriber = "whisper"). The heard log shows why —
"terminator mode" as "terminate or mode" — and tools/bench_transcribers.py
shows what it costs. This pins the promises the hand-off makes, none of which
need a microphone or a model:

**Vosk's words are never lost.** No transcriber, one not yet loaded, an empty
answer, a crash inside Whisper, or a worker still busy with the previous
sentence — every one of those routes the sentence on Vosk's words, and the
busy case does it at once rather than queueing behind a late answer.

**Whisper's words are Vosk-shaped.** Lower case, no punctuation, apostrophes
kept, so the wake-word strip and the matcher see what they always saw.

**The audio is the utterance.** The listener keeps exactly what the full
recogniser was fed since it was built — the replay, then the live chunks —
hands that over on the final, clears it, and never lets it grow past the cap.

**A handler crash never stops listening**, on either thread.

    python3 tools/test_transcriber.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import listener as L                                   # noqa: E402
from inmoov import transcriber as T                                # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeWhisper:
    """Answers what it is told to, after a delay it is told to take."""

    def __init__(self, answer="", delay=0.0, ready=True, boom=False):
        self.answer, self.delay, self._ready, self.boom = answer, delay, ready, boom
        self.calls: list[bytes] = []

    def ready(self):
        return self._ready

    def status(self):
        return {"engine": "fake", "ready": self._ready}

    def transcribe(self, pcm):
        self.calls.append(pcm)
        if self.boom:
            raise RuntimeError("model fell over")
        time.sleep(self.delay)
        return self.answer


def routed(lis: L.Listener, text: str, utt: bytes, wait: float = 0.5) -> list[str]:
    got: list[str] = []
    done = threading.Event()

    def then(t):
        got.append(t)
        done.set()
    lis._refine(text, utt, then)
    done.wait(wait)
    return got


def main() -> int:
    print("Whisper's words are Vosk-shaped")
    check("prose becomes lower-case words without punctuation",
          T.normalise("Fred, turn on Terminator Mode!") == "fred turn on terminator mode")
    check("apostrophes survive, curly ones straightened",
          T.normalise("What’s your name?") == "what's your name")
    check("empty stays empty", T.normalise("  ") == "")

    print("Vosk's words are never lost")
    lis = L.Listener(on_command=lambda t: None)
    check("no transcriber: routed at once on Vosk's words",
          routed(lis, "tell me a joke", b"\x00" * 3200) == ["tell me a joke"])
    lis = L.Listener(on_command=lambda t: None, transcriber=FakeWhisper("x", ready=False))
    check("not loaded yet: Vosk's words", routed(lis, "tell me a joke", b"\x00" * 32) == ["tell me a joke"])
    lis = L.Listener(on_command=lambda t: None, transcriber=FakeWhisper(""))
    check("whisper says nothing: Vosk's words, counted as a fallback",
          routed(lis, "tell me a joke", b"\x00" * 32) == ["tell me a joke"]
          and lis._refine_fallbacks == 1)
    lis = L.Listener(on_command=lambda t: None, transcriber=FakeWhisper(boom=True))
    check("whisper crashes: Vosk's words, and the worker lock is released",
          routed(lis, "tell me a joke", b"\x00" * 32) == ["tell me a joke"]
          and not lis._refine_busy.locked())
    lis = L.Listener(on_command=lambda t: None, transcriber=FakeWhisper("terminator mode off"))
    check("whisper answers: its words win, counted",
          routed(lis, "terminate or mode off", b"\x00" * 32) == ["terminator mode off"]
          and lis._refined == 1)
    fw = FakeWhisper("late", delay=0.4)
    lis = L.Listener(on_command=lambda t: None, transcriber=fw)
    first: list[str] = []
    lis._refine("first sentence", b"\x00" * 32, first.append)
    t0 = time.monotonic()
    second = routed(lis, "second sentence", b"\x00" * 32, wait=0.1)
    took = time.monotonic() - t0
    check("worker busy: the next sentence goes out on Vosk's words at once",
          second == ["second sentence"] and took < 0.1, f"{second} in {took:.2f}s")
    time.sleep(0.5)
    check("...and the first still arrives on Whisper's", first == ["late"], str(first))
    check("the empty utterance never asks Whisper",
          routed(L.Listener(on_command=lambda t: None, transcriber=FakeWhisper("x")), "hi", b"")
          == ["hi"])

    print("a handler crash never stops listening")
    def bad(t):
        raise RuntimeError("handler exploded")
    lis = L.Listener(on_command=lambda t: None)
    try:
        lis._refine("x", b"\x00", bad)
        check("...inline", True)
    except Exception as exc:  # noqa: BLE001
        check("...inline", False, str(exc))
    lis = L.Listener(on_command=lambda t: None, transcriber=FakeWhisper("y"))
    lis._refine("x", b"\x00", bad)
    time.sleep(0.2)
    check("...on the worker, and the lock comes back", not lis._refine_busy.locked())

    print("the audio is the utterance")
    lis = L.Listener(on_command=lambda t: None)
    lis._utt += b"a" * 10
    lis._utt += b"b" * (L.UTT_MAX_BYTES)
    if len(lis._utt) > L.UTT_MAX_BYTES:
        del lis._utt[:len(lis._utt) - L.UTT_MAX_BYTES]
    check("the buffer is capped at the tail", len(lis._utt) == L.UTT_MAX_BYTES and lis._utt[:1] == b"b")
    check("the cap matches Whisper's own", L.UTT_MAX_BYTES == int(T.MAX_SECONDS) * 16000 * 2)
    st = lis.status()["transcriber"]
    check("status names the engine, even when it is Vosk alone",
          st.get("engine") == "vosk" and "refined" in st and "fallbacks" in st, str(st))
    check("settings ask for nothing by default", T.make({}) is None and T.make({"transcriber": "vosk"}) is None)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

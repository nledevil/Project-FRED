#!/usr/bin/env python3
"""Utterance capture: off by default, bounded, and only ever what got through.

This collects real audio for the ASR-model decision bench_asr.py is waiting on,
and it sits on the same privacy line heardlog.py draws — so the tests that
matter are the ones that keep it honest:

  * OFF by default: feed and commit do nothing, write nothing, keep nothing —
    the hearing loop pays nothing and the room is never buffered;
  * when ON, a commit writes a real 16 kHz mono 16-bit WAV, named by the
    heard.jsonl timestamp so a recording pairs with its row;
  * the buffer is a bounded rolling window: uncommitted audio is overwritten,
    never written down, and the directory is capped by file count;
  * a bad path never raises — a full disk must not stop him dispatching.

    python3 tools/test_uttercap.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import uttercap as UC                           # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def secs(n: float) -> bytes:
    """n seconds of (nonzero) 16 kHz mono int16 PCM."""
    return b"\x01\x02" * int(n * UC.SAMPLE_RATE)


def main() -> int:
    print("off by default: nothing is kept and nothing is written")
    d0 = Path(tempfile.mkdtemp())
    c = UC.UtterCapture(d0)                      # enabled defaults to False
    check("starts disabled", c.enabled is False)
    c.feed(secs(1))
    check("feed while off keeps nothing", len(c._buf) == 0)
    check("commit while off writes nothing and returns None", c.commit("2026-09-09T10:00:00") is None)
    check("...and the directory has no WAVs", list(d0.glob("*.wav")) == [])

    print("on: a commit writes a real, correctly-shaped WAV named by the stamp")
    d1 = Path(tempfile.mkdtemp())
    c = UC.UtterCapture(d1, enabled=True, seconds=12)
    c.feed(secs(2))
    path = c.commit("2026-09-09T13:45:07")
    check("commit returned a path", bool(path), str(path))
    check("named by the stamp, colons/dashes stripped",
          path and Path(path).name == "20260909T134507.wav", Path(path).name if path else "")
    with wave.open(path, "rb") as w:
        check("mono", w.getnchannels() == 1)
        check("16-bit", w.getsampwidth() == 2)
        check("16 kHz", w.getframerate() == 16000)
        check("holds ~2 s of frames", abs(w.getnframes() - 2 * 16000) < 16000, str(w.getnframes()))
    check("the buffer was cleared after commit", len(c._buf) == 0)
    check("a second commit with nothing buffered writes nothing", c.commit("2026-09-09T13:45:09") is None)

    print("the buffer is a bounded rolling window — the room is overwritten, not kept")
    d2 = Path(tempfile.mkdtemp())
    c = UC.UtterCapture(d2, enabled=True, seconds=3)     # 3 s window
    c.feed(secs(10))                                     # feed 10 s of "room"
    check("only the last ~3 s is held, the rest overwritten",
          abs(len(c._buf) - 3 * UC._BYTES_PER_SEC) < UC._BYTES_PER_SEC,
          f"{len(c._buf)} bytes vs {3*UC._BYTES_PER_SEC}")
    path = c.commit("2026-09-09T14:00:00")
    with wave.open(path, "rb") as w:
        check("the committed WAV is the bounded window, not all 10 s",
              w.getnframes() <= 4 * 16000, str(w.getnframes()))

    print("discard drops the buffer without writing")
    c.feed(secs(1)); c.discard()
    check("discard emptied the buffer", len(c._buf) == 0)
    check("...and wrote nothing new", len(list(d2.glob("*.wav"))) == 1)

    print("the directory is capped by file count; oldest go first")
    d3 = Path(tempfile.mkdtemp())
    c = UC.UtterCapture(d3, enabled=True, seconds=1, max_files=3)
    for i in range(5):
        c.feed(secs(0.2))
        c.commit(f"2026-09-09T15:00:0{i}")
    wavs = sorted(p.name for p in d3.glob("*.wav"))
    check("no more than max_files kept", len(wavs) == 3, str(wavs))
    check("the survivors are the newest",
          wavs == ["20260909T150002.wav", "20260909T150003.wav", "20260909T150004.wav"], str(wavs))

    print("configure() can turn it on and off, and off clears the buffer")
    c2 = UC.UtterCapture(Path(tempfile.mkdtemp()))
    c2.configure(enabled=True)
    c2.feed(secs(1))
    check("on: it buffers", len(c2._buf) > 0)
    c2.configure(enabled=False)
    check("off: the buffer is dropped at once", len(c2._buf) == 0)

    print("a bad path never raises")
    c3 = UC.UtterCapture(Path("/nonexistent-xyz/deeper"), enabled=True, seconds=2)
    c3.feed(secs(1))
    try:
        r = c3.commit("2026-09-09T16:00:00")
        check("commit swallowed the OSError and returned None", r is None)
    except Exception as exc:  # noqa: BLE001
        check("commit swallowed the OSError and returned None", False, repr(exc))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

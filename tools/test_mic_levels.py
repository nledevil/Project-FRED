#!/usr/bin/env python3
"""Check the mic level history the panel's meter is drawn from.

The meter exists because no single number from this hardware can honestly be
turned into "muted": the PowerConf gates a quiet room to exact zeros, the same
thing a muted mic produces. So the listener reports what it heard and refuses to
diagnose, and these checks are mostly about that refusal holding — that silence
stays distinguishable from a low level, and that a paused mic is never reported
as a silent one.

Runs against a Listener built without hardware; _note_level is fed raw PCM
directly, which is exactly what the capture loop hands it.

    python3 tools/test_mic_levels.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import listener as L                            # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def chunk(*samples: int) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def main() -> int:
    lis = L.Listener(on_command=lambda *a: None, device="null")

    print("levels follow what was fed in")
    for peak in (0, 100, -3000, 12000):
        lis._note_level(chunk(peak, 0, -peak // 2 or 0))
    s = lis.status()
    check("one entry per chunk", len(s["levels"]) == 4, f"{s['levels']}")
    check("peaks are absolute values", s["levels"] == [0, 100, 3000, 12000],
          f"{s['levels']}")
    check("the last chunk is still reported on its own", s["peak"] == 12000,
          f"peak={s['peak']}")
    check("peak_recent is the loudest of the window", s["peak_recent"] == 12000,
          f"peak_recent={s['peak_recent']}")
    check("full_scale is int16", s["full_scale"] == 32767, f"{s['full_scale']}")

    print("silence is a value, not a gap")
    # The distinction the whole feature rests on: a quiet room and a muted mic
    # both arrive as exact zeros, and the meter must show them as bars at zero
    # rather than as no data at all.
    for _ in range(6):
        lis._note_level(chunk(0, 0, 0))
    s = lis.status()
    check("zeros are recorded, not skipped", len(s["levels"]) == 10,
          f"{len(s['levels'])} entries")
    check("...and they are actually zero", s["levels"][-6:] == [0] * 6)
    check("peak_recent still remembers the loud part", s["peak_recent"] == 12000)

    print("the window is bounded")
    for i in range(L.LEVEL_HISTORY * 2):
        lis._note_level(chunk(i % 500))
    s = lis.status()
    check("history is capped", len(s["levels"]) == L.LEVEL_HISTORY,
          f"{len(s['levels'])} == {L.LEVEL_HISTORY}")
    check("the loud chunk has scrolled off", s["peak_recent"] < 12000,
          f"peak_recent={s['peak_recent']}")
    check("six seconds of history at 125ms a chunk",
          abs(L.LEVEL_HISTORY * 0.125 - 6.0) < 0.001, f"{L.LEVEL_HISTORY} chunks")

    print("a paused mic is not a silent one")
    # The original bug this whole area guards against: reporting a mic that
    # isn't capturing as one that is hearing nothing.
    check("capturing is false with no thread", not lis.status()["capturing"])
    check("silent_for is None rather than 0 when not capturing",
          lis.status()["silent_for"] is None, f"{lis.status()['silent_for']!r}")
    lis._paused.set()
    check("...and still None while paused", lis.status()["silent_for"] is None)
    check("levels keep flowing while paused, so the meter does not blank",
          len(lis.status()["levels"]) == L.LEVEL_HISTORY)

    print("odd-length chunks are survivable")
    before = list(lis.status()["levels"])
    lis._note_level(b"\x01\x02\x03")            # not a whole number of int16s
    check("a torn chunk is dropped, not crashed",
          lis.status()["levels"] == before)

    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

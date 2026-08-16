#!/usr/bin/env python3
"""The heard log's promises, without a robot.

    venv/bin/python tools/test_heardlog.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inmoov.heardlog import HeardLog  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "heard.jsonl"
    log = HeardLog(tmp)

    log.append("what time is it", "voice", "matched", action="ask_time", reply="It is noon.")
    log.append("tell me about your cervos", "voice", "claude",
               reply="I have fourteen servos...", event=True)
    log.append("open your mouth", "text", "matched", action="open_mouth")
    log.append("", "voice", "claude")                 # empty: must not be a row
    log.append("zzz unreachable", "voice", "error", event=True)

    rows = log.tail()
    check("four rows, empties dropped", len(rows) == 4, str(len(rows)))
    check("newest first", rows[0]["heard"] == "zzz unreachable")

    missed = log.tail(only_missed=True)
    check("misses exclude the matcher's catches",
          [r["heard"] for r in missed] == ["zzz unreachable",
                                           "tell me about your cervos"],
          str([r["heard"] for r in missed]))
    check("the event flag survives", missed[1]["event"] is True)

    voice = log.tail(only_voice=True)
    check("voice-only drops what was typed",
          all(r["source"] == "voice" for r in voice) and len(voice) == 3)

    check("replies are capped, not transcripts",
          len(json.loads(log.raw().splitlines()[1])["reply"]) <= 120)

    # the file survives a new process: a second instance reads the first's rows
    check("a fresh instance sees the same file", len(HeardLog(tmp).tail()) == 4)

    # the size backstop drops the oldest half
    small = HeardLog(tmp)
    import inmoov.heardlog as hl
    old_max = hl.MAX_BYTES
    hl.MAX_BYTES = 400
    try:
        for i in range(30):
            small.append(f"filler number {i}", "voice", "claude")
    finally:
        hl.MAX_BYTES = old_max
    kept = small.tail(limit=1000)
    check("the backstop keeps the newest rows",
          kept[0]["heard"] == "filler number 29" and len(kept) < 34,
          f"{len(kept)} rows")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}: " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

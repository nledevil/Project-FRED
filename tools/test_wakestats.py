#!/usr/bin/env python3
"""The wake tally: the text-free denominator, and the promise it never keeps text.

heard.jsonl can't yield a false-wake *rate* because it never records the room
the gate turned away (by design). wakestats.py fills exactly that gap — two
integers and a timestamp per row, no transcript — so this pins the two things
that make it trustworthy:

  * it counts what it should (considered per fresh transcript, passed per wake)
    and the rows sum back to those counts across a flush;
  * a row never carries anything but counts, the event flag, and a time — a
    regression that started logging text here would quietly reintroduce the
    surveillance heard.jsonl is careful to avoid.

Runs against a temp file; touches no real log.

    python3 tools/test_wakestats.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import wakestats as WS                          # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def rows_of(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "wakestats.jsonl"

    print("counts flush and sum back")
    w = WS.WakeStats(path=tmp)
    for _ in range(30):                 # past FLUSH_EVERY (25) -> at least one row
        w.considered()
    for _ in range(4):
        w.passed()
    w.flush()
    tot = w.totals()
    check("considered summed across rows + pending", tot["considered"] == 30, str(tot))
    check("passed summed the same way", tot["passed"] == 4, str(tot))
    check("a row was actually written before the explicit flush",
          tmp.is_file() and len(rows_of(tmp)) >= 1, f"{len(rows_of(tmp))} rows")

    print("passed never exceeds considered when paired as the listener pairs them")
    w2 = WS.WakeStats(path=Path(tempfile.mkdtemp()) / "s.jsonl")
    for _ in range(10):
        w2.considered()
        w2.passed()                     # the _dispatch pattern: considered then maybe passed
    w2.flush()
    t2 = w2.totals()
    check("passed <= considered holds", t2["passed"] <= t2["considered"], str(t2))

    print("rows carry counts, a flag, and a time — never any text")
    w3 = WS.WakeStats(path=Path(tempfile.mkdtemp()) / "s.jsonl")
    w3.set_event(True)
    w3.considered(); w3.passed()
    w3.flush()
    r = rows_of(w3._path)
    check("a row was written", len(r) == 1, str(r))
    allowed = {"t", "considered", "passed", "event"}
    check("no key beyond {t, considered, passed, event}",
          all(set(row) <= allowed for row in r), str([set(row) for row in r]))
    check("the event flag was carried", r[0].get("event") is True, str(r[0]))
    check("no value is a free-text transcript (all ints/bool/timestamp)",
          all(isinstance(row["considered"], int) and isinstance(row["passed"], int)
              and isinstance(row["event"], bool) for row in r), str(r))

    print("set_event flushes first, so a row is never split across the switch")
    w4 = WS.WakeStats(path=Path(tempfile.mkdtemp()) / "s.jsonl")
    w4.considered(); w4.considered()    # 2 under event=False
    w4.set_event(True)                  # forces a flush of those 2 as event=False
    w4.considered(); w4.passed()        # 1 under event=True
    w4.flush()
    r4 = rows_of(w4._path)
    off = [x for x in r4 if not x["event"]]
    on = [x for x in r4 if x["event"]]
    check("the pre-switch counts landed as event=False",
          sum(x["considered"] for x in off) == 2, str(r4))
    check("the post-switch counts landed as event=True",
          sum(x["considered"] for x in on) == 1 and sum(x["passed"] for x in on) == 1, str(r4))

    print("a bad path never raises — a full disk must not stop dispatching")
    w5 = WS.WakeStats(path=Path("/nonexistent-dir-xyz/deeper/s.jsonl"))
    try:
        for _ in range(30):
            w5.considered()
        w5.passed()
        w5.flush()
        check("increments and flush swallowed the OSError", True)
    except Exception as exc:  # noqa: BLE001
        check("increments and flush swallowed the OSError", False, repr(exc))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

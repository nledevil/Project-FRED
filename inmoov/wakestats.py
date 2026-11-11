"""How often the wake gate was *asked*, so a false-wake rate finally has a below.

logs/heard.jsonl records what got *through* the wake gate — by design it never
logs the room (see heardlog.py), which is the right privacy line and also the
reason tools/wake_audit.py says, in its own docstring, that it "has no
denominator either." You can count the false wakes whose text still reads as
unaddressed, but not the thousands of sentences the gate correctly ignored, so
"one bad wake" and "one bad wake in ten thousand" look identical.

This closes that gap with the one thing heard.jsonl must not keep: a bare tally.
Every fresh transcript the listener evaluates for his name is a *considered*;
every one that passes the gate (a command, or a bare "Fred") is a *passed*. No
text, ever — two integers and a timestamp — so it says nothing about who was in
the room, only how loud the room was against how often he answered it.

Rows are deltas since the last flush, time-bucketed, appended to a JSONL that
survives the pack-up power cycle. Summed, they are wake_audit's denominator;
read as a series, they are "was he trigger-happy in the second hall but not the
first." Never raises — a full disk must not make the listener stop dispatching.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "logs" / "wakestats.jsonl"
MAX_BYTES = 2 * 1024 * 1024          # counts are tiny; this is decades of fairs
FLUSH_EVERY = 25                     # write a row at least this often (considereds)
FLUSH_SECS = 300.0                   # ...or this often, so a quiet hour still lands


class WakeStats:
    def __init__(self, path: Path = PATH):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._considered = 0            # pending, since the last flushed row
        self._passed = 0
        self._event = False             # is this a fair right now (set by event mode)
        self._last_flush = time.monotonic()

    def set_event(self, on: bool) -> None:
        """Tag subsequent rows with whether event mode is on — the false wakes
        that matter are the ones in front of a crowd. Flushes first, so a row is
        never split across the switch."""
        with self._lock:
            self._flush_locked()
            self._event = bool(on)

    def considered(self) -> None:
        """A fresh (non-continuation) transcript was evaluated against his name."""
        with self._lock:
            self._considered += 1
            self._maybe_flush_locked()

    def passed(self) -> None:
        """...and it passed the gate. Always paired with a considered() before it,
        so passed <= considered always holds when the rows are summed."""
        with self._lock:
            self._passed += 1

    # -- flushing ----------------------------------------------------------
    def _maybe_flush_locked(self) -> None:
        if (self._considered >= FLUSH_EVERY
                or time.monotonic() - self._last_flush >= FLUSH_SECS):
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._considered == 0 and self._passed == 0:
            return
        row = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "considered": self._considered,
            "passed": self._passed,
            "event": self._event,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a") as f:
                f.write(json.dumps(row) + "\n")
            if self._path.stat().st_size > MAX_BYTES:
                self._trim()
        except OSError:
            return                      # drop the row, keep the counts pending
        self._considered = 0
        self._passed = 0
        self._last_flush = time.monotonic()

    def flush(self) -> None:
        """Force the pending counts out — for a clean shutdown or a test."""
        with self._lock:
            self._flush_locked()

    def _trim(self) -> None:
        """Drop the oldest half. Called with the lock held."""
        lines = self._path.read_text().splitlines(keepends=True)
        self._path.write_text("".join(lines[len(lines) // 2:]))

    def totals(self) -> dict:
        """Summed considered/passed across the file plus what is still pending —
        the denominator wake_audit divides by."""
        c = p = 0
        try:
            with self._lock:
                lines = self._path.read_text().splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            c += int(row.get("considered", 0))
            p += int(row.get("passed", 0))
        with self._lock:
            c += self._considered
            p += self._passed
        return {"considered": c, "passed": p}


# One counter per process, shared by the listener that writes it and any tool
# that reads it — module-level for the same reason the heard log is.
_STATS = WakeStats()


def stats() -> WakeStats:
    return _STATS


def set_event(on: bool) -> None:
    _STATS.set_event(on)

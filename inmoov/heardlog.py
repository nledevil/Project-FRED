"""What FRED heard, and what he did with it — the tuning set a fair produces.

The speech model was chosen on cost plus one accuracy win on clean synthetic
speech; the case that actually fails is a child at three feet in a hall of four
hundred, and nothing on a bench can synthesise that. This log is how that case
gets collected: every utterance that reached the brain, with the route it took,
appended to a JSONL file that survives restarts and the pack-up power cycle.

The interesting rows are the ones where ``route`` is not ``matched``: the
command matcher recognised nothing, so either the phrasing is one the matcher
should learn, or — the case this exists for — the recogniser misheard it
("servers" for "servos" is the known one). Reviewed after an event, on the
admin page's Heard tab, or pulled whole as JSONL.

**What is deliberately not logged: anything FRED was not addressed with.** The
listener transcribes the whole room while it waits for the wake word, and a
fair's worth of bystander conversation is surveillance, not tuning data. Only
what passed the wake gate (or was typed at the panel) lands here. The one cost
is that a *misheard wake word* never appears — a child whose "hey fred" came
out "hay fread" was dropped silently — and that trade is taken knowingly.

Unbounded on purpose, with a size backstop: a busy fair is a few hundred rows
of text a day, and the file that runs away is the file something is wrong
with. At the cap, the oldest half is dropped — the newest data is always the
data someone is about to review.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "logs" / "heard.jsonl"
MAX_BYTES = 4 * 1024 * 1024          # the backstop, not the plan


class HeardLog:
    def __init__(self, path: Path = PATH):
        self._path = Path(path)
        self._lock = threading.Lock()

    def append(self, heard: str, source: str, route: str,
               action: str = "", reply: str = "", event: bool = False) -> None:
        """One utterance, one line. Never raises — a full disk must not make
        FRED stop answering people."""
        heard = (heard or "").strip()
        if not heard:
            return
        row = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "heard": heard,
            "source": source,               # voice | text
            "route": route,                 # matched | claude | local | none | error
            "action": action or "",         # the matcher's pattern, when matched
            "reply": (reply or "")[:120],   # enough to judge the answer, not a transcript
            "event": bool(event),           # was event mode on — i.e. was this a fair
        }
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a") as f:
                    f.write(json.dumps(row) + "\n")
                if self._path.stat().st_size > MAX_BYTES:
                    self._trim()
        except OSError:
            pass

    def _trim(self) -> None:
        """Drop the oldest half. Called with the lock held."""
        lines = self._path.read_text().splitlines(keepends=True)
        self._path.write_text("".join(lines[len(lines) // 2:]))

    def tail(self, limit: int = 200, only_missed: bool = False,
             only_voice: bool = False) -> list[dict]:
        """The newest rows, newest first.

        ``only_missed`` keeps the rows where the matcher recognised nothing —
        the review set. ``only_voice`` drops what was typed, because typed text
        says nothing about the microphone.
        """
        try:
            with self._lock:
                lines = self._path.read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if only_missed and row.get("route") == "matched":
                continue
            if only_voice and row.get("source") != "voice":
                continue
            out.append(row)
            if len(out) >= limit:
                break
        return out

    def raw(self) -> str:
        """The whole file, for download."""
        try:
            with self._lock:
                return self._path.read_text()
        except OSError:
            return ""


# One log per process, shared by the assistant that writes and the API that
# reads — module-level for the same reason convlog is passed around as one
# instance: two logs of what was heard would disagree.
_LOG = HeardLog()


def log() -> HeardLog:
    return _LOG

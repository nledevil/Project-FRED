"""Live voice state shared from display_control.py to the animation child.

The daemon receives FRED's voice state over HTTP; the animation runs as a
separate child process and needs it at 30fps. This is the seam between them: the
daemon calls publish(), the animation polls VoiceFeed.

A file in /dev/shm (tmpfs, so RAM — no disk I/O) rather than a pipe or socket:
the child can start, die and respawn without the daemon knowing or caring, and
there's no buffer anyone can fail to drain and wedge. Writes are atomic
(write-temp-then-rename), so the child never reads a half-written frame.

Timing: the head sends ``starts_in`` — seconds from *now* until frame 0 is
audible. The daemon converts it to an absolute ``play_at`` on this Pi's
monotonic clock the moment it arrives, so the child doesn't inherit any of the
transfer latency; it just compares play_at against its own time.monotonic().
Same machine, same CLOCK_MONOTONIC, no clock sync needed.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

VOICE_PATH = Path("/dev/shm/inmoov-voice.json")

STATES = ("idle", "listening", "thinking", "speaking")


def publish(data: dict, path: Path = VOICE_PATH) -> None:
    """Atomically replace the shared voice state."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)               # atomic: readers see old or new, never half
    except OSError:
        pass                                # no /dev/shm: animations just stay idle


class VoiceFeed:
    """Reads the shared voice state. Cheap enough to poll every frame.

    Re-parses only when the file's mtime actually changes — an idle FRED costs
    one stat() per frame, which is nothing.
    """

    def __init__(self, path: Path = VOICE_PATH):
        self._path = path
        self._mtime = -1.0
        self._data: dict = {}

    def poll(self) -> dict:
        """Latest published state, or {} if nothing has been published."""
        try:
            mtime = os.stat(self._path).st_mtime
        except OSError:
            return self._data               # daemon hasn't published yet
        if mtime != self._mtime:
            try:
                self._data = json.loads(self._path.read_text())
                self._mtime = mtime
            except (OSError, ValueError):
                pass                        # mid-rename or corrupt: keep the last good
        return self._data

    def state(self) -> str:
        s = self.poll().get("state", "idle")
        return s if s in STATES else "idle"

    def level(self, now: float | None = None) -> float:
        """Loudness (0..1) at this instant, straight from FRED's real envelope.

        Returns 0.0 outside the clip — including *before* it starts, so the
        lead-in silence reads as a closed mouth rather than a jump.
        """
        d = self.poll()
        levels = d.get("levels")
        play_at, frame_dt = d.get("play_at"), d.get("frame_dt")
        if not levels or play_at is None or not frame_dt:
            return 0.0
        i = int(((now if now is not None else time.monotonic()) - play_at) / frame_dt)
        if 0 <= i < len(levels):
            return float(levels[i])
        return 0.0

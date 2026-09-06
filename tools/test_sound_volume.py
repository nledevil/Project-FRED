#!/usr/bin/env python3
"""Check the playback-level cache: that it caches, expires, and never lies.

The cache exists because Sound.settings() is read from /api/state, which the
chest polls continuously — uncached, every poll spawned an amixer, and the
worst case was a *missing* card, where the failing read ran just as often for a
number that could not change. So the thing worth testing is not the reading
(amixer's job) but the caching around it: that a burst of pollers costs one
read, that the number still goes stale on time, and that setting the volume
never leaves a panel showing the old one.

Runs with no sound card and no amixer: the read itself is stubbed, which is the
point — this is a test about how often we ask, not about what comes back.

    python3 tools/test_sound_volume.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov.sound import Sound, VOLUME_TTL                # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def make(value=70, delay=0.0):
    """A Sound whose card read is counted rather than performed."""
    s = Sound(device="plughw:Fake,0")
    s._volume_ctl = "PCM,0"          # as if discovered while the card was there
    calls = []

    def fake_read():
        calls.append(time.monotonic())
        if delay:
            time.sleep(delay)
        return value
    s._read_volume = fake_read
    return s, calls


print("the cache")
s, calls = make(70)
check("the first read reaches the card", s.volume() == 70 and len(calls) == 1,
      f"{len(calls)} read(s)")

for _ in range(50):
    s.volume()
check("fifty more polls inside the TTL cost nothing", len(calls) == 1,
      f"{len(calls)} read(s) for 51 polls")

print("going stale")
s, calls = make(70)
s.volume()
s._vol_at = time.monotonic() - (VOLUME_TTL + 0.01)      # age it past the TTL
s.volume()
check("a reading older than the TTL is taken again", len(calls) == 2,
      f"{len(calls)} read(s)")

print("a herd on an expired cache")
# The real shape of the load: chest, panel and admin all polling /api/state,
# arriving together on a cache that has just expired. Without the lock each
# would spawn its own amixer.
s, calls = make(70, delay=0.05)
start = threading.Barrier(9)


def poll():
    start.wait()
    s.volume()


threads = [threading.Thread(target=poll) for _ in range(8)]
for t in threads:
    t.start()
start.wait()
for t in threads:
    t.join()
check("eight simultaneous pollers make one read between them", len(calls) == 1,
      f"{len(calls)} read(s) for 8 pollers")

print("setting the volume")
# set_volume must drop the cache. Without this the panel that just moved the
# slider reads back the *old* number for up to a second and snaps the knob
# backwards under the finger that moved it.
s, calls = make(70)
s.volume()                                   # prime it
before = len(calls)
s._amixer = lambda *a: "ok"                  # pretend the card took the set
s.set_volume(30)
s.volume()
check("a set makes the next read go to the card", len(calls) == before + 1,
      f"{len(calls) - before} read(s) after the set")

print("no mixer at all")
# The case that made this necessary: speakerphone unplugged, everything still
# polling. None is a perfectly good thing to cache.
s = Sound(device="plughw:Gone,0")
s._mixer_ok = False                          # as if amixer were missing outright
reads = []
s._read_volume = lambda: reads.append(1) or None
check("a missing mixer reads None", s.volume() is None)
for _ in range(50):
    s.volume()
check("...and does not keep asking", len(reads) == 1,
      f"{len(reads)} read(s) for 51 polls")

print("FAILED" if FAILURES else "all checks passed")
sys.exit(1 if FAILURES else 0)

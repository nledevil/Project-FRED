#!/usr/bin/env python3
"""A snapshot must be a picture of now, or nothing at all.

The bug this exists to stop was silent, convincing, and lasted an evening. A
child asked FRED what he could see and he described her father — glasses, beard,
grey shirt — in detail, accurately, from a photograph taken earlier. He was not
hallucinating and he was not remembering: he was handed a real, sharp, entirely
current-looking JPEG, and the only thing wrong with it was when it was taken.

``_FrameBroker`` deliberately outlives the source, so that viewers surviving a
brief reconnect do not lose the picture. Between snapshots nothing streams and
nothing holds the camera, so the source stops and its last frame stays in the
broker. ``snapshot()`` took that frame if one was present and only waited when
the broker was empty — so after the first capture of a session it never waited
again, and returned the same bytes for hours.

The live MJPEG stream never had this: it waits on the condition for new frames.
Only the snapshot path took what was lying around, and that is the path the
vision tool uses.

So the rule under test is narrow and absolute: a snapshot waits for a frame
published *after* it was asked for. If none comes, it returns None, and the
caller says he cannot see — which is the honest answer, and unlike a stale
picture it is one somebody can notice.

No camera needed: the broker is driven directly.

    python3 tools/test_camera_freshness.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import camera as CAM                            # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeCamera:
    """Just the parts of Camera.snapshot that matter, over a real broker."""

    def __init__(self):
        self._broker = CAM._FrameBroker()
        self.holds = 0

    available = staticmethod(lambda: True)

    def acquire(self):
        self.holds += 1

    def release(self):
        self.holds -= 1

    snapshot = CAM.Camera.snapshot


def publish_after(broker, delay, payload):
    def go():
        time.sleep(delay)
        broker.publish(payload)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def main() -> int:
    print("the broker keeps its frame when the source stops — by design")
    b = CAM._FrameBroker()
    b.publish(b"OLD-PICTURE")
    check("a published frame stays available", b.frame == b"OLD-PICTURE")
    check("...and is stamped with when it arrived", b.at > 0.0, str(b.at))

    print("a snapshot will not hand back that leftover")
    # The exact shape of the bug: a frame is sitting there, nothing is
    # publishing, and the old code returned it instantly.
    cam = FakeCamera()
    cam._broker.publish(b"OLD-PICTURE")
    t0 = time.monotonic()
    got = cam.snapshot(timeout=0.4)
    waited = time.monotonic() - t0
    check("returns None rather than the stale frame", got is None, repr(got))
    check("...having actually waited for a new one", waited >= 0.35,
          f"{waited:.2f}s")
    check("...and let the camera go again", cam.holds == 0, str(cam.holds))

    print("a fresh frame is returned, even with an older one present")
    cam = FakeCamera()
    cam._broker.publish(b"OLD-PICTURE")
    publish_after(cam._broker, 0.15, b"NEW-PICTURE")
    got = cam.snapshot(timeout=2.0)
    check("gets the new one", got == b"NEW-PICTURE", repr(got))

    print("an empty broker behaves the same way")
    cam = FakeCamera()
    publish_after(cam._broker, 0.15, b"FIRST-PICTURE")
    check("waits for the first frame", cam.snapshot(timeout=2.0) == b"FIRST-PICTURE")
    cam = FakeCamera()
    check("and gives up if none comes", cam.snapshot(timeout=0.3) is None)

    print("two snapshots in a row are two different pictures")
    # What the person in front of him actually experiences: ask twice, get two
    # answers about two moments, not one answer twice.
    cam = FakeCamera()
    shots = []
    for i in range(3):
        publish_after(cam._broker, 0.05, f"FRAME-{i}".encode())
        shots.append(cam.snapshot(timeout=2.0))
    check("three asks, three distinct frames", len(set(shots)) == 3, str(shots))

    print("the camera is released even when nothing arrives")
    cam = FakeCamera()
    cam.snapshot(timeout=0.2)
    check("no hold is leaked on the timeout path", cam.holds == 0, str(cam.holds))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

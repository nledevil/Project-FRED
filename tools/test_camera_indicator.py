#!/usr/bin/env python3
"""The privacy LED means "the camera is on" — every way the camera turns on.

FRED's privacy story is that a lit LED means the camera is capturing. That was
only true for MJPEG viewers; face tracking, face-recall sampling, and the
vision-tool snapshot all took a *hold* on the camera and ran it dark. R11 makes
the light follow the real invariant — lit whenever there's a viewer OR a hold —
and this pins it, because a regression here is a camera recording with the light
off, which is the one bug this feature exists to prevent.

Drives a Camera with a null backend (no hardware) and a fake indicator that
records every notify_camera call, so the transitions are exact.

    python3 tools/test_camera_indicator.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov.camera import Camera                           # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeIndicator:
    def __init__(self):
        self.calls = []          # every notify_camera(bool) in order
    def notify_camera(self, on):
        self.calls.append(bool(on))
    @property
    def state(self):
        return self.calls[-1] if self.calls else None


def make_camera():
    """A Camera with no real backend, so only the viewer/hold bookkeeping runs."""
    ind = FakeIndicator()
    cam = Camera(indicator=ind, backend="v4l2", source="/dev/does-not-exist-xyz")
    # Guarantee no hardware path fires regardless of what the box has attached.
    cam._backend = None
    cam.detected = False
    return cam, ind


def main() -> int:
    print("a hold lights the LED, like a viewer does")
    cam, ind = make_camera()
    cam.acquire()                                  # e.g. a vision-tool snapshot
    check("first hold -> LED on", ind.state is True, str(ind.calls))
    cam.acquire()                                  # a second consumer
    check("a second hold does NOT re-notify (transition only)",
          ind.calls == [True], str(ind.calls))
    cam.release()
    check("one of two holds released -> still on", ind.state is True, str(ind.calls))
    cam.release()
    check("last hold released -> LED off", ind.state is False, str(ind.calls))
    check("exactly two transitions total (on, off)", ind.calls == [True, False], str(ind.calls))

    print("a viewer keeps the light on while a hold also runs")
    cam, ind = make_camera()
    cam.acquire()                                  # tracker holds the camera
    check("hold -> on", ind.state is True)
    cam._viewers += 1                              # a stream joins (frames() body, minus the loop)
    cam._update_indicator_locked()
    check("viewer joins while held -> still on, no redundant call",
          ind.calls == [True], str(ind.calls))
    cam.release()                                  # tracker lets go, viewer remains
    check("hold gone but viewer remains -> STILL on (the bug R11 fixes)",
          ind.state is True, str(ind.calls))
    cam._viewers -= 1
    cam._update_indicator_locked()
    check("last user (the viewer) gone -> off", ind.state is False, str(ind.calls))

    print("no indicator configured is safe")
    cam2 = Camera(indicator=None, backend="v4l2", source="/dev/does-not-exist-xyz")
    cam2._backend = None
    try:
        cam2.acquire(); cam2.release()
        check("acquire/release with no LED never raises", True)
    except Exception as exc:  # noqa: BLE001
        check("acquire/release with no LED never raises", False, repr(exc))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

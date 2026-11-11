#!/usr/bin/env python3
"""The wide spotter notices a dead camera, says so, and gets it back.

The failure this guards was found by the architecture review and is this
project's known class: the PanaCast dies mid-run — unplugged, renumbered, or
wedged — and the old loop spun on a failed grab() forever. No reopen, no
last_error, status still said running: true. From the panel that read as a
healthy spotter that mysteriously saw nobody, and the only clue would have
been a bearing that never updated.

What is asserted here is the contract of the heal, driven with fake capture
objects so no camera is touched (this machine has the real one plugged in):

  * a run of failed grabs longer than LOST_AFTER declares the camera lost;
  * the stale bearing is dropped at that moment, not left to age out —
    readings from a camera that is gone are not merely old, they are unowned;
  * last_error tells the truth while the camera is down;
  * the reopen goes back through _open(), so a renumbered node is re-resolved
    by name (that path is covered by tools/test_camera_resolve.py);
  * when the camera returns, the loop resumes and last_error clears;
  * a stop() that lands while a reopen is in flight does not leak the newly
    opened device.

    python3 tools/test_spotter_heal.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import wide_spotter as W                        # noqa: E402

FAILURES: list[str] = []
LOG: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeCap:
    """A capture whose grab() answers from a script. retrieve() always fails,
    which keeps the loop's decode/detect half (and cv2) entirely out of play —
    this test is about the lifecycle, not the pixels."""

    def __init__(self, alive: bool):
        self.alive = alive
        self.released = False
        self.grabs = 0

    def grab(self) -> bool:
        self.grabs += 1
        return self.alive

    def retrieve(self):
        return False, None

    def release(self):
        self.released = True


def make_spotter():
    sp = W.WideSpotter(device="NoSuchCam", log=LOG.append)
    return sp


def run_loop(sp, seconds: float):
    sp._stop.clear()
    t = threading.Thread(target=sp._run, daemon=True)
    t.start()
    time.sleep(seconds)
    sp._stop.set()
    t.join(timeout=2.0)
    return t


def main() -> int:
    # Fast clocks for the test; restored at the end.
    real = (W.LOST_AFTER, W.REOPEN_DELAY)
    W.LOST_AFTER, W.REOPEN_DELAY = 0.15, 0.1

    try:
        print("a dead camera is declared lost, honestly")
        sp = make_spotter()
        dead = FakeCap(alive=False)
        sp._cap = dead
        sp._bearing, sp._faces = 0.5, 2       # pretend it had been seeing someone
        opens = []
        sp._open = lambda: opens.append(1) or None      # camera stays gone
        run_loop(sp, 0.6)
        check("the dying cap was released", dead.released)
        check("last_error says what happened",
              sp.last_error is not None and "stopped delivering" in sp.last_error,
              repr(sp.last_error))
        check("the stale bearing was dropped, not aged out",
              sp._bearing is None and sp._faces == 0,
              f"bearing={sp._bearing} faces={sp._faces}")
        check("...and bearing() agrees", sp.bearing() is None)
        check("the reopen kept retrying while it stayed gone", len(opens) >= 2,
              f"{len(opens)} attempts")
        check("the loss was logged once, not per retry",
              sum("stopped delivering" in l for l in LOG) == 1, str(LOG))

        print("when the camera comes back, the loop resumes")
        sp = make_spotter()
        dead = FakeCap(alive=False)
        healthy = FakeCap(alive=True)
        sp._cap = dead
        attempts = []

        def open_fails_once():
            attempts.append(1)
            if len(attempts) < 2:
                sp.last_error = "cannot open /dev/video9"    # what _open would do
                return None
            return healthy

        sp._open = open_fails_once
        run_loop(sp, 0.8)
        check("it reopened through _open()", len(attempts) >= 2, str(len(attempts)))
        check("the healthy cap is being read", healthy.grabs > 0,
              f"{healthy.grabs} grabs")
        check("last_error cleared on recovery", sp.last_error is None,
              repr(sp.last_error))
        check("...and the recovery was logged",
              any("camera back" in l for l in LOG), str(LOG[-2:]))

        print("a stop during the reopen does not leak the new device")
        sp = make_spotter()
        sp._cap = FakeCap(alive=False)
        late = FakeCap(alive=True)

        def open_after_stop():
            sp._stop.set()                    # stop() lands mid-open
            return late

        sp._open = open_after_stop
        run_loop(sp, 0.5)
        check("the just-opened cap was released", late.released)
        check("...and not adopted", sp._cap is not late)

        print("one dropped frame is not a lost camera")
        sp = make_spotter()

        class Flaky(FakeCap):
            def grab(self):
                self.grabs += 1
                return self.grabs % 3 != 0    # every third grab fails

        flaky = Flaky(alive=True)
        sp._cap = flaky
        sp._open = lambda: (_ for _ in ()).throw(AssertionError("must not reopen"))
        run_loop(sp, 0.5)
        check("intermittent single failures never trigger a reopen",
              sp._cap is flaky and sp.last_error is None,
              repr(sp.last_error))
    finally:
        W.LOST_AFTER, W.REOPEN_DELAY = real

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

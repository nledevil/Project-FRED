#!/usr/bin/env python3
"""Check the touchscreen's coordinate mapping, without a touchscreen.

The panel is rotated 180 degrees and the digitiser is not, so every tap has to
be turned to match. That went unnoticed until the settings cog stopped
responding: the numbers below are real, captured off /dev/input/event4 while
tapping the cog at the bottom right, and they arrived as the top left.

Also checks what the boot actually says, because the rotation is read from the
kernel cmdline rather than configured twice.

    python3 tools/test_touch_map.py
"""
from __future__ import annotations

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.dirname(_HERE)]       # repo layout and flattened Pi layout

import touch                                          # noqa: E402

FAILURES: list[str] = []
W, H = 800, 480


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeTouch(touch.Touch):
    """Just the mapping — no device, no ioctls."""

    def __init__(self, rotate):
        self._max_x, self._max_y = W - 1, H - 1
        self._w, self._h = W, H
        self._rot = rotate


def cmdline_with(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".cmdline", delete=False)
    f.write(text)
    f.close()
    return f.name


def main() -> int:
    print("rotation is read from the kernel cmdline")
    cases = [
        ("video=DSI-1:800x480@60,rotate=180 consoleblank=0", 180),
        ("root=PARTUUID=e9c853a5-02 video=DSI-1:800x480@60,rotate=180", 180),
        ("root=/dev/mmcblk0p2 rootwait", 0),
        ("video=HDMI-A-1:1920x1080@60", 0),
        ("video=DSI-1:800x480@60,rotate=90", 90),
        ("video=DSI-1:800x480@60,rotate=banana", 0),
    ]
    for text, want in cases:
        path = cmdline_with(text)
        got = touch.display_rotation(path)
        os.unlink(path)
        check(f"{text[:44]:44} -> {want}", got == want, str(got))
    check("a missing cmdline reads as unrotated",
          touch.display_rotation("/nonexistent") == 0)

    print("a 180-degree panel turns the tap to match")
    t = FakeTouch(180)
    # Captured from the real panel: tapping the cog at the bottom right.
    for raw, want in [((37, 42), (762, 437)),
                      ((76, 50), (723, 429)),
                      ((0, 0), (799, 479)),
                      ((799, 479), (0, 0)),
                      ((400, 240), (399, 239))]:
        got = t._scale(*raw)
        check(f"{raw} -> {want}", got == want, str(got))

    cog = t._scale(37, 42)
    check("...which lands the cog tap in the bottom-right quadrant",
          cog[0] > W / 2 and cog[1] > H / 2, str(cog))

    print("an unrotated panel is left alone")
    t0 = FakeTouch(0)
    for raw in [(37, 42), (400, 240), (799, 479)]:
        check(f"{raw} unchanged", t0._scale(*raw) == raw, str(t0._scale(*raw)))

    print("a driver reporting its own axis range is still scaled")
    t2 = FakeTouch(0)
    t2._max_x, t2._max_y = 4095, 4095
    got = t2._scale(4095, 4095)
    check("full-scale raw maps to the far corner", got == (799, 479), str(got))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

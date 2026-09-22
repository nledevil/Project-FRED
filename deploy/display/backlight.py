"""The 7" panel's backlight, on or off, through sysfs.

The chest screen "sleeping" used to mean a black rectangle over the animation.
An LCD with a black picture still glows — in a dark room it is a lit grey
slab, and it is the whole reason the screen wants to sleep in the first place.
So sleep now cuts the backlight too, here, and everything else about sleep
(the clock pausing, the touch that wakes it) stays where it was in panel.py.

The Raspberry Pi 7" display's backlight is ``/sys/class/backlight/10-0045``
(the ``rpi_touchscreen_attiny`` driver). ``bl_power`` is the switch: 0 is on,
anything else is off — 4 is FB_BLANK_POWERDOWN, the value the kernel's own
blanking uses. ``brightness`` is left alone so the level someone set survives a
nap. The touch controller (``10-0038``, ft5x06) is a separate device on the
same panel, and its event node stays up with the light off (checked on the
chest, 2026-09-19) — which is what should let a touch wake it. A real finger
on a dark screen is the one check nobody has done from a shell.

Best effort throughout: no backlight node (the NUC running the tests, a
different panel) means every call is a no-op that says so. Two callers, on
purpose: panel.py while it runs, and display_control.py whenever it stops a
panel — a panel killed mid-sleep must not leave the screen dark for whatever
comes next.
"""
from __future__ import annotations

import glob

ON, OFF = "0", "4"

_last: dict = {"value": None}


def node() -> str:
    """The bl_power file for the first backlight, or "" when there is none."""
    for path in sorted(glob.glob("/sys/class/backlight/*/bl_power")):
        return path
    return ""


def set_on(on: bool) -> bool:
    """Switch the backlight. True if a write happened, False if nothing did.

    Remembers the last value written so a caller can ask every frame without
    touching sysfs every frame — the panel's tick is what calls this.
    """
    want = ON if on else OFF
    if _last["value"] == want:
        return False
    path = node()
    if not path:
        return False
    try:
        with open(path, "w") as f:
            f.write(want)
    except OSError:
        return False
    _last["value"] = want
    return True


def force_on() -> bool:
    """Turn it on regardless of what this process last wrote — for start-up
    and for the daemon after killing a panel, when another process may have
    left it off."""
    _last["value"] = None
    return set_on(True)


def is_on() -> bool | None:
    """What sysfs says right now; None when there is no backlight to ask."""
    path = node()
    if not path:
        return None
    try:
        with open(path) as f:
            return f.read().strip() == ON
    except OSError:
        return None


if __name__ == "__main__":                                 # pragma: no cover
    import sys
    arg = (sys.argv[1:] or ["status"])[0]
    if arg in ("on", "off"):
        print("written" if set_on(arg == "on") else "no backlight, or no change")
    else:
        print({"node": node() or None, "on": is_on()})

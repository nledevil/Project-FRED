#!/usr/bin/env python3
"""Exercise the chest settings menu's PIN gate with no screen and no brain.

Two properties matter here and neither is visible by looking at the screen:

* the chest agrees with the brain about what the PIN is, digest for digest —
  the two check independently, so a mismatch would mean a PIN that opens the
  panel and not the touchscreen;
* the stop button on the keypad works while locked. That is the whole reason
  the gate was allowed to exist in front of a menu that owns the e-stop.

    python3 tools/test_pin_gate.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent.parent))     # the repo, for inmoov.auth

import numpy as np                                     # noqa: E402

import pin_gate                                        # noqa: E402
from inmoov import auth                                # noqa: E402

FAILURES: list[str] = []
W, H = 800, 480


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeNet:
    def __init__(self):
        self.calls = []

    def post_cart_stop(self):
        self.calls.append(("stop",))

    def post_cart_clear_estop(self):
        self.calls.append(("clear",))

    def post_cart_controller(self, mode):
        self.calls.append(("mode", mode))


def centre(rect):
    x0, y0, x1, y1 = rect
    return ((x0 + x1) // 2, (y0 + y1) // 2)


def press(pad, net, digits):
    keys = {label: b for label, b in pad._keys}
    for d in digits:
        pad.on_touch("down", *centre(keys[d].rect), net)


def main() -> int:
    frame = np.zeros((H, W, 3), dtype=np.float32)
    net = FakeNet()

    print("the two ends agree")
    # The brain makes the material; the chest checks against it. If these ever
    # drift apart, the PIN opens one surface and not the other.
    material = auth.make_material("4271")
    check("the chest accepts a PIN the brain hashed",
          pin_gate.check("4271", material))
    check("...and rejects a wrong one", not pin_gate.check("4270", material))
    check("the brain accepts what the chest would",
          auth.check_pin({"auth": {"pin": material}}, "4271"))
    check("a short PIN is refused", not pin_gate.check("427", material))
    check("a non-numeric PIN is refused", not pin_gate.check("42a1", material))
    check("empty material means nothing verifies", not pin_gate.check("4271", {}))
    check("junk material does not raise",
          not pin_gate.check("4271", {"salt": "zz", "hash": "nope"}))

    print("the cache")
    original = pin_gate.PIN_PATH
    pin_gate.PIN_PATH = HERE / "pin-test.json"
    try:
        pin_gate.PIN_PATH.unlink(missing_ok=True)
        check("no cache means no PIN", not pin_gate.is_set(pin_gate.load()))
        check("saving works", pin_gate.save(material))
        check("...and round-trips", pin_gate.load() == material)
        check("the cached copy still verifies",
              pin_gate.check("4271", pin_gate.load()))
        check("an empty push clears it", pin_gate.save({}) and not pin_gate.is_set(pin_gate.load()))
        pin_gate.save(material)

        print("the keypad")
        pad = pin_gate.PinPad(pin_gate.load())
        check("a cached PIN starts the menu locked", not pad.unlocked)
        press(pad, net, "4271")
        check("the right PIN unlocks it", pad.unlocked)

        pad = pin_gate.PinPad(material)
        press(pad, net, "0000")
        check("a wrong PIN does not", not pad.unlocked)
        check("...and the entry is cleared for the next try", pad._entry == "")
        press(pad, net, "42")
        keys = {label: b for label, b in pad._keys}
        pad.on_touch("down", *centre(keys["DEL"].rect), net)
        check("DEL removes one digit", pad._entry == "4")
        pad.on_touch("down", *centre(keys["CLEAR"].rect), net)
        check("CLEAR removes them all", pad._entry == "")
        press(pad, net, "4271")
        check("it still opens after fumbling", pad.unlocked)

        print("no PIN at all")
        open_pad = pin_gate.PinPad({})
        check("no PIN means the menu opens straight through", open_pad.unlocked)

        print("the stop button is not behind the gate")
        pad = pin_gate.PinPad(material)
        net.calls.clear()
        check("locked, to be clear", not pad.unlocked)
        pad.on_touch("down", *centre(pad._stop.rect), net)
        check("STOP works while locked", net.calls == [("stop",)], f"{net.calls}")
        check("...and does not unlock anything", not pad.unlocked)

        # ...including once the keypad has locked itself out.
        for _ in range(pin_gate.FREE_TRIES + 1):
            press(pad, net, "0000")
        check("too many wrong tries starts a wait", pad._wait() > 0,
              f"{pad._wait():.0f}s")
        net.calls.clear()
        press(pad, net, "4271")
        check("the right PIN is refused while waiting", not pad.unlocked)
        pad.on_touch("down", *centre(pad._stop.rect), net)
        check("STOP still works while locked out", net.calls == [("stop",)],
              f"{net.calls}")

        print("drawing")
        for name, pad_, snap in (
                ("locked", pin_gate.PinPad(material), {}),
                ("part-entered", pin_gate.PinPad(material), {}),
                ("estop latched", pin_gate.PinPad(material),
                 {"chest": {"cart": {"estop": True}}})):
            if name == "part-entered":
                press(pad_, net, "42")
            frame[:] = 0.0
            try:
                pad_.draw(frame, snap)
                drew = True
            except Exception as exc:                    # noqa: BLE001 — report it
                drew, name = False, f"{name}: {exc!r}"
            check(f"draws the {name} state", drew)
            check(f"...{name} stays finite", bool(np.isfinite(frame).all()))
        check("the stop button is painted",
              float(frame[pin_gate.STOP_Y0:pin_gate.STOP_Y1,
                          pin_gate.STOP_X0:pin_gate.STOP_X1].max()) > 0.0)
    finally:
        pin_gate.PIN_PATH.unlink(missing_ok=True)
        pin_gate.PIN_PATH = original

    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

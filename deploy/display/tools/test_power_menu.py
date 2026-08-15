#!/usr/bin/env python3
"""Check the shutdown menu: the order, the confirm, and that nothing overlaps.

Two kinds of bug live here and both have already happened once.

**The order.** The NUC bridges both wired NICs, so it is the switch for the
robot LAN: power it off before the head and nothing can reach the head to ask.
And the chest draws the screen, so it goes last. Neither is obvious from
looking at the buttons, so the sequence is asserted rather than trusted.

**The geometry.** Controls are hit-tested in a fixed order, so a button drawn
on top of another is not a cosmetic problem — it is a control that silently
does someone else's job. CANCEL originally overlapped the CHEST PI row, and
because CANCEL is checked first, tapping the chest just closed the menu.

Runs anywhere, on the Pi or not: the local power-off is injected, so this never
switches anything off. It did once, before that seam existed.

    python3 tools/test_power_menu.py
"""
from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.dirname(_HERE)]       # repo layout and flattened Pi

import power_menu as pm                              # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def drive(fail=(), pick="all", taps=2):
    """Open the menu, tap something `taps` times, return what was asked of whom."""
    order: list[str] = []

    class Net:
        def post_poweroff(self, machine):
            order.append(machine)
            return machine not in fail

    menu = pm.PowerMenu(log=lambda *_: None,
                        poweroff=lambda: order.append("chest"),
                        settle=0.02)
    menu.show()
    button = menu._all if pick == "all" else dict(menu._rows)[pick]
    x, y = button.rect[0] + 10, button.rect[1] + 10
    for _ in range(taps):
        menu.on_touch("down", x, y, Net())
    time.sleep(0.4)
    return order, menu


def rects():
    """Every tappable band in the overlay, as (label, x0, y0, x1, y1)."""
    menu = pm.PowerMenu(log=lambda *_: None, poweroff=lambda: None)
    out = [("ALL", *menu._all.rect), ("CANCEL", *menu._cancel.rect)]
    out += [(key.upper(), *button.rect) for key, button in menu._rows]
    return out


def overlap(a, b) -> bool:
    return not (a[3] <= b[1] or b[3] <= a[1] or a[4] <= b[2] or b[4] <= a[2])


def main() -> int:
    print("the confirm")
    order, menu = drive(taps=1)
    check("one tap arms and does not fire", order == [], str(order))
    check("...and says so on the button", menu._is_armed("all"))
    order, _ = drive(taps=2)
    check("the second tap fires", order != [])

    print("the order")
    order, _ = drive()
    check("all three are asked", len(order) == 3, str(order))
    check("the head goes before the brain",
          order.index("head") < order.index("nuc"),
          "the NUC is the switch; after it, the head is unreachable")
    check("this screen goes last", order[-1] == "chest")

    print("when a machine will not answer")
    order, menu = drive(fail=("head",))
    check("the brain is still asked", "nuc" in order, str(order))
    check("the chest is spared so the failure can be read",
          "chest" not in order, str(order))
    check("the failure is recorded", menu._failed == ["head"], str(menu._failed))

    print("one machine at a time")
    for pick in ("head", "nuc", "chest"):
        order, _ = drive(pick=pick)
        check(f"{pick} alone powers off only {pick}", order == [pick], str(order))

    print("nothing overlaps anything")
    boxes = rects()
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            check(f"{a[0]} does not overlap {b[0]}", not overlap(a, b),
                  f"{a[1:]} vs {b[1:]}")
    for label, x0, y0, x1, y1 in boxes:
        check(f"{label} is inside the overlay",
              pm.X0 <= x0 and x1 <= pm.X1 and pm.Y0 <= y0 and y1 <= pm.Y1,
              f"{(x0, y0, x1, y1)} vs {(pm.X0, pm.Y0, pm.X1, pm.Y1)}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failed: " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

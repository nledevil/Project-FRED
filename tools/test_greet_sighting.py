#!/usr/bin/env python3
"""V5: the wide camera greets people the ultrasonic cone never sees.

The greeter fired only on a sensor approach event. The wide spotter sees 180°,
so someone walking in from the side got tracked-toward and never greeted. Now N
consecutive spotter sightings synthesize an approach — but only through the same
gate a sensor approach uses, and only once per continuous presence. This pins
that gate, because the failure modes are both bad: greeting nobody (a flicker
detection), and greeting the same lingering person over and over.

    python3 tools/test_greet_sighting.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov.greeter import Greeter                          # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeAssistant:
    def __init__(self):
        self.speaking = False
        self.thinking = False
    def is_speaking(self):
        return self.speaking
    def is_thinking(self):
        return self.thinking
    def speak(self, line):
        pass


def gate_greeter(n=3):
    """A greeter whose on_event is mocked to a counter, isolating the N-gate."""
    g = Greeter(FakeAssistant(), sightings_to_greet=n)
    fired = []
    g.on_event = lambda node, ev: fired.append((node, ev))
    return g, fired


def main() -> int:
    print("N consecutive sightings fire exactly one approach")
    g, fired = gate_greeter(3)
    g.on_sighting(True); g.on_sighting(True)
    check("two sightings do not fire", fired == [], str(fired))
    g.on_sighting(True)
    check("the third fires one synthesized approach", len(fired) == 1, str(fired))
    check("...tagged as an approach from the wide camera",
          fired[0][1].get("event") == "approach" and "camera" in fired[0][0], str(fired))

    print("a continuous presence is greeted once, not every N frames")
    for _ in range(9):               # they keep standing there
        g.on_sighting(True)
    check("still just one greeting while they remain", len(fired) == 1, str(fired))

    print("leaving the frame re-arms it")
    g.on_sighting(False)             # they left
    g.on_sighting(True); g.on_sighting(True)
    check("two more sightings don't fire yet", len(fired) == 1, str(fired))
    g.on_sighting(True)
    check("a fresh arrival greets again", len(fired) == 2, str(fired))

    print("a broken run never reaches N (a flicker is not an approach)")
    g2, fired2 = gate_greeter(3)
    g2.on_sighting(True); g2.on_sighting(False); g2.on_sighting(True)
    g2.on_sighting(False); g2.on_sighting(True)
    check("scattered single sightings never fire", fired2 == [], str(fired2))

    print("disabled: sightings do nothing")
    g3, fired3 = gate_greeter(2)
    g3._enabled = False
    g3.on_sighting(True); g3.on_sighting(True); g3.on_sighting(True)
    check("no fire while disabled", fired3 == [], str(fired3))

    print("the real path connects: on_sighting -> on_event claims the slot")
    g4 = Greeter(FakeAssistant(), cooldown=0.0, sightings_to_greet=3)
    for _ in range(3):
        g4.on_sighting(True)
    check("three sightings claimed a greeting slot (real on_event, sets _last)",
          g4._last != 0.0, str(g4._last))

    print("...but never while he's already speaking (on_event's own gate holds)")
    fa = FakeAssistant(); fa.speaking = True
    g5 = Greeter(fa, cooldown=0.0, sightings_to_greet=3)
    for _ in range(5):
        g5.on_sighting(True)
    check("no slot claimed while he's talking", g5._last == 0.0, str(g5._last))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

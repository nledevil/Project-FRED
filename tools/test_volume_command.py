#!/usr/bin/env python3
"""What FRED may do to his own volume when asked, and what he may not.

Two things are being protected here and neither is about audio.

The first is the **band**. The panel's volume is behind the PIN deliberately —
tools/test_auth.py says why: "Zero is a mute that looks like broken hardware to
whoever is standing there, and full scale in a hall is its own disruption." A
spoken command has no PIN and cannot have one, because anybody standing in front
of him can talk to him. So the voice path gets a floor it cannot mute below and a
ceiling it cannot deafen a room with, and that band is policy rather than taste.

The second is the **local matcher**, which nearly broke something else. "Turn it
up" and "turn left" are one word apart, and the head-rotation rules were already
there. A volume rule that swallowed "turn left" would stop his neck working, and
would do it quietly, so those phrasings are checked here alongside the new ones.

Runs with no sound card: Sound is stubbed, which is the point — this is about
the decisions, not about amixer.

    python3 tools/test_volume_command.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import commands as C                            # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeSound:
    """Just enough Sound to drive the decisions. Records what it was told."""

    def __init__(self, level=50, has_mixer=True, ok=True):
        self.level, self.has_mixer, self.ok = level, has_mixer, ok
        self.sets: list[int] = []

    def available(self):
        return True

    def volume(self):
        return self.level if self.has_mixer else None

    def set_volume(self, pct):
        self.sets.append(int(pct))
        if self.ok:
            self.level = int(pct)
        return self.ok


def ctx_with(sound):
    return types.SimpleNamespace(sound=sound)


def main() -> int:
    print("asking moves it, by a step")
    s = FakeSound(50)
    C.execute_action(ctx_with(s), "set_volume", direction="up")
    check("up steps up", s.level == 50 + C.VOICE_VOLUME_STEP, str(s.level))
    C.execute_action(ctx_with(s), "set_volume", direction="down")
    check("down comes back", s.level == 50, str(s.level))
    C.execute_action(ctx_with(s), "set_volume", percent=42)
    check("an absolute level is taken", s.level == 42, str(s.level))

    print("he cannot mute or deafen himself by voice")
    # The whole reason this is not just a pass-through to Sound.set_volume: at an
    # event, "Fred, mute yourself" is a thing a child will say, and a robot that
    # goes silent reads as broken to everyone else standing there.
    s = FakeSound(50)
    C.execute_action(ctx_with(s), "set_volume", percent=0)
    check("zero is refused, floored instead", s.level == C.VOICE_VOLUME_MIN,
          str(s.level))
    s = FakeSound(50)
    C.execute_action(ctx_with(s), "set_volume", percent=100)
    check("full scale is refused, capped instead", s.level == C.VOICE_VOLUME_MAX,
          str(s.level))
    s = FakeSound(C.VOICE_VOLUME_MIN)
    for _ in range(5):
        C.execute_action(ctx_with(s), "set_volume", direction="down")
    check("stepping down repeatedly cannot walk past the floor",
          s.level == C.VOICE_VOLUME_MIN, str(s.level))
    check("...and stops calling the card once it is there", len(s.sets) == 0,
          f"{len(s.sets)} writes")

    print("it says something useful when it did nothing")
    s = FakeSound(C.VOICE_VOLUME_MAX)
    out = C.execute_action(ctx_with(s), "set_volume", direction="up")
    # "Done" when nothing moved is how a robot gets asked the same thing four
    # times in a row.
    check("says it is already at the limit", "as loud as" in out.lower(), out)
    check("...and names the level", str(C.VOICE_VOLUME_MAX) in out, out)
    s = FakeSound(50)
    out = C.execute_action(ctx_with(s), "set_volume", percent=100)
    check("a capped request says so rather than claiming success",
          "as far as i go" in out.lower(), out)
    check("...and points at the panel for more", "panel" in out.lower(), out)

    # "Already at 60" and "already at my ceiling" are different facts, and
    # confusing them told somebody 60 percent was his limit when it is 85.
    s = FakeSound(60)
    out = C.execute_action(ctx_with(s), "set_volume", percent=60)
    check("asking for the level it is already at does not claim a limit",
          "limit" not in out.lower() and "as loud as" not in out.lower(), out)
    check("...and still says where it is", "60" in out, out)
    check("...and writes nothing", s.sets == [], str(s.sets))

    print("asking without asking for a change just reports")
    s = FakeSound(63)
    out = C.execute_action(ctx_with(s), "set_volume")
    check("no direction and no percent reports the level", "63" in out, out)
    check("...and changes nothing", s.sets == [], str(s.sets))

    print("no mixer, or a card that refuses, is said plainly")
    s = FakeSound(50, has_mixer=False)
    out = C.execute_action(ctx_with(s), "set_volume", direction="up")
    check("a card with no level control says so", "no level control" in out, out)
    s = FakeSound(50, ok=False)
    out = C.execute_action(ctx_with(s), "set_volume", direction="up")
    check("a refused write is reported, not claimed", "couldn't" in out.lower(), out)
    out = C.execute_action(types.SimpleNamespace(sound=None), "set_volume",
                           direction="up")
    check("no audio at all does not raise", "audio" in out.lower(), out)
    s = FakeSound(50)
    out = C.execute_action(ctx_with(s), "set_volume", percent="loud")
    check("a level that is not a number is refused", s.sets == [], out)

    print("the spoken phrasings reach it")
    for text, want in [("turn it up", "up"), ("louder", "up"),
                       ("speak up", "up"), ("volume up", "up"),
                       ("turn it down", "down"), ("quieter", "down"),
                       ("keep it down", "down")]:
        got = C.match_local(text)
        check(f"{text!r} -> volume {want}",
              got is not None and got[0] == "set_volume"
              and got[1].get("direction") == want, str(got))
    got = C.match_local("how loud are you")
    check("'how loud are you' reports rather than changes",
          got == ("set_volume", {}), str(got))

    print("...without stealing the head's phrasings")
    # The near-miss this file exists for. These were matching turn_head long
    # before there was a volume command, and they must go on doing so.
    for text, want in [("turn left", "turn_head"), ("turn right", "turn_head"),
                       ("turn your head left", "turn_head"),
                       ("face left", "turn_head"),
                       ("turn around", "drive"),
                       ("look up", "look"), ("look down", "look")]:
        got = C.match_local(text)
        check(f"{text!r} still means {want}",
              got is not None and got[0] == want, str(got))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

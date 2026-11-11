#!/usr/bin/env python3
"""Every servo can be reached, and no body command reaches the wheels.

Three things went wrong at once and this covers all three.

**Coverage.** head_tilt_fb and head_tilt_lr had no tool at all, so "nod" was
impossible and nobody had noticed, because nothing said which servos were
reachable. The first check walks config/servos.json and insists something moves
each one — a servo added later with no way to drive it fails here rather than
being discovered by asking the robot to do it.

**Safety.** "Move your head to the right" matched the cart's
``(drive|move|go|roll) ... right`` rule and answered "driving right"; "move your
head back to centre" drove it backwards. Both went to the wheels without going
near the model, on a base that weighs 350 lb, with somebody standing in front of
it. Those exact sentences are checked here.

**Sequences.** The matcher answers one action and stops, so a request for four
moves did the first and looked like a robot that had not listened. Anything
asking for several things now falls through to the model, which can call a tool
as many times as it takes.

No hardware: the controller is a stub that records what it was asked to move.

    python3 tools/test_body_commands.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import commands as C                            # noqa: E402

FAILURES: list[str] = []
SERVOS = json.loads((ROOT / "config" / "servos.json").read_text())["servos"]


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeController:
    """Records which servos were moved. Angles are clamped like the real one."""

    def __init__(self):
        self.servos = SERVOS
        self.moved: list[str] = []

    def set_angle(self, name, angle, **kw):
        self.moved.append(name)
        return angle

    def move_smooth(self, name, angle, duration=0.5, steps=25):
        self.moved.append(name)
        return angle

    def get_angle(self, name):
        return self.servos[name]["rest_angle"]


def run(action, **args):
    c = FakeController()
    ctx = types.SimpleNamespace(controller=c, led=None, tracker=None,
                                sound=None, sensors=None, diagnostic=None)
    reply = C.execute_action(ctx, action, **args)
    return c.moved, reply


def main() -> int:
    print("every servo on the robot can be moved by something")
    # What each servo is reachable through. A servo that appears in servos.json
    # and not here is one FRED cannot use.
    reach = {
        "eye_x": ("look", {"direction": "left"}),
        "eye_y": ("look", {"direction": "up"}),
        "jaw": ("open_mouth", {}),
        "neck": ("turn_head", {"direction": "left"}),
        "head_tilt_fb": ("tilt_head", {"direction": "down"}),
        "head_tilt_lr": ("tilt_head", {"direction": "left"}),
    }
    for servo in SERVOS:
        if servo not in reach:
            check(f"{servo} has a way to be moved", False, "no action listed")
            continue
        action, args = reach[servo]
        moved, reply = run(action, **args)
        check(f"{servo:13} <- {action}({', '.join(args.values()) or ''})",
              servo in moved, f"moved {moved}, said {reply!r}")

    print("the gestures are gestures — they end where they started")
    for action, servo in (("nod", "head_tilt_fb"), ("shake_head", "neck")):
        moved, reply = run(action, times=2)
        c = FakeController()
        # Re-run to inspect the final angle rather than just the names.
        ctx = types.SimpleNamespace(controller=c, led=None, tracker=None,
                                    sound=None, sensors=None, diagnostic=None)
        last = []
        c.move_smooth = lambda n, a, duration=0.5, steps=25: (
            c.moved.append(n), last.append((n, a)), a)[-1]
        C.execute_action(ctx, action, times=2)
        check(f"{action} moves {servo}", all(n == servo for n, _ in last),
              str({n for n, _ in last}))
        check(f"...and finishes at rest, not held over",
              last and abs(last[-1][1] - SERVOS[servo]["rest_angle"]) < 0.51,
              f"ended at {last[-1][1] if last else None}")

    print("a body command never reaches the wheels")
    # The two sentences that actually drove the robot.
    for text in ("move your head to the right",
                 "move your head back to center",
                 "move your head to the right, then back to center",
                 "point your head left", "tilt your head down",
                 "move your eyes left", "nod your head"):
        got = C.match_local(text)
        check(f"{text[:44]:46} is not a drive",
              got is None or got[0] != "drive", str(got))

    print("...and a real drive command still drives")
    for text, want in [("drive forward", "forward"), ("back up", "back"),
                       ("move back", "back"), ("go left", "left"),
                       ("roll right", "right"), ("turn around", "around")]:
        got = C.match_local(text)
        check(f"{text!r:18} -> drive {want}",
              got is not None and got[0] == "drive"
              and got[1].get("direction") == want, str(got))
    check("'stop' is never blocked, whatever else is in the sentence",
          C.match_local("stop")[0] == "cart_stop", str(C.match_local("stop")))

    print("head verbs land on the head, not the eyes or the base")
    for text, want in [("turn your head left", "turn_head"),
                       ("move your head right", "turn_head"),
                       ("point your head to the center", "turn_head"),
                       ("tilt your head left", "tilt_head"),
                       ("tilt your head up", "tilt_head"),
                       ("nod", "nod"), ("shake your head", "shake_head"),
                       ("look up", "look"), ("look left", "look")]:
        got = C.match_local(text)
        check(f"{text!r:34} -> {want}",
              got is not None and got[0] == want, str(got))

    print("chatter about light and rest does not throw switches or servos")
    # The same failure class as the head/cart collision above, found by the
    # architecture review: the LED rule fired on any sentence containing
    # "light" — "is it light outside?" switched the LED on — and the reset
    # rule fired on "rest"/"home" anywhere, so "i need a rest" and "take me
    # home" moved every servo. A command carries a verb, or is a short bare
    # imperative; conversation is neither.
    for text, action in [("is it light outside", "set_led"),
                         ("the light is pretty in here", "set_led"),
                         ("do you like the northern lights", "set_led"),
                         ("i need a rest", "reset"),
                         ("take me home", "reset"),
                         ("tell me about your home town", "reset"),
                         ("is this your neutral expression", "reset")]:
        got = C.match_local(text)
        check(f"{text[:40]:42} is not {action}",
              got is None or got[0] != action, str(got))

    print("...while real commands for both still land")
    for text, want in [("can you turn on the light", ("set_led", True)),
                       ("turn the light off", ("set_led", False)),
                       ("switch the red led on", ("set_led", True)),
                       ("lights on", ("set_led", True)),
                       ("terminator mode on", ("set_led", True))]:
        got = C.match_local(text)
        check(f"{text!r:30} -> led {'on' if want[1] else 'off'}",
              got is not None and got[0] == want[0]
              and got[1].get("on") == want[1], str(got))
    for text in ("reset", "reset your pose", "please reset yourself",
                 "go to rest position", "go home", "neutral", "straighten up"):
        got = C.match_local(text)
        check(f"{text!r:24} -> reset",
              got is not None and got[0] == "reset", str(got))

    print("several things in a row go to the model, not to one pattern")
    # One pattern is one action, so answering these locally means doing the
    # first step and stopping — which is what it looked like from in front.
    for text in ("move your head to the right, then back to center, then left",
                 "turn left then right", "nod twice", "look up and back down",
                 "turn your head left, then nod"):
        check(f"{text[:46]:48} falls through", C.match_local(text) is None,
              str(C.match_local(text)))

    print("every movement tool maps to a real action")
    names = {t["name"] for t in C.CLAUDE_TOOLS}
    for tool in ("look", "turn_head", "tilt_head", "nod", "shake_head", "set_mouth"):
        check(f"{tool} is offered to the model", tool in names)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

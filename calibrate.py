#!/usr/bin/env python3
"""Interactive servo calibration for the InMoov PCA9685 setup.

Find the safe mechanical limits of each servo BEFORE trusting the config.
Move a servo in small steps and watch the mechanism; record the angles where
it reaches its physical end-stops (with a little margin), plus a sensible rest.

Usage:
    ./venv/bin/python calibrate.py            # calibrate all servos
    ./venv/bin/python calibrate.py jaw neck   # calibrate just these

Commands while calibrating a servo:
    <number>   move to that angle (degrees, clamped 0–180)
    + / -      nudge up / down by the current step
    step N     change the nudge step (default 5)
    min        record CURRENT angle as this servo's min_angle
    max        record CURRENT angle as this servo's max_angle
    rest       record CURRENT angle as this servo's rest_angle
    show       print this servo's recorded values
    next       finish this servo, move to the next
    save       write all recorded values back to config/servos.json
    quit       exit (prompts to save)

SAFETY: start near the middle (90°) and step slowly. If a servo buzzes or
strains at an end, back off — that's past the mechanical limit.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from inmoov.servo_controller import ServoController, CONFIG_PATH, load_config


def main(argv: list[str]) -> int:
    config = load_config()
    ctrl = ServoController(config=config, move_to_rest=False)
    if ctrl.mock:
        print("\n*** MOCK MODE — no hardware. You can practise the workflow, but\n"
              "    recorded values won't reflect a real mechanism. ***\n")

    names = argv or list(config["servos"])
    for n in names:
        if n not in config["servos"]:
            print(f"skip unknown servo {n!r}")
            continue
        calibrate_one(ctrl, config, n)

    save_prompt(config)
    ctrl.relax()
    return 0


def calibrate_one(ctrl: ServoController, config: dict, name: str) -> None:
    s = config["servos"][name]
    step = 5.0
    angle = s.get("rest_angle", 90)
    print(f"\n=== {name} — ch{s['channel']} — {s.get('description','')} ===")
    print(f"current limits: {s['min_angle']}–{s['max_angle']}, rest {s['rest_angle']}")
    ctrl.set_angle(name, angle)  # note: uses existing limits; widen them first if needed

    while True:
        angle = ctrl.get_angle(name) or angle
        try:
            cmd = input(f"[{name} @ {angle:.0f}° step {step:.0f}] > ").strip().lower()
        except EOFError:
            return
        if not cmd:
            continue
        if cmd in ("next", "n"):
            return
        if cmd in ("quit", "q"):
            save_prompt(config)
            ctrl.relax()
            sys.exit(0)
        if cmd == "save":
            write_config(config)
            continue
        if cmd == "show":
            print(json.dumps(s, indent=2))
            continue
        if cmd == "+":
            _raw_move(ctrl, s, name, angle + step)
            continue
        if cmd == "-":
            _raw_move(ctrl, s, name, angle - step)
            continue
        if cmd.startswith("step"):
            try:
                step = float(cmd.split()[1])
            except (IndexError, ValueError):
                print("usage: step N")
            continue
        if cmd in ("min", "max", "rest"):
            s[f"{cmd}_angle"] = round(angle, 1)
            print(f"  recorded {cmd}_angle = {s[f'{cmd}_angle']}")
            continue
        try:
            _raw_move(ctrl, s, name, float(cmd))
        except ValueError:
            print("  ? unknown command (type a number, +, -, min, max, rest, next, save, quit)")


def _raw_move(ctrl: ServoController, s: dict, name: str, target: float) -> None:
    """Move ignoring config limits (temporarily widen) so we can FIND the limits.

    Still hard-clamped to 0–180, the servo's absolute range.
    """
    target = max(0.0, min(180.0, target))
    old = (s["min_angle"], s["max_angle"])
    s["min_angle"], s["max_angle"] = 0, 180
    try:
        ctrl.set_angle(name, target)
    finally:
        s["min_angle"], s["max_angle"] = old


def save_prompt(config: dict) -> None:
    try:
        ans = input("\nSave recorded values to config/servos.json? [y/N] ").strip().lower()
    except EOFError:
        ans = "n"
    if ans == "y":
        write_config(config)


def write_config(config: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    print(f"  saved -> {CONFIG_PATH}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

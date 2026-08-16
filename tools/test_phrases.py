#!/usr/bin/env python3
"""The phrase deck's promises, without a robot.

    venv/bin/python tools/test_phrases.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inmoov import phrases  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    phrases.PATH = Path(tempfile.mkdtemp()) / "phrases.json"

    deck = phrases.load()
    check("a fresh robot has the starter deck", "Crowd" in deck and len(deck) == 4)
    check("the starter deck is a copy, not the defaults themselves",
          phrases.load() is not deck and phrases.DEFAULTS["Crowd"][0] in deck["Crowd"])

    out = phrases.add("Jokes", "Why did the robot cross the road?")
    check("adding creates the tab", "Jokes" in out["deck"])
    check("...and it persists", "Jokes" in phrases.load())
    check("a duplicate is refused",
          "error" in phrases.add("Jokes", "Why did the robot cross the road?"))
    check("an empty phrase is refused", "error" in phrases.add("Jokes", "  "))
    check("a phrase without a tab is refused", "error" in phrases.add("", "hello"))
    check("a speech is refused", "error" in phrases.add("Jokes", "x" * 500))
    check("tab names are capped to fit a chip",
          all(len(t) <= phrases.TAB_MAX
              for t in phrases.add("T" * 60, "fits")["deck"]))

    out = phrases.remove("Jokes", "Why did the robot cross the road?")
    check("removing the last phrase removes the tab", "Jokes" not in out["deck"])
    check("removing what is not there says so", "error" in phrases.remove("Nope", "x"))

    phrases.PATH.write_text("{not json")
    check("a corrupt file falls back to the starter deck",
          "Crowd" in phrases.load())
    phrases.PATH.write_text('["a", "list"]')
    check("...and so does the wrong shape", "Crowd" in phrases.load())

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}: " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Event-mode wake-gate strictness (V4): tighter in a hall, back to normal after.

In a crowded hall the follow-up window is open on a room full of talk and
near-homophones of his name get said. Event mode tightens the gate three ways,
and switching it off puts every one of them back — this pins both directions,
because a strictness that doesn't relax is a robot that got quietly deaf and
nobody remembered why.

    python3 tools/test_wake_strict.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov.listener import (Listener, NAME_MIN_PARTIALS, FOLLOWUP_WINDOW,     # noqa: E402
                             EVENT_NAME_PARTIALS_BONUS, EVENT_FOLLOWUP_WINDOW)

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    lis = Listener(on_command=lambda t: None)

    print("default (event mode off): the gate is relaxed")
    check("name-partials at the base", lis._name_min_partials == NAME_MIN_PARTIALS)
    check("no position limit", lis._wake_max_pos is None)
    check("full follow-up window", lis._followup_window == FOLLOWUP_WINDOW)
    check("his name counts anywhere in the sentence",
          lis._strip("well anyway fred stop") == "stop", repr(lis._strip("well anyway fred stop")))

    print("event mode on: three knobs tighten")
    lis.set_event_strict(True)
    check("name-partials raised by the bonus",
          lis._name_min_partials == NAME_MIN_PARTIALS + EVENT_NAME_PARTIALS_BONUS,
          str(lis._name_min_partials))
    check("follow-up window shortened", lis._followup_window == EVENT_FOLLOWUP_WINDOW)
    check("his name must be in the first two tokens: 'fred stop' passes",
          lis._strip("fred stop") == "stop", repr(lis._strip("fred stop")))
    check("'hey fred stop' passes (name at token 1)",
          lis._strip("hey fred stop") == "stop", repr(lis._strip("hey fred stop")))
    check("a name buried mid-sentence is REJECTED",
          lis._strip("well anyway fred stop") is None, repr(lis._strip("well anyway fred stop")))
    check("'my friend fred said' does not wake him",
          lis._strip("my friend fred said hi") is None, repr(lis._strip("my friend fred said hi")))
    check("the detector needs more partials to fire",
          lis._name_min_partials > NAME_MIN_PARTIALS)

    print("event mode off again: everything relaxes back")
    lis.set_event_strict(False)
    check("name-partials restored", lis._name_min_partials == NAME_MIN_PARTIALS)
    check("position limit removed", lis._wake_max_pos is None)
    check("follow-up window restored", lis._followup_window == FOLLOWUP_WINDOW)
    check("a buried name wakes him again (relaxed)",
          lis._strip("well anyway fred stop") == "stop", repr(lis._strip("well anyway fred stop")))

    print("arm() uses the current follow-up window")
    lis.set_event_strict(True)
    lis.arm()                     # no arg -> the (now short) event window
    check("armed after arm()", lis.is_armed())
    # exact seconds aren't asserted (monotonic clock), but the window it used is
    # the event one, which the restore test above already proved differs.

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

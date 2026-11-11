#!/usr/bin/env python3
"""Turn-taking: who FRED thinks is talking to him, and when.

The listener's hardest behaviour has no test until now, and it is exactly the
behaviour a refactor breaks silently: the wake word anywhere in a sentence, the
window that lets the answer to his own question skip the wake word, the bare
"Fred" that arms and prompts, and — the one a person notices instantly —
"saying his name over him stops him." All of it funnels through _dispatch and
the two pure functions under it, which take plain text and call back, so it
drives without a microphone, a recogniser, or a thread.

Every case here is a real rule from listener.py's own docstrings:
  * _strip_wake keeps whatever follows the name, so "Fred turn your head" and
    "hey Fred turn your head" are the same command.
  * an open window (bare-wake or a question he asked) takes the next utterance
    as a command, unstripped — and consumes the window, so a reply that ends on
    another question can re-arm cleanly.
  * armed=True (the barge-in path) skips the wake word outright.
  * anything else with no wake word is not for him, and is dropped.

    python3 tools/test_dispatch.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import listener as L                            # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def make():
    """A Listener that touches no hardware, recording its callbacks."""
    heard = {"commands": [], "wakes": 0}
    lis = L.Listener(on_command=lambda t: heard["commands"].append(t),
                     on_wake=lambda: heard.__setitem__("wakes", heard["wakes"] + 1),
                     device="null")
    return lis, heard


def main() -> int:
    print("_strip_wake keeps what follows the name, wherever it sits")
    for text, want in [("fred turn your head", "turn your head"),
                       ("hey fred turn your head", "turn your head"),
                       ("fred", ""),                       # bare name
                       ("okay fred what time is it", "what time is it"),
                       ("what time is it", None),          # no name -> not for him
                       ("alfred stop", "stop"),            # a heard variant of the name
                       ("my friend told me", None)]:       # 'friend' is not a wake word
        got = L._strip_wake(text)
        check(f"{text!r:34} -> {want!r}", got == want, repr(got))

    print("_wants_him is 'is his name in it', the barge-in test")
    check("named -> yes", L._wants_him("fred stop talking"))
    check("unnamed -> no", not L._wants_him("stop talking"))

    print("a plain command needs his name; without it he stays out of it")
    lis, heard = make()
    lis._dispatch("what is the weather", time.monotonic())
    check("unaddressed speech is dropped", heard["commands"] == [] and heard["wakes"] == 0,
          str(heard))
    lis._dispatch("fred what is the weather", time.monotonic())
    check("named speech is a command, name stripped",
          heard["commands"] == ["what is the weather"], str(heard["commands"]))

    print("a bare 'Fred' prompts and opens a window")
    lis, heard = make()
    check("not armed to begin with", not lis.is_armed())
    lis._dispatch("fred", time.monotonic())
    check("bare name says 'Yes?' (on_wake)", heard["wakes"] == 1)
    check("...and no command yet", heard["commands"] == [])
    check("...and the window is now open", lis.is_armed())
    # the very next utterance is the command, no wake word needed
    lis._dispatch("turn your head left", time.monotonic())
    check("the follow-up is taken as a command",
          heard["commands"] == ["turn your head left"], str(heard["commands"]))
    check("...and the window closed behind it", not lis.is_armed(),
          "a consumed window must not linger")

    print("arm() opens the window the assistant uses after asking a question")
    lis, heard = make()
    lis.arm(9.0)
    check("armed after arm()", lis.is_armed())
    lis._dispatch("the blue one", time.monotonic())
    check("the answer skips the wake word entirely",
          heard["commands"] == ["the blue one"], str(heard["commands"]))
    check("window consumed", not lis.is_armed())

    print("an expired window is no window: the wake word is required again")
    lis, heard = make()
    lis._armed_until = time.monotonic() - 0.01     # a window that just lapsed
    check("is_armed() is false once it lapses", not lis.is_armed())
    lis._dispatch("the blue one", time.monotonic())
    check("speech after the window lapses is dropped without a name",
          heard["commands"] == [], str(heard["commands"]))

    print("armed=True is the barge-in path: name optional, taken verbatim")
    lis, heard = make()
    lis._dispatch("stop talking", time.monotonic(), armed=True)
    check("armed dispatch takes even un-named speech",
          heard["commands"] == ["stop talking"], str(heard["commands"]))

    print("disarm() shuts an open window early")
    lis, heard = make()
    lis.arm(9.0)
    lis.disarm()
    check("disarmed", not lis.is_armed())
    lis._dispatch("the blue one", time.monotonic())
    check("nothing is taken after disarm", heard["commands"] == [])

    print("re-arming: a question inside an open window still consumes then re-arms")
    # The rule the _dispatch comment calls out: the window is cleared BEFORE the
    # handler runs, so a handler that asks its own question (and calls arm())
    # leaves a fresh window, not a doubled one.
    lis, heard = make()
    lis.arm(9.0)

    def ask_back(_text):
        # the assistant's real pattern: answer, then re-arm on a question
        lis.arm(9.0)
    lis._on_command = ask_back
    lis._dispatch("which one did you mean", time.monotonic())
    check("the window the handler opened survives", lis.is_armed(),
          "consumed-before-handler is what makes re-arming clean")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

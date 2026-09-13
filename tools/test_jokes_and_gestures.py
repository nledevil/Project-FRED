#!/usr/bin/env python3
"""A joke that isn't improvised, and a dance that ends where it started (V2).

"Tell me a joke" and "can you dance?" are the two things children actually ask
a robot, and until this they had no affordance: the model improvised the same
three robot puns for a whole queue, and the honest answer to the dance was a
sentence. Now there is a book (inmoov/jokes.py) and a routine
(commands._gesture), and this pins the promises each one makes:

**The book deals without repeating.** Every joke is told once before any is
told twice, and the reshuffle at the end of the deck never puts the joke just
told straight back on top — the one repeat a deck exists to prevent.

**The matcher hears a request, not a mention.** "Tell me a joke" answers
offline. "I have a joke for you" and "wanna hear a joke?" are a child offering
one, and must fall through to the model, which can listen. "I like dancing" is
not a request to dance.

**A gesture is a gesture.** The dance touches more than one servo and ends at
rest on every one it touched. A build with no neck says so instead of raising.

**Both tools are wired end to end** — a schema entry, a run_tool branch and an
execute_action branch — so the model can reach them for the phrasings the
matcher misses.

No hardware: the controller is a stub that records moves.

    python3 tools/test_jokes_and_gestures.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import commands as C                            # noqa: E402
from inmoov import jokes                                    # noqa: E402

FAILURES: list[str] = []
SERVOS = json.loads((ROOT / "config" / "servos.json").read_text())["servos"]


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeController:
    def __init__(self, servos=None):
        self.servos = servos if servos is not None else SERVOS
        self.moves: list[tuple[str, float]] = []

    def set_angle(self, name, angle, **kw):
        self.moves.append((name, angle))
        return angle

    def move_smooth(self, name, angle, duration=0.5, steps=25):
        self.moves.append((name, angle))
        return angle


def ctx_with(controller, book=None):
    return types.SimpleNamespace(controller=controller, led=None, tracker=None,
                                 sound=None, sensors=None, diagnostic=None,
                                 jokes=book)


def main() -> int:
    print("the book deals every joke once before any joke twice")
    book = jokes.JokeBook(rng=random.Random(7))
    n = len(book)
    check("the built-in deck is big enough that a 3-minute chat can't drain it",
          n >= 30, f"{n} jokes")
    check("every joke is short enough to be spoken",
          all(len(j) <= jokes.TEXT_MAX for j in jokes.DEFAULTS))
    check("no joke appears twice in the book", len(set(jokes.DEFAULTS)) == n)
    first_pass = [book.next() for _ in range(n)]
    check("one pass through deals each joke exactly once",
          sorted(first_pass) == sorted(jokes.DEFAULTS),
          f"{len(set(first_pass))} distinct of {n}")
    # The boundary, many times over: whatever the shuffle does, the first joke
    # of the next deck is never the last joke of this one.
    boundary_repeats = 0
    for seed in range(200):
        b = jokes.JokeBook(["a", "b", "c"], rng=random.Random(seed))
        told = [b.next() for _ in range(9)]
        boundary_repeats += sum(1 for x, y in zip(told, told[1:]) if x == y)
    check("no joke is ever followed by itself across a reshuffle (200 seeds)",
          boundary_repeats == 0, f"{boundary_repeats} repeats")
    check("an empty book says so instead of raising",
          "run out" in jokes.JokeBook([]).next())

    print("the operator's file replaces the deck only when it is usable")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "jokes.json"
        quiet = lambda *_: None                                   # noqa: E731
        check("no file -> the built-in deck",
              jokes.load_deck(p, log=quiet) == list(jokes.DEFAULTS))
        p.write_text(json.dumps(["  Knock knock. ", "", 42, "Who's there?"]))
        got = jokes.load_deck(p, log=quiet)
        check("a list of strings replaces it, trimmed, non-strings dropped",
              got == ["Knock knock.", "Who's there?"], str(got))
        p.write_text("{not json")
        check("a file that won't parse -> the built-in deck",
              jokes.load_deck(p, log=quiet) == list(jokes.DEFAULTS))
        p.write_text(json.dumps({"jokes": ["wrong shape"]}))
        check("the wrong shape -> the built-in deck",
              jokes.load_deck(p, log=quiet) == list(jokes.DEFAULTS))

    print("the matcher hears a request for a joke, not a mention of one")
    for text in ("tell me a joke", "can you tell me a joke", "Fred, tell us a joke!",
                 "do you know any jokes", "you got any jokes?", "say something funny",
                 "make me laugh", "another joke", "give me your best joke"):
        got = C.match_local(text)
        check(f"{text!r:34} -> tell_joke", got is not None and got[0] == "tell_joke", str(got))
    for text in ("I have a joke for you", "wanna hear a joke?", "that's a joke",
                 "what do you think of my joke", "I'll tell you a joke",
                 "is this a joke", "you're funny",
                 # found in review: negated, past tense, or about jokes
                 "don't tell me a joke", "did you say a joke?", "do you know what a joke is",
                 "you know a lot of jokes", "tell me something funny that happened"):
        got = C.match_local(text)
        check(f"{text!r:34} is not a joke request", got is None or got[0] != "tell_joke", str(got))

    print("...and a request to dance, not a mention of dancing")
    for text in ("can you dance", "dance for me", "do a dance", "show me your dance moves",
                 "bust a move"):
        got = C.match_local(text)
        check(f"{text!r:34} -> dance",
              got is not None and got == ("gesture", {"routine": "dance"}), str(got))
    for text in ("I like dancing", "we went to a dance", "do you watch dancing with the stars",
                 "I have dance class later",
                 # found in review
                 "don't dance", "there's a dance at school on friday", "is that a dance?",
                 "i heard you can dance", "why can't you dance"):
        got = C.match_local(text)
        check(f"{text!r:34} is not a dance request",
              got is None or got[0] != "gesture", str(got))
    got = C.match_local("look around")
    check("'look around' -> look_around, not the eyes-only look",
          got == ("gesture", {"routine": "look_around"}), str(got))
    got = C.match_local("look left")
    check("'look left' still moves the eyes", got is not None and got[0] == "look", str(got))
    for text in ("look around and tell me what you see", "what do you see when you look around",
                 "take a look around and describe the room", "look around, is anyone there"):
        got = C.match_local(text)
        check(f"{text[:40]!r:42} is a question for the camera, not a gesture",
              got is None or got[0] != "gesture", str(got))
    check("'take a look around' is still the gesture",
          C.match_local("take a look around") == ("gesture", {"routine": "look_around"}))

    print("a gesture is a gesture — several servos, and it ends at rest on all of them")
    for name in C.GESTURES:
        c = FakeController()
        reply = C.execute_action(ctx_with(c), "gesture", routine=name)
        touched = {n for n, _ in c.moves}
        check(f"{name} moves more than one servo", len(touched) > 1, str(touched))
        last = {}
        for n, a in c.moves:
            last[n] = a
        off = {n: a for n, a in last.items() if abs(a - SERVOS[n]["rest_angle"]) > 0.51}
        check(f"{name} finishes at rest on every servo it touched", not off, str(off))
        check(f"{name} stays inside the calibrated travel",
              all(SERVOS[n]["min_angle"] - 0.01 <= a <= SERVOS[n]["max_angle"] + 0.01
                  for n, a in c.moves), str(c.moves[:3]))
        check(f"{name} says something afterwards", bool(reply), repr(reply))
    c = FakeController(servos={"jaw": SERVOS["jaw"]})
    reply = C.execute_action(ctx_with(c), "gesture", routine="dance")
    check("no neck: says so, moves nothing", "wired" in reply and not c.moves, repr(reply))
    reply = C.execute_action(ctx_with(FakeController()), "gesture", routine="moonwalk")
    check("an unknown gesture lists the real ones", "dance" in reply, repr(reply))

    print("both tools are wired end to end")
    names = {t["name"] for t in C.CLAUDE_TOOLS}
    check("tell_joke and gesture are in CLAUDE_TOOLS", {"tell_joke", "gesture"} <= names)
    schema = next(t for t in C.CLAUDE_TOOLS if t["name"] == "gesture")["input_schema"]
    check("the gesture schema's enum is the routines that exist",
          tuple(schema["properties"]["routine"]["enum"]) == C.GESTURES, str(schema))
    book = jokes.JokeBook(["only one"], rng=random.Random(1))
    out = C.run_tool(ctx_with(FakeController(), book), "tell_joke", {})
    check("run_tool tell_joke deals from the ctx's book", out == "only one", repr(out))
    c = FakeController()
    out = C.run_tool(ctx_with(c), "gesture", {"routine": "dance"})
    check("run_tool gesture dances", c.moves and "dance" in out.lower(), repr(out))
    check("a matched joke is the one offline reply the brain remembers",
          "tell_joke" in C.REMEMBERED_ACTIONS)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

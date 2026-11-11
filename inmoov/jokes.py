"""FRED's joke book — the one answer to "tell me a joke" that isn't improvised.

Left to the model, "tell me a joke" gets the same three robot puns for a whole
queue of children, each one a two-second cloud round trip, and the fourth child
hears the first child's joke. So the jokes are a list, curated here, and the
model is told to read from it rather than invent (see ``commands.CLAUDE_TOOLS``
and the system prompt in ``brain.py``). The matcher answers the plain phrasings
without a model at all.

**Child-safe by construction.** Every line below is the kind a nine-year-old
tells a grandparent. Nothing here is generated at run time, so nothing here
can drift. If you add one, keep it short — it is spoken aloud, and the
synthesiser is slower than realtime — and keep it clean.

**No repeats until the deck is out.** ``JokeBook.next()`` deals from a shuffled
deck and reshuffles only when it is empty, so a joke is not heard twice until
every other joke has been told once, and the reshuffle never puts the joke just
told at the top of the new deck. A 180-second conversation cannot exhaust a
deck this size, which is what "deduped within the conversation" costs here:
nothing. Across a whole event day it also means the queue hears the whole book
before any of it comes round again.

**Content, not configuration.** ``config/jokes.json`` — a JSON list of strings
— replaces the built-in deck if it exists, the same arrangement
``phrases.py`` uses for the operator's deck. It is not seeded automatically:
the defaults are the book until someone writes their own, and deleting the
file goes back to them. A file that will not parse is ignored with one line
on the console rather than leaving him with no jokes.
"""
from __future__ import annotations

import json
import random
import threading
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "config" / "jokes.json"

TEXT_MAX = 200          # the same ceiling as a deck phrase, for the same reason

DEFAULTS: tuple[str, ...] = (
    "Why did the robot go on vacation? He needed to recharge his batteries.",
    "What do you call a robot who likes to dance? A boogie-bot.",
    "Why was the robot so bad at soccer? He kept getting a bad kick out of it.",
    "What's a robot's favorite kind of music? Heavy metal.",
    "Why did the computer go to the doctor? It had a virus.",
    "What did one wall say to the other wall? Meet you at the corner.",
    "Why don't scientists trust atoms? Because they make up everything.",
    "What do you call a fish with no eyes? A fsh.",
    "Why did the bicycle fall over? It was two tired.",
    "What do you call cheese that isn't yours? Nacho cheese.",
    "Why can't you give Elsa a balloon? Because she'll let it go.",
    "What has hands but can't clap? A clock.",
    "Why did the math book look so sad? It had too many problems.",
    "What do you call a sleeping dinosaur? A dino-snore.",
    "How does the ocean say hello? It waves.",
    "Why did the cookie go to the doctor? It was feeling crummy.",
    "What do you call a bear with no teeth? A gummy bear.",
    "Why are ghosts such bad liars? Because you can see right through them.",
    "What's orange and sounds like a parrot? A carrot.",
    "Why did the student eat his homework? Because the teacher said it was a piece of cake.",
    "What do you get when you cross a snowman and a dog? Frostbite.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "What kind of shoes do robots wear? Re-boots.",
    "Why did the robot cross the road? It was programmed to.",
    "What did the robot say to the vending machine? You're my kind of machine.",
    "What do robots eat for a snack? Computer chips.",
    "Why was six afraid of seven? Because seven eight nine.",
    "What did the left eye say to the right eye? Between us, something smells.",
    "How do you make a tissue dance? You put a little boogie in it.",
    "What do you call a pig that does karate? A pork chop.",
    "Why did the golfer bring two pairs of pants? In case he got a hole in one.",
    "What did the zero say to the eight? Nice belt.",
    "Why don't eggs tell jokes? They'd crack each other up.",
    "What do you call a boomerang that doesn't come back? A stick.",
    "Why did the robot get glasses? To improve his eye-Q.",
    "What's a robot's favorite snack? Micro-chips and dip.",
    "What do you call a robot who takes the long way round? R2-detour.",
    "Why was the robot angry? Somebody kept pushing his buttons.",
    "What do clouds wear under their raincoats? Thunderwear.",
    "Why did the banana go to the doctor? It wasn't peeling well.",
)


def load_deck(path: Path = PATH, log=print) -> list[str]:
    """The operator's jokes if the file is usable, otherwise the defaults.

    Usable means a JSON list with at least one non-empty string in it; each
    entry is trimmed and capped at TEXT_MAX. Anything else — missing, empty,
    malformed, the wrong shape — falls back, and says so once for the two
    failure cases that mean somebody tried.
    """
    if not path.exists():
        return list(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a bad file must not empty the book
        log(f"[jokes] {path.name} unreadable ({exc}); using the built-in deck")
        return list(DEFAULTS)
    jokes = [str(j).strip()[:TEXT_MAX] for j in raw
             if isinstance(j, str) and str(j).strip()] if isinstance(raw, list) else []
    if not jokes:
        log(f"[jokes] {path.name} holds no jokes; using the built-in deck")
        return list(DEFAULTS)
    return jokes


class JokeBook:
    """A shuffled deck of jokes that deals every card before repeating any.

    ``next()`` is safe to call from the voice thread and the web thread at
    once — both routes to the brain exist, and both can ask for a joke.
    """

    def __init__(self, jokes: list[str] | None = None, rng: random.Random | None = None):
        self._jokes = list(jokes) if jokes is not None else load_deck()
        self._rng = rng or random.Random()
        self._deck: list[str] = []
        self._last: str | None = None
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._jokes)

    def _reshuffle(self) -> None:
        self._deck = list(self._jokes)
        self._rng.shuffle(self._deck)
        # Dealt from the end. If the reshuffle put the joke just told at the
        # end, it would be the very next one out — the one repeat a deck is
        # meant to make impossible. Swap it to the bottom instead.
        if len(self._deck) > 1 and self._deck[-1] == self._last:
            self._deck[0], self._deck[-1] = self._deck[-1], self._deck[0]

    def next(self) -> str:
        """The next joke, or an honest sentence if the book is empty."""
        with self._lock:
            if not self._jokes:
                return "I've run out of jokes. Somebody needs to teach me some."
            if not self._deck:
                self._reshuffle()
            self._last = self._deck.pop()
            return self._last


# The book the tools use when the caller's ``ctx`` doesn't carry one of its
# own (tests do; the robot doesn't need to). One process, one deck, so the
# voice path and the panel deal from the same shuffle.
DEFAULT_BOOK = JokeBook()

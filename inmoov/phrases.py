"""The deck of one-tap things to say, and the tabs that organise it.

At an event the operator is holding a phone one-handed while FRED mishears a
child, and the queue needs managing *now*. Typing is not an option; a deck of
canned lines is. "Who's next?", "Let me think about that one" — tapped, spoken,
done, through the same /api/say the sound tests use.

Content, not configuration: it lives in config/phrases.json beside settings,
seeded from the defaults below on first use, and everything after that is the
operator's. Deleting the file resets the deck — which is the recovery story,
documented here because someone will eventually want it.

Caps are generous but real. A phrase is something FRED can say while a child
waits, not a speech; a tab name has to fit on a chip; and a deck someone has
scripted five hundred lines into has stopped being a deck.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "config" / "phrases.json"

TEXT_MAX = 200
TAB_MAX = 24
PHRASES_MAX = 300

# The starter deck. Tab order here is display order, and stays whatever the
# file says once one exists.
DEFAULTS: dict[str, list[str]] = {
    "Crowd": [
        "Who's next?",
        "One at a time, please — I only have two ears!",
        "Ask me about my servos!",
        "Come closer, I don't bite.",
    ],
    "Stalling": [
        "Let me think about that one.",
        "Hmm, that's a good question!",
        "My brain is thinking very hard right now.",
        "Could you say that again, a little slower?",
    ],
    "About me": [
        "I'm FRED — the Facial Recognition and Expression Droid!",
        "Most of my parts came out of a 3D printer.",
        "I run on three computers — one is in my head!",
        "My eyes, jaw and neck are moved by fourteen servos.",
    ],
    "Manners": [
        "Thanks for coming to see me!",
        "Goodbye! Come back soon.",
        "Nice to meet you!",
        "You're very welcome.",
    ],
}

_lock = threading.Lock()


def load() -> dict[str, list[str]]:
    """The deck from disk, or the starter deck. Never raises: a robot whose
    phrase file is corrupt should still have something to say."""
    try:
        with open(PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {k: list(v) for k, v in DEFAULTS.items()}
    if not isinstance(data, dict):
        return {k: list(v) for k, v in DEFAULTS.items()}
    out: dict[str, list[str]] = {}
    for tab, items in data.items():
        if isinstance(tab, str) and isinstance(items, list):
            out[tab] = [str(x) for x in items if str(x).strip()]
    return out or {k: list(v) for k, v in DEFAULTS.items()}


def _save(deck: dict[str, list[str]]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PATH, "w") as f:
        json.dump(deck, f, indent=2)
        f.write("\n")


def add(tab: str, text: str) -> dict:
    """Add a phrase, creating its tab if new. Returns the deck or an error."""
    tab = (tab or "").strip()[:TAB_MAX]
    text = (text or "").strip()
    if not tab or not text:
        return {"error": "a phrase needs words and a tab"}
    if len(text) > TEXT_MAX:
        return {"error": f"keep it under {TEXT_MAX} characters — it has to be said aloud"}
    with _lock:
        deck = load()
        if sum(len(v) for v in deck.values()) >= PHRASES_MAX:
            return {"error": f"the deck is full ({PHRASES_MAX} phrases)"}
        items = deck.setdefault(tab, [])
        if text in items:
            return {"error": "that phrase is already on the deck"}
        items.append(text)
        _save(deck)
        return {"deck": deck}


def remove(tab: str, text: str) -> dict:
    """Drop a phrase; a tab emptied by it goes too — an empty chip is clutter."""
    with _lock:
        deck = load()
        items = deck.get(tab or "")
        if not items or text not in items:
            return {"error": "not on the deck"}
        items.remove(text)
        if not items:
            del deck[tab]
        _save(deck)
        return {"deck": deck}

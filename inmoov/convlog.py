"""A small in-memory conversation/event log for FRED.

Everything FRED hears, says, and notices flows through one ring buffer so the web
UI can render a ChatGPT-style transcript — including voice interactions that
happen while no browser is open. Entries are polled incrementally by id.

Thread-safe; bounded (old entries drop off). Not persisted across restarts —
it's a live transcript, not history storage.
"""
from __future__ import annotations

import collections
import threading
import time


class ConversationLog:
    """Bounded, thread-safe, incrementally-pollable log of dialogue + events.

    Each entry: ``{id, t, role, text, source, actions}`` where ``role`` is one of
    ``user`` (something said/typed to FRED), ``fred`` (his reply), or ``event``
    (a system note like a face detection or an action). ``id`` is a monotonic
    sequence number the client uses to fetch only what's new.
    """

    def __init__(self, maxlen: int = 300):
        self._items = collections.deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, role: str, text: str, source: str | None = None,
            actions=None) -> dict:
        text = (text or "").strip()
        if not text:
            return {}
        with self._lock:
            self._seq += 1
            item = {"id": self._seq, "t": time.time(), "role": role,
                    "text": text, "source": source, "actions": list(actions or [])}
            self._items.append(item)
            return item

    # convenience wrappers
    def user(self, text, source="text"):
        return self.add("user", text, source=source)

    def fred(self, text, source=None, actions=None):
        return self.add("fred", text, source=source, actions=actions)

    def event(self, text):
        return self.add("event", text)

    def since(self, since_id: int = 0) -> list[dict]:
        with self._lock:
            return [x for x in self._items if x["id"] > since_id]

    def head(self) -> int:
        """Highest id assigned so far. A client whose last-seen id exceeds this
        knows the log was reset (process restart) and should re-sync from 0."""
        with self._lock:
            return self._seq

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

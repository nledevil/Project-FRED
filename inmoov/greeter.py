"""Greet someone who walks up, without being asked.

The sensor node fires an ``approach`` event when someone comes inside its near
threshold. This turns that into FRED saying hello — the first thing he does on
his own initiative rather than in reply to a question.

The whole design is about *not* being annoying, because a robot that greets you
every four seconds is worse than one that never does:

* **Cooldown.** The node's own hysteresis stops the reading chattering, but a
  person who lingers at the edge of the cone, or two sensors that both see the
  same arrival, would still fire twice. One greeting per ``cooldown`` seconds.
* **Never interrupt.** If FRED is already speaking, or is mid-thought waiting on
  Claude, the approach is ignored rather than queued — talking over himself to
  greet someone he is already talking to is the worst possible behaviour.
* **Off the ingest path.** Speaking takes seconds; the sensor ingest that
  triggered it must not wait, or the relay's POST times out and payloads drop.
  The greeting runs on its own thread.
* **Honours the handoff.** When the hardware is released to MyRobotLab, FRED
  must not grab the jaw servo back to say hello.

Canned phrases rather than a Claude round-trip: a greeting has to land while the
person is still in front of him, and an API call per approach is both slow and
billable for something that says "hello".
"""
from __future__ import annotations

import random
import threading
import time

GREETINGS = (
    "Oh, hello there.",
    "Hi there.",
    "Hello. I see you.",
    "Well hello.",
    "Hey there.",
    "Oh — hi.",
)


class Greeter:
    """Turns ``approach`` events into a spoken greeting. Never raises."""

    def __init__(self, assistant, log=None, enabled: bool = True,
                 cooldown: float = 90.0, phrases=None, blocked=None):
        # blocked() -> bool: something else owns the hardware right now.
        self._assistant = assistant
        self._log = log
        self._enabled = bool(enabled)
        self._cooldown = float(cooldown)
        self._phrases = tuple(phrases) if phrases else GREETINGS
        self._blocked = blocked
        self._last = 0.0
        self._lock = threading.Lock()

    # -- configuration -----------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    def configure(self, enabled=None, cooldown=None) -> dict:
        if enabled is not None:
            self._enabled = bool(enabled)
        if cooldown is not None:
            self._cooldown = max(0.0, float(cooldown))
        return {"enabled": self._enabled, "cooldown": self._cooldown}

    def state(self) -> dict:
        since = time.monotonic() - self._last if self._last else None
        return {"enabled": self._enabled, "cooldown": self._cooldown,
                "last_greeting_ago": round(since, 1) if since is not None else None}

    # -- the hook ----------------------------------------------------------
    def on_event(self, node: str, event: dict) -> None:
        """SensorHub's on_event hook. Cheap, non-blocking, swallows everything."""
        try:
            if not self._enabled or not isinstance(event, dict):
                return
            if str(event.get("event")) != "approach":
                return
            if self._blocked is not None and self._blocked():
                return
            a = self._assistant
            if a is None or a.is_speaking() or a.is_thinking():
                return
            # Claim the slot under the lock *before* speaking, so two sensors
            # seeing the same person arrive can't both get through.
            with self._lock:
                now = time.monotonic()
                if self._last and (now - self._last) < self._cooldown:
                    return
                self._last = now
            threading.Thread(target=self._greet, args=(node,),
                             name="greeter", daemon=True).start()
        except Exception:
            pass                      # a greeting is never worth breaking ingest for

    def _greet(self, node: str) -> None:
        line = random.choice(self._phrases)
        try:
            if self._log is not None:
                self._log.event(f"👋 Greeted someone approaching the {node}")
            self._assistant.speak(line)
        except Exception as exc:      # noqa: BLE001 - audio can fail; don't crash the thread
            print(f"[Greeter] greeting failed: {exc}")

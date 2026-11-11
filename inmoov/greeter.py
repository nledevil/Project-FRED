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
                 cooldown: float = 90.0, phrases=None, sightings_to_greet: int = 3):
        self._assistant = assistant
        self._log = log
        self._enabled = bool(enabled)
        self._cooldown = float(cooldown)
        self._phrases = tuple(phrases) if phrases else GREETINGS
        self._last = 0.0
        self._lock = threading.Lock()
        # V5: the wide camera sees 180°, the ultrasonic cone doesn't. N consecutive
        # spotter sightings count as an approach, fired once per continuous
        # presence (reset when the person leaves the frame), through the same
        # cooldown/never-interrupt gate as a sensor approach.
        self._sightings_needed = max(1, int(sightings_to_greet))
        self._sightings = 0
        self._greeted_presence = False

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

    def on_sighting(self, seen: bool, node: str = "wide camera") -> None:
        """Wide-spotter per-cycle hook (V5): a face was seen this detect cycle,
        or not. N consecutive sightings synthesize an approach — so someone
        entering from the side, whom the ultrasonic cone never sees, still gets
        greeted. Fires once per continuous presence: a miss (they left the frame)
        re-arms it. The actual greeting still passes through on_event's cooldown
        and never-interrupt gate, so this can't talk over him or double-greet.
        Cheap and non-blocking; swallows everything."""
        try:
            if not self._enabled:
                return
            if not seen:
                self._sightings = 0
                self._greeted_presence = False
                return
            if self._greeted_presence:
                return                       # already greeted this arrival; wait for them to leave
            self._sightings += 1
            if self._sightings >= self._sightings_needed:
                self._sightings = 0
                self._greeted_presence = True
                self.on_event(node, {"event": "approach"})
        except Exception:
            pass                             # a greeting is never worth breaking the detect loop

    def _greet(self, node: str) -> None:
        line = random.choice(self._phrases)
        try:
            if self._log is not None:
                self._log.event(f"👋 Greeted someone approaching the {node}")
            self._assistant.speak(line)
        except Exception as exc:      # noqa: BLE001 - audio can fail; don't crash the thread
            print(f"[Greeter] greeting failed: {exc}")

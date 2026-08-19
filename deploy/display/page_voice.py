"""Put FRED into listening mode, from the robot rather than from a laptop.

One button, because that is the whole job: this is the thing you want to press
while standing in front of him, and every extra control on the page is one more
thing to hit by accident with the same thumb.

The button paints the state the *brain* reports, not the state we asked for.
A tap posts and then lets the next poll tell us what happened — an optimistic
toggle would show "listening" for a robot that never started, which on a panel
you glance at is worse than a slow answer. The tap is latched as PENDING until
the poller catches up so it doesn't just look ignored for a second.
"""
from __future__ import annotations

import time


PENDING_FOR = 3.0                   # seconds a tap stays "PENDING" if nothing changes


class VoicePage:
    title = "VOICE"

    def __init__(self):
        self._pending_until = 0.0
        self._pending_to: bool | None = None

    # ---- input ------------------------------------------------------------

    @staticmethod
    def _listening(snap: dict) -> bool:
        return bool(((snap.get("nuc") or {}).get("voice") or {}).get("listening"))

    @staticmethod
    def _available(snap: dict) -> bool:
        voice = (snap.get("nuc") or {}).get("voice")
        return bool(voice and voice.get("available"))

    def view(self, snap: dict) -> dict:
        """The page as data: what the button says and whether it does anything.

        Split out of draw() so the Qt panel reaches the same conclusions — that
        the brain is unreachable, or that voice is unavailable and pressing
        would 503 — without a second copy of the reasoning.
        """
        reachable = bool(snap.get("nuc"))
        listening = self._listening(snap)
        available = self._available(snap)
        pending = (self._pending_to is not None
                   and time.monotonic() < self._pending_until
                   and listening != self._pending_to)
        if self._pending_to is not None and listening == self._pending_to:
            self._pending_to = None          # the brain caught up

        if not reachable:
            return {"label": "NO LINK", "on": False, "ink": "bad", "live": False,
                    "status": "CANNOT REACH THE BRAIN", "statusInk": "bad", "hint": ""}
        if not available:
            return {"label": "N/A", "on": False, "ink": "dim", "live": False,
                    "status": "VOICE UNAVAILABLE ON BRAIN", "statusInk": "dim",
                    "hint": ""}

        said = (snap.get("nuc") or {}).get("voice") or {}
        if said.get("speaking"):
            status = "SPEAKING"
        elif said.get("thinking"):
            status = "THINKING"
        elif listening and (said.get("mic") or {}).get("armed"):
            # He has just asked something and is holding the mic open for the
            # reply. Worth its own line: the whole point is that the person does
            # not have to say his name, and standing at the robot is exactly
            # where you would otherwise wait for a prompt that never comes.
            status = "LISTENING FOR YOUR ANSWER"
        elif listening:
            # Not "HEY FRED": the wake word is the name on its own, and a panel
            # that tells people otherwise is where the "hey" habit comes from.
            status = "LISTENING FOR FRED"
        else:
            status = "NOT LISTENING"
        return {"label": "PENDING" if pending else ("ON" if listening else "OFF"),
                "on": bool(listening), "live": True,
                "ink": "dim" if pending else ("ok" if listening else "ink"),
                "status": status, "statusInk": "dim",
                "hint": "TAP TO TURN " + ("OFF" if listening else "ON")}

    def toggle(self, net) -> None:
        """What a tap on the button does, wherever the tap came from."""
        want = not self._listening(net.snapshot())
        self._pending_to = want
        self._pending_until = time.monotonic() + PENDING_FOR
        net.post_voice(want)

    # ---- drawing ----------------------------------------------------------

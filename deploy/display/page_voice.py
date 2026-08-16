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

import menu_ui as ui

BTN = (250, 150, 550, 260)          # x0, y0, x1, y1
PENDING_FOR = 3.0                   # seconds a tap stays "PENDING" if nothing changes


class VoicePage:
    title = "VOICE"

    def __init__(self):
        self._button = ui.Button(*BTN, scale=4)
        self._pending_until = 0.0
        self._pending_to: bool | None = None

    # ---- input ------------------------------------------------------------
    def on_touch(self, kind: str, x: int, y: int, net) -> None:
        if kind != "down" or not self._button.hit(x, y):
            return
        self.toggle(net)

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
        elif listening:
            status = "LISTENING FOR HEY FRED"
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
    def draw(self, frame, snap: dict) -> None:
        reachable = bool(snap.get("nuc"))
        listening = self._listening(snap)
        available = self._available(snap)

        pending = (self._pending_to is not None
                   and time.monotonic() < self._pending_until
                   and listening != self._pending_to)
        if self._pending_to is not None and listening == self._pending_to:
            self._pending_to = None          # the brain caught up

        ui.text(frame, "WAKE WORD LISTENER", 250, 100, ui.DIM_INK, 2)

        if not reachable:
            self._button.draw(frame, on=False, ink=ui.BAD_INK, label="NO LINK")
            ui.text(frame, "CANNOT REACH THE BRAIN", 250, 290, ui.BAD_INK, 2)
            return
        if not available:
            # No mic or no Vosk model on the brain: pressing this would 503.
            self._button.draw(frame, on=False, ink=ui.DIM_INK, label="N/A")
            ui.text(frame, "VOICE UNAVAILABLE ON BRAIN", 250, 290, ui.DIM_INK, 2)
            return

        label = "PENDING" if pending else ("ON" if listening else "OFF")
        ink = ui.DIM_INK if pending else (ui.OK_INK if listening else ui.INK)
        self._button.draw(frame, on=listening, ink=ink, label=label)

        said = (snap.get("nuc") or {}).get("voice") or {}
        if said.get("speaking"):
            state = "SPEAKING"
        elif said.get("thinking"):
            state = "THINKING"
        elif listening:
            state = "LISTENING FOR HEY FRED"
        else:
            state = "NOT LISTENING"
        ui.text(frame, state, 250, 290, ui.DIM_INK, 2)
        ui.text(frame, "TAP TO TURN " + ("OFF" if listening else "ON"),
                250, 316, ui.DIM_INK, 1)

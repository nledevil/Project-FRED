#!/usr/bin/env python3
"""The thinking earcon (V1): "Hmm..." when a model keeps him quiet too long.

Two to four seconds of dead air before an answer makes a child assume he did
not hear, and repeat the question — which queues or barges a second turn. The
fix is a canned "Hmm..." from the TTS cache, played by the speaker thread
while it waits for the first sentence. These checks pin down when it plays and,
more importantly, when it must not:

  * a slow model turn gets the earcon, then the answer — and the answer is
    padded as a continuation, since the earcon already opened the device
  * a fast turn gets no earcon: the answer itself is the signal, and a "Hmm"
    in front of it would only push it later
  * it plays once per turn, never for a turn no model is answering (the wake
    "Yes?", a greeting, a matched command), and never after a barge-in
  * while it plays he is still *thinking*, not answering: the chest HUD's
    THINKING state survives it, and comes straight back when it ends
  * earcon_after = 0 turns the whole thing off

Driven with a fake Sound (records what was played, "plays" for 50 ms), a fake
brain (sleeps, then emits), and a servo-less controller — no microphone, no
piper, no model, no network.

    python3 tools/test_thinking_earcon.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import assistant as A                          # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


# ---- fakes ----------------------------------------------------------------
class FakeSound:
    """Renders instantly, plays for 50 ms, and remembers every clip and what
    the assistant's thinking flag looked like while it 'played'."""
    CLIP = 0.05

    def __init__(self):
        self.rendered: list[str] = []
        self.played: list[tuple[str, bool]] = []      # (path, pad)
        self.thinking_seen: dict[str, set] = {}       # path -> {thinking samples}
        self._path = None
        self._until = 0.0
        self.assistant = None

    def can_speak(self):
        return True

    def render_tts(self, text, speed=150, voice="en"):
        self.rendered.append(text)
        return f"/fake/{text}.wav"

    def play_file(self, path, wait=False, pad=True):
        self.played.append((path, pad))
        self._path, self._until = path, time.monotonic() + self.CLIP
        return True

    def is_playing(self):
        playing = time.monotonic() < self._until
        if playing and self.assistant is not None:
            self.thinking_seen.setdefault(self._path, set()).add(
                self.assistant.is_thinking())
        return playing

    def audio_epoch(self):
        return time.monotonic()

    def stop(self):
        self._until = 0.0


class FakeBrain:
    """Calls on_thinking, waits ``delay``, then answers in one sentence. Records
    what the assistant's flags were the instant the answer became available."""
    def __init__(self, assistant, delay: float, reply="Here is the answer."):
        self.a, self.delay, self.reply = assistant, delay, reply
        self.before_answer = None

    def respond(self, text, on_sentence=None, on_thinking=None):
        if on_thinking:
            on_thinking()
        time.sleep(self.delay)
        self.before_answer = (self.a.is_speaking(), self.a.is_thinking())
        on_sentence(self.reply)
        return {"reply": self.reply, "source": "claude", "actions": []}

    def ai_available(self):
        return True


class FakeController:
    servos: dict = {}

    def get_angle(self, name):
        return None

    def set_angle(self, name, angle):
        pass


def make(earcon_after: float, delay: float, sound: FakeSound | None = None):
    sound = sound or FakeSound()
    a = A.Assistant(FakeController(), None, None, sound, earcon_after=earcon_after)
    sound.assistant = a
    a.brain = FakeBrain(a, delay)
    return a, sound


def main() -> int:
    # The heard log writes a real file; a test must not append to the show's.
    A.heardlog.log = lambda: types.SimpleNamespace(append=lambda **k: None)
    earcon = f"/fake/{A.EARCON_TEXT}.wav"

    print("a slow model turn: the earcon, then the answer")
    a, s = make(earcon_after=0.15, delay=0.6)
    a.converse("what is the moon made of", source="voice")
    paths = [p for p, _ in s.played]
    check("the earcon played first", paths[:1] == [earcon], str(paths))
    check("...then the answer", paths == [earcon, "/fake/Here is the answer..wav"], str(paths))
    check("the earcon is the cached line, not a fresh render of something else",
          s.rendered[0] == A.EARCON_TEXT, str(s.rendered))
    pads = dict(s.played)
    check("the earcon takes the cold-start pad", pads[earcon] is True)
    check("the answer takes the continuation pad (device already open)",
          pads["/fake/Here is the answer..wav"] is False, str(s.played))
    check("he was still thinking the whole time the earcon played",
          s.thinking_seen.get(earcon) == {True}, str(s.thinking_seen.get(earcon)))
    check("...and back to THINKING, not SPEAKING, once it ended",
          a.brain.before_answer == (False, True), str(a.brain.before_answer))
    check("the answer cleared thinking", False in s.thinking_seen.get(paths[-1], set()))
    check("nothing is left armed for the next turn", a._earcon_due == 0.0)
    check("the turn ends not thinking, not speaking",
          not a.is_thinking() and not a.is_speaking())

    print("a fast turn: no earcon, the answer is the signal")
    a, s = make(earcon_after=0.3, delay=0.0)
    a.converse("hi", source="voice")
    paths = [p for p, _ in s.played]
    check("only the answer played", paths == ["/fake/Here is the answer..wav"], str(paths))
    check("...with the cold-start pad, as the first clip", s.played[0][1] is True)
    check("the earcon was never rendered", A.EARCON_TEXT not in s.rendered, str(s.rendered))

    print("a turn arriving just under the wire also stays clean")
    a, s = make(earcon_after=0.3, delay=0.15)
    a.converse("hi", source="voice")
    check("no earcon when the answer beats the delay",
          [p for p, _ in s.played] == ["/fake/Here is the answer..wav"], str(s.played))

    print("earcon_after = 0 turns it off")
    a, s = make(earcon_after=0.0, delay=0.4)
    a.converse("slow one", source="voice")
    check("a slow turn plays only the answer",
          [p for p, _ in s.played] == ["/fake/Here is the answer..wav"], str(s.played))
    check("status reports it off", a.status()["earcon_after"] == 0.0)

    print("a turn no model is answering never gets one")
    a, s = make(earcon_after=0.05, delay=0.0)
    # speak() directly — the wake "Yes?", a greeting — never sets the clock,
    # and a long render of that line must not read as thinking.
    real_render = s.render_tts

    def slow_render(text, speed=150, voice="en"):
        time.sleep(0.2)                    # piper on a cold voice
        return real_render(text, speed, voice)
    s.render_tts = slow_render
    a.speak("Yes?")
    check("a direct utterance plays alone", [p for p, _ in s.played] == ["/fake/Yes?.wav"],
          str(s.played))
    check("on_thinking never fired for it", a._earcon_due == 0.0)

    print("a barge-in while he is thinking suppresses it")
    a, s = make(earcon_after=0.15, delay=0.6)
    threading.Timer(0.05, a.interrupt).start()
    a.converse("long question", source="voice")
    check("nothing played at all", s.played == [], str(s.played))
    check("the earcon was not rendered either", A.EARCON_TEXT not in s.rendered, str(s.rendered))
    check("the turn still ended cleanly", not a.is_thinking() and not a.is_speaking())

    print("the earcon does not carry over: a second turn starts its own clock")
    a, s = make(earcon_after=0.15, delay=0.4)
    a.converse("one", source="voice")
    a.brain.delay = 0.0
    a.converse("two", source="voice")
    paths = [p for p, _ in s.played]
    check("first turn: earcon + answer; second: answer only",
          paths == [earcon, "/fake/Here is the answer..wav", "/fake/Here is the answer..wav"],
          str(paths))

    print("warm_earcon pre-renders it, and survives a dead TTS")
    a, s = make(earcon_after=0.15, delay=0.0)
    a.warm_earcon()
    check("the earcon is in the cache before anyone asks", s.rendered == [A.EARCON_TEXT],
          str(s.rendered))
    s.render_tts = lambda *a_, **k: (_ for _ in ()).throw(RuntimeError("piper gone"))
    a.warm_earcon()
    check("a render failure at boot is swallowed", True)
    a2, s2 = make(earcon_after=0.05, delay=0.3)
    s2.render_tts = lambda text, speed=150, voice="en": None if text == A.EARCON_TEXT \
        else f"/fake/{text}.wav"
    a2.converse("q", source="voice")
    check("no TTS for the earcon: the answer still comes, on the cold-start pad",
          s2.played == [("/fake/Here is the answer..wav", True)], str(s2.played))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

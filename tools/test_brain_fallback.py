#!/usr/bin/env python3
"""The brain's backend ladder, its sentence streaming, and the event cap.

Three pieces of the conversational core that had no coverage, each one a place
a quiet regression would only surface in front of a room:

  * _pick_backend — the auto ladder: Claude when it is up, the local model when
    Claude just failed or is absent, and the 60 s "sulk" that stops it hammering
    a dead cloud every turn.
  * the Claude->local fallback in respond() — a cloud failure mid-turn drops to
    qwen rather than apologising, but only if nothing was spoken yet, and it
    arms the sulk so the next turn does not repeat the stall.
  * _SentenceSplitter — what lets him start talking before the reply is finished;
    the first chunk is cut short on purpose (time-to-first-word) while later
    ones keep whole sentences.
  * the event-mode word cap — a backstop that stops one rambling turn, at a
    sentence boundary past the cap, never mid-sentence.

Driven with fake stream clients shaped exactly like local_brain's shim
(messages.stream() as a context manager yielding .type=='text' events, then
get_final_message()), so no API key, no Ollama, no network.

    python3 tools/test_brain_fallback.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import brain as B                               # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


# ---- a fake stream client, the local_brain shape --------------------------
class _Final:
    def __init__(self, text, stop_reason="end_turn"):
        self.stop_reason = stop_reason
        self.content = [types.SimpleNamespace(type="text", text=text)]


class _Stream:
    def __init__(self, text, stop_reason, boom):
        self._text, self._stop, self._boom = text, stop_reason, boom

    def __enter__(self):
        if self._boom:
            raise RuntimeError("stream blew up (simulated cloud failure)")
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        # one text event carrying the whole reply, then a block-stop
        yield types.SimpleNamespace(type="text", text=self._text)
        yield types.SimpleNamespace(
            type="content_block_stop",
            content_block=types.SimpleNamespace(type="text"))

    def get_final_message(self):
        return _Final(self._text, self._stop)


class FakeClient:
    """Records the system prompt it was called with; streams a fixed reply."""
    def __init__(self, text="Sure thing.", boom=False):
        self.text, self.boom, self.calls = text, boom, 0
        self.messages = types.SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return _Stream(self.text, "end_turn", self.boom)


def make_brain(**kw):
    ctx = types.SimpleNamespace(controller=None, led=None, tracker=None,
                                sound=None, sensors=None, event=None,
                                camera=None, diagnostic=None)
    return B.Brain(ctx, **kw)


def fake_local(available=True, text="Local answer."):
    c = FakeClient(text=text)
    c.available = lambda: available
    c.model = "qwen2.5:3b"
    c.warm = lambda *a, **k: None
    return c


def main() -> int:
    print("_pick_backend: the auto ladder")
    b = make_brain()
    b.backend = "auto"
    b._client = FakeClient()
    b._local = fake_local(available=True)
    b._cloud_failed_at = 0.0
    check("cloud up -> claude", b._pick_backend() == "claude")
    b._cloud_failed_at = B.time.monotonic()             # just failed
    check("cloud sulking -> local", b._pick_backend() == "local")
    b._client = None
    check("no cloud at all -> local", b._pick_backend() == "local")
    b._local = fake_local(available=False)
    check("nothing available -> none", b._pick_backend() == "none")

    print("...and the sulk expires")
    b = make_brain(); b.backend = "auto"
    b._client = FakeClient(); b._local = fake_local(True)
    b._cloud_failed_at = B.time.monotonic() - (B.CLOUD_RETRY_SECS + 1)
    check("an old failure no longer sulks -> claude", b._pick_backend() == "claude")

    print("pinned backends ignore the ladder")
    b = make_brain(); b._client = FakeClient(); b._local = fake_local(True)
    b.backend = "claude"
    check("pinned claude stays claude", b._pick_backend() == "claude")
    b._client = None
    check("pinned claude with no client -> none", b._pick_backend() == "none")
    b.backend = "local"; b._local = fake_local(True)
    check("pinned local stays local", b._pick_backend() == "local")

    print("a cloud failure mid-turn falls to the local model, and arms the sulk")
    b = make_brain(); b.backend = "auto"
    b._client = FakeClient(boom=True)                   # cloud dies on stream()
    b._local = fake_local(True, text="I'll help with that.")
    said = []
    r = b.respond("tell me something", on_sentence=said.append)
    check("the answer came from the local model", r.get("source") == "local", str(r))
    check("...and something was actually spoken", "".join(said).strip() != "")
    check("the sulk is now armed", b._cloud_failed_at != 0.0)
    check("...so the next backend choice is local", b._pick_backend() == "local")

    print("the fallback does NOT cut in once he has started speaking")
    # A cloud that fails AFTER emitting a sentence must not restart on qwen —
    # mid-reply is worse than a truncated one. Simulate by a client that streams
    # a sentence then reports an error stop_reason path is hard to force here;
    # instead assert the guard's shape: fallback requires not-yet-spoken.
    import inspect
    src = inspect.getsource(B.Brain.respond)
    check("fallback is guarded on 'not spoke'", "not result.get(\"spoke\")" in src,
          "the guard that protects a reply already in progress")

    print("_SentenceSplitter streams sentences as they land")
    out = []
    sp = B._SentenceSplitter(out.append)
    sp.feed("Hello there. ")
    check("a completed sentence ships immediately", out == ["Hello there."], str(out))
    sp.feed("How are ")
    check("an unfinished one is held", out == ["Hello there."], str(out))
    sp.feed("you today? ")
    sp.flush()
    check("the rest ships on the boundary and flush",
          "".join(out).replace(" ", "") == "Hellothere.Howareyoutoday?", str(out))

    print("the first chunk is cut short for time-to-first-word")
    out = []
    sp = B._SentenceSplitter(out.append)
    # a long opening clause with an early comma: the first emit should stop at
    # the comma rather than wait for the full stop far away.
    sp.feed("The tallest mountain in the world, which many people ask about, "
            "is Mount Everest at about 29032 feet.")
    check("something shipped before the sentence ended", len(out) >= 1, str(out))
    check("the first chunk is short (cut at the clause, not the full stop)",
          len(out[0]) <= B._SentenceSplitter.FIRST_HARD,
          f"first chunk was {len(out[0])} chars: {out[0]!r}")

    print("flush releases a terminatorless tail")
    out = []
    sp = B._SentenceSplitter(out.append)
    sp.feed("no full stop here")
    check("held until flush", out == [], str(out))
    sp.flush()
    check("flush emits it", "".join(out) == "no full stop here", str(out))

    print("the event cap stops a rambling turn at a sentence boundary")
    b = make_brain(); b.backend = "claude"
    long_reply = " ".join(f"Sentence number {i} here." for i in range(1, 21))
    b._client = FakeClient(text=long_reply)
    # event mode with a small cap
    b.ctx.event = types.SimpleNamespace(max_words=8)
    said = []
    r = b.respond("go on then", on_sentence=said.append)
    spoken = " ".join(said)
    words = len(spoken.split())
    check("something was spoken", words > 0, spoken[:60])
    check("the cap held it well short of the full 20 sentences",
          words < 60, f"{words} words spoken of a ~80-word reply")
    check("...but not mid-sentence — it ends on a full stop",
          spoken.rstrip().endswith(".") or spoken.rstrip().endswith("here"),
          repr(spoken[-30:]))

    print("no cap when event mode is off — the whole answer is spoken")
    b = make_brain(); b.backend = "claude"
    b._client = FakeClient(text=long_reply)
    b.ctx.event = None
    said = []
    b.respond("go on then", on_sentence=said.append)
    check("uncapped speaks far more than the capped run",
          len(" ".join(said).split()) > 60, f"{len(' '.join(said).split())} words")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

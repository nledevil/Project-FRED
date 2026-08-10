"""FRED's 'brain' — turn a line of recognised speech (or typed text) into an
action and a spoken reply.

Hybrid, as chosen for this build:
  1. **Local first.** ``commands.match_local`` handles the known commands
     instantly, offline, at zero cost.
  2. **Claude fallback.** Anything else goes to Claude (Anthropic API) with the
     same actions exposed as tools, so it can both *answer* open questions and
     *act* on natural-language commands the matcher didn't catch. Replies are
     kept short because they're spoken aloud.

Degrades gracefully: with no ``ANTHROPIC_API_KEY`` the local commands still work
and open questions get a polite "my AI brain isn't connected" reply.
"""
from __future__ import annotations

import os
import re
import time

from . import commands, sysinfo

try:
    import anthropic
    _ANTHROPIC_ERR = None
except Exception as exc:  # noqa: BLE001
    anthropic = None
    _ANTHROPIC_ERR = exc

# Haiku answers in ~0.7 s where Opus takes ~1.7 s (measured on this rig). FRED's
# replies are one or two spoken sentences over a small tool set, which Haiku
# handles well, and a talking head is judged on how fast it answers.
MODEL = "claude-haiku-4-5-20251001"

# Only some models accept output_config={"effort": ...}; Haiku 4.5 rejects it
# with a 400, so we send it only where it's supported.
_EFFORT_MODELS = ("claude-opus-4", "claude-sonnet-5", "claude-fable-5")

# A sentence ends at .!?… possibly followed by a closing quote/bracket, then
# whitespace. Requiring the trailing whitespace keeps "3.5" and "8 p.m." intact.
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"'’”)\]]*\s")

# A clause boundary — where we may cut the *first* chunk early to start talking
# sooner. Piper already pauses at a comma, so the seam is inaudible.
_CLAUSE_END = re.compile(r"(?<=[,;:—–])\s")

# "Forget what we were talking about" — a spoken reset of the conversation
# memory, so the next visitor at an event starts clean. Kept distinct from the
# servo "reset/home" command (see commands._PATTERNS) by requiring these phrases.
_NEW_CONV = re.compile(
    r"\b(new conversation|start over|start fresh|starting over|"
    r"let'?s start (over|fresh|again)|forget (what|everything|all|that|it)|"
    r"clear your memory|never ?mind (all )?(that|it))\b", re.I)

# How much of the recent back-and-forth FRED replays into each Claude turn, so
# follow-ups ("who was Einstein?" → "when was he born?") resolve. Exchanges, not
# messages — one exchange is a user line plus FRED's reply. Bounded to keep the
# token cost (and latency) of every turn small.
HISTORY_MAX_EXCHANGES = 6
# Drop the whole history after this long with no interaction, so a fresh person
# walking up to FRED isn't answered in the context of the last kid's questions.
HISTORY_IDLE_SECS = 180.0

SYSTEM = (
    "You are FRED, a friendly animatronic robot head — an InMoov build driven by "
    "a Raspberry Pi. You hear people through a microphone and reply through a "
    "speaker, so your replies are spoken aloud: keep them short and natural, "
    "no markdown, no lists, no emoji, no stage directions. "
    # Every word costs time: the speech synthesiser runs slower than realtime, so
    # a rambling answer keeps the listener waiting before FRED even starts.
    "Be brief — usually one sentence, never more than two, and under 30 words. "
    "Lead with the answer, then stop; don't pad with pleasantries or caveats. "
    "You have a body you can move with your tools — your jaw/mouth, your eyes, "
    "turning your head/neck left and right, face tracking, and a red 'terminator' "
    "LED. You may also have a 360-degree surround camera on a mast above you: "
    "look_around returns real photos of any direction, and scan_surroundings "
    "tells you where people are — use them whenever someone asks what you can "
    "see or who is around, and describe what's actually in the pictures. You "
    "may sit on a motorised cart: drive_cart makes short, safety-governed "
    "moves when your operator allows it, and stop_cart always stops. "
    "You can also sense things about yourself: the current date and time, "
    "your own network address, and the temperature of your processor — all in the "
    "facts below, which are read fresh every time you answer. Never claim you lack "
    "a sensor for something listed there. When someone asks you to do "
    "something you have a tool for, call the tool, then give a brief spoken "
    "confirmation. For general questions, just answer briefly and "
    "conversationally. If you didn't catch what they meant, say so and ask them "
    "to repeat. Your name is FRED, short for Facial Recognition and Expression "
    "Droid."
)


class _SentenceSplitter:
    """Turn a stream of text deltas into whole sentences, emitted as they land.

    This is what lets FRED start speaking before Claude has finished writing: the
    first sentence goes to the synthesiser while the rest is still arriving.

    Fragments shorter than ``MIN_EMIT`` are held back so an abbreviation ("Mr.
    Smith") isn't mistaken for a sentence boundary, and a run of text with no
    terminator is broken at a word boundary past ``MAX_BUFFER`` so a rambling
    reply still starts playing. ``flush()`` releases whatever is left.

    The *first* chunk is special. Piper renders at roughly 0.7x realtime, so the
    length of the first chunk is essentially FRED's time-to-first-word: one long
    opening sentence ("The tallest mountain in the world is Mount Everest,
    standing at about 29,032 feet above sea level.") costs seconds of silence
    before he says anything. So once the opening runs past ``FIRST_MAX`` we cut it
    at the earliest clause boundary — a comma piper would have paused at anyway —
    and failing that at a word boundary. Later chunks are already covered by the
    previous one playing, so they keep whole sentences and natural prosody.
    """

    MIN_EMIT = 8        # chars; shorter than any real sentence, longer than "Mr."
    MAX_BUFFER = 240    # chars; force a break rather than wait for a full stop
    FIRST_MAX = 60      # chars; past this, cut the opening chunk at a clause
    FIRST_HARD = 100    # chars; past this, cut the opening chunk anywhere sane

    def __init__(self, emit):
        self._emit = emit
        self._buf = ""
        self._shipped = False        # has the first chunk gone out?

    def feed(self, delta: str) -> None:
        self._buf += delta
        while self._step():
            pass

    def _step(self) -> bool:
        """Emit at most one chunk. True if it did (so feed() tries again)."""
        m = _SENTENCE_END.search(self._buf)
        if m and len(self._buf[:m.start()].strip()) >= self.MIN_EMIT:
            return self._cut(m.end())
        if not self._shipped and len(self._buf) >= self.FIRST_MAX:
            cut = self._first_cut()
            if cut:
                return self._cut(cut)
        if len(self._buf) > self.MAX_BUFFER:
            cut = self._buf.rfind(" ", 0, self.MAX_BUFFER)
            if cut > 0:
                return self._cut(cut)
        return False

    def _first_cut(self) -> int:
        """Where to break the opening chunk: earliest clause boundary, else a word
        boundary once it's clearly one long run-on. 0 = don't cut yet."""
        for m in _CLAUSE_END.finditer(self._buf):
            if len(self._buf[:m.start()].strip()) >= self.MIN_EMIT:
                return m.end()
        if len(self._buf) >= self.FIRST_HARD:
            return max(0, self._buf.rfind(" ", 0, self.FIRST_HARD))
        return 0

    def _cut(self, at: int) -> bool:
        head, self._buf = self._buf[:at], self._buf[at:]
        return self._ship(head)

    def flush(self) -> None:
        head, self._buf = self._buf, ""
        self._ship(head)

    def _ship(self, s: str) -> bool:
        s = s.strip()
        if not s:
            return False
        self._shipped = True
        self._emit(s)
        return True


class Brain:
    """Local-first, Claude-backed intent handler."""

    def __init__(self, ctx, api_key: str | None = None, model: str | None = None,
                 history_exchanges: int = HISTORY_MAX_EXCHANGES,
                 history_idle_secs: float = HISTORY_IDLE_SECS):
        self.ctx = ctx
        self.model = model or MODEL
        self._client = None
        self._ai_error = None
        # Rolling conversation memory: the last few *final* user/assistant text
        # pairs (never the intermediate tool_use/tool_result blocks), replayed
        # into each Claude turn so FRED can follow "he/it/that" back-references.
        self._history: list[dict] = []
        self._history_at = 0.0                 # monotonic time of the last turn
        self._max_exchanges = max(0, history_exchanges)
        self._idle_secs = history_idle_secs
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if anthropic is not None and key:
            try:
                self._client = anthropic.Anthropic(api_key=key)
            except Exception as exc:  # noqa: BLE001
                self._ai_error = str(exc)
        elif anthropic is None:
            self._ai_error = "anthropic SDK not installed"
        else:
            self._ai_error = "no ANTHROPIC_API_KEY"

    def ai_available(self) -> bool:
        return self._client is not None

    # ---- conversation memory ---------------------------------------------
    def clear_history(self) -> None:
        """Forget the running conversation (idle timeout, reset phrase, or an
        explicit control from the web panel)."""
        self._history = []

    def _remember(self, user_text: str, reply: str) -> None:
        """Append one completed exchange, trimmed to the last N. Stores plain
        text only — the tool_use/tool_result turns are dropped so the replayed
        history stays cheap."""
        if self._max_exchanges <= 0:
            return
        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": reply})
        keep = self._max_exchanges * 2
        if len(self._history) > keep:
            self._history = self._history[-keep:]

    def respond(self, text: str, on_sentence=None) -> dict:
        """Return {reply, source, actions}. ``source`` is local|claude|none|error.

        ``on_sentence`` (optional) is called with each sentence of the reply the
        moment it's complete — for the Claude path that happens *while the rest is
        still streaming*, so the caller can start speaking early. Every path calls
        it for everything it will return in ``reply``, so a caller that speaks
        from the callback must not also speak the return value.
        """
        emit = on_sentence or (lambda _s: None)
        text = (text or "").strip()
        if not text:
            return {"reply": "", "source": "none", "actions": []}

        # A gap long enough that this is probably a new person: start clean so
        # follow-up resolution doesn't reach back into the previous chat.
        now = time.monotonic()
        if self._history and (now - self._history_at) > self._idle_secs:
            self._history = []
        self._history_at = now

        # "Let's start over" — an explicit spoken reset of the memory.
        if _NEW_CONV.search(text):
            self.clear_history()
            reply = "Okay, let's start fresh. What would you like to know?"
            emit(reply)
            return {"reply": reply, "source": "local", "actions": ["new_conversation"]}

        local = commands.match_local(text)
        if local:
            name, args = local
            try:
                reply = commands.execute_action(self.ctx, name, **args)
            except Exception as exc:  # noqa: BLE001 - same deal as the tool loop below:
                # a hardware glitch should make FRED apologise, not 500 the caller.
                print(f"[Brain] action {name} failed: {exc}")
                reply = "Sorry, something went wrong with my hardware."
                emit(reply)
                return {"reply": reply, "source": "error", "actions": [name],
                        "error": str(exc)}
            if reply:
                emit(reply)
            return {"reply": reply, "source": "local", "actions": [name]}

        if self._client is None:
            reply = ("Sorry, I can't answer that — my A.I. brain isn't "
                     "connected yet.")
            emit(reply)
            return {"reply": reply, "source": "none", "actions": []}

        return self._ask_claude(text, emit)

    # ---- Claude fallback --------------------------------------------------
    def _ask_claude(self, text: str, emit) -> dict:
        # Replay the recent exchanges so back-references resolve. This is a fresh
        # list; the tool loop appends its intermediate turns here without
        # touching self._history, which only ever holds final text pairs.
        messages = self._history + [{"role": "user", "content": text}]
        actions: list[str] = []
        said: list[str] = []                 # every sentence handed to emit()
        system = f"{SYSTEM}\n\n{sysinfo.context_block()}"   # live facts, fixed for this turn

        def ship(sentence: str) -> None:
            said.append(sentence)
            emit(sentence)

        try:
            for _ in range(4):                       # bounded tool loop
                kwargs = dict(model=self.model, max_tokens=400, system=system,
                              messages=messages, tools=commands.CLAUDE_TOOLS)
                if self.model.startswith(_EFFORT_MODELS):
                    kwargs["output_config"] = {"effort": "low"}  # snappy — it's spoken
                splitter = _SentenceSplitter(ship)
                # Stream so the first sentence reaches the speaker while the rest
                # is still being written. Text blocks arrive before tool_use ones,
                # so FRED says "Sure," and *then* the servo moves.
                with self._client.messages.stream(**kwargs) as stream:
                    for delta in stream.text_stream:
                        splitter.feed(delta)
                    resp = stream.get_final_message()
                splitter.flush()
                if resp.stop_reason != "tool_use":
                    break
                messages.append({"role": "assistant", "content": resp.content})
                results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        actions.append(block.name)
                        try:
                            out = commands.run_tool(self.ctx, block.name, block.input)
                        except Exception as exc:  # noqa: BLE001 - a wedged servo or I2C
                            # glitch shouldn't kill the turn. Hand the failure back and
                            # let FRED tell the user, mid-conversation, what went wrong.
                            print(f"[Brain] tool {block.name} failed: {exc}")
                            out = f"That failed: {exc}"
                        results.append({"type": "tool_result",
                                        "tool_use_id": block.id, "content": out})
                messages.append({"role": "user", "content": results})
            reply = " ".join(said).strip()
            if reply:
                # Only genuine spoken answers go into memory — not the "Done." /
                # "didn't catch that" fillers, which would just add noise to the
                # replayed context.
                self._remember(text, reply)
            else:
                reply = "Done." if actions else "Sorry, I didn't catch that."
                emit(reply)
            return {"reply": reply, "source": "claude", "actions": actions}
        except Exception as exc:  # noqa: BLE001 - network/API hiccup shouldn't crash the loop
            trouble = "I'm having trouble reaching my brain right now."
            if not said:            # already talking? don't cut in with an apology
                emit(trouble)
            return {"reply": " ".join(said).strip() or trouble,
                    "source": "error", "actions": actions, "error": str(exc)}

"""FRED's 'brain' — turn a line of recognised speech (or typed text) into an
action and a spoken reply.

Hybrid, as chosen for this build:
  1. **Matcher first.** ``commands.match_local`` handles the known commands
     instantly, offline, at zero cost.
  2. **An LLM for the rest.** Anything else goes to a model with the same actions
     exposed as tools, so it can both *answer* open questions and *act* on
     natural-language commands the matcher didn't catch. Replies are kept short
     because they're spoken aloud.

Step 2 has two backends, chosen by ``backend``:
  * ``"claude"`` — the Anthropic API. Best answers, needs internet.
  * ``"local"``  — a local model via Ollama (``inmoov/local_brain.py``). Runs on
    this machine with no network at all.
  * ``"auto"``   — Claude when it's reachable, local when it isn't. FRED is
    taken to events without reliable WiFi, so the fallback is the point: he gets
    a worse answer instead of no answer.

The local client deliberately mimics the Anthropic SDK's shape, so the streaming
sentence splitter and the tool loop below are backend-agnostic — ``_ask_llm``
runs identically either way.

Degrades gracefully: with no ``ANTHROPIC_API_KEY`` and no local model the
matcher still works and open questions get a polite "my AI brain isn't
connected" reply.
"""
from __future__ import annotations

import base64
import os
import re
import time

from . import commands, sysinfo
from .local_brain import (DEFAULT_HOST as LOCAL_HOST, DEFAULT_MODEL as LOCAL_MODEL,
                          LocalClient)

try:
    import anthropic
    _ANTHROPIC_ERR = None
except Exception as exc:  # noqa: BLE001
    anthropic = None
    _ANTHROPIC_ERR = exc

BACKENDS = ("auto", "claude", "local")

# In "auto", how long a Claude failure keeps us on the local model before we try
# the cloud again. Without this, every utterance during an outage pays a full
# network timeout before falling back — which the listener hears as a hang.
CLOUD_RETRY_SECS = 60.0

# Haiku answers in ~0.7 s where Opus takes ~1.7 s (measured on this rig). FRED's
# replies are one or two spoken sentences over a small tool set, which Haiku
# handles well, and a talking head is judged on how fast it answers.
MODEL = "claude-haiku-4-5-20251001"

# The shortest gap between two *camera grabs*, in seconds. A look costs a camera
# start and ~1 s of latency, and nothing stops Claude calling the tool on every
# turn of a conversation about what it saw once. Inside this window the last
# frame is re-sent instead (see Brain._look), so a follow-up question still gets
# a real picture — this bounds the camera thrash, not the answers.
LOOK_MIN_SECS = 12.0

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
    "You are FRED, a friendly animatronic robot head — an InMoov build, designed, "
    "built and coded by Ryan Schultz. Three computers run you, so never call "
    "yourself a Raspberry Pi robot: an ASUS NUC is your brain and runs your "
    "speech, your vision and this conversation; a Raspberry Pi in your head "
    "drives your servos and camera; a second Raspberry Pi in your chest handles "
    "your sensors and touchscreen. That is all you know about your own build — "
    "don't invent parts, materials or where anything is mounted, and keep the "
    "answer to a sentence unless asked for more. "
    "You hear people through a microphone and reply through a "
    "speaker, so your replies are spoken aloud: keep them short and natural, "
    "no markdown, no lists, no emoji, no stage directions. "
    # Every word costs time: the speech synthesiser runs slower than realtime, so
    # a rambling answer keeps the listener waiting before FRED even starts.
    "Be brief — usually one sentence, never more than two, and under 30 words. "
    "Lead with the answer, then stop; don't pad with pleasantries or caveats. "
    "You have a body you can move with your tools — your jaw/mouth, your eyes, "
    "turning your head/neck left and right, face tracking, and a red 'terminator' "
    "LED. "
    # The prompt above has always told FRED he has vision. Until the look tool
    # existed that was a promise he couldn't keep, and he'd describe a room he
    # had never seen. Now the sight is real, but only through the tool — so the
    # rule that matters is that he must look before he claims to have looked.
    f"You can see, but only by calling your {commands.VISION_TOOL} tool, which "
    "shows you what your eye camera is pointed at right now. Call it before "
    "answering anything about what is in front of you or what someone is "
    "wearing, holding or showing you. Until you call it you have not seen "
    "anything this turn: never describe, guess at or invent what is in front of "
    "you, and if the look fails just say you couldn't see. "
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
                 history_idle_secs: float = HISTORY_IDLE_SECS,
                 backend: str = "auto", local_model: str | None = None,
                 local_host: str | None = None, vision: bool = True,
                 look_min_secs: float = LOOK_MIN_SECS):
        self.ctx = ctx
        self.vision = bool(vision)
        self._look_min_secs = max(0.0, float(look_min_secs))
        # Per camera ("eyes"/"wide"): when it was last grabbed, and the frame
        # itself, re-sent for repeat looks inside the rate-limit window. Held
        # because the conversation memory drops tool blocks, so a follow-up
        # question arrives with no picture in context at all.
        self._last_look: dict[str, float] = {}
        self._last_frame: dict[str, bytes] = {}
        self.model = model or MODEL
        self.backend = backend if backend in BACKENDS else "auto"
        self._client = None
        self._ai_error = None
        # The local model. Constructing this is free — it opens no connection and
        # only probes the daemon when asked — so it's always built, and whether
        # it's usable is answered by local_available() at call time.
        self._local = LocalClient(host=local_host or LOCAL_HOST,
                                  model=local_model or LOCAL_MODEL)
        self._cloud_failed_at = 0.0      # monotonic; 0 = cloud not known-bad
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

    def _look(self, which: str, tool_input: dict | None = None):
        """Answer the vision tool — with a picture, or with why there isn't one.

        Returns either image content blocks for the tool_result or a plain
        sentence. Every failure path returns a sentence: handing back nothing
        would leave Claude to fill the gap, and the failure mode this whole
        feature exists to fix is FRED describing a room he never saw.

        Only Claude gets the image. local_brain._to_ollama flattens tool_result
        content with str(), so an image block would reach the local model as a
        page of base64 — a text-only backend has to be told it can't see.
        """
        if not self.vision:
            return "My eyes are switched off at the moment."
        if which != "claude":
            return "I can't see right now — I'm running on my local brain."
        # Cameras are rate-limited and cached separately: they point at
        # different things, so a wide look must never be answered with the eye
        # camera's frame just because that one is recent.
        source = str((tool_input or {}).get("camera") or "eyes")
        if source not in ("eyes", "wide"):
            source = "eyes"
        now = time.monotonic()
        waited = now - self._last_look.get(source, 0.0)
        cached = self._last_frame.get(source)
        if cached is not None and waited < self._look_min_secs:
            # Inside the window, re-send the frame he already took rather than
            # grabbing a new one. It must be re-sent, not merely referred to:
            # _history keeps only final text, so by the next turn the previous
            # image is gone from the conversation entirely. Telling Claude to
            # "answer from the look you already have" instead of handing the
            # picture back got a confidently wrong shirt colour in testing —
            # the exact fabrication this tool exists to stop.
            return self._frame_blocks(cached, source,
                                      f"a moment ago ({waited:.0f}s)")
        jpeg, why = commands.capture_view(self.ctx, source=source)
        if jpeg is None:
            return why
        self._last_look[source] = now
        self._last_frame[source] = jpeg
        return self._frame_blocks(jpeg, source, "right now")

    @staticmethod
    def _frame_blocks(jpeg: bytes, source: str, when: str) -> list[dict]:
        """Wrap a JPEG as the content blocks of a tool_result."""
        lens = ("your wide chest camera, which takes in the whole room"
                if source == "wide" else "your eye camera, pointed where you are facing")
        return [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(jpeg).decode("ascii")}},
            {"type": "text",
             "text": f"This is what {lens} saw {when}. Describe only what is "
                     "actually in this picture."},
        ]

    def ai_available(self) -> bool:
        """True when *some* LLM backend can answer an open question."""
        if self.backend == "claude":
            return self._client is not None
        if self.backend == "local":
            return self._local.available()
        return self._client is not None or self._local.available()

    def local_available(self) -> bool:
        return self._local.available()

    def set_backend(self, backend: str) -> str:
        """Switch backend at runtime. Unknown values fall back to 'auto'."""
        self.backend = backend if backend in BACKENDS else "auto"
        self._cloud_failed_at = 0.0        # a deliberate switch clears the sulk
        return self.backend

    def set_vision(self, on: bool) -> bool:
        """Turn looking on or off at runtime.

        Switching off drops the cached frames too: they are pictures of whoever
        was in front of him, and "stop looking" should not leave the last one
        sitting in memory ready to be re-sent.
        """
        self.vision = bool(on)
        if not self.vision:
            self._last_frame.clear()
            self._last_look.clear()
        return self.vision

    def status(self) -> dict:
        """What the admin panel shows about the brain."""
        cam = getattr(self.ctx, "camera", None)
        spotter = getattr(self.ctx, "spotter", None)
        return {"backend": self.backend, "backends": list(BACKENDS),
                "claude_model": self.model, "claude_ready": self._client is not None,
                "claude_error": self._ai_error,
                "local_model": self._local.model, "local_ready": self._local.available(),
                "local_host": self._local.host, "local_models": self._local.models(),
                "active": self._pick_backend(),
                # Vision: the switch, and whether a look would actually work.
                # Separate on purpose — "on but no camera" and "camera but
                # switched off" are different problems and the panel should be
                # able to say which. Cloud-only, hence the backend in the test.
                "vision": self.vision,
                "vision_ready": bool(self.vision and self._client is not None
                                     and cam is not None and cam.available()),
                "vision_wide_ready": bool(spotter is not None and spotter.is_running()),
                "vision_min_seconds": self._look_min_secs}

    def _pick_backend(self) -> str:
        """Which backend a question would actually go to right now."""
        if self.backend == "claude":
            return "claude" if self._client is not None else "none"
        if self.backend == "local":
            return "local" if self._local.available() else "none"
        # auto: prefer Claude, unless it just failed us or isn't configured.
        cloud_sulking = (self._cloud_failed_at
                         and time.monotonic() - self._cloud_failed_at < CLOUD_RETRY_SECS)
        if self._client is not None and not cloud_sulking:
            return "claude"
        if self._local.available():
            return "local"
        return "claude" if self._client is not None else "none"

    def warm_local(self) -> None:
        """Pre-load the local model and the prompt prefix it will reuse (see
        LocalClient.warm). Safe on any thread — this is the one place that knows
        both halves of that prefix, so it hands them over."""
        if self.backend in ("auto", "local"):
            self._local.warm(SYSTEM, commands.CLAUDE_TOOLS)

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

        choice = self._pick_backend()
        if choice == "none":
            reply = ("Sorry, I can't answer that — my A.I. brain isn't "
                     "connected yet.")
            emit(reply)
            return {"reply": reply, "source": "none", "actions": []}

        if choice == "claude":
            result = self._ask_llm(text, emit, "claude")
            # In auto, a Claude failure is exactly the case this whole module
            # exists for: fall through to the local model rather than apologise.
            # Only when nothing was spoken yet — cutting in mid-reply would be
            # worse than the truncated answer.
            if (result.get("source") == "error" and self.backend == "auto"
                    and not result.get("spoke") and self._local.available()):
                print("[Brain] Claude failed — falling back to the local model.")
                self._cloud_failed_at = time.monotonic()
                return self._ask_llm(text, emit, "local")
            return result
        return self._ask_llm(text, emit, "local")

    # ---- LLM turn (identical for both backends) ---------------------------
    def _ask_llm(self, text: str, emit, which: str) -> dict:
        # Replay the recent exchanges so back-references resolve. This is a fresh
        # list; the tool loop appends its intermediate turns here without
        # touching self._history, which only ever holds final text pairs.
        # The live facts (clock, chip temperature) ride on *this turn's* user
        # message rather than in the system prompt, and that placement is a
        # latency decision, not a style one. A local model re-reads its whole
        # prompt from the first byte that differs from the last one, and the
        # system block plus the tool schemas is ~1150 of the ~1240 tokens. With a
        # clock in it, every utterance changed byte ~1100 and re-read the lot:
        # 28 s before FRED said a word, measured, every single time. Static, the
        # prefix is cached and only the facts and the question are read: ~1.5 s.
        # Claude doesn't care either way — it reads the same tokens — so both
        # backends share this one shape.
        messages = self._history + [
            {"role": "user", "content": f"{sysinfo.context_block()}\n\n{text}"}]
        actions: list[str] = []
        said: list[str] = []                 # every sentence handed to emit()
        system = SYSTEM                      # static: see the note above

        def ship(sentence: str) -> None:
            said.append(sentence)
            emit(sentence)

        # Both clients expose the same messages.stream(...) surface — that is the
        # whole design of local_brain — so everything below is backend-agnostic.
        client = self._client if which == "claude" else self._local
        model = self.model if which == "claude" else self._local.model

        try:
            for _ in range(4):                       # bounded tool loop
                kwargs = dict(model=model, max_tokens=400, system=system,
                              messages=messages, tools=commands.CLAUDE_TOOLS)
                if which == "claude" and model.startswith(_EFFORT_MODELS):
                    kwargs["output_config"] = {"effort": "low"}  # snappy — it's spoken
                splitter = _SentenceSplitter(ship)
                # Stream so the first sentence reaches the speaker while the rest
                # is still being written. Text blocks arrive before tool_use ones,
                # so FRED says "Sure," and *then* the servo moves.
                with client.messages.stream(**kwargs) as stream:
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
                            if block.name == commands.VISION_TOOL:
                                # Returns image blocks, not a sentence — the one
                                # tool whose result Claude looks at rather than
                                # reads. See Brain._look.
                                out = self._look(which, block.input)
                            else:
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
            return {"reply": reply, "source": which, "actions": actions}
        except Exception as exc:  # noqa: BLE001 - network/API hiccup shouldn't crash the loop
            # Don't apologise out loud yet: in auto, respond() may still retry
            # this turn on the local model. ``spoke`` tells it whether cutting in
            # would interrupt something already being said.
            print(f"[Brain] {which} turn failed: {exc}")
            if said or self.backend != "auto" or which == "local":
                trouble = "I'm having trouble reaching my brain right now."
                if not said:        # already talking? don't cut in with an apology
                    emit(trouble)
                return {"reply": " ".join(said).strip() or trouble,
                        "source": "error", "actions": actions,
                        "spoke": bool(said), "error": str(exc)}
            return {"reply": "", "source": "error", "actions": actions,
                    "spoke": False, "error": str(exc)}

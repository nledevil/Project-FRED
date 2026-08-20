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

from . import commands, face_id, sysinfo
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
    # "forget me" earns its place now that there is a face to forget as well as
    # a transcript. Somebody who asks a robot at a public event to forget them
    # should be able to say it the obvious way and have it actually happen.
    r"forget (me|about me)|clear your memory|"
    r"never ?mind (all )?(that|it))\b", re.I)

# How much of the recent back-and-forth FRED replays into each Claude turn, so
# follow-ups ("who was Einstein?" → "when was he born?") resolve. Exchanges, not
# messages — one exchange is a user line plus FRED's reply. Bounded to keep the
# token cost (and latency) of every turn small.
HISTORY_MAX_EXCHANGES = 6
# Drop the whole history after this long with no interaction, so a fresh person
# walking up to FRED isn't answered in the context of the last kid's questions.
# The same number governs how long a *face* survives (see inmoov/face_id.py):
# the two memories are halves of one conversation and expire together.
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

# What gets added to SYSTEM depends on which backend is answering, because the
# two have genuinely different senses. Both are constants: the prefix has to stay
# byte-identical turn to turn or the local model reprocesses the whole prompt —
# that is the 28 s stall the note in _ask_llm is about.
#
# Claude's half exists because search results pull the wrong way: they arrive as
# prose with citations and symbols, and the measured answer to "weather in 60440"
# came back at ~60 words with a registered-trademark sign in it. FRED speaks his
# answers, so the length and plainness rules have to be restated where the search
# happens rather than left to the general brevity rule further up.
SYSTEM_WEB = (
    " You can look things up on the internet when the answer depends on "
    "something current — weather, news, prices, scores, when something opens. "
    "Say the answer in your own words, in one or two short spoken sentences: no "
    "symbols like the degree or trademark sign, no lists, no reading out URLs. "
    "For anything settled — history, arithmetic, how something works — just "
    "answer, don't look it up."
)
# Covers both ways FRED can end up without the web: running on the local model,
# which never gets the tool at all (see commands.web_search_tool), and the switch
# being off. Deliberately doesn't name a reason, because the two reasons differ
# and a wrong one is worse than none. Same voice as the vision refusal in _look,
# for the same reason: "I can't" with no why sounds like a fault.
SYSTEM_NO_WEB = (
    " You have no way to look anything up on the internet right now. If someone "
    "asks for something current, like the weather or the news, say that plainly "
    "rather than guessing at it."
)

# Face recall's half. Two rules, and the second one is the important one: FRED
# must never be the one to decide he recognises somebody. The facts either say
# so or they don't, and "don't" includes every turn where the camera saw nothing
# — which at an event is most of them. Without this he will happily greet the
# first person of the day as an old friend.
#
# The honesty line is not decoration either. Children ask "will you remember
# me?", and the true answer is a good one: for a few minutes, and then it is
# gone and there is no copy. He should be able to say that.
SYSTEM_FACES = (
    " You can tell when the person in front of you is one you have already been "
    "talking to in this conversation — but only when the facts below say so. "
    "Never say you recognise, remember or have met anyone unless it is stated "
    "there, and never guess at a name. Your memory for a face lasts only as "
    "long as this conversation, a few minutes, and nothing about it is stored "
    "anywhere or kept afterwards. Say that plainly if anyone asks."
)


def _web_place(location: dict | None) -> str:
    """The 'you are here' line, or "" when no location is configured.

    user_location on the tool only tilts which results rank highest — it is not
    something the model can read. Without being told where he is, FRED searched
    "weather today", got results for half the country and asked the visitor
    where they were standing. He is the one who knows.
    """
    if not location:
        return ""
    where = ", ".join(str(location[k]) for k in ("city", "region")
                      if location.get(k))
    if not where:
        return ""
    return (f" You are in {where}. When someone asks about local conditions "
            "without naming a place — the weather, what it's like outside — "
            "that is the place they mean.")


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
                 look_min_secs: float = LOOK_MIN_SECS,
                 web_search: bool = True, web_location: dict | None = None,
                 face_recall: bool = True, face_hold_camera: bool = False):
        self.ctx = ctx
        self.vision = bool(vision)
        # Noticing a returning visitor. Rides on the same switch as vision — it
        # is the same camera looking at the same person — and on the same idle
        # timeout as the conversation, which is the promise the feature was
        # agreed on. Nothing it holds is ever written down; see face_id.py.
        self.face_recall = bool(face_recall)
        # Off by default: face recall rides on frames the camera is producing
        # anyway rather than asking for the camera itself. On a robot where face
        # tracking is usually running that is free and sufficient; on one where
        # it isn't, this is the switch that makes the feature work at all — at
        # the price of the camera running because somebody spoke to him. See
        # FaceId.attend.
        self.face_hold_camera = bool(face_hold_camera)
        self.faces = face_id.FaceId(idle_secs=history_idle_secs)
        # Web search costs money per search and only works on Claude. Switchable
        # like vision, and persisted the same way, because "no lookups today" is
        # an event-day decision rather than a code change.
        self.web_search = bool(web_search)
        self._web_location = dict(web_location) if web_location else None
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

    # ---- faces ------------------------------------------------------------
    def _face_recall_on(self) -> bool:
        """Whether FRED is noticing faces at all right now."""
        return self.face_recall and self.vision and self.faces.available()

    def attend_faces(self) -> None:
        """Somebody is talking: let the recogniser watch for a while.

        Public because ``respond()`` is slightly too late to be the only caller.
        A burst needs about five frames — two and a half seconds at the sampler's
        pace — and respond() builds its message immediately, so a visitor who
        walks back up is recognised on their *second* question rather than their
        first. Calling this from the wake word instead (Assistant._on_wake) buys
        the whole length of the utterance plus the transcription, which is
        usually enough. It is idempotent and cheap: it extends the window if the
        loop is already running.

        The frame source hands back None unless the sensor is already running
        for some other reason (face tracking, someone watching the panel), so by
        default face recall is a passenger on frames that exist anyway and never
        turns a camera on — the cheap thing and the defensible one. Set
        ``face_hold_camera`` if this robot's face tracking is usually off and
        the feature would otherwise never see anyone.
        """
        if not self._face_recall_on():
            return
        cam = getattr(self.ctx, "camera", None)
        if cam is None:
            return
        hold = (cam.acquire, cam.release) if self.face_hold_camera else None
        self.faces.attend(
            lambda: cam.capture_gray() if cam.is_streaming() else None, hold=hold)

    def _face_facts(self) -> str:
        """The one line about who is standing there, or "" for "no idea".

        Only ever says somebody has come *back*. While they are still standing
        there the conversation history already covers it, and the difference
        between "I know you're here" and "I know you were here before" is the
        whole feature.
        """
        if not self._face_recall_on():
            return ""
        who = self.faces.current()
        if who is None or who.sightings < 2:
            return ""
        line = ("\n\nThe person in front of you now is the same one who was "
                "talking to you earlier in this conversation, and has come "
                "back.")
        if who.notes:
            said = "; ".join(f'"{n}"' for n in who.notes)
            line += f" Earlier they said: {said}."
        return line + " Mention it only if it fits naturally.\n"

    def set_face_recall(self, on: bool) -> bool:
        """Turn returning-visitor recognition on or off. Off forgets at once —
        a switch that left the faces sitting in memory would be a lie."""
        self.face_recall = bool(on)
        if not self.face_recall:
            self.faces.stop()
            self.faces.forget_all()
        return self.face_recall

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
        if source == "eyes" and self._face_recall_on():
            # A frame taken because somebody asked him to look is the best kind
            # of evidence there is — he is pointed at the person. Only the eye
            # camera: the wide one is a 3840x1080 panorama whose faces are small
            # and lens-warped, and a cascade pass over it costs far more than
            # the frame is worth.
            self.faces.observe_jpeg(jpeg)
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
            # Same argument, one step further: the faces are a distillation of
            # those same pictures, so "stop looking" has to drop them too.
            self.faces.stop()
            self.faces.forget_all()
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
                "vision_min_seconds": self._look_min_secs,
                # Faces: the switch, whether it could work, and how many people
                # are in memory right now. Deliberately a count and not the
                # visitors themselves — what somebody said to FRED is not
                # something to put on a status page.
                "face_recall": self.face_recall,
                "face_recall_ready": self._face_recall_on(),
                "faces_known": len(self.faces.status()["visitors"]),
                # Same split as vision: the switch, and whether a lookup would
                # land. Cloud-only, so a local-only robot reads "on, not ready"
                # rather than looking broken.
                "web_search": self.web_search,
                "web_search_ready": bool(self.web_search and self._client is not None),
                "web_search_location": self._web_location}

    def set_web_search(self, on: bool) -> bool:
        """Turn internet lookups on or off. Returns the new state."""
        self.web_search = bool(on)
        return self.web_search

    def _system_for(self, which: str) -> str:
        """The system prompt this backend gets, told the truth about the web.

        Face recall is in here rather than in the per-turn facts because the
        rule that matters is the *prohibition* — never claim to recognise
        anybody unless told — and a rule that only appears on the turns where
        somebody was recognised is no rule at all. Both backends get it: the
        fact it guards is plain text, so the local model can use it too.
        """
        faces = SYSTEM_FACES if self._face_recall_on() else ""
        if which == "claude" and self.web_search:
            return SYSTEM + faces + SYSTEM_WEB + _web_place(self._web_location)
        return SYSTEM + faces + SYSTEM_NO_WEB

    def _tools_for(self, which: str) -> list:
        """The tool list that backend may actually use.

        The web tool is Claude's alone — it is executed by Anthropic, so handing
        it to the local model would advertise a capability nothing on this robot
        can carry out. See commands.web_search_tool.
        """
        if which != "claude" or not self.web_search:
            return commands.CLAUDE_TOOLS
        return commands.CLAUDE_TOOLS + [commands.web_search_tool(self._web_location)]

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
        explicit control from the web panel).

        Takes the faces with it. "Let's start over" and "a new person walked
        up" are the same event as far as this robot is concerned, and half a
        reset — a cleared transcript with the last visitor's face still in
        memory — would be the worst of both.
        """
        self._history = []
        self.faces.forget_all()

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
            self.faces.forget_all()
        self._history_at = now
        # Somebody is talking to him, so somebody is standing in front of him:
        # the one moment worth spending frames on. (FaceId expires faces on its
        # own clock as well — the promise has to hold even if nobody ever speaks
        # to him again.)
        self.attend_faces()

        # "Let's start over" — an explicit spoken reset of the memory.
        if _NEW_CONV.search(text):
            self.clear_history()
            reply = "Okay, let's start fresh. What would you like to know?"
            emit(reply)
            return {"reply": reply, "source": "local", "matched": "new_conversation",
                    "actions": ["new_conversation"]}

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
            # ``matched`` marks the offline fast path for the heard log: a row
            # without it is a row the matcher recognised nothing in, which is
            # the set worth reviewing after an event.
            return {"reply": reply, "source": "local", "matched": name,
                    "actions": [name]}

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
        # Event mode's answer cap, or None. Asked for per turn rather than held,
        # because the switch is thrown from the panel mid-event.
        cap = getattr(getattr(self.ctx, "event", None), "max_words", None)
        brief = ("\n\nYou are at a public event: answer in one short sentence, "
                 f"under {cap} words. Someone is waiting behind this person.\n"
                 if cap else "")
        # Who is standing there, when he has come back — a per-turn fact like
        # the clock and the chip temperature, and here for the same reason: it
        # changes every turn, so it must not be in the cached prefix.
        messages = self._history + [
            {"role": "user",
             "content": f"{sysinfo.context_block()}{brief}{self._face_facts()}\n\n{text}"}]
        actions: list[str] = []
        said: list[str] = []                 # every sentence handed to emit()
        # Static per backend: see the note above. What the suffix says depends on
        # the backend and the switch, but for any one of them it is the same
        # bytes every turn, so neither prompt cache is churned.
        system = self._system_for(which)
        tools = self._tools_for(which)
        spoken_words = 0
        capped = False

        def ship(sentence: str) -> None:
            # The cap is on what is *spoken*, not on what is generated: the cost
            # being managed is the listener's time. The mic is paused while he
            # talks (the USB codec wedges if capture and playback overlap), so a
            # long answer cannot be cut short by voice and a child has to wait it
            # out. Tokens are already bounded by max_tokens; attention is not.
            #
            # It stops at the first sentence boundary *past* the cap, so one long
            # sentence still gets said in full. That is deliberate — half a
            # spoken sentence sounds like a fault, not like brevity — and it
            # means this is a backstop against rambling, not a word-exact limit.
            # The instruction in the prompt is what actually keeps answers short;
            # measured, it took a 48-word answer to 23 against a cap of 25. This
            # is here for the turn where that instruction is ignored.
            nonlocal spoken_words, capped
            if capped:
                return
            said.append(sentence)
            emit(sentence)
            if cap is not None:
                spoken_words += len(sentence.split())
                capped = spoken_words >= cap

        # Both clients expose the same messages.stream(...) surface — that is the
        # whole design of local_brain — so everything below is backend-agnostic.
        client = self._client if which == "claude" else self._local
        model = self.model if which == "claude" else self._local.model

        try:
            for _ in range(4):                       # bounded tool loop
                kwargs = dict(model=model, max_tokens=400, system=system,
                              messages=messages, tools=tools)
                if which == "claude" and model.startswith(_EFFORT_MODELS):
                    kwargs["output_config"] = {"effort": "low"}  # snappy — it's spoken
                splitter = _SentenceSplitter(ship)
                # Stream so the first sentence reaches the speaker while the rest
                # is still being written. Text blocks arrive before tool_use ones,
                # so FRED says "Sure," and *then* the servo moves.
                with client.messages.stream(**kwargs) as stream:
                    # Events, not text_stream, for the sake of one character. A
                    # web search splits the reply into two text blocks — one
                    # before the search, one after — and text_stream concatenates
                    # them with nothing in between, so "...zip code." + "Thunder-
                    # storms..." arrives as "code.Thunderstorms". _SENTENCE_END
                    # needs whitespace after the full stop, so without this the
                    # two sentences are spoken as one long run-on. A block that
                    # ends is a sentence that ended; say so.
                    for event in stream:
                        if event.type == "text":
                            splitter.feed(event.text)
                        elif (event.type == "content_block_stop"
                                and event.content_block.type == "text"):
                            splitter.feed("\n")
                    resp = stream.get_final_message()
                splitter.flush()
                if resp.stop_reason == "pause_turn":
                    # A server-side search ran long and the API parked the turn
                    # part-way. It is not finished — the old `!= "tool_use"` test
                    # read this as "done" and stopped FRED mid-answer, which
                    # sounds like he lost his train of thought. Hand the paused
                    # turn straight back, unchanged: the blocks carry the
                    # encrypted search results the API needs to resume, so they
                    # must not be filtered or rebuilt on the way through.
                    messages.append({"role": "assistant", "content": resp.content})
                    continue
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
                # ...and pin what they asked to the face in front of him, so
                # "you were asking me about servos" has something to name. A
                # no-op when no face has been seen: a note with nobody attached
                # to it is just an unlabelled record of what a child said.
                if self._face_recall_on():
                    self.faces.remember(text)
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

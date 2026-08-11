"""A local LLM backend for FRED, so he still thinks with no internet.

Talks to `Ollama <https://ollama.com>`_ on localhost. The point of this module is
the *shape* of what it exposes: it mimics the small slice of the Anthropic SDK
that ``brain._ask_claude`` already drives —

    with client.messages.stream(model=…, system=…, messages=…, tools=…) as s:
        for delta in s.text_stream:
            …
        final = s.get_final_message()      # .stop_reason, .content[].type/.name/.input/.id

— so the streaming sentence splitter, the bounded tool loop, and the
conversation memory all work against a local model **unchanged**. The only thing
that differs is which client object gets constructed.

Wire format notes (Anthropic → Ollama), all handled in ``_to_ollama``:
  * Tools: ``{name, description, input_schema}`` → ``{type:function,
    function:{name, description, parameters}}``.
  * A ``system`` string is a separate kwarg for Anthropic, but the first message
    for Ollama.
  * Tool *results* are a user-role message holding ``tool_result`` blocks for
    Anthropic; Ollama wants one ``role:"tool"`` message each.
  * Ollama doesn't issue tool-call ids, so we mint them — the ids only have to
    round-trip within one turn, and the tool loop pairs them by position anyway.

Model choice matters more than it looks. Benchmarked through FRED's real system
prompt and tool set on this machine (Intel Arc iGPU, Vulkan):

    qwen2.5:3b    1.05 s to first word    8/8 tool calls    ~17 tok/reply
    llama3.2:3b   1.04 s                  6/8               ~13 tok/reply
    qwen3:4b      0.65 s                  4/8               ~152 tok/reply

qwen3 is a *reasoning* model: it spends a hundred-plus tokens thinking, and on
this build the thinking leaks into ``content`` (so FRED would say it out loud)
while its tool calls get lost. Don't use a reasoning model for a talking head.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

DEFAULT_HOST = "http://127.0.0.1:11434"
# Benchmarked best of the small models for this job — see the module docstring.
DEFAULT_MODEL = "qwen2.5:3b"

# A local model is a fixed cost we control, unlike a network round trip, so the
# timeout only has to cover a pathological generation, not congestion.
_TIMEOUT = 120.0
# How long a failed health check is trusted before we probe again. Long enough
# that a dead daemon doesn't cost a probe every utterance, short enough that
# starting Ollama makes FRED notice within a conversation.
_HEALTH_TTL = 30.0


class LocalBrainError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Anthropic-shaped response objects. Only the attributes brain.py touches.
# --------------------------------------------------------------------------
class _TextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, id: str, name: str, input: dict):   # noqa: A002 - mirrors the SDK
        self.id, self.name, self.input = id, name, input


class _FinalMessage:
    def __init__(self, content: list, stop_reason: str):
        self.content, self.stop_reason = content, stop_reason


def _to_ollama(system: str, messages: list, tools: list | None) -> tuple[list, list | None]:
    """Translate an Anthropic-style (system, messages, tools) into Ollama's."""
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})

    for msg in messages:
        role, content = msg.get("role"), msg.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        # A list of blocks: either our own assistant turn (text + tool_use) or a
        # user turn carrying tool_result blocks.
        text_parts, tool_calls, results = [], [], []
        for block in content or []:
            btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            get = (block.get if isinstance(block, dict) else lambda k, d=None: getattr(block, k, d))
            if btype == "text":
                text_parts.append(get("text") or "")
            elif btype == "tool_use":
                tool_calls.append({"function": {"name": get("name"),
                                                "arguments": get("input") or {}}})
            elif btype == "tool_result":
                results.append(get("content") or "")
        if results:
            # One Ollama tool message per result. Ollama keys these by position/
            # name rather than id, so the content is what actually matters.
            out.extend({"role": "tool", "content": str(r)} for r in results)
            continue
        entry: dict = {"role": role, "content": " ".join(p for p in text_parts if p).strip()}
        if tool_calls:
            entry["tool_calls"] = tool_calls
        out.append(entry)

    if not tools:
        return out, None
    conv = [{"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("input_schema") or {}}}
            for t in tools]
    return out, conv


class _Stream:
    """Context manager exposing ``text_stream`` then ``get_final_message()``."""

    def __init__(self, host: str, payload: dict):
        self._host, self._payload = host, payload
        self._resp = None
        self._text: list[str] = []
        self._calls: list[_ToolUseBlock] = []
        self._done = False

    def __enter__(self):
        req = urllib.request.Request(
            f"{self._host}/api/chat", data=json.dumps(self._payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            self._resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
        except (urllib.error.URLError, OSError) as exc:
            raise LocalBrainError(f"cannot reach Ollama at {self._host}: {exc}") from exc
        return self

    def __exit__(self, *exc):
        if self._resp is not None:
            self._resp.close()
        return False

    @property
    def text_stream(self):
        """Yield text deltas as they arrive; bank tool calls for the final message."""
        n = 0
        for line in self._resp:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue                      # a partial/garbled line: skip it
            if d.get("error"):
                raise LocalBrainError(str(d["error"]))
            msg = d.get("message") or {}
            chunk = msg.get("content") or ""
            if chunk:
                self._text.append(chunk)
                yield chunk
            for call in (msg.get("tool_calls") or []):
                fn = call.get("function") or {}
                name = fn.get("name")
                if not name:
                    continue
                args = fn.get("arguments")
                if isinstance(args, str):     # some builds send JSON as a string
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                n += 1
                self._calls.append(_ToolUseBlock(f"local_{n}", name, args or {}))
            if d.get("done"):
                break
        self._done = True

    def get_final_message(self) -> _FinalMessage:
        text = "".join(self._text).strip()
        content: list = []
        if text:
            content.append(_TextBlock(text))
        content.extend(self._calls)
        # "tool_use" is the signal brain.py's loop watches for to run tools and
        # go round again; anything else ends the turn.
        return _FinalMessage(content, "tool_use" if self._calls else "end_turn")


class LocalClient:
    """Anthropic-SDK-shaped client backed by a local Ollama server."""

    def __init__(self, host: str = DEFAULT_HOST, model: str = DEFAULT_MODEL):
        self.host = host.rstrip("/")
        self.model = model
        self._lock = threading.Lock()
        self._healthy: bool | None = None
        self._checked = 0.0
        self.messages = self._Messages(self)

    # ---- health ----------------------------------------------------------
    def available(self, force: bool = False) -> bool:
        """Is the Ollama daemon up and serving our model? Cached for a few
        seconds so the 'auto' backend can ask this on every utterance."""
        with self._lock:
            now = time.monotonic()
            if not force and self._healthy is not None and now - self._checked < _HEALTH_TTL:
                return self._healthy
            self._checked = now
            self._healthy = self._probe()
            return self._healthy

    def _probe(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=2.0) as r:
                tags = json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            return False
        names = {m.get("name", "") for m in (tags.get("models") or [])}
        # Accept "qwen2.5:3b" matching a listed "qwen2.5:3b" exactly, or the bare
        # name when the tag is implied.
        return any(n == self.model or n.split(":")[0] == self.model.split(":")[0]
                   for n in names)

    def models(self) -> list[str]:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=2.0) as r:
                return sorted(m.get("name", "") for m in (json.loads(r.read()).get("models") or []))
        except (urllib.error.URLError, OSError, ValueError):
            return []

    def warm(self) -> None:
        """Load the model into memory so the first real reply isn't slow.

        Never raises — it runs on a boot thread nobody joins, and a failed
        warm-up just means the first utterance pays the load.
        """
        try:
            req = urllib.request.Request(
                f"{self.host}/api/chat",
                data=json.dumps({"model": self.model, "stream": False,
                                 "messages": [{"role": "user", "content": "hi"}],
                                 "options": {"num_predict": 1}}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=_TIMEOUT):
                pass
        except Exception as exc:      # noqa: BLE001
            print(f"[LocalBrain] warm-up failed: {exc}")

    # ---- the SDK-shaped surface -----------------------------------------
    class _Messages:
        def __init__(self, client: "LocalClient"):
            self._c = client

        def stream(self, *, model=None, max_tokens: int = 400, system: str = "",
                   messages: list | None = None, tools: list | None = None,
                   **_ignored) -> _Stream:
            msgs, conv_tools = _to_ollama(system, messages or [], tools)
            payload = {
                "model": model or self._c.model,
                "messages": msgs,
                "stream": True,
                # Low temperature: FRED answers factual questions and picks tools.
                # Creativity here reads as unreliability.
                "options": {"temperature": 0.3, "num_predict": int(max_tokens)},
            }
            if conv_tools:
                payload["tools"] = conv_tools
            return _Stream(self._c.host, payload)

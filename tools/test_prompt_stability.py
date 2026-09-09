#!/usr/bin/env python3
"""The brain's prompt is warmed as it is used, and the robot names itself right.

Three small contracts from the architecture review, each of which failed
silently — nothing crashed, the robot just paid or said the wrong thing.

**The warm prompt.** local_brain.warm()'s own docstring says "pass the *same*
system prompt and tools the real turns use" — reading the prefix is the ~28 s
half of loading a local model, and warming with different bytes just moves
that cost onto whoever asks the first question. warm_local() passed bare
SYSTEM while live turns compose _system_for("local"); the prefixes diverged at
the first appended clause, so the warm bought the weights and little else.

**The effort list.** Requests carry output_config.effort only for models named
in _EFFORT_MODELS, and claude-opus-5 was missing while opus-4 was present — a
future swap to the newer Opus would silently stop sending effort, with nothing
failing anywhere. The tuple is asserted here so a new family gets added to the
test the same day it is added to the code.

**The identity page.** whoami reported local_model unconditionally, so the
IDENTITY page called qwen "the model" even while Claude was doing the talking
— misleading during exactly the "what is this robot running" check the page
exists for.

Runs with no API key, no Ollama, and no hardware: the local client is replaced
with a recorder and the brain's status is stubbed where needed.

    python3 tools/test_prompt_stability.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import whoami                                   # noqa: E402
from inmoov.brain import Brain, _EFFORT_MODELS              # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def make_brain(**kw):
    ctx = types.SimpleNamespace(controller=None, led=None, tracker=None,
                                sound=None, sensors=None, event=None,
                                camera=None, diagnostic=None)
    return Brain(ctx, **kw)


def main() -> int:
    print("the local model is warmed with the prompt live turns actually use")
    b = make_brain()
    got = {}
    b._local = types.SimpleNamespace(
        warm=lambda system, tools: got.update(system=system, tools=tools))
    b.warm_local()
    check("warm ran for the auto backend", bool(got))
    check("system half matches _system_for('local') byte for byte",
          got.get("system") == b._system_for("local"))
    check("tools half matches _tools_for('local')",
          got.get("tools") == b._tools_for("local"))
    check("...and it is not the bare SYSTEM constant",
          got.get("system") != __import__("inmoov.brain", fromlist=["SYSTEM"]).SYSTEM,
          "warming with bare SYSTEM was the bug")

    print("the prefix the warm bought is the prefix every local turn reuses")
    # The 28 s reread triggers on the first byte that differs, so the promise
    # is byte-stability across turns, not mere similarity.
    check("two compositions are identical",
          b._system_for("local") == b._system_for("local"))
    check("the local prompt never advertises the web",
          "look things up" not in b._system_for("local"))

    print("every current model family is in the effort list")
    for family in ("claude-opus-4", "claude-opus-5", "claude-sonnet-5",
                   "claude-fable-5"):
        check(f"{family} carries effort", family in _EFFORT_MODELS)
    check("haiku stays out deliberately — it is the default and takes none",
          not any(m.startswith("claude-haiku") for m in _EFFORT_MODELS))

    print("the identity page names the model that is answering")
    def brain_stub(active):
        return types.SimpleNamespace(status=lambda: {
            "backend": "auto", "active": active,
            "claude_model": "claude-haiku-4-5-20251001",
            "local_model": "qwen2.5:3b"})
    on_cloud = whoami.state(brain=brain_stub("claude"))["brain"]
    on_local = whoami.state(brain=brain_stub("local"))["brain"]
    check("cloud turns are attributed to Claude",
          on_cloud["model"] == "claude-haiku-4-5-20251001", str(on_cloud))
    check("local turns are attributed to qwen",
          on_local["model"] == "qwen2.5:3b", str(on_local))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

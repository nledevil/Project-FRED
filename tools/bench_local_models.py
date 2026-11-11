#!/usr/bin/env python3
"""Time the local fallback models the way FRED actually uses them.

The question a bigger local model has to answer is not "is it smarter" but
"does the first word still arrive before the earcon" — the local brain exists
for a child standing in front of him when the venue WiFi has gone, and its
whole design is latency: the cached prompt prefix, the 1.5 s "Hmm..." line,
first word under two seconds.

So this benches with the *real* request: the same system prompt and tool
schemas the brain hands the local model (Brain._system_for / _tools_for,
through local_brain._to_ollama), the same per-turn facts block on the user
message, the same options. For each model it measures:

  cold      loading the weights and reading the prefix once (what warm_local
            pays at boot, and what a visitor pays if the warm-up was skipped)
  re-read   prompt tokens evaluated on a warm turn — should be ~the facts +
            the question (~90); ~1200 means the prefix cache is not working
            for that model, which is the 28 s stall brain.py describes
  ttft      time to the first spoken token (or the tool call), p50 / worst
  tok/s     generation speed
  answer    wall time for the whole reply, p50

and shows each model's actual reply to each question, because a model that
is fast and wrong is not a candidate either. Six questions: facts about
himself, a general question, one the prompt says to refuse (weather, no web),
one that must become a tool call, and two event-shaped ones.

Runs against the live Ollama on this machine. It does not touch the brain
and makes no sound; it does load other models into the GPU for a while, so a
visitor talking to FRED during the run gets a slower local answer. The
configured model is re-warmed at the end, exactly as boot does.

    venv/bin/python tools/bench_local_models.py
    venv/bin/python tools/bench_local_models.py --models qwen2.5:3b,qwen2.5:7b
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import types
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import brain as B                              # noqa: E402
from inmoov import local_brain, settings, sysinfo          # noqa: E402  (settings.load_settings)

QUESTIONS = [
    "what is your name and who built you?",
    "how many computers run you?",
    "why is the sky blue?",
    "what's the weather going to be like tomorrow?",
    "can you turn your head to the left?",
    "what's the biggest number you know?",
]


def make_brain() -> B.Brain:
    ctx = types.SimpleNamespace(controller=None, led=None, tracker=None, sound=None,
                                sensors=None, event=None, camera=None, diagnostic=None)
    return B.Brain(ctx, api_key=None)


def chat(host: str, payload: dict):
    """Stream one /api/chat call. Returns (ttft_s, wall_s, text, tool, final)."""
    req = urllib.request.Request(f"{host}/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    ttft = None
    text, tool, final = "", None, {}
    with urllib.request.urlopen(req, timeout=300) as r:
        for line in r:
            if not line.strip():
                continue
            chunk = json.loads(line)
            msg = chunk.get("message") or {}
            if msg.get("tool_calls") and tool is None:
                tool = msg["tool_calls"][0].get("function", {}).get("name")
                ttft = ttft or time.perf_counter() - t0
            piece = msg.get("content") or ""
            if piece.strip() and ttft is None:
                ttft = time.perf_counter() - t0
            text += piece
            if chunk.get("done"):
                final = chunk
    return ttft, time.perf_counter() - t0, text, tool, final


def bench(host: str, model: str, system: str, tools: list, num_thread: int) -> dict:
    msgs, conv_tools = local_brain._to_ollama(system, [{"role": "user", "content": "hi"}], tools)
    base = {"model": model, "keep_alive": "15m"}
    # Cold: weights + the whole prefix, one token out. Unload first so it is
    # honestly cold, not warm from a previous run.
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{host}/api/generate", data=json.dumps({"model": model, "keep_alive": 0}).encode(),
            headers={"Content-Type": "application/json"}), timeout=60).read()
    except Exception:  # noqa: BLE001 - not loaded is fine
        pass
    t0 = time.perf_counter()
    _, _, _, _, final = chat(host, {**base, "stream": True, "messages": msgs, "tools": conv_tools,
                                    "options": {"num_predict": 1, "num_thread": num_thread}})
    cold = time.perf_counter() - t0
    prefix_tokens = final.get("prompt_eval_count")

    turns = []
    for q in QUESTIONS:
        user = f"{sysinfo.context_block()}\n\n{q}"
        msgs, conv_tools = local_brain._to_ollama(system, [{"role": "user", "content": user}], tools)
        payload = {**base, "stream": True, "messages": msgs, "tools": conv_tools,
                   "options": {"temperature": 0.3, "num_predict": 400, "num_thread": num_thread}}
        ttft, wall, text, tool, final = chat(host, payload)
        ev, evd = final.get("eval_count") or 0, final.get("eval_duration") or 1
        turns.append({"q": q, "ttft": ttft, "wall": wall, "text": text.strip(), "tool": tool,
                      "reread": final.get("prompt_eval_count"),
                      # How long those prompt tokens took: Ollama counts cached
                      # tokens in prompt_eval_count, so the count alone cannot
                      # tell a cache hit from a fast re-read. The time can.
                      "prompt_ms": (final.get("prompt_eval_duration") or 0) / 1e6,
                      "tps": ev / (evd / 1e9) if evd else 0.0})
    return {"model": model, "cold": cold, "prefix_tokens": prefix_tokens, "turns": turns}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default="qwen2.5:3b,qwen3:4b,qwen2.5:7b,llama3.1:8b")
    ap.add_argument("--host", default=None)
    args = ap.parse_args()

    cfg = settings.load_settings().get("brain", {})
    host = args.host or cfg.get("local_host") or "http://127.0.0.1:11434"
    configured = cfg.get("local_model") or "qwen2.5:3b"
    brain = make_brain()
    system = brain._system_for("local")
    tools = brain._tools_for("local")
    num_thread = local_brain._NUM_THREAD
    print(f"prefix: {len(system)} chars of system prompt + {len(tools)} tools; "
          f"num_thread={num_thread}; host={host}\n")

    results = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        print(f"== {model}", flush=True)
        try:
            r = bench(host, model, system, tools, num_thread)
        except Exception as exc:  # noqa: BLE001
            print(f"   failed: {exc}\n")
            continue
        results.append(r)
        for t in r["turns"]:
            what = f"-> tool {t['tool']}" if t["tool"] else t["text"].replace("\n", " ")[:110]
            print(f"   ttft {t['ttft'] or 0:5.2f}s  answer {t['wall']:5.2f}s  "
                  f"prompt {t['reread']:>5} tok in {t['prompt_ms']:6.0f} ms  "
                  f"{t['tps']:5.1f} tok/s | {t['q'][:38]:38} | {what}")
        print()

    print(f"{'model':14} {'cold':>6} {'prefix':>6} {'re-read':>7} {'prompt ms':>9} {'ttft p50':>8} "
          f"{'ttft max':>8} {'tok/s':>6} {'answer p50':>10}")
    for r in results:
        ts = r["turns"]
        ttfts = [t["ttft"] for t in ts if t["ttft"]]
        print(f"{r['model']:14} {r['cold']:6.1f} {r['prefix_tokens'] or 0:6d} "
              f"{int(statistics.median(t['reread'] or 0 for t in ts)):7d} "
              f"{statistics.median(t['prompt_ms'] for t in ts):9.0f} "
              f"{statistics.median(ttfts):8.2f} {max(ttfts):8.2f} "
              f"{statistics.median(t['tps'] for t in ts):6.1f} "
              f"{statistics.median(t['wall'] for t in ts):10.2f}")
    print("\nttft p50 is what a person waits after the thinking line starts; "
          "under ~1.5 s reads as prompt, over it as slow.")

    # Leave the robot as boot leaves it: the configured model loaded, prefix read.
    print(f"\nre-warming {configured} for the brain")
    local_brain.LocalClient(host=host, model=configured).warm(system, tools)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Compare Vosk models on this machine: what they hear, and what they cost.

FRED's speech model is the small one, and the suspicion is that it is why a
child walks away thinking he ignored them. Swapping it is a download and a path
change — which is exactly why it is worth measuring first rather than after.

**What this can and cannot tell you.** Cost is measured honestly: load time,
resident memory, and the real-time factor (seconds of CPU per second of audio),
which is the number that matters on a machine already giving about four cores
to the wide camera. Accuracy is only as good as the audio you feed it, and the
case that actually fails — a seven-year-old, three feet away, in a hall with
four hundred people in it — is not something this or any bench here can
synthesise. Feed it real recordings from an event and the comparison is real;
feed it clean speech and it will tell you all the models are fine, which is
already known and is not the question.

    # transcripts and cost, one model
    python3 tools/bench_asr.py --wav recordings/*.wav

    # side by side, and where they disagree
    python3 tools/bench_asr.py --wav recordings/*.wav \\
        --model vosk-model-small-en-us-0.15 --model vosk-model-en-us-0.22-lgraph

    # cost only, no audio needed: how long does it take to load and how big is it
    python3 tools/bench_asr.py --load-only --model vosk-model-en-us-0.22

Run it while the spotter is running if that is how FRED is used — the real-time
factor with four cores already spoken for is the number that decides whether a
bigger model is viable here, not the one measured on an idle machine.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import resource
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"

try:
    from vosk import KaldiRecognizer, Model, SetLogLevel
except Exception as exc:                                  # noqa: BLE001
    print(f"vosk is not importable here: {exc}")
    raise SystemExit(2)


def rss_mb() -> float:
    """Resident set size of this process, in MB."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def transcribe(model: Model, path: Path) -> tuple[str, float, float]:
    """Return (text, audio seconds, wall seconds)."""
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError(f"{path.name}: need 16-bit mono WAV")
        rate = wf.getframerate()
        audio_s = wf.getnframes() / float(rate)
        rec = KaldiRecognizer(model, rate)
        started = time.monotonic()
        words: list[str] = []
        while True:
            data = wf.readframes(4000)
            if not data:
                break
            if rec.AcceptWaveform(data):
                words.append(json.loads(rec.Result()).get("text", ""))
        words.append(json.loads(rec.FinalResult()).get("text", ""))
        wall = time.monotonic() - started
    return " ".join(w for w in words if w).strip(), audio_s, wall


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", action="append", default=[],
                    help="directory name under models/ (repeatable)")
    ap.add_argument("--wav", nargs="*", default=[],
                    help="16-bit mono WAV files (globs are expanded)")
    ap.add_argument("--load-only", action="store_true",
                    help="measure load time and memory, transcribe nothing")
    args = ap.parse_args()

    names = args.model or ["vosk-model-small-en-us-0.15"]
    paths = [Path(p) for pattern in args.wav for p in sorted(glob.glob(pattern))]
    if not paths and not args.load_only:
        print("no audio given. Use --wav, or --load-only for cost alone.\n"
              "Real recordings from an event are the only ones worth trusting "
              "here — see the module docstring.")
        return 1

    SetLogLevel(-1)
    results: dict[str, dict] = {}
    for name in names:
        directory = MODELS / name
        if not directory.is_dir():
            print(f"  {name}: not in models/ — download it first")
            continue
        before = rss_mb()
        t0 = time.monotonic()
        model = Model(str(directory))
        load_s = time.monotonic() - t0
        size_mb = sum(f.stat().st_size for f in directory.rglob("*") if f.is_file()) / 1e6

        print(f"\n{name}")
        print(f"  on disk {size_mb:8.0f} MB   load {load_s:5.2f} s   "
              f"resident +{rss_mb() - before:.0f} MB")

        said, audio_total, wall_total = {}, 0.0, 0.0
        for path in paths:
            try:
                text, audio_s, wall = transcribe(model, path)
            except Exception as exc:                       # noqa: BLE001
                print(f"  {path.name}: {exc}")
                continue
            said[path.name] = text
            audio_total += audio_s
            wall_total += wall
            print(f"  {path.name:28} {wall / max(audio_s, 1e-6):5.2f}x  {text!r}")
        if audio_total:
            # The number that decides viability here: >1.0 means it cannot keep
            # up with speech in real time, and it is measured against whatever
            # else this machine happens to be doing right now.
            print(f"  real-time factor over {audio_total:.1f}s of audio: "
                  f"{wall_total / audio_total:.2f}x")
        results[name] = {"said": said, "rtf": wall_total / audio_total if audio_total else None}
        del model

    if len(results) > 1 and paths:
        print("\nwhere they disagree")
        base = names[0]
        disagreed = 0
        for path in paths:
            heard = {n: results[n]["said"].get(path.name, "") for n in results}
            if len(set(heard.values())) > 1:
                disagreed += 1
                print(f"  {path.name}")
                for n, text in heard.items():
                    print(f"    {n:34} {text!r}")
        if not disagreed:
            print("  they agreed on every file — which, on clean audio, is the "
                  "expected and uninformative result")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Measure the PCM reopen cost on the audio device — the number V3 turns on.

FRED speaks one aplay per sentence, and each reopens the PCM. gap_lead_in (0.12 s
today) is silence prepended to every clip after the first to cover that reopen so
the sentence doesn't lose its opening syllable; lead_in (0.3 s live, a stale
override from the departed USB speakerphone) covers a cold start. Both were tuned
for other hardware and a race — aplay vs the listener's arecord restart — that no
longer exists now the reSpeaker keeps the mic open through a reply.

This measures the reopen cost directly and *ear-free*: it plays a short silent
clip many times and subtracts the clip's own duration, leaving the open+start+
close overhead. It needs no volume, so it's safe to run at any hour. If the
overhead is well under gap_lead_in, that padding is now mostly dead latency — the
data the by-ear tuning ("does the first word still make it?") starts from.

    tools/audio_latency.py                       # default device from settings
    tools/audio_latency.py -D plughw:C16K6Ch,0 -n 20 -d 0.05

Reports min / median / max overhead. It does NOT change any setting — reducing
lead_in/gap_lead_in is a by-ear call, because clipping is something only an ear
confirms, and this tool has none.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def silent_wav(path: Path, seconds: float, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def main() -> int:
    default_dev = "plughw:C16K6Ch,0"
    try:
        from inmoov.settings import load_settings
        default_dev = str(load_settings().get("sound", {}).get("device") or default_dev)
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-D", "--device", default=default_dev, help="ALSA device (aplay -D)")
    ap.add_argument("-n", "--trials", type=int, default=15)
    ap.add_argument("-d", "--clip", type=float, default=0.05, help="silent clip length (s)")
    args = ap.parse_args()

    if not shutil.which("aplay"):
        print("aplay not found — cannot measure")
        return 1

    tmp = Path(tempfile.mkdtemp()) / "silent.wav"
    silent_wav(tmp, args.clip)

    print(f"device      {args.device}")
    print(f"clip        {args.clip*1000:.0f} ms silent, x{args.trials} trials")
    print("measuring reopen overhead (elapsed - clip length)...\n")

    overheads = []
    for i in range(args.trials):
        t0 = time.monotonic()
        r = subprocess.run(["aplay", "-q", "-D", args.device, str(tmp)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        elapsed = time.monotonic() - t0
        if r.returncode != 0:
            print(f"  trial {i+1}: aplay failed ({r.stderr.decode()[:80].strip()})")
            continue
        overheads.append(max(0.0, elapsed - args.clip))

    if not overheads:
        print("no successful trials — wrong device?")
        return 1

    lo, mid, hi = min(overheads), median(overheads), max(overheads)
    print(f"  reopen overhead:  min {lo*1000:6.1f} ms   median {mid*1000:6.1f} ms   max {hi*1000:6.1f} ms")
    print()
    print(f"  gap_lead_in is 0.12 s = 120 ms. Median reopen here is {mid*1000:.0f} ms.")
    if mid < 0.04:
        print("  -> the reopen is cheap on this device; gap_lead_in is mostly dead")
        print("     latency. Lower it by ear (does each sentence still open cleanly?).")
    else:
        print("  -> the reopen still costs real time; keep gap_lead_in near this.")
    print()
    print("  lead_in (cold start) and clipping are acoustic — confirm those by ear")
    print("  with sound on. This tool changes nothing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

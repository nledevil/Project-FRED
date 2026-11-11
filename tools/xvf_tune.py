#!/usr/bin/env python3
"""Read, write, and re-apply the reSpeaker's own gain/AGC — with a clip meter beside it.

Channel 0 of the array clips in a merely-noisy room and worse in a hall, and the
fix is on the *device*, not in software: the XVF3800's ASR-output gain and its AGC
target/ceiling. Those were unreadable and unwritable from this repo until now
(inmoov/xvf_params.py). This is the bench front-end for them — and, crucially, it
measures the clip rate before and after a change, so a tuning is a number you
watched improve, not a value you hoped was better.

    tools/xvf_tune.py --show                       # read every known parameter
    tools/xvf_tune.py --clip --seconds 8           # measure the clip rate now
    tools/xvf_tune.py --set PP_AGCDESIREDLEVEL=0.002 --clip
                                                   # measure, write, measure again
    tools/xvf_tune.py --apply-config               # write settings.voice.xvf_params

A --set with --clip is the intended workflow: it records the clip rate, applies
the change (read-back verified), and records it again, printing both. To make a
tuning permanent, put the values in settings.voice.xvf_params — apply_from_settings
re-applies them at every boot, because the device forgets across a power cycle.

Writes are verified by reading the value straight back; a mismatch is reported,
not swallowed. Reads/writes cost nothing when the array is absent — they just say
so and exit.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np                                         # noqa: E402

from inmoov import xvf_params as XP                        # noqa: E402
from inmoov.settings import load_settings                  # noqa: E402

CLIP_AT = 32000            # |sample| at/above this (of 32767) is effectively clipped


def measure_clip(device: str, channels: int, channel: int, seconds: float) -> dict:
    """Capture from the array and report the fraction of channel-`channel` samples
    that are pinned near full scale — the thing the AGC/gain is there to keep down."""
    try:
        raw = subprocess.run(
            ["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", "16000",
             "-c", str(channels), "-d", str(int(max(1, seconds))), "-t", "raw"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=seconds + 10).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {"error": f"capture failed: {exc}"}
    if not raw:
        return {"error": "no audio captured (wrong device? array unplugged?)"}
    a = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        a = a[channel::channels]          # de-interleave to the one channel
    if a.size == 0:
        return {"error": "capture ended mid-frame"}
    clipped = int(np.count_nonzero(np.abs(a.astype(np.int32)) >= CLIP_AT))
    peak = int(np.max(np.abs(a.astype(np.int32))))
    return {"samples": int(a.size), "clipped": clipped,
            "clip_pct": 100.0 * clipped / a.size, "peak": peak}


def show_clip(label: str, m: dict) -> None:
    if "error" in m:
        print(f"  clip rate {label}: {m['error']}")
    else:
        print(f"  clip rate {label}: {m['clip_pct']:.4f}%  "
              f"({m['clipped']}/{m['samples']} samples >= {CLIP_AT}, peak {m['peak']})")


def parse_set(items: list[str]) -> dict:
    out = {}
    for item in items or []:
        if "=" not in item:
            print(f"ignoring {item!r}: expected NAME=VALUE")
            continue
        name, _, val = item.partition("=")
        name = name.strip()
        if name not in XP.PARAMS:
            print(f"ignoring {name!r}: not a known parameter ({', '.join(XP.PARAMS)})")
            continue
        kind = XP.PARAMS[name][2]
        out[name] = int(val) if kind == "int32" else float(val)
    return out


def main() -> int:
    st = load_settings()
    voice = st.get("voice", {})
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", action="store_true", help="read and print every known parameter")
    ap.add_argument("--set", nargs="*", metavar="NAME=VALUE", help="write parameters (verified)")
    ap.add_argument("--apply-config", action="store_true",
                    help="write settings.voice.xvf_params (what boot applies)")
    ap.add_argument("--clip", action="store_true", help="measure the clip rate")
    ap.add_argument("--seconds", type=float, default=6.0, help="clip capture length")
    ap.add_argument("--device", default="plughw:C16K6Ch,0", help="ALSA capture device")
    ap.add_argument("--channels", type=int, default=int(voice.get("mic_channels", 6)))
    ap.add_argument("--channel", type=int, default=int(voice.get("mic_channel", 0)))
    args = ap.parse_args()

    if not any([args.show, args.set is not None, args.apply_config, args.clip]):
        ap.print_help()
        return 0

    def clip():
        return measure_clip(args.device, args.channels, args.channel, args.seconds)

    xp = XP.XvfParams(log=print)
    if not xp.open():
        # A clip measurement needs no control handle, so still allow --clip.
        print(f"reSpeaker control unavailable: {xp.error}")
        if args.clip and not (args.show or args.set is not None or args.apply_config):
            show_clip("now", clip())
            return 0
        return 1
    try:
        if args.show:
            print("reSpeaker XVF3800 parameters")
            for name in XP.PARAMS:
                print(f"  {name:20} = {xp.read(name)!r}")

        before = clip() if (args.clip and (args.set or args.apply_config)) else None

        if args.set:
            wanted = parse_set(args.set)
            print("writing:" if wanted else "nothing valid to write")
            for name, val in wanted.items():
                back = xp.write(name, val, verify=True)
                print(f"  {name:20} -> {back!r}  "
                      + ("OK" if back is not None else f"FAILED ({xp.error})"))

        if args.apply_config:
            params = voice.get("xvf_params") or {}
            if not params:
                print("settings.voice.xvf_params is empty — nothing to apply")
            else:
                print("applying settings.voice.xvf_params:")
                for name, r in xp.apply(params).items():
                    print(f"  {name:20} -> {r}")

        if args.clip:
            if before is not None:
                show_clip("before", before)
                show_clip("after ", clip())
            else:
                show_clip("now", clip())
    finally:
        xp.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

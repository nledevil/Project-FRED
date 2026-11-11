#!/usr/bin/env python3
"""Watch every direction the mic array will give you, against speech energy.

The array answers "which way was that" in four places, and telling them apart
needs them side by side — spread alone is actively misleading, which is how the
free-running beam got picked here once already.

``DOA_VALUE`` (20, 18)
    Integer angle plus a speech byte. Lives in the *LED* resource: it drives the
    ring's direction mode. Coarse and latchy; not a control signal.

``AEC_AZIMUTH_VALUES`` (33, 75) -> beam1, beam2, free-running, auto-select
    Per beam, not per microphone — a single omni capsule has no direction at
    all, since direction only exists in the difference between capsules. (The
    six *audio* channels are beams too, while AEC_ASROUTONOFF is 1.) Beams 1
    and 2 are fixed and sit still. The **free-running** beam has the widest
    spread and is the trap: it wanders a hundred degrees through a silent room,
    which looks like tracking and is noise.

``AEC_SPENERGY_VALUES`` (33, 80)
    Per-beam speech energy. 0.0 between sentences, tens of thousands during one.
    This column is what tells tracking from wandering — watch a beam move while
    its energy is zero and you have your answer.

``AUDIO_MGR_SELECTED_AZIMUTHS`` (35, 11) -> processed, auto-select
    Index 0 is the device's own answer: speech energy weighed across the fixed
    beams, NaN when nobody is talking. This is what inmoov/mic_doa.py uses.

    python3 tools/doa_probe.py                  # live, 30 seconds
    python3 tools/doa_probe.py --seconds 90
    python3 tools/doa_probe.py --log doa.csv    # for comparing against where
                                                # you were actually standing

To settle which source to trust, stand at a known angle and talk: the column
that agrees with where you are is the answer. Ambient room noise cannot tell you
this, because there is no ground truth in it.

Safe to run while FRED is up — a control transfer on a different USB interface
from the audio, and the panel's poller can read alongside it.
"""
from __future__ import annotations

import argparse
import math
import struct
import sys
import time

try:
    import usb.core
    import usb.util
except ImportError:
    sys.exit("needs pyusb: venv/bin/pip install pyusb")

VENDOR_ID = 0x2886
_TIMEOUT_MS = 500
# (resource id, command id, payload bytes) — see inmoov/mic_doa.py.
DOA = (20, 18, 4)
AZIMUTH = (33, 75, 16)
SELECTED = (35, 11, 8)
ENERGY = (33, 80, 16)


# The device answers a read it is not ready for with this rather than failing,
# and wants to be asked again after a moment. AEC_AZIMUTH_VALUES does it on most
# first attempts. Sleeping between tries is what works — hammering it starves the
# servicer and fails far more often. See inmoov/mic_doa.py.
RETRY_STATUS = 64
RETRY_BACKOFF_S = 0.010
MAX_ATTEMPTS = 5


def read(dev, spec):
    """One vendor control read, payload without the leading status byte."""
    resid, cmdid, length = spec
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(RETRY_BACKOFF_S)
        try:
            raw = dev.ctrl_transfer(
                usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR
                | usb.util.CTRL_RECIPIENT_DEVICE,
                0, 0x80 | cmdid, resid, length + 1, _TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - a busy device is not a crash
            return None
        data = raw.tolist()
        if not data:
            return None
        if data[0] == 0:
            return data[1:]
        if data[0] != RETRY_STATUS:
            return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--hz", type=float, default=10.0)
    ap.add_argument("--log", help="also write CSV here")
    args = ap.parse_args()

    dev = usb.core.find(idVendor=VENDOR_ID)
    if dev is None:
        return print("no reSpeaker found on the bus") or 1
    ver = read(dev, (48, 0, 3))
    print(f"reSpeaker firmware {'.'.join(map(str, ver)) if ver else '?'}")
    print("Walk a slow arc while talking. The column that follows you is the one"
          " to use.\n")
    print(f"{'t':>6} {'DOA':>4} | {'fixed1':>7} {'fixed2':>7} {'free':>7} "
          f"{'auto':>7} | {'PROC':>7} | speech energy per beam")

    out = open(args.log, "w") if args.log else None
    if out:
        out.write("t,speech,doa,fixed1,fixed2,free,auto,processed,"
                  "e_fixed1,e_fixed2,e_free,e_auto\n")

    # Per-beam spread over the run: the beam that is actually tracking a person
    # walking around is the one whose value covers a wide range, while a parked
    # beam sits on one number. That is the whole question, so total it up rather
    # than making someone eyeball a scrolling column.
    lo = [999.0] * 4
    hi = [-999.0] * 4
    prev = [None] * 4
    t0 = time.monotonic()
    try:
        while (t := time.monotonic() - t0) < args.seconds:
            d = read(dev, DOA)
            a = read(dev, AZIMUTH)
            sel = read(dev, SELECTED)
            en = read(dev, ENERGY)
            if d is None or a is None or sel is None or en is None or len(d) < 3:
                time.sleep(1.0 / args.hz)
                continue
            doa = (d[0] + d[1] * 256) % 360
            speech = bool(d[2])
            degs = [math.degrees(v) % 360 for v in struct.unpack("<ffff", bytes(a[:16]))]
            proc = struct.unpack("<ff", bytes(sel[:8]))[0]
            energy = struct.unpack("<ffff", bytes(en[:16]))

            # Spread is only meaningful where there was speech to explain it —
            # that is the whole lesson of this tool.
            for i, v in enumerate(degs):
                if max(energy) > 1.0:
                    lo[i], hi[i] = min(lo[i], v), max(hi[i], v)
                prev[i] = v

            pstr = "    NaN" if math.isnan(proc) else f"{math.degrees(proc) % 360:7.1f}"
            print(f"{t:6.1f} {doa:4d} | " + " ".join(f"{v:7.1f}" for v in degs)
                  + f" | {pstr} | " + " ".join(f"{int(e):>8d}" for e in energy))
            if out:
                out.write(f"{t:.2f},{int(speech)},{doa},"
                          + ",".join(f"{v:.2f}" for v in degs)
                          + f",{'' if math.isnan(proc) else f'{math.degrees(proc) % 360:.2f}'},"
                          + ",".join(f"{e:.1f}" for e in energy) + "\n")
            time.sleep(1.0 / args.hz)
    except KeyboardInterrupt:
        pass
    finally:
        if out:
            out.close()

    print("\nspread *while speech energy was present* — a beam that only moves"
          "\nwhen nobody is talking is chasing noise, however wide it looks:")
    for i in range(4):
        name = ("fixed1", "fixed2", "free-running", "auto-select")[i]
        if hi[i] < lo[i]:
            print(f"   {name:<13}: never read while speech was present")
        else:
            print(f"   {name:<13}: {lo[i]:7.1f} .. {hi[i]:7.1f}   "
                  f"span {hi[i] - lo[i]:6.1f}deg")
    if args.log:
        print(f"\nwrote {args.log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

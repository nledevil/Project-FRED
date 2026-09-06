#!/usr/bin/env python3
"""The DOA bearing mapping, tested without a microphone array.

What the hardware gives you is a compass angle in the *board's* frame, which
depends on how it was screwed in. What the face tracker consumes is -1..+1 with
+ meaning "turn right". Everything interesting lives in the conversion between
those two, and none of it needs a device: the reads are stubbed and the mapping
is driven directly.

The cases that matter are the ones that are wrong in a way nobody would notice
from a bench test in a quiet room — a stale angle acted on as if it were live,
and the wrap at the back of the robot sending the head the wrong way as somebody
walks around him.

    python3 tools/test_mic_doa.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import mic_doa as M                             # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def heard(doa: M.MicDoa, angle: int, ago: float = 0.0) -> None:
    """Pretend the array steered to ``angle`` with speech on it ``ago`` seconds back."""
    doa._heard_angle = angle
    doa._heard_at = time.monotonic() - ago
    doa._angle, doa._speech = angle, True


def near(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


def main() -> int:
    print("an angle folds into -180..+180 around forward")
    for raw, want in [(0, 0), (90, 90), (180, -180), (181, -179),
                      (270, -90), (359, -1), (360, 0)]:
        check(f"{raw:>3}deg -> {want:>4}", M._wrap180(raw) == want,
              str(M._wrap180(raw)))

    print("the mapping puts a voice on the right side of the robot")
    doa = M.MicDoa(span_deg=90.0)
    heard(doa, 0)
    check("dead ahead is no deflection", near(doa.bearing(), 0.0), str(doa.bearing()))
    heard(doa, 90)
    check("90deg to one side is full deflection", near(doa.bearing(), 1.0),
          str(doa.bearing()))
    heard(doa, 270)
    check("...and the other side is the opposite sign", near(doa.bearing(), -1.0),
          str(doa.bearing()))
    heard(doa, 45)
    check("halfway is half a hint", near(doa.bearing(), 0.5), str(doa.bearing()))

    print("behind him clamps rather than wrapping")
    # The failure this guards: someone walking around the robot crosses the back,
    # and a wrapped mapping would flip the sign there — sending the head snapping
    # the wrong way at the exact moment they are hardest to find.
    doa = M.MicDoa(span_deg=90.0)
    for angle in (91, 135, 179):
        heard(doa, angle)
        check(f"{angle}deg still says 'right', hard", near(doa.bearing(), 1.0),
              str(doa.bearing()))
    for angle in (181, 225, 269):
        heard(doa, angle)
        check(f"{angle}deg still says 'left', hard", near(doa.bearing(), -1.0),
              str(doa.bearing()))

    print("a stale voice is no opinion at all")
    # The whole reason the speech flag is read: the array holds its last steer
    # angle forever, so without this the head drifts toward whoever spoke last,
    # however long ago that was.
    doa = M.MicDoa(stale_after=4.0)
    check("nothing heard yet is None, not 0.0", doa.bearing() is None,
          str(doa.bearing()))
    heard(doa, 90, ago=1.0)
    check("a recent voice is acted on", near(doa.bearing(), 1.0), str(doa.bearing()))
    heard(doa, 90, ago=9.0)
    check("an old one is not", doa.bearing() is None, str(doa.bearing()))
    check("...and status still shows the angle, so it can be seen",
          doa.status()["heard_angle"] == 90 and doa.status()["bearing"] is None,
          str(doa.status()["bearing"]))

    print("mounting is calibrated, not assumed")
    doa = M.MicDoa(span_deg=90.0, mount_offset=180.0)
    heard(doa, 180)
    check("a board mounted backwards still reports dead ahead as 0",
          near(doa.bearing(), 0.0), str(doa.bearing()))
    heard(doa, 270)
    check("...and its right is still right", near(doa.bearing(), 1.0),
          str(doa.bearing()))

    doa = M.MicDoa(span_deg=90.0, invert=True)
    heard(doa, 90)
    check("invert flips the sides", near(doa.bearing(), -1.0), str(doa.bearing()))

    print("capture_forward measures the offset instead of asking for it")
    doa = M.MicDoa(stale_after=4.0)
    out = doa.capture_forward()
    check("refuses when nothing has been heard", not out["ok"], out.get("error", ""))
    heard(doa, 137, ago=9.0)
    out = doa.capture_forward()
    check("refuses a stale reading rather than calibrating to a ghost",
          not out["ok"], out.get("error", ""))
    check("...and leaves the offset alone", doa.mount_offset == 0.0)
    heard(doa, 137, ago=0.5)
    out = doa.capture_forward()
    check("takes a live one", out["ok"] and doa.mount_offset == 137.0,
          f"offset={doa.mount_offset}")
    check("...after which that direction reads as straight ahead",
          near(doa.bearing(), 0.0), str(doa.bearing()))

    print("configure ignores what it should")
    doa = M.MicDoa()
    doa.configure(span_deg=45.0, invert=True, nonsense="x", enabled=None)
    t = doa.tuning()
    check("known keys applied", t["span_deg"] == 45.0 and t["invert"] is True)
    check("None means 'not mentioned', not 'set to false'", t["enabled"] is True)
    check("unknown keys are dropped, not stored", "nonsense" not in t)
    doa.configure(span_deg=0)
    check("a zero span cannot divide by zero", doa.span_deg >= 1.0,
          str(doa.span_deg))
    doa.configure(mount_offset=450)
    check("an out-of-range offset is folded, not stored raw",
          0 <= doa.mount_offset < 360, str(doa.mount_offset))

    print("a compass median survives the wrap and drops a lone outlier")
    # A plain median would answer 350 here, which is the failure this exists to
    # avoid: capture_forward puts *straight ahead* at the array's zero, so the
    # wrap is exactly where the robot spends its time.
    check("[350, 355, 5] medians near 355, not 350",
          M._circular_median([350.0, 355.0, 5.0]) == 355.0,
          str(M._circular_median([350.0, 355.0, 5.0])))
    check("a single stray sample is discarded, not averaged in",
          M._circular_median([50.0, 52.0, 117.0, 51.0, 53.0]) in (51.0, 52.0),
          str(M._circular_median([50.0, 52.0, 117.0, 51.0, 53.0])))
    check("a steady reading is returned unchanged",
          M._circular_median([90.0] * 5) == 90.0)

    doa = M.MicDoa(smooth_n=5, span_deg=90.0)
    for a in (50.0, 52.0, 117.0, 51.0, 53.0):
        doa._recent.append(a)
        doa._heard_angle = M._circular_median(list(doa._recent))
    doa._heard_at = time.monotonic()
    check("...so the outlier never reaches the head",
          doa.bearing() is not None and doa.bearing() < 0.7, str(doa.bearing()))
    check("smoothing can be switched off", M.MicDoa(smooth_n=0).smooth_n == 1)
    doa.configure(smooth_n=3)
    check("resizing keeps what still fits", doa.smooth_n == 3
          and len(doa._recent) == 3, f"{doa.smooth_n}/{len(doa._recent)}")

    print("the source defaults to the device's own answer, not a raw beam")
    # The mistake this guards against: the free-running beam has the widest
    # spread of the four and looks like the tracking one, but it wanders through
    # a silent room. Picking a source by spread picks the noise.
    check("default source is the processed DoA",
          M.MicDoa().source == M.SOURCE_PROCESSED, M.MicDoa().source)
    check("an unknown source falls back rather than breaking",
          M.MicDoa(source="nonsense").source == M.SOURCE_PROCESSED)
    d = M.MicDoa()
    d.configure(source=M.SOURCE_BEAM)
    check("a raw beam can be selected for diagnosis", d.source == M.SOURCE_BEAM)
    d.configure(source="rubbish")
    check("...but rubbish does not overwrite it", d.source == M.SOURCE_BEAM)
    check("both sources are offered", set(M.SOURCES) ==
          {M.SOURCE_PROCESSED, M.SOURCE_BEAM})

    print("the beam index is picked, and cannot point off the end")
    doa = M.MicDoa(beam=2)
    check("the default tracks the measured beam", M.DEFAULT_BEAM == 2)
    check("a beam past the last is clamped",
          M.MicDoa(beam=99).beam == 3, str(M.MicDoa(beam=99).beam))
    check("a negative beam is clamped to the first",
          M.MicDoa(beam=-1).beam == 0)
    doa.configure(beam=1)
    check("configure moves it", doa.beam == 1)
    check("...and it round-trips through tuning", doa.tuning()["beam"] == 1)

    print("disabled says nothing, whatever it heard")
    doa = M.MicDoa(enabled=False)
    heard(doa, 90)
    check("bearing is None while disabled", doa.bearing() is None)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

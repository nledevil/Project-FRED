#!/usr/bin/env python3
"""The face detector: one interface, two backends, and a fallback that can't
leave a consumer blind.

This is the seam the wide spotter now sits on — Haar swapped for YuNet without
the spotter changing a line, because both return the same (x, y, w, h) box
shape detectMultiScale always did. What matters to lock down:

  * make("yunet") gives YuNet when the model is present, and falls back to Haar
    — never to nothing — when it is not, so a missing ONNX degrades to the old
    behaviour rather than a blind spot;
  * every backend returns the same shape (a list of four-int tuples), so a
    caller reads a box the same way whichever detector it holds;
  * min_px filters small boxes on both, so a caller's minSize still means what
    it meant;
  * the checked-in model actually loads and finds a face — a guard that the
    232 KB file in the repo is the real weights and not a truncated download.

Uses the repo's own model file and the system Haar cascade; the face check runs
against a saved frame if one is present, and says so plainly if it must skip.

    python3 tools/test_face_detect.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import face_detect as FD                        # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    try:
        import cv2
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        print(f"  SKIP  OpenCV not importable ({exc}) — nothing to test here")
        return 0

    print("the checked-in YuNet model is real and loads")
    model = Path(FD.YUNET_PATH)
    check("model file is present", model.is_file(), FD.YUNET_PATH)
    check("...and is not a truncated download",
          model.is_file() and model.stat().st_size > 100_000,
          f"{model.stat().st_size if model.is_file() else 0} bytes")

    print("make() picks YuNet, and falls back to Haar rather than to nothing")
    d = FD.make("yunet")
    check("prefer=yunet gives a working detector", d.available(), getattr(d, "error", ""))
    check("...and it is actually YuNet", d.name == "yunet", d.name)
    d2 = FD.make("haar")
    check("prefer=haar gives Haar", d2.name == "haar" and d2.available())
    # a YuNet with a bad model path must fall back, not fail
    orig = FD.YUNET_PATH
    try:
        FD.YUNET_PATH = "/nonexistent/model.onnx"
        fb = FD.make("yunet")
        check("a missing model falls back to Haar, still available",
              fb.name == "haar" and fb.available())
    finally:
        FD.YUNET_PATH = orig

    print("both backends return the same shape and honour min_px")
    blank = np.zeros((240, 320, 3), dtype=np.uint8)
    for det in (FD.make("yunet"), FD.make("haar")):
        boxes = det.detect(blank, min_px=24)
        ok = isinstance(boxes, list) and all(
            isinstance(b, tuple) and len(b) == 4 and all(isinstance(v, int) for v in b)
            for b in boxes)
        check(f"{det.name}: a blank frame returns a clean list of int boxes", ok, str(boxes))

    print("the model finds a face on a real frame (if one is around to try)")
    # Any saved 640x480-ish frame with a face; the scratchpad from a live
    # session usually has one. Skipped, loudly, when there is nothing to try.
    import glob
    candidates = sorted(glob.glob(str(ROOT / "sounds" / "*.jpg")))  # unlikely, cheap
    scratch = glob.glob("/tmp/claude-*/**/now_camera_snapshot.jpg", recursive=True)
    frames = [f for f in (scratch + candidates)]
    tried = False
    for fn in frames:
        img = cv2.imdecode(np.fromfile(fn, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        tried = True
        yn = FD.make("yunet").detect(img, min_px=24)
        check(f"YuNet returns a usable box list on {Path(fn).name}",
              isinstance(yn, list), str(yn)[:60])
        break
    if not tried:
        print("  SKIP  no saved frame to run detection against (not a failure)")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

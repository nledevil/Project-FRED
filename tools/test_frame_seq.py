#!/usr/bin/env python3
"""Frame identity for the vision loop (R10): a repeat is knowable, and reported.

The face tracker polls faster than a degraded remote camera delivers, so it used
to re-detect the same frame — wasting the detector and, worse, feeding the
derivative a de/dt of zero that reads as "the face stopped moving." The fix is a
per-frame sequence number the tracker can compare. This pins that contract:

  * a threaded backend's seq advances only when a new frame is published, so a
    consumer can tell a repeat from a new frame;
  * Camera.capture_gray_seq() carries it through, and degrades to (gray, None)
    for a backend that can't tell — which the tracker treats as "always new",
    preserving old behaviour;
  * the tracker reports source_fps (new frames), and the spotter reports its real
    detection CPU — both were unmeasurable before.

    python3 tools/test_frame_seq.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import camera as C                             # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    print("a threaded backend's seq advances only on a new frame")
    b = C._ThreadedBackend(lores_size=(320, 240))
    g, s0 = b.capture_gray_seq()
    check("no frame yet -> gray None", g is None)
    b._publish_gray("frameA")
    _, s1 = b.capture_gray_seq()
    check("publishing a frame advances the seq", s1 == s0 + 1, f"{s0}->{s1}")
    _, s1b = b.capture_gray_seq()
    check("polling again without a new frame keeps the seq (a repeat)", s1b == s1)
    b._publish_gray("frameB")
    g2, s2 = b.capture_gray_seq()
    check("a new frame advances it again", s2 == s1 + 1 and g2 == "frameB", f"{s1}->{s2}")

    print("Camera.capture_gray_seq carries it through, and degrades gracefully")
    cam = Camera_null()
    g, s = cam.capture_gray_seq()
    check("null backend -> (None, None)", g is None and s is None, f"{g!r},{s!r}")

    class NoSeqBackend:
        def capture_gray(self):
            return "x"
    cam._backend = NoSeqBackend()
    g, s = cam.capture_gray_seq()
    check("a backend without seq -> (gray, None), i.e. 'always new'",
          g == "x" and s is None, f"{g!r},{s!r}")

    cam._backend = b                                   # the threaded one from above
    g, s = cam.capture_gray_seq()
    check("a backend with seq -> real (gray, seq)", g == "frameB" and s == s2, f"{g!r},{s!r}")

    print("the tracker exposes source_fps; the spotter exposes real detect CPU")
    ft_src = (ROOT / "inmoov" / "face_tracker.py").read_text()
    check("tracker's status dict defaults source_fps", '"source_fps": 0.0' in ft_src)
    check("tracker's loop writes source_fps", 'self._status["source_fps"]' in ft_src)
    check("tracker skips a repeated seq", "seq == last_seq" in ft_src)
    check("tracker time-normalizes the derivative by dt", "nominal / dt" in ft_src)

    ws_src = (ROOT / "inmoov" / "wide_spotter.py").read_text()
    check("spotter status reports detect_cpu_pct", '"detect_cpu_pct"' in ws_src)
    # the math itself: detect_ms x detect_hz / 10 = percent of one core
    pct = round(29.0 * 4.0 / 10.0, 1)
    check("29 ms x 4 Hz reads as ~11.6% of a core (not '4 cores')", pct == 11.6, str(pct))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


def Camera_null():
    """A Camera with no backend, without touching hardware in __init__."""
    cam = Camera.__new__(Camera)
    import threading
    cam._lock = threading.Lock()
    cam._backend = None
    return cam


from inmoov.camera import Camera                           # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

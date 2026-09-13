#!/usr/bin/env python3
"""Two vision costs that were paid for nobody (R10, deferred pair).

**The relay decoded every frame for a tracker that wasn't there.** The head
Pi's MJPEG stream is relayed to panel viewers untouched — that was the point —
but the relay also JPEG-decoded and shrank every frame to a grayscale that only
a *hold* (face tracking, the vision tool) ever reads. With a panel open and
tracking off, that was fifteen decodes a second of a 1280-wide frame for
nothing. The backend now carries ``gray_wanted``, which the Camera sets from
its hold count; the pump skips the decode while it is False, and the stale
frame is dropped when the last hold goes so the next hold's first read is
honest.

**A look after a head move re-sent the old view.** Inside one turn the vision
tool re-sends a frame taken seconds ago rather than grabbing another — right
when he has not moved, a fabrication when he has: "turn left and tell me what
you see" described the view from before the turn. Brain._after_tool forgets
the eye camera's frame after any tool that moves the head; the wide camera on
his chest survives everything but the cart.

No hardware: the pump is fed a canned stream; the brain is built with the
fallback test's fakes.

    python3 tools/test_camera_gray_gate.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import io
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import camera as cam                          # noqa: E402
from inmoov import brain as B                             # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class Broker:
    def __init__(self):
        self.frames = []

    def publish(self, jpeg):
        self.frames.append(jpeg)


def stream(n: int) -> io.BytesIO:
    """n tiny 'JPEGs' — SOI, a byte, EOI — the pump only needs the markers."""
    return io.BytesIO(b"".join(b"--b\r\n\r\n\xff\xd8\x00\xff\xd9\r\n" for _ in range(n)))


def pumped(wanted: bool):
    be = cam._MjpegBackend("http://head/stream", (160, 120))
    be._broker = Broker()
    be.gray_wanted = wanted
    decoded = []
    be._decode_gray = lambda jpeg: decoded.append(jpeg)
    be._pump(stream(4))
    return be, decoded


def main() -> int:
    print("the relay decodes gray only while a hold wants it")
    be, decoded = pumped(False)
    check("no hold: four frames relayed, none decoded",
          len(be._broker.frames) == 4 and decoded == [], f"{len(be._broker.frames)} / {len(decoded)}")
    be, decoded = pumped(True)
    check("a hold: every frame decoded once", len(decoded) == 4, str(len(decoded)))
    check("a fresh backend wants nothing until told",
          cam._MjpegBackend("http://x", (1, 1)).gray_wanted is False)

    print("the Camera sets it from its hold count, and drops the stale frame")
    c = cam.Camera.__new__(cam.Camera)
    be = cam._MjpegBackend("http://head/stream", (160, 120))
    be._gray = "old frame"
    c._backend = be
    c._holds = 0
    c._viewers = 0
    c._indicator = None
    c._lit = False
    c._lock = __import__("threading").Lock()
    c._can_start_locked = lambda: False
    c._running_locked = lambda: False
    c.acquire()
    check("acquire: the backend wants gray", be.gray_wanted is True)
    c.acquire()
    c.release()
    check("one of two holds released: still wanted", be.gray_wanted is True and c._holds == 1)
    c.release()
    check("last hold released: not wanted, and the stale frame is gone",
          be.gray_wanted is False and be._gray is None, f"{be.gray_wanted} {be._gray!r}")

    print("a head move forgets the eye camera's frame, and only that")
    ctx = types.SimpleNamespace(controller=None, led=None, tracker=None, sound=None,
                                sensors=None, event=None, camera=None, diagnostic=None)
    b = B.Brain(ctx, api_key=None)
    for tool in sorted(B.Brain._EYES_MOVED):
        b._last_frame = {"eyes": b"e", "wide": b"w"}
        b._last_look = {"eyes": 1.0, "wide": 1.0}
        b._after_tool(tool)
        wide_kept = ("wide" in b._last_frame) == (tool not in B.Brain._ALL_MOVED)
        check(f"{tool:11} forgets eyes" + ("" if tool in B.Brain._ALL_MOVED else ", keeps wide"),
              "eyes" not in b._last_frame and "eyes" not in b._last_look and wide_kept,
              str(b._last_frame))
    b._last_frame = {"eyes": b"e", "wide": b"w"}
    b._after_tool("read_sensors")
    check("a tool that moves nothing forgets nothing", b._last_frame == {"eyes": b"e", "wide": b"w"})
    b._after_tool("tell_joke")
    check("...a joke included", "eyes" in b._last_frame)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

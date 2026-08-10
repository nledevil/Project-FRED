#!/usr/bin/env python3
"""Bench test for the 360° surround stack — no Insta360, no cart, no Pi needed.

Runs the whole path on the mock scene: Camera360's synthetic equirect → the
gnomonic dewarp (with a geometry self-check) → SurroundVision motion sectors,
presence tracking and the face-tracker bearing → the cart governor gating a
mock drive command → the Claude look_around tool building real image blocks.

    ./venv/bin/python demo360.py             # a few seconds, prints PASS/FAIL
    ./venv/bin/python demo360.py --save out/ # also dump JPEGs to eyeball

This is the pre-hardware confidence test: when the X5 arrives, the only new
variables are the UVC device node and the mount's front_yaw — everything
downstream of a frame has already run.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from inmoov.camera360 import Camera360  # noqa: E402
from inmoov.surround import SurroundVision  # noqa: E402
from inmoov.cart import Cart  # noqa: E402
from inmoov import commands  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗ FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    save_dir = None
    if "--save" in sys.argv:
        i = sys.argv.index("--save")
        save_dir = Path(sys.argv[i + 1] if i + 1 < len(sys.argv) else "out")
        save_dir.mkdir(parents=True, exist_ok=True)

    print("— Camera360 (mock scene) —")
    cam = Camera360(source="mock", size=(1440, 720), fps=20.0, front_yaw=0.0)
    check("OpenCV available", cam.available())
    if not cam.available():
        print("Install python3-opencv / opencv-python to run this bench.")
        return 1

    frame = cam.snapshot()
    check("snapshot delivers an equirect frame", frame is not None
          and frame.shape[:2] == (720, 1440), f"shape {None if frame is None else frame.shape}")

    # Geometry self-check on a synthetic frame that *encodes its own yaw*: the
    # red channel ramps 0..255 across the equirect's width (i.e. across 360° of
    # yaw), green ramps down its height (pitch). The centre pixel of a view
    # aimed at (yaw, pitch) must therefore read back exactly that yaw/pitch —
    # a direct test of the gnomonic projection, independent of scene content.
    import numpy as np
    ramp = np.zeros((720, 1440, 3), dtype=np.uint8)
    ramp[:, :, 2] = (np.arange(1440) * 255 // 1439)[None, :]           # R = yaw
    ramp[:, :, 1] = (np.arange(720) * 255 // 719)[:, None]             # G = pitch
    for yaw, pitch in ((0, 0), (90, 0), (180, 0), (-90, 0), (45, 20), (-135, -30)):
        v = cam.view(yaw=yaw, pitch=pitch, fov=90, out_size=(400, 300), frame=ramp)
        got_r, got_g = int(v[150, 200, 2]), int(v[150, 200, 1])
        want_r = round((((yaw + cam.front_yaw) / 360.0 + 0.5) % 1.0) * 255)
        want_g = round((0.5 - pitch / 180.0) * 255)
        ok = abs(got_r - want_r) <= 3 and abs(got_g - want_g) <= 3
        check(f"view({yaw:+d}°,{pitch:+d}°) samples the right equirect spot", ok,
              f"R {got_r} (want {want_r}), G {got_g} (want {want_g})")

    cam.acquire()                       # keep frames flowing for the view calls
    try:
        if save_dir is not None:
            import cv2
            for yaw, label in ((0, "fwd"), (90, "right"), (180, "back"), (-90, "left")):
                v = cam.view(yaw=yaw, pitch=0, fov=90, out_size=(400, 300))
                if v is not None:
                    cv2.imwrite(str(save_dir / f"view_{label}.jpg"), v)

        jpg = cam.jpeg_view(yaw=0, fov=90)
        check("jpeg_view returns a JPEG", bool(jpg) and jpg[:2] == b"\xff\xd8")
        pano = cam.jpeg_pano()
        check("jpeg_pano returns a JPEG", bool(pano) and pano[:2] == b"\xff\xd8")
        if save_dir is not None and pano:
            (save_dir / "pano.jpg").write_bytes(pano)

        print("— SurroundVision (motion + presence on the mock's wandering person) —")
        sv = SurroundVision(cam, fps=8.0)
        check("surround available", sv.available())
        sv.start()
        # The mock person orbits at 20°/s; give the background model and the
        # tracker a few seconds to see genuine motion.
        deadline = time.monotonic() + 6.0
        seen_presence = None
        while time.monotonic() < deadline:
            st = sv.status()
            if st["presences"]:
                seen_presence = st["presences"][0]
                break
            time.sleep(0.25)
        check("a moving presence is tracked", seen_presence is not None,
              str(seen_presence))
        b = sv.bearing()
        check("bearing() yields a face-tracker hint", b is not None
              and -1.0 <= (b if b is not None else 0) <= 1.0, f"bearing {b}")
        summary = sv.spoken_summary()
        check("spoken_summary is speakable", "movement" in summary
              or "someone" in summary, summary)

        print("— Cart governor (mock serial) —")
        cart = Cart(port="/dev/nonexistent", guard_cb=sv.nav_assess, enabled=True)
        st = cart.drive(0, 100, ttl=1.5)
        time.sleep(0.4)                 # a few keepalive ticks
        st = cart.status()
        check("cart runs in mock mode", st["mock"] is True)
        g = st["guard"]
        # The mock person has no range estimate (motion-only), so whenever it's
        # in the forward ±45° arc the governor must cap speed at half.
        in_path = seen_presence is not None and abs(seen_presence["yaw"]) <= 45
        if in_path:
            check("governor caps speed for motion in path",
                  st["governed_speed"] <= 50, f"governed {st['governed_speed']}, {g}")
        else:
            check("governor leaves a clear path alone",
                  st["governed_speed"] == 100 or g["factor"] < 1.0,
                  f"governed {st['governed_speed']}, {g}")
        cart.drive(0, 100, ttl=0.2)
        time.sleep(0.6)
        check("command TTL decays to a stop", cart.status()["governed_speed"] == 0
              and cart.status()["speed"] == 0)
        cart.shutdown()

        print("— Claude look_around tool —")
        import types
        ctx = types.SimpleNamespace(camera360=cam, surround=sv, cart=cart,
                                    sensors=None)
        blocks = commands.look_around_blocks(ctx, "all")
        ok = (isinstance(blocks, list)
              and sum(1 for x in blocks if x.get("type") == "image") == 4
              and all(x["source"]["media_type"] == "image/jpeg"
                      for x in blocks if x.get("type") == "image"))
        check("look_around('all') builds 4 labelled image blocks", ok,
              f"{type(blocks).__name__} of {len(blocks) if isinstance(blocks, list) else '-'}")
        spoken = commands.execute_action(ctx, "scan_surroundings")
        check("scan_surroundings speaks", isinstance(spoken, str) and spoken, spoken)

        sv.stop()
    finally:
        cam.release()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("All checks passed — the 360 stack is ready for the X5 to arrive.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Exercise the chest cart page's stop control with no screen and no cart.

test_cart_driver.py checks that the driver stops; test_cart_api.py checks that
the HTTP route stops. This checks the last link: that the *button* a person
actually presses, standing next to the base, sends what it says it sends.

Worth testing rather than eyeballing, because the interesting behaviour is a
small state machine with a clock in it, and both of its halves are safety
properties in opposite directions:

* stop must fire on the first tap, in every state, including when the page has
  no cart data at all — a stop that waits for a confirmation is not a stop;
* clear must NOT fire on the first tap, because releasing a latched e-stop
  re-arms 350 lb of steel and this is a screen you carry around.

Rendering is checked too, but only for the things that would make the control
unusable rather than ugly: that the stop button cannot be hit by a thumb aiming
at a mode button, and that its labels fit inside it.

    python3 tools/test_cart_page.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                                     # noqa: E402

import page_cart                                       # noqa: E402
from font5x7 import text_width                         # noqa: E402

FAILURES: list[str] = []

W, H = 800, 480


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeClock:
    """Stands in for page_cart's ``time``, so the confirm window can lapse
    without the test sleeping through it."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


class FakeNet:
    """Records what the page would have posted."""

    def __init__(self):
        self.calls: list[tuple] = []

    def post_cart_controller(self, mode: str) -> None:
        self.calls.append(("mode", mode))

    def post_cart_stop(self) -> None:
        self.calls.append(("stop",))

    def post_cart_clear_estop(self) -> None:
        self.calls.append(("clear",))


def snap(**cart) -> dict:
    return {"chest": {"cart": cart}}


def centre(rect) -> tuple[int, int]:
    x0, y0, x1, y1 = rect
    return ((x0 + x1) // 2, (y0 + y1) // 2)


def main() -> int:
    clock = FakeClock()
    page_cart.time = clock                      # before any page is built

    frame = np.zeros((H, W, 3), dtype=np.float32)
    page = page_cart.CartPage()
    net = FakeNet()
    stop_xy = centre(page._stop.rect)

    print("geometry")
    # --- the stop button must not overlap anything else -------------------
    sx0, sy0, sx1, sy1 = page._stop.rect
    overlaps = [m for m, b in page._buttons
                if not (b.rect[2] <= sx0 or b.rect[0] >= sx1
                        or b.rect[3] <= sy0 or b.rect[1] >= sy1)]
    check("stop button overlaps no mode button", not overlaps, ",".join(overlaps))
    check("stop button is on screen", 0 <= sx0 < sx1 <= W and 0 <= sy0 < sy1 <= H,
          f"{page._stop.rect}")
    check("stop button is a thumb-sized target",
          (sx1 - sx0) >= 200 and (sy1 - sy0) >= 150, f"{sx1 - sx0}x{sy1 - sy0} px")
    for label, scale in (("STOP", 8), ("CLEAR", 8), ("E-STOP LATCHED", 2),
                         ("TAP AGAIN", 2), ("LATCHES", 2)):
        check(f"label {label!r} fits the button",
              text_width(label, scale) <= (sx1 - sx0),
              f"{text_width(label, scale)} <= {sx1 - sx0} px")
    for _mode, b in page._buttons:
        check("mode button stays clear of the stop column", b.rect[2] <= sx0,
              f"mode right edge {b.rect[2]} <= stop left edge {sx0}")
    # The telemetry strip went in under the modes; it must not have landed on
    # top of them, or on the stop button.
    tel = (page_cart.BTN_X0, page_cart.TEL_Y0, page_cart.BTN_X1, page_cart.TEL_Y1)
    check("telemetry sits below every mode button",
          all(b.rect[3] <= tel[1] for _m, b in page._buttons),
          f"lowest mode edge {max(b.rect[3] for _m, b in page._buttons)} <= {tel[1]}")
    check("telemetry stays clear of the stop column", tel[2] <= sx0,
          f"{tel[2]} <= {sx0}")
    check("telemetry is on screen", 0 <= tel[1] < tel[3] <= H, str(tel))

    print("stop fires on the first tap")
    # --- unreachable cart: still stops ------------------------------------
    page.draw(frame, {})                        # no chest data at all
    page.on_touch("down", *stop_xy, net)
    check("stop posts with no cart data on screen", net.calls == [("stop",)],
          f"{net.calls}")

    net.calls.clear()
    page.draw(frame, snap(controller_mode="off", estop=False, connected=True))
    page.on_touch("down", *stop_xy, net)
    check("stop posts an estop, not a plain stop", net.calls == [("stop",)],
          f"{net.calls}")

    net.calls.clear()
    page.on_touch("up", *stop_xy, net)
    check("release does not post anything", net.calls == [], f"{net.calls}")

    print("clearing asks twice")
    latched = snap(controller_mode="off", estop=True, connected=True)
    net.calls.clear()
    page.draw(frame, latched)
    page.on_touch("down", *stop_xy, net)
    check("first tap on a latched estop does not clear it", net.calls == [],
          f"{net.calls}")
    check("...it arms the confirmation instead", page._is_armed())

    page.draw(frame, latched)
    page.on_touch("down", *stop_xy, net)
    check("second tap clears the estop", net.calls == [("clear",)], f"{net.calls}")
    check("clearing disarms", not page._is_armed())

    # --- the arming lapses -------------------------------------------------
    net.calls.clear()
    page.draw(frame, latched)
    page.on_touch("down", *stop_xy, net)        # arm
    clock.advance(page_cart.CONFIRM_S + 0.1)
    check("the confirmation lapses on its own", not page._is_armed(),
          f"after {page_cart.CONFIRM_S + 0.1}s")
    page.draw(frame, latched)
    page.on_touch("down", *stop_xy, net)        # must re-arm, not clear
    check("a lapsed confirmation does not clear the estop", net.calls == [],
          f"{net.calls}")
    check("...it arms again", page._is_armed())

    # --- an estop cleared elsewhere disarms us -----------------------------
    page.draw(frame, snap(controller_mode="off", estop=False, connected=True))
    check("an estop cleared elsewhere drops our arming", not page._is_armed())

    print("the stop button is not a mode button")
    net.calls.clear()
    page.draw(frame, snap(controller_mode="off", estop=False, connected=True))
    page.on_touch("down", *stop_xy, net)
    check("tapping stop never changes the controller mode",
          all(c[0] != "mode" for c in net.calls), f"{net.calls}")
    check("tapping stop leaves no pending mode", page._pending is None,
          f"pending={page._pending}")

    net.calls.clear()
    page.draw(frame, latched)
    page.on_touch("down", *stop_xy, net)        # arm the clear
    page.on_touch("down", *centre(page._buttons[1][1].rect), net)   # then a mode
    check("a mode tap is not a confirmation", net.calls == [("mode", "takeover")],
          f"{net.calls}")
    check("...and it disarms the clear", not page._is_armed())

    print("drawing")
    # --- every state renders, in bounds, without an exception --------------
    states = {
        "no data": {},
        "idle": snap(controller_mode="off", estop=False, connected=True),
        "latched": latched,
        "no pico": snap(controller_mode="takeover", estop=False, connected=False),
        "host locked": snap(controller_mode="takeover", estop=False,
                            connected=True, host_locked=True,
                            controller={"connected": True, "deadman": False}),
        "driving": snap(controller_mode="takeover", estop=False, connected=True,
                        controller={"connected": True, "deadman": True,
                                    "speed": -0.5, "steer": 0.25}),
        # Telemetry: present, absent, stale, and low enough to colour.
        "live telemetry": snap(controller_mode="off", estop=False, connected=True,
                               battery_v=38.4, board_temp_c=27.0,
                               mainboard_seen=True, telemetry_age=0.4),
        "flat battery": snap(controller_mode="off", estop=False, connected=True,
                             battery_v=31.2, board_temp_c=41.0,
                             mainboard_seen=True, telemetry_age=0.6),
        "stale telemetry": snap(controller_mode="off", estop=False, connected=True,
                                battery_v=38.4, board_temp_c=27.0,
                                mainboard_seen=True, telemetry_age=42.0),
        "no mainboard": snap(controller_mode="off", estop=False, connected=True,
                             battery_v=None, board_temp_c=None,
                             mainboard_seen=False),
    }
    for name, s in states.items():
        frame[:] = 0.0
        try:
            page.draw(frame, s)
            drew = True
        except Exception as exc:                        # noqa: BLE001 — report it
            drew, name = False, f"{name}: {exc!r}"
        check(f"draws the {name} state", drew)
        check(f"...{name} leaves the frame finite", bool(np.isfinite(frame).all()))
        check(f"...{name} paints the stop button",
              float(frame[sy0:sy1, sx0:sx1].max()) > 0.0)

    # The armed face must actually look different, or the confirm is invisible.
    # Sampled 20px in, not 5: the themes gave buttons a corner radius of 14, so
    # 5px from the corner is outside the shape and reads background in every
    # state. This check compared black to black and failed for months while the
    # button itself was fine — it is amber armed and red latched.
    inset = 20
    page.draw(frame, latched)
    page.on_touch("down", *stop_xy, net)
    frame[:] = 0.0
    page.draw(frame, latched)
    armed_face = frame[sy0 + inset, sx0 + inset].copy()
    clock.advance(page_cart.CONFIRM_S + 0.1)
    frame[:] = 0.0
    page.draw(frame, latched)
    idle_face = frame[sy0 + inset, sx0 + inset].copy()
    check("the armed stop button looks different from the latched one",
          not np.array_equal(armed_face, idle_face),
          f"{tuple(armed_face)} vs {tuple(idle_face)}")

    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

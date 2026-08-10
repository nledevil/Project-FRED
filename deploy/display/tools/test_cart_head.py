#!/usr/bin/env python3
"""Head-side cart tests: the client, and the actions Claude and the matcher use.

Runs the real chest-side stack (cart_driver + display_control's routes) against
a simulated Pico, points a real CartClient at it over HTTP, and drives it the
way the robot actually will.

The property under test is the one that makes a self-propelled robot safe to
give to a language model: **no single call can make the cart move for long.**
nudge() has to keep talking for its whole duration, is capped, and stops at the
end — and if the caller vanishes mid-move, the chest Pi ends it anyway.

Needs the venv (requests):

    ./venv/bin/python deploy/display/tools/test_cart_head.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import cart_driver                                     # noqa: E402
import display_control                                 # noqa: E402
from test_cart_driver import FakePico, check, wait_until, FAILURES  # noqa: E402

from inmoov import cart as cart_mod                    # noqa: E402
from inmoov import commands                            # noqa: E402

PORT = 8098


def main() -> int:
    from http.server import ThreadingHTTPServer

    master, slave = os.openpty()
    pico = FakePico(master)
    pico.start()

    drv = cart_driver.CartDriver(port=os.ttyname(slave), watchdog=0.5,
                                 log=lambda m: None)
    drv.start()
    display_control.Handler.cart = drv
    display_control.Handler.relay = None
    display_control.Handler.metrics = None
    display_control.Handler.token = ""
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), display_control.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    client = cart_mod.CartClient(host="127.0.0.1", port=PORT)
    ctx = types.SimpleNamespace(cart=client,
                                cart_cfg={"speed": 150, "turn": 150,
                                          "step_seconds": 1.0})

    print("head-side cart client and actions\n")
    try:
        wait_until(lambda: drv.state()["connected"])
        pico.telemetry()
        check("client reads state over HTTP", client.state().get("connected"))

        # --- nudge keeps the cart alive, then ends it ---
        t0 = time.monotonic()
        client.nudge(0, 150, 1.0)
        moved = wait_until(lambda: pico.speed == 150, timeout=1.0)
        check("nudge starts the cart", moved, f"pico speed={pico.speed}")
        # Still moving well past the 0.5s watchdog: proof it is being re-sent.
        time.sleep(0.7)
        check("nudge outlives the watchdog by repeating", pico.speed == 150,
              f"still {pico.speed} at t+{time.monotonic()-t0:.1f}s")
        stopped = wait_until(lambda: pico.speed == 0, timeout=2.0)
        check("nudge stops itself at the end of its duration", stopped,
              f"stopped at t+{time.monotonic()-t0:.1f}s")

        # --- the duration is capped ---
        r = client.nudge(0, 150, 999)
        check("nudge duration is capped", r["seconds"] == cart_mod.MAX_NUDGE_S,
              f"asked 999s, got {r['seconds']}s")
        client.stop()
        wait_until(lambda: pico.speed == 0)

        # --- a caller that vanishes mid-move still ends with a stopped cart ---
        client.nudge(0, 200, 5.0)
        wait_until(lambda: pico.speed == 200)
        client._cancel.set()                  # simulate the head dying mid-nudge
        t0 = time.monotonic()
        stopped = wait_until(lambda: pico.speed == 0, timeout=2.0)
        check("an abandoned move still ends in a stop", stopped,
              f"{time.monotonic()-t0:.2f}s after the caller went away")

        # --- the spoken actions ---
        reply = commands.execute_action(ctx, "drive", direction="forward")
        check("drive action moves and speaks", reply == "Moving forward."
              and wait_until(lambda: pico.speed == 150), reply)
        commands.execute_action(ctx, "cart_stop")
        wait_until(lambda: pico.speed == 0)

        reply = commands.execute_action(ctx, "drive", direction="back")
        check("backwards is a negative speed",
              wait_until(lambda: pico.speed == -150), f"{reply} pico={pico.speed}")
        commands.execute_action(ctx, "cart_stop")

        reply = commands.execute_action(ctx, "drive", direction="around")
        check("turn around steers without driving forward",
              wait_until(lambda: pico.steer == 150 and pico.speed == 0),
              f"{reply} steer={pico.steer} speed={pico.speed}")
        commands.execute_action(ctx, "cart_stop")
        wait_until(lambda: pico.steer == 0)

        reply = commands.execute_action(ctx, "drive", direction="sideways")
        check("an unknown direction is refused, not guessed",
              "forward" in reply and pico.speed == 0, reply)

        # --- the matcher's route into the same actions ---
        got = commands.match_local("come here")
        reply = commands.execute_action(ctx, got[0], **got[1])
        check("'come here' drives forward through the matcher",
              reply == "Moving forward.", f"{got} -> {reply}")
        commands.execute_action(ctx, "cart_stop")

        # --- PS2 priority reaches the spoken reply ---
        pico.set_ps2(True)
        pico.telemetry()
        wait_until(lambda: drv.state()["ps2_active"])
        reply = commands.execute_action(ctx, "drive", direction="forward")
        check("PS2 priority is explained, not silently ignored",
              "controller" in reply.lower(), reply)
        pico.set_ps2(False)
        pico.telemetry()

        # --- no cart at all ---
        bare = types.SimpleNamespace()
        check("no drive base is stated plainly",
              "don't have a drive base" in commands.execute_action(
                  bare, "drive", direction="forward"),
              commands.execute_action(bare, "drive", direction="forward"))
    finally:
        try:
            client.stop()
        except Exception:                       # noqa: BLE001
            pass
        srv.shutdown()
        drv.shutdown()
        pico.shutdown()
        time.sleep(0.2)
        os.close(slave)
        os.close(master)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("OK: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""End-to-end test of the cart's HTTP surface against a simulated Pico.

test_cart_driver.py checks the driver in isolation; this checks the path the
head actually uses — display_control's routing, the JSON shapes, and the two
behaviours that matter when something has gone wrong:

* ``/api/cart/stop`` succeeds even with no cart driver running, because a caller
  who gets an error back from "stop" has nothing useful left to try.
* the watchdog still fires when commands arrive over HTTP rather than in-process,
  which is the real deployment.

    python3 tools/test_cart_api.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import cart_driver                                     # noqa: E402
import display_control                                 # noqa: E402
from test_cart_driver import FakePico, check, wait_until, FAILURES  # noqa: E402

PORT = 8099                                            # test-only, not 8081


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


def get(path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=3) as r:
        return json.loads(r.read())


def main() -> int:
    from http.server import ThreadingHTTPServer

    master, slave = os.openpty()
    pico = FakePico(master)
    pico.start()

    drv = cart_driver.CartDriver(port=os.ttyname(slave), watchdog=0.5,
                                 log=lambda m: None)
    drv.start()

    # A supervisor would spawn animations we do not want here; the cart routes
    # never touch it, so leave it unset and only wire what we are testing.
    display_control.Handler.cart = drv
    display_control.Handler.relay = None
    display_control.Handler.metrics = None
    display_control.Handler.token = ""

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), display_control.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    print("cart HTTP API against a simulated Pico\n")
    try:
        wait_until(lambda: drv.state()["connected"])
        pico.telemetry()

        s = get("/api/cart")
        check("GET /api/cart reports the link", s.get("connected"),
              f"port={s.get('port')}")

        r = post("/api/cart/drive", {"steer": 0, "speed": 150})
        check("POST /api/cart/drive moves the cart",
              r.get("ok") and wait_until(lambda: pico.speed == 150),
              f"pico speed={pico.speed}")

        r = post("/api/cart/drive", {"steer": 5000, "speed": 5000})
        check("drive clamps over-range requests",
              r.get("clamped") and r["speed"] == cart_driver.SPEED_LIMIT,
              f"steer={r['steer']} speed={r['speed']}")

        # The watchdog, over the real transport.
        t0 = time.monotonic()
        stopped = wait_until(lambda: pico.speed == 0, timeout=2.0)
        check("watchdog stops a cart commanded over HTTP", stopped,
              f"{time.monotonic() - t0:.2f}s after the last POST")

        r = post("/api/cart/stop", {"estop": True})
        check("POST /api/cart/stop latches an estop", r.get("estop"), json.dumps(r))
        r = post("/api/cart/drive", {"steer": 0, "speed": 100})
        check("drive is refused while the estop is latched", "error" in r,
              r.get("error", ""))
        r = post("/api/cart/stop", {"clear_estop": True})
        check("estop can be cleared", not r.get("estop"), json.dumps(r))

        # Stop must work even with no driver at all.
        display_control.Handler.cart = None
        r = post("/api/cart/stop", {})
        check("stop still succeeds with the cart driver disabled",
              r.get("ok") and r.get("stopped"), json.dumps(r))
        r = post("/api/cart/drive", {"speed": 100})
        check("drive reports plainly when the driver is disabled", "error" in r,
              r.get("error", ""))
        s = get("/api/cart")
        check("GET /api/cart reports disabled rather than failing",
              s.get("enabled") is False, json.dumps(s))
    finally:
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

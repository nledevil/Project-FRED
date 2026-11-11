#!/usr/bin/env python3
"""Watch the cart's Pico live, over HTTP, while you poke at the hardware.

For the bring-up question the other cart tools can't answer, because they all
run against a *simulated* Pico: what is the real one doing right now, and did
that thing I just plugged in change anything?

Runs from any machine on the robot LAN and touches nothing — it polls
``GET /api/cart`` on the chest and prints transitions. The driver keeps its
single handle on the serial port, so nothing here can interfere with the
watchdog or move the cart.

    python3 tools/watch_cart.py                 # defaults to the chest
    python3 tools/watch_cart.py --host 10.0.0.11 --seconds 120

**Reading a PS2 controller off this, with the hoverboard powered off.** The
firmware answers every drive command with one of two lines, and the driver is
sending ``0 0`` at 10 Hz the whole time:

    cmd -> steer: 0  speed: 0                            no controller seen
    PS2 controller active - USB drive commands ignored   controller has priority

So ``last_line`` reports the controller even when the hoverboard mainboard is
dark and no ``fb`` telemetry is flowing — which matters, because ``src: PS2`` in
the telemetry is the *other* way to see it and that channel needs the mainboard
awake. Watch for either that line or ``PS2 controller connected (analog mode)``.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

WATCH = ("connected", "port", "ps2_active", "source", "mainboard_seen",
         "estop", "moving", "battery_v", "board_temp_c", "last_line",
         "last_error", "watchdog_stops")


def get(url: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def interesting(state: dict) -> dict:
    return {k: state.get(k) for k in WATCH}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="10.0.0.11")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--hz", type=float, default=8.0)
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}/api/cart"
    print(f"watching {url} for {args.seconds:.0f}s — poke the hardware now\n")
    print(f"{'t':>6}  change")
    print("-" * 72)

    prev: dict | None = None
    t0 = time.monotonic()
    period = 1.0 / max(0.5, args.hz)
    misses = 0
    while time.monotonic() - t0 < args.seconds:
        state = get(url)
        now = round(time.monotonic() - t0, 1)
        if state is None:
            misses += 1
            if misses in (1, 20):        # say it once, then once more if it persists
                print(f"{now:6}  ! no answer from the chest")
            time.sleep(period)
            continue
        misses = 0
        cur = interesting(state)
        if prev is None:
            for k, v in cur.items():
                if v not in (None, "", False):
                    print(f"{now:6}  {k} = {v}")
            print(f"{now:6}  --- watching for changes ---")
        else:
            for k, v in cur.items():
                if v != prev[k]:
                    flag = "  <<<" if k in ("ps2_active", "source") else ""
                    print(f"{now:6}  {k}: {prev[k]!r} -> {v!r}{flag}")
        prev = cur
        time.sleep(period)

    print("-" * 72)
    if prev:
        ps2 = prev.get("ps2_active")
        line = str(prev.get("last_line") or "")
        if ps2 or "PS2" in line:
            print("PS2: the firmware sees a controller.")
        elif line.startswith("cmd ->"):
            print("PS2: no controller — the firmware is still answering host commands,\n"
                  "     which it would not do with one connected.")
        else:
            print(f"PS2: inconclusive; last line was {line!r}")
        if not prev.get("mainboard_seen"):
            print("Mainboard: silent. Telemetry (battery, temp, src:) needs it powered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Check the panel PIN: the digest, the rate limiting, and which routes it gates.

The last of those is the part most worth testing. A PIN is only as good as the
list of things behind it, and that list is 25 decorators spread over a thousand
lines of app.py — exactly the kind of thing that silently loses an entry. So
this asserts the whole split explicitly, in both directions: everything that
moves him is gated, and the things deliberately left open (status, the camera,
speech, and above all the cart's STOP) still answer without a PIN.

    python3 tools/test_auth.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import auth                                  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


# Every route that must require the PIN, and every route that must not. Written
# out rather than derived, so that adding an endpoint and forgetting to decide
# about it shows up here as an unlisted route instead of a silent default.
GATED = [
    "/api/settings", "/api/handoff", "/api/brain", "/api/audit",
    "/api/cart/drive", "/api/cart/controller", "/api/hotspot",
    "/api/display", "/api/display/metrics", "/api/led", "/api/track",
    "/api/voice", "/api/command", "/api/log/clear", "/api/move", "/api/rest",
    "/api/relax", "/api/record", "/api/channel", "/api/identify", "/api/save",
    "/api/camera", "/api/sounds/terminator/upload", "/api/sounds/terminator/delete",
    "/api/auth/pin", "/api/auth/pin/clear",
    # The AP password is the outermost credential on the robot now that
    # the access point comes up at boot with a route to the internet.
    "/api/hotspot/config",
]
OPEN = [
    "/api/cart/stop",       # never, ever gated
    "/api/say", "/api/sound/play", "/api/sound/stop",
    "/api/state", "/api/health", "/api/positions", "/api/mouth", "/api/log",
    "/api/sensors/ingest", "/api/auth/login", "/api/auth/logout",
    # Read-only views of live state, and the sound board.
    "/api/cart", "/api/sensors", "/api/sounds", "/api/display/animations",
    "/api/sounds/terminator", "/api/sounds/terminator/play",
    # /api/auth has to answer before the page has a PIN to answer with, and
    # /api/auth/material guards itself by address rather than by session --
    # a session must never be able to fetch the digest.
    "/api/auth", "/api/auth/material",
    # Identity, not control: hostnames, addresses, uptimes and the git
    # revision, none of which is a secret and all of which is already
    # implied by /api/state. Kept open so the chest panel can show it
    # without a session it has no way to obtain.
    "/api/whoami",
    # Which port this browser should open the terminals on. The URLs are not
    # the secret -- the terminals authenticate for themselves, and both doors
    # are on ports a scan of this host would find anyway. Gating it would also
    # be backwards: the AP guest who cannot reach the tailnet door is exactly
    # the one who needs to be told about the other one.
    "/api/terminals",
]


def route_map() -> dict:
    """Which decorators sit on each route, read off app.py's source.

    Parsed rather than imported: importing app.py builds a servo controller, a
    camera and an assistant, which is a lot of hardware to stand up to find out
    whether a decorator is present.
    """
    src = (ROOT / "web" / "app.py").read_text()
    routes: dict[str, set] = {}
    pattern = re.compile(r'^@app\.(get|post)\("([^"]+)"\)\n((?:@\w+\n)*)', re.M)
    for verb, path, decorators in pattern.findall(src):
        routes.setdefault(path, set()).update(
            d.strip() for d in decorators.split("\n") if d.strip())
    return routes


def main() -> int:
    print("the digest")
    material = auth.make_material("1234")
    check("a stored PIN is a salt and a digest, never the PIN",
          "1234" not in str(material), str(material)[:60])
    check("the right PIN verifies", auth.check_pin({"auth": {"pin": material}}, "1234"))
    check("a wrong PIN does not", not auth.check_pin({"auth": {"pin": material}}, "1235"))
    check("two identical PINs get different salts",
          auth.make_material("1234")["salt"] != material["salt"])
    check("no PIN set means nothing is gated", not auth.is_set({}))
    check("a PIN set is reported as set", auth.is_set({"auth": {"pin": material}}))
    for bad in ("123", "12345", "abcd", "", None, "12 4", "١٢٣٤"):
        check(f"{bad!r} is not a valid PIN", not auth.normalise(bad))
    check("'0000' is a valid PIN", auth.normalise("0000") == "0000")

    print("who is asking")
    settings = {"auth": {"pin": material}}
    for addr in ("10.0.0.10", "10.0.0.11", "127.0.0.1", "::1"):
        check(f"{addr} is the robot's own LAN", auth.is_trusted(addr, settings))
    for addr in ("192.168.50.24", "192.168.68.15", "8.8.8.8", "", "not-an-ip"):
        check(f"{addr!r} is not trusted", not auth.is_trusted(addr, settings))

    print("rate limiting")
    addr = "192.168.50.99"
    check("a fresh address may try", auth.locked_for(addr) == 0.0)
    for i in range(auth.FREE_TRIES):
        auth.note_failure(addr)
    check(f"{auth.FREE_TRIES} wrong tries are still free", auth.locked_for(addr) == 0.0)
    wait = auth.note_failure(addr)
    check("the next one starts the wait", wait > 0, f"{wait:.0f}s")
    check("...and it is enforced", auth.locked_for(addr) > 0)
    longer = auth.note_failure(addr)
    check("the wait doubles", longer > wait, f"{wait:.0f}s -> {longer:.0f}s")
    capped = wait
    for _ in range(20):
        capped = auth.note_failure(addr)
    check("the wait is capped", capped == auth.LOCK_MAX_S, f"{capped:.0f}s")
    auth.note_success(addr)
    check("a correct PIN clears the record", auth.locked_for(addr) == 0.0)
    check("one address's failures do not lock another",
          auth.locked_for("192.168.50.100") == 0.0)

    print("sessions")
    token = auth.open_session()
    check("a new session is valid", auth.valid_session(token))
    check("a made-up token is not", not auth.valid_session("nonsense"))
    check("an empty token is not", not auth.valid_session(""))
    auth.close_session(token)
    check("a closed session is not", not auth.valid_session(token))
    a, b = auth.open_session(), auth.open_session()
    check("sessions are distinct", a != b)
    auth.close_all_sessions()
    check("changing the PIN signs everyone out",
          not auth.valid_session(a) and not auth.valid_session(b))

    print("what the PIN actually gates")
    routes = route_map()
    check("app.py's routes were parsed", len(routes) > 30, f"{len(routes)} routes")
    for path in GATED:
        decorators = routes.get(path)
        check(f"gated: {path}", decorators is not None and "@protected" in decorators,
              "" if decorators and "@protected" in decorators
              else ("MISSING @protected" if decorators is not None else "route not found"))
    for path in OPEN:
        decorators = routes.get(path)
        check(f"open:  {path}", decorators is not None and "@protected" not in decorators,
              "" if decorators is not None and "@protected" not in decorators
              else ("unexpectedly GATED" if decorators else "route not found"))

    # The one that matters most, said twice on purpose.
    check("STOP is reachable without a PIN, always",
          "@protected" not in routes.get("/api/cart/stop", set()))

    listed = set(GATED) | set(OPEN)
    unlisted = sorted(p for p in routes
                      if p.startswith("/api/") and p not in listed)
    check("every /api route has been decided about", not unlisted,
          "undecided: " + ", ".join(unlisted) if unlisted else "")

    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""LED device server — the head Pi's BCM16 status LED, over HTTP/JSON.

Same split as ``servo_server.py``: the brain runs on the NUC, but the LED is
soldered to a Raspberry Pi GPIO header and cannot move. x86 has no GPIO at all,
so ``inmoov/led.py`` degrades to a silent no-op there and the panel's LED button
answers 503. This exposes the real pin over the network; pair it with
``inmoov/remote_led.py`` on the NUC.

Deliberately thinner than the servo server. There is exactly one output line and
no limits, no calibration and no lock to enforce, so the whole protocol is "read
the state" and "set the state".

``camera_indicator`` is **not** implemented here on purpose. That flag decides
whether the *camera starting* should drive the LED, and the camera lives on the
NUC — so the NUC-side client owns the policy and this server only ever does what
it is told. Keeping the decision next to the thing that triggers it means the
admin toggle keeps working even while this Pi is unreachable.

Environment:
  LED_PORT   listen port (default 8083; 8081 is the camera stream, 8082 servos)
  LED_PIN    BCM pin number (default 16)
  LED_TOKEN  shared secret for X-Led-Token; empty = no auth
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http import server
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inmoov.led import Led  # noqa: E402

PORT = int(os.environ.get("LED_PORT", "8083"))
PIN = int(os.environ.get("LED_PIN", "16"))
TOKEN = os.environ.get("LED_TOKEN", "").strip()

_led: Led | None = None
_lock = threading.Lock()


class _Handler(server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # The LED is nowhere near as chatty as the servos, but the cost of leaving
    # Nagle on is the same ~40 ms per reply, and the panel toggles feel sticky
    # at that latency. See servo_server.py for the full explanation.
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if not TOKEN:
            return True
        if self.headers.get("X-Led-Token", "") == TOKEN:
            return True
        self._send(401, {"error": "bad or missing X-Led-Token"})
        return False

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _state(self) -> dict:
        return {"available": _led.available(), "on": _led.is_on, "pin": PIN}

    # ---- routes -----------------------------------------------------------
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        if self.path == "/api/health":
            return self._send(200, {"ok": True, **self._state()})
        if not self._authed():
            return None
        if self.path == "/api/led":
            with _lock:
                return self._send(200, self._state())
        return self._send(404, {"error": f"no such endpoint {self.path!r}"})

    def do_POST(self):  # noqa: N802
        if not self._authed():
            return None
        if self.path != "/api/led":
            return self._send(404, {"error": f"no such endpoint {self.path!r}"})
        body = self._body()
        if "on" not in body:
            return self._send(400, {"error": 'expected {"on": true|false}'})
        # 503 rather than a silent success: if the GPIO never initialised, the
        # caller must be able to tell "I turned it off" from "there is no LED".
        if not _led.available():
            return self._send(503, {"error": f"no LED on BCM{PIN}"})
        with _lock:
            _led.set(bool(body["on"]))
            return self._send(200, self._state())


class _Server(server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    global _led
    # camera_indicator=False: this process has no camera, and the NUC client
    # applies that policy itself before it ever calls us.
    _led = Led(pin=PIN, camera_indicator=False)
    state = "HARDWARE" if _led.available() else "NO GPIO (no-op)"
    print(f"[led_server] serving on :{PORT} — BCM{PIN}, {state}, "
          f"auth={'on' if TOKEN else 'off'}")
    _Server(("0.0.0.0", PORT), _Handler).serve_forever()


if __name__ == "__main__":
    main()

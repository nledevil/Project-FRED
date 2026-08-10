#!/usr/bin/env python3
"""Standalone servo device server for the InMoov head Pi.

Exposes the head's ``ServoController`` (PCA9685 over I2C) as a small HTTP/JSON
API so the NUC — which is the brain now — can drive the servos over ethernet.
The head Pi keeps the I2C bus because that is physically where it is; everything
that decides *what* to move lives on the other end of the wire.

Deliberately owns ONLY the servos — no camera, no audio — so it coexists with
``camera_stream.py`` serving the imx708 on the same Pi. Stdlib only (the same
choice ``camera_stream.py`` makes), so it needs nothing installed beyond what
the controller itself imports.

Endpoints:
  GET  /api/health              liveness + mock/suspended flags, no hardware touched
  GET  /api/servos              full state: config, last-commanded angles, locks
  POST /api/servos              batch move: {"angles": {"eye_x": 90, "eye_y": 100}}
  POST /api/servos/<name>       single move: {"angle": 90, "enforce_limits": true}
  POST /api/servos/<name>/channel   {"channel": 13}
  POST /api/servos/<name>/identify  {"sweep": 12, "cycles": 3}
  POST /api/rest                every servo to its rest angle
  POST /api/relax               {"name": "jaw"} or {} for all — cuts pulses
  POST /api/suspend             release the I2C bus (hand off to MyRobotLab)
  POST /api/resume              take it back
  POST /api/config              adopt a new servos.json: {"servos": {...}}

Every move returns the angle the controller *actually* applied after clamping,
so the caller's cached position stays truthful rather than drifting from the
hardware whenever a request is clamped or refused.

Config via env:
  SERVO_PORT          (8082)
  SERVO_TOKEN         shared secret; when set, required as X-Servo-Token
  SERVO_LOCKED        comma-separated servos that must NOT move (see SAFETY)
  SERVO_MOVE_TO_REST  "1" to sweep to rest on startup (default "0" — see SAFETY)

SAFETY
------
Two deliberate departures from ``ServoController``'s own defaults:

* **Startup never moves anything.** ``ServoController(move_to_rest=True)`` drives
  every servo to its rest angle the moment it is constructed. For a service that
  systemd may start at boot, on its own, with nobody watching, that is the wrong
  default — so this flips it. Set ``SERVO_MOVE_TO_REST=1`` to opt back in.
* **Locked servos are refused, at the far end.** ``SERVO_LOCKED`` names servos
  this server will not move for anyone; they answer 423 and are reported in
  ``/api/servos``. This exists so a mechanical constraint can be enforced on the
  machine holding the I2C bus, rather than trusted to every caller — if the
  eyes physically cannot travel (e.g. a cable fouling them), locking them here
  means no client, buggy or otherwise, can drive them.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http import server
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inmoov.servo_controller import ServoController, load_config, CONFIG_PATH  # noqa: E402

PORT = int(os.environ.get("SERVO_PORT", "8082"))
TOKEN = os.environ.get("SERVO_TOKEN", "").strip()
LOCKED = {s.strip() for s in os.environ.get("SERVO_LOCKED", "").split(",") if s.strip()}
MOVE_TO_REST = os.environ.get("SERVO_MOVE_TO_REST", "0") == "1"

_ctrl: ServoController | None = None
_lock = threading.Lock()      # serialise requests; the controller guards I2C itself,
                              # but batch moves should not interleave with a rest sweep

# Fields /api/config may change. `channel` is handled separately (it needs the
# controller's own remap to stop driving the old port), and anything not listed
# here — servo names, the i2c block — is ignored rather than half-applied.
_TUNABLE = ("min_angle", "max_angle", "rest_angle", "pulse_min_us",
            "pulse_max_us", "actuation_range", "invert", "description")


def _merged(name: str, src: dict, cur: dict) -> dict:
    """Overlay `src` onto the servo's current settings and sanity-check the
    result. Raises ValueError on anything that would be unsafe to drive.

    Validating the *merged* dict rather than the incoming one means a partial
    update (just a rest_angle, say) is still checked against the limits it will
    actually live under.
    """
    out = dict(cur)
    for k in _TUNABLE:
        if k in src:
            out[k] = src[k]

    for k in ("min_angle", "max_angle", "rest_angle", "pulse_min_us",
              "pulse_max_us", "actuation_range"):
        v = out.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{name}.{k} must be a number, got {v!r}")

    rng = float(out["actuation_range"])
    lo, hi, rest = float(out["min_angle"]), float(out["max_angle"]), float(out["rest_angle"])
    if not 0 < rng <= 360:
        raise ValueError(f"{name}.actuation_range {rng} outside 0..360")
    if lo > hi:
        raise ValueError(f"{name}: min_angle {lo} > max_angle {hi}")
    if not (0 <= lo <= rng and 0 <= hi <= rng):
        raise ValueError(f"{name}: limits {lo}..{hi} outside the servo's 0..{rng}")
    # rest == a limit is legitimate: the jaw rests exactly on min_angle so the
    # mouth defaults closed. Only rest *outside* the limits is rejected.
    if not lo <= rest <= hi:
        raise ValueError(f"{name}: rest_angle {rest} outside limits {lo}..{hi}")
    if float(out["pulse_min_us"]) >= float(out["pulse_max_us"]):
        raise ValueError(f"{name}: pulse_min_us must be below pulse_max_us")

    out["invert"] = bool(out.get("invert", False))
    out["description"] = str(out.get("description", ""))
    return out


def _persist() -> None:
    """Write the live config back to servos.json, atomically."""
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(_ctrl.config, f, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def _adopt_config(body: dict) -> dict:
    """Adopt a servo config pushed by the brain and persist it.

    This exists because this server is the authority: it reads servos.json once
    at startup and RemoteServoController pulls limits/rest from here on every
    refresh. Without this endpoint a calibration saved on the NUC never reaches
    the hardware, and Rest keeps using whatever this machine booted with.

    **Never moves a servo.** If new limits exclude where a servo currently sits,
    that is reported in ``outside_limits`` and left alone — the next commanded
    move clamps it back into range. Deciding to move belongs to a caller with
    eyes on the robot, not to a config push.
    """
    incoming = body.get("servos")
    if not isinstance(incoming, dict) or not incoming:
        raise ValueError('expected {"servos": {"<name>": {...}, ...}}')

    known = set(_ctrl.servos)
    unknown = sorted(set(incoming) - known)
    shared = [n for n in incoming if n in known]
    if not shared:
        raise ValueError(f"none of the pushed servos exist here; got {unknown}, "
                         f"known: {sorted(known)}")

    # Validate the whole batch first: a half-applied calibration is worse than a
    # rejected one, since the caller would have no idea which half landed.
    validated = {}
    for n in shared:
        src = incoming[n]
        if not isinstance(src, dict):
            raise ValueError(f"{n}: expected an object, got {type(src).__name__}")
        validated[n] = _merged(n, src, _ctrl.servos[n])

    changed, rechannelled, outside = [], [], {}
    for n, new in validated.items():
        s = _ctrl.servos[n]

        # Channel first, via the controller, so the old port stops being driven
        # and the new one gets this servo's pulse range.
        want_ch = incoming[n].get("channel", s["channel"])
        if isinstance(want_ch, (int, float)) and int(want_ch) != s["channel"]:
            _ctrl.set_channel(n, int(want_ch))
            rechannelled.append(n)

        if any(s.get(k) != new[k] for k in _TUNABLE):
            changed.append(n)
        s.update(new)                      # _ctrl.servos IS _ctrl.config["servos"]

        # Pulse width and actuation range live on the PCA9685 channel, not only
        # in the dict, so they have to be re-applied to take effect.
        if not _ctrl.mock and not _ctrl.is_suspended():
            with _ctrl._io_lock:
                port = _ctrl._kit.servo[s["channel"]]
                port.set_pulse_width_range(s["pulse_min_us"], s["pulse_max_us"])
                port.actuation_range = s.get("actuation_range", 180)

        at = _ctrl.get_angle(n)
        if at is not None and not s["min_angle"] <= at <= s["max_angle"]:
            outside[n] = at

    _persist()
    return {"updated": sorted(changed), "rechannelled": sorted(rechannelled),
            "unchanged": sorted(set(shared) - set(changed)), "ignored_unknown": unknown,
            "not_in_push": sorted(known - set(incoming)),
            "outside_limits": outside, "saved": str(CONFIG_PATH)}


class _Handler(server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"        # keep-alive: the tracker sends ~15 req/s

    # Without this every reply costs ~40 ms. The response goes out as several
    # small writes (status line, headers, body); Nagle holds the tail waiting for
    # an ACK that the client's delayed-ACK timer is in no hurry to send, and the
    # two sit there staring at each other. That caps the servo link at ~24 Hz —
    # below the tracker's frame rate — for no reason but buffering.
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):   # quieter than the default one-line-per-request
        pass

    # ---- plumbing ---------------------------------------------------------
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
        if self.headers.get("X-Servo-Token", "") == TOKEN:
            return True
        self._send(401, {"error": "bad or missing X-Servo-Token"})
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
        return {
            "mock": _ctrl.mock,
            "suspended": _ctrl.is_suspended(),
            "locked": sorted(LOCKED),
            "servos": {
                name: {
                    "channel": s["channel"],
                    "min_angle": s["min_angle"],
                    "max_angle": s["max_angle"],
                    "rest_angle": s["rest_angle"],
                    "actuation_range": s.get("actuation_range", 180),
                    "invert": bool(s.get("invert", False)),
                    "description": s.get("description", ""),
                    "current": _ctrl.get_angle(name),
                    "locked": name in LOCKED,
                }
                for name, s in _ctrl.servos.items()
            },
        }

    def _move(self, name: str, angle, enforce_limits: bool = True):
        """Apply one move. Returns (angle, error) — angle is what the hardware
        was actually told, which is not always what was asked for."""
        if name not in _ctrl.servos:
            return None, (404, f"unknown servo {name!r}")
        if name in LOCKED:
            return None, (423, f"{name} is locked (SERVO_LOCKED) and will not be moved")
        try:
            value = float(angle)
        except (TypeError, ValueError):
            return None, (400, f"angle must be a number, got {angle!r}")
        return _ctrl.set_angle(name, value, enforce_limits=enforce_limits), None

    # ---- routes -----------------------------------------------------------
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        if self.path == "/api/health":
            # Answered without taking _lock so it stays truthful even while a
            # slow move (identify, rest sweep) is in flight.
            return self._send(200, {"ok": True, "mock": _ctrl.mock,
                                    "suspended": _ctrl.is_suspended(),
                                    "locked": sorted(LOCKED)})
        if not self._authed():
            return None
        if self.path == "/api/servos":
            with _lock:
                return self._send(200, self._state())
        return self._send(404, {"error": f"no such endpoint {self.path!r}"})

    def do_POST(self):  # noqa: N802
        if not self._authed():
            return None
        path, body = self.path.rstrip("/"), self._body()
        try:
            with _lock:
                return self._route_post(path, body)
        except Exception as exc:  # noqa: BLE001 - one bad request must not kill the server
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _route_post(self, path: str, body: dict):
        if path == "/api/servos":
            angles = body.get("angles")
            if not isinstance(angles, dict) or not angles:
                return self._send(400, {"error": 'expected {"angles": {"name": deg, ...}}'})
            enforce = bool(body.get("enforce_limits", True))
            applied, errors = {}, {}
            for name, angle in angles.items():
                value, err = self._move(name, angle, enforce)
                if err:
                    errors[name] = err[1]
                else:
                    applied[name] = value
            # 207: a batch can partly succeed, and the caller needs to know which
            # half did. Reporting 200 here would hide a locked or unknown servo.
            return self._send(207 if errors else 200,
                              {"angles": applied, "errors": errors} if errors
                              else {"angles": applied})

        if path == "/api/rest":
            skipped = []
            for name, s in _ctrl.servos.items():
                if name in LOCKED:
                    skipped.append(name)
                    continue
                _ctrl.set_angle(name, s["rest_angle"])
            return self._send(200, {"angles": dict(_ctrl._current), "skipped_locked": skipped})

        if path == "/api/config":
            try:
                return self._send(200, _adopt_config(body))
            except ValueError as exc:
                # A rejected calibration must say why — this is the one endpoint
                # whose failure the operator will otherwise read as "saved fine".
                return self._send(400, {"error": str(exc)})

        if path == "/api/relax":
            name = body.get("name")
            if name is not None and name not in _ctrl.servos:
                return self._send(404, {"error": f"unknown servo {name!r}"})
            _ctrl.relax(name)
            return self._send(200, {"relaxed": name or "all"})

        if path == "/api/suspend":
            _ctrl.suspend()
            return self._send(200, {"suspended": True})

        if path == "/api/resume":
            # resume() ends with a rest sweep, which would move locked servos —
            # so refuse rather than quietly violate the lock.
            if LOCKED:
                return self._send(423, {"error": "cannot resume while servos are "
                                                 f"locked: {sorted(LOCKED)}"})
            _ctrl.resume()
            return self._send(200, {"suspended": False})

        parts = path.strip("/").split("/")        # api/servos/<name>[/<action>]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "servos":
            name = parts[2]
            action = parts[3] if len(parts) > 3 else None
            if name not in _ctrl.servos:
                return self._send(404, {"error": f"unknown servo {name!r}"})

            if action is None:
                value, err = self._move(name, body.get("angle"),
                                        bool(body.get("enforce_limits", True)))
                if err:
                    return self._send(err[0], {"error": err[1]})
                return self._send(200, {"name": name, "angle": value})

            if action == "channel":
                try:
                    ch = _ctrl.set_channel(name, int(body.get("channel")))
                except (TypeError, ValueError) as exc:
                    return self._send(400, {"error": str(exc)})
                return self._send(200, {"name": name, "channel": ch})

            if action == "identify":
                if name in LOCKED:
                    return self._send(423, {"error": f"{name} is locked; identify would move it"})
                _ctrl.identify(name, sweep=float(body.get("sweep", 12.0)),
                               cycles=int(body.get("cycles", 3)),
                               dwell=float(body.get("dwell", 0.13)))
                return self._send(200, {"name": name, "identified": True})

            return self._send(404, {"error": f"no such action {action!r}"})

        return self._send(404, {"error": f"no such endpoint {path!r}"})


class _Server(server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    global _ctrl
    _ctrl = ServoController(config=load_config(), move_to_rest=MOVE_TO_REST)
    mode = "MOCK" if _ctrl.mock else "HARDWARE"
    print(f"[servo_server] serving on :{PORT} — mode={mode}, "
          f"move_to_rest={MOVE_TO_REST}, auth={'on' if TOKEN else 'off'}")
    if LOCKED:
        print(f"[servo_server] LOCKED (will refuse to move): {sorted(LOCKED)}")
    _Server(("0.0.0.0", PORT), _Handler).serve_forever()


if __name__ == "__main__":
    main()

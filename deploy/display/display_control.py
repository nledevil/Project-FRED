#!/usr/bin/env python3
"""Animation supervisor + HTTP control API for the InMoov chest display.

Runs on the display Pi (the one wired to the 7" DSI panel), not the head. It
owns the framebuffer animation as a *child process*, so the head's web panel can
switch looks over the LAN — no editing ExecStart, no systemctl restart, no SSH.
Switching is just "kill the child, spawn the next one", so it lands in ~100ms.

    GET  /api/animations -> preset list; drives the admin dropdown
    GET  /api/state      -> selected preset, pid, uptime, last crash
    GET  /api/sensors    -> the sensor relay's health (see below)
    POST /api/animation  -> {"animation": "reactor-copper"}; switch now
    POST /api/metrics    -> {"enabled": true}; overlay the live sensor readout
                            on top of whatever animation is running
    POST /api/voice      -> FRED's live voice state + speech envelope, handed to
                            the animation through voice_state.py
    GET  /api/cart       -> drive base state: telemetry, PS2 priority, watchdog
    POST /api/cart/drive -> {"steer": 0, "speed": 150}; must be repeated or the
                            watchdog stops the cart (see cart_driver.py)
    POST /api/cart/stop  -> {"estop": false}; stop now, optionally latching
    POST /api/cart/controller -> {"mode": "off"|"takeover"|"only"}; who may drive

It also carries the **sensor relay**: the Pico in FRED's stomach plugs into this
Pi, and sensor_relay.py reads its USB-serial stream and forwards it to the brain
over the robot LAN. That lives here rather than in its own unit because this
is already the supervised, always-on process on this Pi — one thing to install,
one thing to restart. It is strictly best-effort and shares nothing with the
animation, so a missing Pico or an unreachable head can't disturb the screen.

And the **cart driver**: the hoverboard base's Pico is on this Pi too, for the
same physical reason. That one is not best-effort — it moves a 350 lb-rated
chassis — so the safety layer lives at this end of the link rather than the
brain's. The brain has to keep asking for motion; if it stops, or the link dies,
cart_driver stops the cart without waiting to be told. Same process again, but
note it is shut down *first* on the way out, so the cart is stopped before
anything else is torn down.

If a token is configured (--token, or DISPLAY_TOKEN in the environment) every
request must carry it as ``X-Display-Token`` — the same shape as the head's
sensor ingest. LAN-only either way: this speaks plain HTTP and drives a screen.

Stdlib only — there's no Flask on this Pi, and a control plane this small doesn't
justify adding one. Runs as root because /dev/fb0 needs it.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cart_driver                          # noqa: E402 — sibling module
import cog_hud                              # noqa: E402 — sibling module
import gamepad as gamepad_mod               # noqa: E402 — sibling module
import metrics_hud                          # noqa: E402 — sibling module
import pin_gate                             # noqa: E402 — sibling module
import sensor_relay                         # noqa: E402 — sibling module
import touch                                # noqa: E402 — sibling module
import voice_state                          # noqa: E402 — sibling module

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"          # remembers the pick across reboots


def read_state() -> dict:
    """Everything remembered across reboots: the animation pick, the HUD flag."""
    try:
        d = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def write_state(**changes) -> None:
    """Merge keys into the state file.

    Merging rather than overwriting because there are now two independent
    writers — the animation picker and the metrics toggle — and a blind write
    from either would silently drop the other's setting.
    """
    d = read_state()
    d.update(changes)
    try:
        STATE_PATH.write_text(json.dumps(d, indent=2) + "\n")
    except OSError:
        pass                              # read-only fs: the live change still works

# The dropdown the head shows is exactly this list, flattened so each entry is
# one concrete look: variants (--copper, --talk) are presets, not extra widgets.
PRESETS = [
    {"id": "reactor",        "label": "Arc Reactor",          "argv": ["reactor.py"]},
    {"id": "reactor-copper", "label": "Arc Reactor (Copper)", "argv": ["reactor.py", "--copper"]},
    {"id": "flux",           "label": "Flux Capacitor",       "argv": ["flux.py"]},
    {"id": "face",           "label": "Face (live voice)",    "argv": ["face.py"]},
    {"id": "voice-hud",      "label": "Voice HUD",            "argv": ["voice_hud.py"]},
    {"id": "voice-hud-c",    "label": "Voice HUD (native)",   "argv": ["voice_hud"]},
    {"id": "face-talk",      "label": "Face (demo talk)",     "argv": ["face.py", "--talk"]},
    {"id": "off",            "label": "Off (blank screen)",   "argv": None},
    # The settings menu is a child like any other — it owns the framebuffer and
    # dies on SIGTERM — but it is not a *look*, so it is hidden from the head's
    # dropdown. You reach it by tapping the cog, and it is entered and left by
    # the two paths below (the touch watcher, and /api/animation/restore).
    {"id": "settings",       "label": "Settings menu",        "argv": ["settings_menu.py"],
     "hidden": True},
]
PRESET_BY_ID = {p["id"]: p for p in PRESETS}
DEFAULT_PRESET = "reactor"

# Crash handling. A child that dies faster than _MIN_HEALTHY_RUN didn't really
# run — but one fast exit isn't proof it's broken (someone may have just killed
# it), so only give up after _MAX_FAST_FAILS in a row. That still can't hot-loop,
# and a genuinely broken script (bad import, no fb0) latches within a few seconds
# with its error visible to the head.
_MIN_HEALTHY_RUN = 3.0
_MAX_FAST_FAILS = 3
_RESPAWN_DELAY = 2.0

# The child's stderr goes to a file, not a PIPE: a PIPE nobody drains fills its
# ~64KB buffer and blocks the animation forever. A file can't wedge the child.
# It lives here beside state.json, deliberately not in /tmp — this runs as root,
# and a fixed path in a world-writable dir is a symlink attack waiting to happen.
CHILD_LOG = HERE / "child-stderr.log"


def _blank_screen() -> None:
    """Black the panel. Only safe once the child has exited (it mmaps fb0 too)."""
    try:
        sys.path.insert(0, str(HERE))
        from fb import Framebuffer          # noqa: PLC0415 — optional, needs numpy
        with Framebuffer() as fb:
            fb.clear()
    except Exception:
        pass                                # no panel / no numpy: nothing to blank


class Supervisor:
    """Owns the running animation child and the watchdog that respawns it."""

    def __init__(self, workdir: Path, python: str = sys.executable):
        self._dir = workdir
        self._py = python
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._preset = DEFAULT_PRESET
        self._restore_to = DEFAULT_PRESET   # where the cog menu goes back to
        self._started_at = 0.0
        self._error = ""                    # last crash, surfaced in /api/state
        self._fails = 0                     # consecutive too-fast exits
        self._stop = threading.Event()
        self._watch = threading.Thread(target=self._watchdog, daemon=True)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.select(self._load_choice(), persist=False)
        self._watch.start()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            self._kill_child()

    # -- child process -----------------------------------------------------
    def _kill_child(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()                     # a wedged animation still frees the fb
            proc.wait(timeout=2)

    def _spawn(self, preset: dict) -> None:
        argv = list(preset["argv"])
        # A ".py" entry runs under this interpreter; anything else is a compiled
        # animation sitting in the work directory, so it runs on its own. Both
        # keep the same contract — own the framebuffer, exit on SIGTERM — and the
        # supervisor cannot tell the difference.
        argv = ([self._py] + argv if argv[0].endswith(".py")
                else ["./" + argv[0]] + argv[1:])
        try:
            err = open(CHILD_LOG, "wb")     # truncate: only the latest run matters
        except OSError:
            err = subprocess.DEVNULL
        try:
            self._proc = subprocess.Popen(
                argv, cwd=str(self._dir),
                stdout=subprocess.DEVNULL,
                stderr=err,                 # a file, never a PIPE — see CHILD_LOG
            )
        finally:
            if err is not subprocess.DEVNULL:
                err.close()                 # the child keeps its own dup'd fd
        self._started_at = time.monotonic()

    @staticmethod
    def _last_error(returncode: int) -> str:
        """Best guess at why the child died, for the head to display."""
        try:
            tail = CHILD_LOG.read_text(errors="replace").strip().splitlines()
        except OSError:
            tail = []
        if tail:
            return tail[-1][:200]
        if returncode is not None and returncode < 0:
            return f"killed by signal {-returncode}"
        return f"exited with code {returncode}"

    def select(self, preset_id: str, persist: bool = True) -> dict:
        """Switch to ``preset_id`` now. Raises KeyError if it isn't a preset."""
        preset = PRESET_BY_ID[preset_id]
        with self._lock:
            self._kill_child()
            self._preset = preset_id
            # Track the last real *look*, so the menu always has somewhere to go
            # back to. Recording it on the way in here (rather than remembering
            # "the previous preset" on the way out) is what stops the menu ever
            # becoming its own restore target and trapping you in it.
            if not preset.get("hidden"):
                self._restore_to = preset_id
            self._error = ""
            self._fails = 0                 # an explicit pick clears a latched crash
            if preset["argv"] is None:
                self._started_at = 0.0
                _blank_screen()
            else:
                self._spawn(preset)
            # A hidden preset is never the boot pick: the menu is somewhere you
            # go, not something the panel should come up sitting in.
            if persist and not preset.get("hidden"):
                self._save_choice(preset_id)
            return self._state_locked()

    def restore(self) -> dict:
        """Leave a hidden preset for the look that was showing before it."""
        with self._lock:
            back = self._restore_to
            if not PRESET_BY_ID[self._preset].get("hidden"):
                return self._state_locked()     # nothing to leave
        return self.select(back, persist=False)

    def _watchdog(self) -> None:
        """Respawn a child that dies on its own; give up only if it keeps failing."""
        while not self._stop.wait(1.0):
            with self._lock:
                preset = PRESET_BY_ID[self._preset]
                if preset["argv"] is None or self._proc is None:
                    continue                # nothing to supervise ("off", or latched)
                if self._proc.poll() is None:
                    continue                # still running

                ran = time.monotonic() - self._started_at
                self._fails = self._fails + 1 if ran < _MIN_HEALTHY_RUN else 0
                if self._fails >= _MAX_FAST_FAILS:
                    # Repeatedly dying on startup: stop and report, don't hot-loop.
                    self._error = self._last_error(self._proc.returncode)
                    self._proc = None
                    continue
                self._proc = None

            # Outside the lock: a select() during the pause must not block.
            if self._stop.wait(_RESPAWN_DELAY):
                return
            with self._lock:
                if self._proc is None and not self._error \
                        and PRESET_BY_ID[self._preset]["argv"] is not None:
                    self._spawn(PRESET_BY_ID[self._preset])

    # -- persistence -------------------------------------------------------
    def _load_choice(self) -> str:
        pid = read_state().get("animation")
        if pid not in PRESET_BY_ID or PRESET_BY_ID[pid].get("hidden"):
            return DEFAULT_PRESET           # never boot into the settings menu
        return pid

    def _save_choice(self, preset_id: str) -> None:
        write_state(animation=preset_id)

    # -- state -------------------------------------------------------------
    def _state_locked(self) -> dict:
        running = self._proc is not None and self._proc.poll() is None
        return {
            "animation": self._preset,
            "label": PRESET_BY_ID[self._preset]["label"],
            "running": running,
            "pid": self._proc.pid if running else None,
            "uptime": round(time.monotonic() - self._started_at, 1) if running else 0.0,
            "error": self._error,
        }

    def state(self) -> dict:
        with self._lock:
            return self._state_locked()


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def _uptime_s() -> float | None:
    """Seconds since this Pi booted. /proc/uptime is two numbers in a file —
    no dependency, and the same on the NUC."""
    try:
        with open("/proc/uptime") as fh:
            return round(float(fh.read().split()[0]), 1)
    except (OSError, ValueError, IndexError):
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "InMoovDisplay/1.0"
    supervisor: Supervisor
    relay: "sensor_relay.SensorRelay | None" = None
    metrics: "metrics_hud.MetricsPublisher | None" = None
    cart: "cart_driver.CartDriver | None" = None
    token: str = ""

    def log_message(self, *a):              # keep the journal to real events
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Display-Token")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if not self.token or self.headers.get("X-Display-Token") == self.token:
            return True
        self._send(403, {"error": "bad or missing display token"})
        return False

    def do_OPTIONS(self):                   # browsers preflight the token header
        self._send(204, {})

    def do_GET(self):
        if not self._authed():
            return
        if self.path.startswith("/api/animations"):
            self._send(200, {"animations": [{"id": p["id"], "label": p["label"]}
                                            for p in PRESETS
                                            if not p.get("hidden")]})
        elif self.path.startswith("/api/state"):
            # metrics rides along so the head's admin panel can reflect the
            # toggle from the same poll it already does for the animation.
            # ...and so does this Pi's own name and uptime, for the INFO tab —
            # it is two cheap reads and saves the menu a second request.
            self._send(200, {**self.supervisor.state(),
                             "metrics": bool(self.metrics and self.metrics.enabled),
                             "hostname": _hostname(), "uptime_s": _uptime_s()})
        elif self.path.startswith("/api/cart"):
            self._send(200, self.cart.state() if self.cart
                       else {"enabled": False})
        elif self.path.startswith("/api/pin"):
            # Whether a PIN is cached, never the digest itself: this port is
            # reachable from the robot LAN and the digest is worth guarding.
            self._send(200, {"pin_set": pin_gate.is_set(pin_gate.load())})
        elif self.path.startswith("/api/sensors"):
            # Not the sensor data itself — that goes straight to the head. This
            # is the relay's own health, so you can tell "the Pico is unplugged"
            # from "the head is unreachable" without SSHing in.
            self._send(200, self.relay.state() if self.relay
                       else {"enabled": False})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return
        if not (self.path.startswith("/api/animation")
                or self.path.startswith("/api/voice")
                or self.path.startswith("/api/metrics")
                or self.path.startswith("/api/pin")
                or self.path.startswith("/api/cart")):
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            self._send(400, {"error": "body must be JSON"})
            return

        if self.path.startswith("/api/voice"):
            self._send(200, self._voice(data))
            return
        if self.path.startswith("/api/metrics"):
            self._send(200, self._metrics(data))
            return
        if self.path.startswith("/api/pin"):
            # The brain telling us its PIN changed, so the settings menu's gate
            # does not lag behind it. Cached rather than consulted live: the
            # menu has to work with the brain switched off.
            ok = pin_gate.save(dict(data.get("pin") or {}))
            self._send(200 if ok else 500,
                       {"cached": ok, "pin_set": pin_gate.is_set(pin_gate.load())})
            return
        if self.path.startswith("/api/cart"):
            self._send(200, self._cart(data))
            return
        # Matched before the generic animation switch below, which would
        # otherwise read "restore" as a body with no animation in it.
        if self.path.startswith("/api/animation/restore"):
            self._send(200, self.supervisor.restore())
            return
        try:
            self._send(200, self.supervisor.select(str(data.get("animation", ""))))
        except KeyError:
            self._send(400, {"error": f"unknown animation {data.get('animation')!r}"})

    def _metrics(self, data: dict) -> dict:
        """Turn the sensor overlay on or off, and remember it across reboots.

        The animation child picks the change up from /dev/shm on its next frame,
        so this lands in ~30ms with no restart and no effect on what's playing.
        """
        if self.metrics is None:
            return {"error": "sensor relay disabled; no metrics to show"}
        enabled = self.metrics.set_enabled(bool(data.get("enabled")))
        write_state(metrics=enabled)
        return {"ok": True, "metrics": enabled}

    def _cart(self, data: dict) -> dict:
        """Drive or stop the cart.

        Stop is matched before drive and answered even when the driver is
        disabled: a stop request must never fail on a technicality, and a caller
        that gets an error back from "stop" has no good next move.
        """
        if self.path.startswith("/api/cart/stop"):
            if self.cart is None:
                return {"ok": True, "stopped": True, "note": "cart driver disabled"}
            if data.get("clear_estop"):
                return self.cart.clear_estop()
            return self.cart.stop(estop=bool(data.get("estop")))
        if self.cart is None:
            return {"error": "cart driver disabled on this Pi"}
        if self.path.startswith("/api/cart/drive"):
            return self.cart.drive(data.get("steer", 0), data.get("speed", 0))
        if self.path.startswith("/api/cart/controller"):
            # Who may drive: off / takeover / only. Persisted, because this is a
            # standing decision about the robot, not a per-session one.
            try:
                mode = self.cart.set_controller_mode(str(data.get("mode", "")))
            except ValueError as exc:
                return {"error": str(exc)}
            write_state(controller_mode=mode)
            return {"ok": True, "controller_mode": mode}
        return {"error": "unknown cart action"}

    def _voice(self, data: dict) -> dict:
        """Hand FRED's voice state to the animation via /dev/shm.

        The head sends ``starts_in`` (seconds from now until frame 0 is audible);
        it becomes an absolute ``play_at`` on *this* Pi's monotonic clock right
        here, so the animation never inherits the network latency.
        """
        state = str(data.get("state", "idle"))
        out = {"state": state if state in voice_state.STATES else "idle",
               "seq": data.get("seq", 0)}
        levels = data.get("levels")
        if levels:
            try:
                out["levels"] = [float(v) for v in levels]
                out["frame_dt"] = float(data.get("frame_dt") or 0.05)
                out["play_at"] = time.monotonic() + float(data.get("starts_in") or 0.0)
            except (TypeError, ValueError):
                return {"error": "bad envelope"}
        else:
            # A state-only update (idle/listening/thinking): keep whatever
            # envelope is current so a late "speaking" tick can't wipe the wave.
            prev = voice_state.VoiceFeed().poll()
            for k in ("levels", "frame_dt", "play_at"):
                if k in prev:
                    out[k] = prev[k]
        voice_state.publish(out)
        return {"ok": True, "state": out["state"], "seq": out["seq"]}


class CogWatcher:
    """Opens the settings menu when the cog is tapped.

    Lives in the daemon rather than in the animations for the obvious reason:
    the animations are replaced constantly and half of them are a C binary. The
    daemon is the one process that is always here, and it already runs as root,
    which the input device requires.

    It reads the touchscreen even while the menu itself has it open — two
    processes reading one evdev node each get their own event queue, so neither
    steals from the other. What stops them acting on the same tap is the guard
    below: while a hidden preset is showing, this watcher does nothing at all.
    """

    def __init__(self, supervisor: Supervisor, target: str = "settings"):
        self._sup = supervisor
        self._target = target
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="cog-watch",
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        dev = touch.open_touch()
        if dev is None:
            return                          # no touchscreen: no cog, no complaint
        print("cog watcher: touchscreen ready", flush=True)
        try:
            while not self._stop.is_set():
                for kind, x, y in dev.poll(timeout=0.2):
                    if kind != "down" or not cog_hud.hit(x, y):
                        continue
                    if PRESET_BY_ID[self._sup.state()["animation"]].get("hidden"):
                        continue            # the menu is up; it owns the screen
                    try:
                        self._sup.select(self._target, persist=False)
                    except KeyError:
                        pass
        finally:
            dev.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--dir", default=str(HERE), help="where the animation scripts live")
    ap.add_argument("--token", default=os.environ.get("DISPLAY_TOKEN", ""),
                    help="shared secret; clients send it as X-Display-Token")
    ap.add_argument("--sensor-port", default=os.environ.get("SENSOR_PORT", "auto"),
                    help="sensor node tty: a path, a glob, or 'auto'")
    ap.add_argument("--sensor-url", default=os.environ.get("SENSOR_URL",
                                                           sensor_relay.DEFAULT_URL),
                    help="the head's ingest endpoint")
    ap.add_argument("--sensor-token", default=os.environ.get("SENSOR_TOKEN", ""),
                    help="matches the head's settings.json sensors.token")
    ap.add_argument("--no-sensor-relay", action="store_true",
                    help="don't read the stomach sensor node at all")
    ap.add_argument("--cart-port", default=os.environ.get("CART_PORT", "auto"),
                    help="cart Pico tty: a path, a glob, or 'auto' "
                         "(auto never selects the MicroPython sensor node)")
    ap.add_argument("--cart-watchdog", type=float,
                    default=float(os.environ.get("CART_WATCHDOG",
                                                 cart_driver.WATCHDOG_S)),
                    help="seconds of head silence before the cart is stopped")
    ap.add_argument("--no-cart", action="store_true",
                    help="don't drive the hoverboard base at all")
    ap.add_argument("--no-cog", action="store_true",
                    help="don't watch the touchscreen for the settings cog")
    args = ap.parse_args()

    # Children inherit this, which is how the settings menu authenticates its
    # own restore call. Set here rather than only in the unit so --token works
    # the same way as DISPLAY_TOKEN=.
    if args.token:
        os.environ["DISPLAY_TOKEN"] = args.token

    sup = Supervisor(Path(args.dir))
    sup.start()
    Handler.supervisor = sup
    Handler.token = args.token

    cog = None
    if not args.no_cog:
        cog = CogWatcher(sup)
        cog.start()

    relay = metrics = None
    if not args.no_sensor_relay:
        # The overlay flag is restored here, not in the animation: the child is
        # replaced on every preset switch and must not own persistent state.
        metrics = metrics_hud.MetricsPublisher(
            enabled=bool(read_state().get("metrics")))
        relay = sensor_relay.SensorRelay(
            port=args.sensor_port, url=args.sensor_url, token=args.sensor_token,
            log=lambda m: print(m, flush=True), on_payload=metrics.set_payload)
        relay.start()
        print(f"sensor overlay: {'on' if metrics.enabled else 'off'}", flush=True)
    Handler.relay = relay
    Handler.metrics = metrics

    cart = pad = None
    if not args.no_cart:
        # The hand controller is read here rather than by the firmware: the PS2
        # receiver that used to arbitrate in the Pico stopped answering, and its
        # USB replacement lives on this Pi. Started even in "off" mode so the
        # panel can say whether a controller is present before you enable it.
        pad = gamepad_mod.Gamepad(log=lambda m: print(m, flush=True))
        pad.start()
        cart = cart_driver.CartDriver(port=args.cart_port,
                                      watchdog=args.cart_watchdog,
                                      log=lambda m: print(m, flush=True),
                                      gamepad=pad,
                                      controller_mode=str(read_state().get(
                                          "controller_mode", "off")))
        cart.start()
    Handler.cart = cart

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"display control on {args.host}:{args.port} "
          f"({'token required' if args.token else 'no token'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Cart first: everything else here is pixels, but this one is wheels.
        if cart:
            cart.shutdown()
        if relay:
            relay.shutdown()
        if pad:
            pad.shutdown()
        if cog:
            cog.stop()
        sup.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()

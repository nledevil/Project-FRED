"""One button: is FRED actually all there?

Built for the moment it is for: a STEM fair, a robot just carried in and
plugged back together, and nobody with a laptop. Pressing SELF TEST on the
chest's menu (or in the web admin) asks every part of him whether it is
working, from the brain's side, and answers in one list — then FRED says the
verdict out loud, which is also the speaker test, because only ears can tell
that sound really left the speaker.

Two runs, kept apart on purpose:

* **The check** (``SelfTest``) never moves him. Each part is asked directly —
  the head's servo link, the arm boards (pinged over USB), both cameras
  (two fresh frames that must differ, the frozen-camera failure), the chest
  daemon, the front sensors, the cart, the access point, the uplink, the
  brains. Checks run in parallel, each with its own time limit, so one dead
  machine costs seconds, not a minute of timeouts in a row.
* **The motion test** (``MotionTest``) wiggles each joint a few degrees, one
  at a time, for when you are standing clear. Head joints use the head's own
  identify sweep; arm joints are only moved if they are already switched on,
  and only around where they already are — enabling an arm joint sends it to
  rest at full speed, which is not something a test button should do.

Three verdicts, as in tools/preflight.py (the shell-side cousin of this,
which also covers things the app cannot see, like watchdog timers and
backups): ``pass``; ``warn`` — working, but know this before the doors open;
``fail`` — something he needs is not there. Only fails make the run NOT READY.

Everything here reads the live objects web/app.py already holds, handed in
as a namespace, so this module never imports the app and the checks can be
exercised with fakes (tools/test_selftest.py).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"

# The order the list is shown in: where a person would look, head to base.
GROUPS = ("Head", "Arms", "Chest and base", "Eyes and ears", "Voice and brain",
          "Network", "Computer")

CHECK_TIMEOUT = 15.0        # per check; a camera's two frames fit inside it
INTERNET_PROBE = "https://api.anthropic.com/"


class Check:
    def __init__(self, group: str, name: str, fn, timeout: float = CHECK_TIMEOUT):
        self.group, self.name, self.fn, self.timeout = group, name, fn, timeout


# --------------------------------------------------------------- the checks
# Each takes the parts namespace and returns (verdict, detail). The detail is
# written for whoever is standing at the robot: what is wrong and what to try,
# not a field name. Missing parts (an attribute that is None) are a skip, so a
# build without, say, a cart does not fail for not having one.

def _two_frames(snap) -> tuple[str, str]:
    """Two fresh snapshots that must differ — the same bytes twice is a camera
    that enumerates, opens and answers, but has frozen."""
    a = snap()
    if not a:
        return FAIL, "no picture"
    time.sleep(0.4)
    b = snap()
    if not b:
        return FAIL, "first picture came, the second did not"
    if hashlib.md5(a).digest() == hashlib.md5(b).digest():
        return FAIL, "frozen: two pictures, identical — unplug and replug it"
    return PASS, f"live ({len(b) // 1024} KB frames)"


def head_servos(p):
    ctrl = p.ctrl
    if ctrl is None:
        return SKIP, "no head servo link"
    refresh = getattr(ctrl, "refresh_state", None)
    if refresh:
        refresh()                           # ask the head now, not last poll
    st = ctrl.status() if hasattr(ctrl, "status") else {}
    if st.get("mock"):
        return FAIL, "running in mock mode — no servos are really being driven"
    if st.get("online") is False:
        return FAIL, (st.get("error") or "head Pi not answering") + \
            " — check the head Pi's power and network cable"
    if st.get("locked"):
        return WARN, "locked joints: " + ", ".join(st["locked"])
    return PASS, "head Pi answering"


def status_led(p):
    led = p.led
    if led is None:
        return SKIP, "no status LED configured"
    st = led.status()
    if st.get("available"):
        return PASS, "answering"
    return FAIL, st.get("error") or "LED server on the head Pi not answering"


def head_camera(p):
    cam = p.camera
    if cam is None:
        return SKIP, "no head camera configured"
    if not cam.available():
        return FAIL, "camera not available — check the head Pi's camera service"
    return _two_frames(lambda: cam.snapshot(timeout=5.0))


def arm_board(side: str):
    def check(p):
        boards = p.boards
        if boards is None:
            return SKIP, "no arm servo boards on this build"
        d = boards.describe()
        if d.get("mock"):
            return FAIL, "pyserial missing — arm boards cannot be reached"
        mine = [b for b in d["boards"] if b.get("side") == side]
        if not mine:
            return FAIL, (f"no board assigned to the {side} arm — plug it in, "
                          "then give it a side in Admin → Arm servos")
        b = mine[0]
        ping = getattr(boards, "ping", None)
        if ping is not None:
            try:
                ping(b["uid"])                # a fresh answer, not a cached flag
                b = next(x for x in boards.describe()["boards"]
                         if x["uid"] == b["uid"])
            except Exception as exc:  # noqa: BLE001 - reported below
                return FAIL, f"board {b['uid']} not answering: {exc}"
        if not b.get("online"):
            return FAIL, (f"board {b['uid']} not connected — check its USB "
                          "cable to the NUC")
        if not b.get("pca"):
            return FAIL, ("microcontroller answers but its servo controller "
                          "does not — check the 4 wires between them")
        if b.get("tripped"):
            return WARN, f"tripped ({b['tripped']}) — outputs cut until re-enabled"
        joints = len(b.get("servos") or {})
        if d.get("stopped"):
            return WARN, f"{joints} joints, but Stop the arms is on"
        if joints == 0:
            return WARN, "connected, but no joints are assigned yet"
        return PASS, f"{joints} joints, board {b['uid']}"
    return check


def stray_boards(p):
    boards = p.boards
    if boards is None:
        return SKIP, ""
    strangers = boards.describe().get("strangers") or []
    if strangers:
        return WARN, "; ".join(f"{s['port'].split('/')[-1]}: {s['error']}"
                               for s in strangers)
    return PASS, "every USB serial device accounted for"


def chest_display(p):
    disp = p.display
    if disp is None or not disp.configured():
        return SKIP, "no chest display configured"
    try:
        st = disp.state()
    except Exception as exc:  # noqa: BLE001
        return FAIL, (f"chest Pi not answering ({exc}) — check its power "
                      "and network cable")
    if st.get("error"):
        return FAIL, f"animation crashed: {str(st['error'])[:80]}"
    if not st.get("running") and st.get("animation") != "off":
        return WARN, f"nothing running for {st.get('animation')!r}"
    return PASS, f"showing {st.get('animation')}"


def front_sensors(p):
    hub = p.sensors
    if hub is None:
        return SKIP, "no sensor hub"
    nodes = hub.state().get("nodes") or {}
    online = [n for n, s in nodes.items() if s.get("online")]
    if online:
        return PASS, "reporting: " + ", ".join(online)
    if nodes:
        ago = min(s.get("last_seen_ago", 0) for s in nodes.values())
        return FAIL, (f"last heard {ago:.0f} s ago — check the sensor board's "
                      "USB cable to the chest Pi")
    return FAIL, ("never heard from — the ultrasonic/motion sensor board is "
                  "not plugged in, or not running")


def cart(p):
    c = p.cart
    if c is None or not c.configured():
        return SKIP, "no cart configured"
    if not (p.settings.get("cart") or {}).get("enabled"):
        return SKIP, "cart switched off in settings"
    try:
        st = c.state()
    except Exception as exc:  # noqa: BLE001
        return FAIL, f"cart driver not answering: {exc}"
    if not st.get("connected"):
        return FAIL, "cart's Pico not connected to the chest Pi"
    if not st.get("mainboard_seen"):
        return FAIL, ("Pico connected but no word from the hoverboard "
                      "mainboard — is the base switched on?")
    if st.get("estop"):
        return WARN, "emergency stop is latched — clear it before driving"
    batt = st.get("battery_v")
    return PASS, "mainboard talking" + (f", battery {batt:.1f} V" if batt else "")


def cart_controller(p):
    c = p.cart
    if c is None or not c.configured() or \
            not (p.settings.get("cart") or {}).get("enabled"):
        return SKIP, ""
    try:
        st = c.state()
    except Exception:  # noqa: BLE001 - the cart check reports the link
        return SKIP, "cart not answering"
    ctl = st.get("controller") or {}
    if ctl.get("connected"):
        return PASS, f"connected ({st.get('controller_mode')} mode)"
    return WARN, "hand controller not connected — switch it on to drive by hand"


def wide_camera(p):
    sp = p.spotter
    if sp is None:
        return SKIP, "no wide camera"
    if not sp.is_running():
        # Starting it costs about four cores; a test button should not.
        return WARN, ("not running, so not tested — switch the wide camera on "
                      "and run again to check it")
    return _two_frames(lambda: sp.snapshot(timeout=3.0))


def mic_array(p):
    doa = p.mic_doa
    if doa is None:
        return SKIP, "no mic array"
    st = doa.status()
    if not st.get("available"):
        return FAIL, (st.get("error") or "reSpeaker not found") + \
            " — check its USB cable"
    if st.get("error"):
        return FAIL, st["error"]
    if not st.get("running"):
        return WARN, "found, but voice direction is switched off"
    return PASS, f"hearing direction ({st.get('reads', 0)} reads)"


def wake_listener(p):
    a = p.assistant
    if a is None or not a.available():
        return FAIL, "voice assistant unavailable"
    st = a.status()
    if not st.get("listening"):
        return WARN, "not listening for his name — turn it on from either panel"
    mic = st.get("mic") or {}
    if not mic.get("capturing"):
        return FAIL, "listening switched on, but the microphone is not capturing"
    return PASS, "listening for \"Fred\""


def speech_recognition(p):
    t = p.transcriber
    if t is None:
        return SKIP, "not configured"
    st = t.status()
    if st.get("ready"):
        return PASS, f"ready on {st.get('device') or 'cpu'}"
    if st.get("error"):
        return FAIL, st["error"]
    return WARN, "still loading"


def voice_out(p):
    s = p.sound
    if s is None or not s.available():
        return FAIL, "no sound output"
    if not s.can_speak():
        return FAIL, "no voice engine — he cannot talk"
    tts = (s.settings() or {}).get("tts")
    return (PASS, f"{tts} ready") if tts == "piper" else \
        (WARN, f"using {tts}, not his own voice")


def claude(p):
    a = p.assistant
    st = a.brain.status() if a is not None else {}
    if not st.get("claude_ready"):
        return WARN, (st.get("claude_error") or "no API key") + \
            " — he will answer from the local brain"
    return PASS, "API key loaded"


def local_brain(p):
    a = p.assistant
    st = a.brain.status() if a is not None else {}
    if not st.get("local_ready"):
        return FAIL, ("local model not answering — offline he has no brain; "
                      "check the ollama service")
    lib = (p.inference_device() or {}).get("library", "")
    if lib.lower() == "cpu":
        return FAIL, ("running on the CPU, not the graphics chip — answers will "
                      "take ~30 s; restart ollama")
    return PASS, f"{st.get('local_model') or 'ready'}" + (f" on {lib}" if lib else "")


def access_point(p):
    st = p.hotspot_state()
    if not st.get("configured"):
        return SKIP, "no access point on this machine"
    if not st.get("enabled"):
        return WARN, "FRED's hotspot is off — phones cannot reach the panel"
    n = len(st.get("clients") or [])
    return PASS, f"broadcasting {st.get('ssid')!r}, {n} connected"


def uplink(p):
    st = p.uplink_state()
    if st.get("error") and not st.get("ssid"):
        return WARN, st["error"]
    if st.get("ssid"):
        sig = st.get("signal")
        return PASS, f"on {st['ssid']!r}" + (f", signal {sig}" if sig else "")
    return WARN, "not on any WiFi — no internet; join one in Admin → WiFi"


def internet(p):
    req = urllib.request.Request(INTERNET_PROBE, method="HEAD")
    try:
        urllib.request.urlopen(req, timeout=5.0)
    except urllib.error.HTTPError:
        pass                                # any HTTP answer means reachable
    except Exception as exc:  # noqa: BLE001
        return WARN, (f"cannot reach the internet ({type(exc).__name__}) — "
                      "Claude, weather and web search are off; the local "
                      "brain still works")
    return PASS, "Claude's servers reachable"


def tailscale(p):
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                             text=True, timeout=5.0).stdout
        state = json.loads(out).get("BackendState", "")
    except Exception:  # noqa: BLE001
        return WARN, "tailscale not answering — remote access only"
    return (PASS, "connected") if state == "Running" else \
        (WARN, f"{state or 'down'} — remote access only, FRED is unaffected")


def disk(p):
    u = shutil.disk_usage("/")
    free = 100 * u.free / u.total
    if free < 5:
        return FAIL, f"{free:.0f}% free"
    return (PASS if free > 15 else WARN), f"{free:.0f}% free"


CHECKS: list[Check] = [
    Check("Head", "Head servos", head_servos),
    Check("Head", "Status LED", status_led),
    Check("Head", "Eye camera", head_camera),
    Check("Arms", "Left arm board", arm_board("left")),
    Check("Arms", "Right arm board", arm_board("right")),
    Check("Arms", "Other USB serial devices", stray_boards),
    Check("Chest and base", "Chest display", chest_display),
    Check("Chest and base", "Front sensors", front_sensors),
    Check("Chest and base", "Cart", cart),
    Check("Chest and base", "Cart hand controller", cart_controller),
    Check("Eyes and ears", "Wide camera", wide_camera),
    Check("Eyes and ears", "Microphone array", mic_array),
    Check("Eyes and ears", "Listening for his name", wake_listener),
    Check("Eyes and ears", "Speech recognition", speech_recognition),
    Check("Voice and brain", "Voice", voice_out),
    Check("Voice and brain", "Claude", claude),
    Check("Voice and brain", "Local brain", local_brain),
    Check("Network", "FRED's hotspot", access_point),
    Check("Network", "House WiFi", uplink, timeout=20.0),
    Check("Network", "Internet", internet),
    Check("Network", "Tailscale", tailscale),
    Check("Computer", "Disk space", disk),
]


# ------------------------------------------------------------------ runners
class _Run:
    """Shared bookkeeping: one run at a time, results readable while it goes."""

    def __init__(self, log=None):
        self._lock = threading.Lock()
        self._log = log
        self._running = False
        self._started = self._finished = None
        self._results: list[dict] = []
        self._total = 0

    def _put(self, row: dict) -> None:
        with self._lock:
            self._results.append(row)

    def status(self) -> dict:
        with self._lock:
            rows = list(self._results)
            out = {"running": self._running, "started_at": self._started,
                   "finished_at": self._finished, "total": self._total,
                   "done": len(rows)}
        order = {g: i for i, g in enumerate(GROUPS)}
        rows.sort(key=lambda r: (order.get(r["group"], 99), r.get("seq", 0)))
        counts = {k: sum(1 for r in rows if r["status"] == k)
                  for k in (PASS, WARN, FAIL, SKIP)}
        out["results"] = rows
        out["counts"] = counts
        out["verdict"] = (None if out["running"] or not rows else
                          "not_ready" if counts[FAIL] else "ready")
        return out


class SelfTest(_Run):
    def __init__(self, parts, speak=None, log=None, checks=None):
        super().__init__(log)
        self.parts = parts
        self._speak = speak
        self.checks = list(checks if checks is not None else CHECKS)

    def start(self) -> bool:
        """Begin a run in the background. False if one is already going."""
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._started, self._finished = time.time(), None
            self._results, self._total = [], len(self.checks)
        threading.Thread(target=self._run, name="selftest", daemon=True).start()
        return True

    def run(self) -> dict:
        """Synchronous, for tests and the CLI."""
        if not self.start():
            raise RuntimeError("a self-test is already running")
        while self.status()["running"]:
            time.sleep(0.05)
        return self.status()

    def _one(self, seq: int, chk: Check) -> None:
        t0 = time.monotonic()
        try:
            verdict, detail = chk.fn(self.parts)
        except Exception as exc:  # noqa: BLE001 - a check that crashes is a finding
            verdict, detail = FAIL, f"check crashed: {type(exc).__name__}: {exc}"
        self._finish(seq, chk, verdict, detail, time.monotonic() - t0)

    def _finish(self, seq: int, chk: Check, verdict: str, detail: str,
                took: float) -> None:
        """Record one check's result, once: a check that answers after its
        time limit has already been written up as a fail and is dropped."""
        with self._lock:
            if seq in self._recorded:
                return
            self._recorded.add(seq)
            self._results.append({"group": chk.group, "name": chk.name,
                                  "status": verdict, "detail": detail,
                                  "seq": seq, "ms": round(took * 1000)})

    def _run(self) -> None:
        self._recorded: set[int] = set()
        # One thread per check, so every check gets its own whole time limit:
        # a pool would start a check late, behind slow ones, and its clock
        # would already be running. They are daemons — one wedged on a dead
        # machine is abandoned at its limit, never waited for.
        start = time.monotonic()
        threads = []
        for i, c in enumerate(self.checks):
            t = threading.Thread(target=self._one, args=(i, c),
                                 name=f"selftest-{i}", daemon=True)
            t.start()
            threads.append((t, i, c))
        for t, i, c in threads:
            t.join(max(0.0, start + c.timeout - time.monotonic()))
            if t.is_alive():
                self._finish(i, c, FAIL, f"no answer within {c.timeout:g} s",
                             c.timeout)
        st = self.status()
        line = spoken_verdict(st)
        with self._lock:
            self._running = False
            self._finished = time.time()
        if self._log is not None:
            c = st["counts"]
            self._log.event(f"🩺 Self-test: {c[PASS]} pass, {c[WARN]} warn, "
                            f"{c[FAIL]} fail")
        if self._speak is not None:
            try:
                self._speak(line)
            except Exception:  # noqa: BLE001 - the list is still on screen
                pass


def spoken_verdict(st: dict) -> str:
    """The line FRED says at the end — short, and the speaker test itself."""
    fails = [r for r in st["results"] if r["status"] == FAIL]
    warns = sum(1 for r in st["results"] if r["status"] == WARN)
    if not fails:
        tail = (f" There {'is' if warns == 1 else 'are'} {warns} "
                f"warning{'s' if warns != 1 else ''} on the screen.") if warns else ""
        return "Self-test complete. All systems working." + tail
    names = [r["name"].lower() for r in fails[:3]]
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    more = f", and {len(fails) - 3} more" if len(fails) > 3 else ""
    n = len(fails)
    return (f"Self-test complete. I found {n} problem{'s' if n != 1 else ''}: "
            f"{listed}{more}. Details are on the screen.")


class MotionTest(_Run):
    """Wiggle each joint a few degrees, one at a time. Only on a deliberate press.

    Head joints: the head server's own identify sweep, which it has always
    done from the admin page. Arm joints: only those already switched on, and
    only ±SWEEP around their current angle — never enabled from here, because
    enabling sends a joint to rest at full speed.
    """

    SWEEP = 5.0
    DWELL = 0.35

    def __init__(self, parts, log=None):
        super().__init__(log)
        self.parts = parts

    def _hw(self):
        """The app's hardware lock, the one its own move endpoints take, so a
        wiggle never interleaves with a slider on the admin page."""
        lock = getattr(self.parts, "lock", None)
        return lock if lock is not None else contextlib.nullcontext()

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._started, self._finished = time.time(), None
            self._results, self._total = [], 0
        threading.Thread(target=self._run, name="motiontest", daemon=True).start()
        return True

    def _run(self) -> None:
        seq = 0
        try:
            for group, name, fn in self._plan():
                try:
                    verdict, detail = fn()
                except Exception as exc:  # noqa: BLE001
                    verdict, detail = FAIL, f"{type(exc).__name__}: {exc}"
                self._put({"group": group, "name": name, "status": verdict,
                           "detail": detail, "seq": seq})
                seq += 1
        finally:
            with self._lock:
                self._running = False
                self._finished = time.time()

    def _plan(self) -> list:
        p = self.parts
        plan = []
        ctrl = p.ctrl
        if ctrl is not None:
            audit = getattr(ctrl, "is_audit", lambda: False)()
            for name in sorted(getattr(ctrl, "servos", {}) or {}):
                def head(name=name):
                    if audit:
                        return SKIP, "audit mode — motion suppressed"
                    with self._hw():
                        ctrl.identify(name, sweep=self.SWEEP, cycles=1)
                    time.sleep(0.6)
                    st = ctrl.status() if hasattr(ctrl, "status") else {}
                    if st.get("online") is False:
                        return FAIL, st.get("error") or "head Pi stopped answering"
                    return PASS, "wiggled — did you see it move?"
                plan.append(("Head", name.replace("_", " "), head))
        boards = p.boards
        if boards is not None:
            d = boards.describe()
            for b in d["boards"]:
                side = b.get("side") or "unassigned"
                for joint, s in sorted(b.get("servos", {}).items(),
                                       key=lambda kv: kv[1]["channel"]):
                    label = f"{side} {joint}".replace("_", " ")
                    def arm(b=b, joint=joint, s=s):
                        if d.get("audit"):
                            return SKIP, "audit mode — motion suppressed"
                        if d.get("stopped"):
                            return SKIP, "Stop the arms is on"
                        if not b.get("online"):
                            return FAIL, "board not connected"
                        if not s.get("name"):
                            return SKIP, "board has no side assigned"
                        if not s.get("live") or s.get("current") is None:
                            return SKIP, ("not switched on — enable it in Admin → "
                                          "Arm servos to include it")
                        cur = float(s["current"])
                        lo = max(s["min_angle"], cur - self.SWEEP)
                        hi = min(s["max_angle"], cur + self.SWEEP)
                        for a in (lo, hi, cur):
                            with self._hw():
                                boards.set_angle(s["name"], a)
                            time.sleep(self.DWELL)
                        return PASS, "wiggled — did you see it move?"
                    plan.append(("Arms", label, arm))
        with self._lock:
            self._total = len(plan)
        return plan

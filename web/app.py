#!/usr/bin/env python3
"""Web control panel for the InMoov head.

A small Flask app that serves a single-page UI with a live slider per servo,
Rest/Relax controls, and a calibration mode that unlocks the full physical
range and can record + save limits back to config/servos.json.

Run from the project root:
    ./venv/bin/python web/app.py            # http://<pi-ip>:8080

Works with no hardware (MOCK mode) and drives real servos once I2C is live.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from functools import wraps
from pathlib import Path

# make the project root importable when run as `python web/app.py`
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask, Response, jsonify, render_template, request  # noqa: E402

from inmoov.servo_controller import ServoController, CONFIG_PATH, load_config  # noqa: E402
from inmoov.remote_servo import RemoteServoController  # noqa: E402
from inmoov.camera import Camera  # noqa: E402
from inmoov.face_tracker import FaceTracker, TUNABLE  # noqa: E402
from inmoov.wide_spotter import WideSpotter  # noqa: E402
from inmoov.assistant import Assistant  # noqa: E402
from inmoov.brain import BACKENDS  # noqa: E402
from inmoov import hotspot as hotspot_mod  # noqa: E402
from inmoov import sysinfo  # noqa: E402
from inmoov.convlog import ConversationLog  # noqa: E402
from inmoov.led import Led  # noqa: E402
from inmoov.remote_led import RemoteLed  # noqa: E402
from inmoov.sound import Sound  # noqa: E402
from inmoov.sensors import SensorHub, SerialSensorReader  # noqa: E402
from inmoov import cart as cart_mod  # noqa: E402
from inmoov.cart import CartClient, CartError  # noqa: E402
from inmoov.display import DisplayClient, DisplayError, VoicePusher  # noqa: E402
from inmoov.greeter import Greeter  # noqa: E402
from inmoov.settings import load_settings, save_settings  # noqa: E402
from inmoov import auth  # noqa: E402
from inmoov import whoami as whoami_mod  # noqa: E402

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024   # cap clip uploads at 32 MB

# Terminator-mode audio clips live here; uploads are converted to .wav.
TERMINATOR_DIR = ROOT / "sounds" / "terminator"
_UPLOAD_EXTS = {".wav", ".mp3", ".m4a", ".mp4", ".aac", ".ogg", ".oga",
                ".opus", ".flac", ".wma", ".aif", ".aiff", ".webm"}

_config = load_config()
_settings = load_settings()                  # admin-editable UI/camera preferences
# Hardware handoff: when released, another process (MyRobotLab) owns the shared
# I2C/audio/camera. If we boot into that state, don't grab the servos (skip the
# rest sweep); the objects are suspended just after construction, below.
_boot_released = bool(_settings.get("hardware", {}).get("released", False))
# Audit (dry run) mode, persisted like the handoff. Read here — before the servo
# controller is built — because booting into an audit must skip the rest sweep
# too: the whole promise is that nothing moves.
_boot_audit = bool(_settings.get("audit", {}).get("enabled", False))
_servo_cfg = _settings.get("servo", {})
_servo_host = str(_servo_cfg.get("remote_host", "") or "").strip()
if _servo_host:
    # Servos live on another machine (the head Pi holds the I2C bus; the brain
    # runs here). Same interface, so nothing downstream knows the difference.
    _ctrl = RemoteServoController(_servo_host,
                                  port=int(_servo_cfg.get("remote_port", 8082)),
                                  token=str(_servo_cfg.get("remote_token", "") or ""),
                                  config=_config)
    if not _boot_released and not _boot_audit:
        _ctrl.rest()                 # the local controller does this in its ctor
else:
    _ctrl = ServoController(config=_config,  # auto mock when /dev/i2c-1 absent
                            move_to_rest=not _boot_released and not _boot_audit)
_led_cfg = _settings.get("led", {})
_led_indicator = bool(_led_cfg.get("camera_indicator", True))
_led_host = str(_led_cfg.get("remote_host", "") or "").strip()
if _led_host:
    # Same reasoning as the servos above: the LED is soldered to a Pi header and
    # x86 has no GPIO, so the pin is driven over the network. Same interface, so
    # nothing downstream (camera.py's notify_camera, /api/led) knows.
    _status_led = RemoteLed(_led_host,
                            port=int(_led_cfg.get("remote_port", 8083)),
                            token=str(_led_cfg.get("remote_token", "") or ""),
                            camera_indicator=_led_indicator)
else:
    _status_led = Led(pin=16,                 # BCM16 status LED (no-op if no GPIO)
                      camera_indicator=_led_indicator)
_cam_cfg = _settings.get("camera", {})
_camera = Camera(indicator=_status_led,      # lazily starts on first stream viewer; lights the LED
                 rotate_180=bool(_cam_cfg.get("flip", False)),
                 af_mode=int(_cam_cfg.get("af_mode", 0)),
                 lens_position=float(_cam_cfg.get("lens_position", 2.0)),
                 backend=str(_cam_cfg.get("backend", "auto") or "auto"),
                 source=_cam_cfg.get("source") or None)
_snd_cfg = _settings.get("sound", {})
_voice_cfg = _settings.get("voice", {})
_sound = Sound(device=_snd_cfg.get("device", "default"),  # aplay-based, no-op if no audio
               enabled=bool(_snd_cfg.get("enabled", True)),
               lead_in=float(_snd_cfg.get("lead_in", 0.0)),  # pad opening words on slow-to-wake devices
               sync_offset=float(_snd_cfg.get("sync_offset", 0.0)),  # lip-sync trim
               audit=_boot_audit)           # dry run: render + time speech, play nothing
# Load the piper voice now, off the request path: the first synthesis pays a
# ~1.7s model load, and we'd rather spend it at boot than on FRED's first reply.
threading.Thread(target=_sound.warm, name="tts-warm", daemon=True).start()
_log = ConversationLog()                     # live transcript: dialogue + detections/events
_track_cfg = {k: v for k, v in (_settings.get("track") or {}).items()
              if k in TUNABLE}             # drop stale/unknown keys: a hand-edited
                                           # settings.json must not break startup
# Remote smart-sensor nodes (a Pico in the stomach) push readings/events here,
# relayed by the chest Pi over the robot LAN. Events land in the transcript.
# Built first because two things downstream consume it: the tracker takes a
# left/right bearing off the ultrasonics, and the assistant hands the whole hub
# to Claude as a tool so FRED can answer "is anyone there?" from real hardware.
_sensor_cfg = _settings.get("sensors", {})
_sensors = SensorHub(on_event=None, log=_log,
                     offline_after=float(_sensor_cfg.get("offline_after", 10.0)))

# Wide-angle spotter: the PanaCast on the chest touchscreen, looking out at
# ~180 degrees. Built before the tracker because the tracker consumes it.
_spot_cfg = _settings.get("spotter", {})
_spotter = WideSpotter(device=int(_spot_cfg.get("device", 0)),
                       detect_hz=float(_spot_cfg.get("detect_hz", 4.0)),
                       detect_width=int(_spot_cfg.get("detect_width", 1920)))


def _bearing_hint():
    """Where is somebody, when the head camera has nothing?

    The PanaCast answers first: it is an actual face detector across ~180
    degrees, so it can tell a person from the furniture and give a real bearing.
    The chest ultrasonics remain the fallback for when it is off, blind or
    starting up — they see a wide arc too, they just cannot tell you what they
    are looking at. Both return None for "no opinion", which the tracker
    distinguishes from "straight ahead".
    """
    if _spot_cfg.get("enabled", True):
        hint = _spotter.bearing()
        if hint is not None:
            return hint
    return _sensors.bearing(left=_sensor_cfg.get("bearing_left", "dist_left"),
                            right=_sensor_cfg.get("bearing_right", "dist_right"))


_tracker = FaceTracker(_camera, _ctrl,       # face-follow: eyes + neck + head tilt
                       event_cb=_log.event,  # logs face-detected / lost / tracking on-off
                       bearing_cb=_bearing_hint,
                       **_track_cfg)         # bench tuning persisted from /api/track
_assistant = Assistant(_ctrl, _status_led, _tracker, _sound,  # voice: wake word + Claude + lip-sync
                       device=_snd_cfg.get("device", "plughw:0,0"), log=_log,
                       mic_gain=float(_voice_cfg.get("gain", 1.0)),
                       model=_voice_cfg.get("model") or None,
                       sensors=_sensors,
                       brain_cfg=_settings.get("brain", {}))   # cloud/local routing
# Auto-greet: the first thing FRED does unprompted. Wired after the assistant
# because it needs one, and attached to the hub afterwards because the hub was
# needed to build the assistant — the dependency is genuinely circular.
_greet_cfg = _settings.get("greet", {})
_greeter = Greeter(_assistant, log=_log,
                   enabled=bool(_greet_cfg.get("enabled", True)),
                   cooldown=float(_greet_cfg.get("cooldown", 90.0)),
                   blocked=lambda: _handoff_released)
_sensors.set_on_event(_greeter.on_event)
_serial_sensors = SerialSensorReader(
    _sensors, port=_sensor_cfg.get("serial_port", "/dev/ttyACM0"),
    baud=int(_sensor_cfg.get("serial_baud", 115200)), log=_log)
# The chest display Pi (a second Pi on the 7" DSI panel) — no host set = feature
# off. Nothing here touches the head's hardware, so it ignores the handoff.
_display_cfg = _settings.get("display", {})
_display = DisplayClient(host=str(_display_cfg.get("host", "") or ""),
                         port=int(_display_cfg.get("port", 8081)),
                         token=str(_display_cfg.get("token", "") or ""))
# Mirrors FRED's voice state + speech envelope to the chest panel, so its
# waveform is the same data as the jaw and the web face. Off the voice path by
# design: it polls, and a dead chest Pi can never stall a reply.
_voice_pusher = VoicePusher(_display, _assistant)
_voice_pusher.start()
# The hoverboard drive base. Its Pico is on the chest Pi, so it rides on the same
# connection as the display — one chest Pi, one address. Disabled by default:
# a robot that can move itself should not start able to, and the safety layer
# that stops it lives on the chest Pi, not here (see inmoov/cart.py).
_cart_cfg = _settings.get("cart", {})
_cart = CartClient(host=str(_display_cfg.get("host", "") or ""),
                   port=int(_display_cfg.get("port", 8081)),
                   token=str(_display_cfg.get("token", "") or ""))


def _cart_enabled() -> bool:
    """Both configured and switched on. Checked per request, so the admin
    toggle takes effect without a restart."""
    return bool(_cart_cfg.get("enabled")) and _cart.configured()


# Hand the cart to the action layer so "come here" and Claude's drive tool can
# reach it. cart_cfg goes too: speeds are policy, not something commands.py
# should hardcode.
_assistant.ctx.cart = _cart
_assistant.ctx.cart_cfg = _cart_cfg
# The head camera, so Claude's look tool can see through FRED's eyes. Same
# object the /camera/* routes and the face tracker use — snapshot() starts the
# sensor if it is idle, so a look works even with nobody watching the stream.
_assistant.ctx.camera = _camera
_lock = threading.Lock()                     # serialize hardware access

# Whether the shared hardware is currently released to another owner (MyRobotLab).
# Seeded from persisted settings; the objects are actually suspended just below.
_handoff_released = _boot_released


def _apply_handoff(release: bool) -> None:
    """Release the shared hardware (I2C servos, audio card, camera) to another
    owner — MyRobotLab — or take it back. Only ONE stack may drive the hardware at
    a time. Idempotent; safe to call at boot (nothing is running yet)."""
    global _handoff_released
    if release:
        _assistant.stop()          # stop the wake-word listener → frees the mic (arecord)
        _tracker.stop()            # stop face tracking → drops its camera hold
        _spotter.stop()            # release the PanaCast — it is shared hardware too
        _camera.suspend()          # force-stop the sensor, report unavailable
        _sound.suspend()           # stop playback, block new
        _ctrl.suspend()            # relax servos + release the I2C/PCA9685 bus
        _status_led.off()          # drop the status LED so nothing stays lit
    else:
        _ctrl.resume()             # re-open I2C, re-apply ranges, return to rest
        _sound.resume()
        _camera.resume()           # sensor restarts lazily on the next viewer
        if _spot_cfg.get("enabled", True):
            _spotter.start()       # retake the PanaCast; no-op if already running
        # The voice listener and face tracker are left OFF — re-arm them from
        # their own toggles, as after any boot.
    _handoff_released = release


def _handoff_state() -> dict:
    return {"released": _handoff_released,
            "servo_suspended": _ctrl.is_suspended(),
            "sound_suspended": _sound.is_suspended(),
            "camera_suspended": _camera.is_suspended()}


# Audit (dry run) mode. Unlike the handoff, this blocks NOTHING at the API layer:
# every control still works and still reports what it would have done. The
# suppression happens one level down, inside the controller/sound/cart objects,
# which is what lets the panel stay fully interactive while the robot sits still.
_audit_mode = _boot_audit


def _apply_audit(on: bool) -> None:
    """Enter or leave audit mode. Idempotent; safe to call at boot.

    Order matters on the way in: stop what is already in motion before the
    objects stop accepting motion, so nothing is left mid-clip or rolling on a
    watchdog nobody is feeding any more. The servos are NOT relaxed — they hold
    their pose, because letting the head sag is exactly the motion we're here to
    prevent.

    On the way out the servos go to rest. During an audit the readouts track
    commanded angles while the hardware sat still, so the two have diverged;
    rest is the one pose both are known to agree on, and it's the same sweep the
    app does at boot.
    """
    global _audit_mode
    on = bool(on)
    if on:
        _sound.stop()              # cut any clip that is mid-play
        _cart.set_audit(True)      # stops the cart for real on the way in
        _ctrl.set_audit(True)      # hold position, accept no new motion
        _sound.set_audit(True)     # render + time speech, but open no device
    else:
        _sound.set_audit(False)
        _cart.set_audit(False)
        _ctrl.set_audit(False)
        if not _handoff_released:
            _ctrl.rest()           # re-sync hardware and readout to a known pose
    _audit_mode = on


def _audit_state() -> dict:
    return {"audit": _audit_mode,
            "servo_audit": _ctrl.is_audit(),
            "sound_audit": _sound.is_audit(),
            "cart_audit": _cart.is_audit()}


SESSION_COOKIE = "fred_pin"


def _authed() -> bool:
    """May this request touch the settings, or anything that moves him?

    Three ways to be allowed, in the order they are cheapest to answer: no PIN
    has been set (nothing is gated until one is), the caller is one of the
    robot's own machines on the wired LAN, or it holds a live session.
    """
    if not auth.is_set(_settings):
        return True
    if auth.is_trusted(request.remote_addr or "", _settings):
        return True
    return auth.valid_session(request.cookies.get(SESSION_COOKIE, ""))


def _needs_pin():
    """A 401 when this request has not unlocked, else None.

    Same shape as _blocked_by_handoff: guards read as one line at the top of the
    handler, and the reason comes back as JSON the panel can act on.
    """
    if _authed():
        return None
    return jsonify({"error": "locked; enter the panel PIN", "locked": True}), 401


def protected(fn):
    """Require the PIN for this route. Ordering matters — see api_cart_stop for
    the one thing that must never carry this."""
    @wraps(fn)
    def wrapper(*a, **kw):
        if (locked := _needs_pin()):
            return locked
        return fn(*a, **kw)
    return wrapper


def _blocked_by_handoff():
    """A 409 response when the hardware is released to MyRobotLab, else None. Used
    to reject endpoints that would otherwise grab the shared hardware back."""
    if _handoff_released:
        return jsonify({"error": "hardware is released to MyRobotLab — turn the "
                                 "handoff off in the admin panel first",
                        "handoff": _handoff_state()}), 409
    return None


def _state() -> dict:
    # Ask the servo link to freshen first, so the panel's MOCK/LIVE badge is the
    # head's answer and not a stale guess from before it was reachable. No-op on
    # a local controller, and on a healthy remote one.
    _ctrl.refresh_state()
    servos = {}
    for name, s in _config["servos"].items():
        servos[name] = {
            "channel": s["channel"],
            "description": s.get("description", ""),
            "min_angle": s["min_angle"],
            "max_angle": s["max_angle"],
            "rest_angle": s["rest_angle"],
            "actuation_range": s.get("actuation_range", 180),
            "current": _ctrl.get_angle(name),
        }
    channels = _config.get("i2c", {}).get("channels", 16)
    camera = _camera.settings() if _camera.available() else None
    sound = _sound.settings() if _sound.available() else None
    return {"mock": _ctrl.mock, "servo_link": _ctrl.status(),
            "channels": channels, "camera": camera,
            "sound": sound, "led": _status_led.status(), "track": _tracker.status(),
            "spotter": _spotter.status(),
            "voice": _assistant.status(), "servos": servos, "settings": _settings,
            "handoff": _handoff_state(), "audit": _audit_state(),
            "brain": _assistant.brain.status(),
            "sensors": _sensors.state(), "greet": _greeter.state()}


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/admin")
def admin():
    """Not @protected, deliberately: gating the *page* would answer a browser
    with raw JSON. The markup is a shell — every value in it arrives from
    /api/settings, which is gated, so an unlocked visitor gets an empty form and
    the keypad, which is the behaviour we wanted anyway."""
    return render_template("admin.html")


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Bare IPv4/hostname for the chest display — deliberately no scheme, port or path.
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$")


@app.get("/api/auth")
def api_auth_state():
    """Whether a PIN exists, and whether this caller is past it.

    Deliberately open: the panel has to be able to ask "should I show a keypad?"
    before it has anything to show one with. It leaks only that the robot has a
    PIN, which anyone can discover by trying a gated endpoint anyway.
    """
    return jsonify({
        "pin_set": auth.is_set(_settings),
        "unlocked": _authed(),
        "trusted": auth.is_trusted(request.remote_addr or "", _settings),
        "locked_for": round(auth.locked_for(request.remote_addr or ""), 1),
    })


@app.post("/api/auth/login")
def api_auth_login():
    """Trade a correct PIN for a session cookie.

    The wait is checked before the digest, so a locked-out caller costs nothing
    to refuse and cannot use the timing to learn anything.
    """
    addr = request.remote_addr or ""
    if (wait := auth.locked_for(addr)) > 0:
        return jsonify({"error": f"too many wrong tries; wait {wait:.0f}s",
                        "locked_for": round(wait, 1)}), 429
    if not auth.is_set(_settings):
        return jsonify({"error": "no PIN is set on this panel"}), 400
    pin = (request.get_json(force=True) or {}).get("pin", "")
    if not auth.check_pin(_settings, pin):
        wait = auth.note_failure(addr)
        return jsonify({"error": "wrong PIN", "locked_for": round(wait, 1)}), 403
    auth.note_success(addr)
    resp = jsonify({"unlocked": True})
    resp.set_cookie(SESSION_COOKIE, auth.open_session(), httponly=True,
                    samesite="Lax", max_age=auth.SESSION_S)
    return resp


@app.post("/api/auth/logout")
def api_auth_logout():
    auth.close_session(request.cookies.get(SESSION_COOKIE, ""))
    resp = jsonify({"unlocked": False})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.post("/api/auth/pin")
@protected
def api_auth_set_pin():
    """Set or change the PIN. Protected, so only someone already in may do it —
    and when no PIN is set, everyone is already in, which is how the first one
    gets chosen.

    Changing it closes every open session, including the caller's own. Anything
    else would leave whoever knew the old PIN still holding the panel.
    """
    data = request.get_json(force=True) or {}
    new = auth.normalise(data.get("pin", ""))
    if not new:
        return jsonify({"error": f"the PIN must be {auth.PIN_LENGTH} digits"}), 400
    # A change needs the old one even from an unlocked session: the session may
    # be a laptop someone walked away from, and the LAN is trusted wholesale.
    if auth.is_set(_settings) and not auth.check_pin(_settings, data.get("current", "")):
        return jsonify({"error": "the current PIN is wrong"}), 403
    _settings.setdefault("auth", {})["pin"] = auth.make_material(new)
    save_settings(_settings)
    auth.close_all_sessions()
    pushed, why = _push_pin_to_chest()
    resp = jsonify({"pin_set": True, "chest_synced": pushed, "chest_error": why})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.post("/api/auth/pin/clear")
@protected
def api_auth_clear_pin():
    """Remove the PIN, reopening the panel. Needs the current one."""
    data = request.get_json(force=True) or {}
    if auth.is_set(_settings) and not auth.check_pin(_settings, data.get("current", "")):
        return jsonify({"error": "the current PIN is wrong"}), 403
    _settings.setdefault("auth", {}).pop("pin", None)
    save_settings(_settings)
    auth.close_all_sessions()
    _push_pin_to_chest()
    resp = jsonify({"pin_set": False})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/api/auth/material")
def api_auth_material():
    """The salt and digest, for the chest touchscreen to check a PIN itself.

    **Robot LAN only, never a session.** A digest handed to the access point is
    ten thousand offline guesses, which is an afternoon — the whole reason the
    PIN is worth anything is that this never leaves the wired network.

    The chest asks for this rather than being pushed it because its menu must
    keep working with the brain switched off: it caches the answer and falls
    back to that cache. See deploy/display/pin_gate.py.
    """
    if not auth.is_trusted(request.remote_addr or "", _settings):
        return jsonify({"error": "robot LAN only"}), 403
    return jsonify({"pin_set": auth.is_set(_settings),
                    "pin": auth.material(_settings)})


def _push_pin_to_chest() -> tuple[bool, str]:
    """Tell the chest the PIN changed, so its screen does not lag behind.

    Best effort by design: the chest pulls this itself when its menu opens, so a
    failure here costs freshness rather than correctness. The admin page reports
    it anyway — "I changed the PIN and the touchscreen still wants the old one"
    should be answerable without guessing.
    """
    try:
        return (bool(_display.push_pin(auth.material(_settings)
                                       if auth.is_set(_settings) else {})), "")
    except (DisplayError, OSError) as exc:
        return False, str(exc)


@app.get("/api/settings")
@protected
def api_get_settings():
    return jsonify(_settings)


@app.post("/api/settings")
@protected
def api_set_settings():
    """Validate + persist admin settings, and apply the camera ones live.

    Accepts a partial payload shaped like settings.json; unknown/absent keys are
    left untouched. Camera defaults are also pushed to the live camera so a save
    takes effect immediately (not just on next restart)."""
    data = request.get_json(force=True) or {}

    if "iris_color" in data:
        color = str(data["iris_color"])
        if not _HEX_RE.match(color):
            return jsonify({"error": "iris_color must be a #rrggbb hex string"}), 400
        _settings["iris_color"] = color

    led = data.get("led")
    if isinstance(led, dict):
        cur = _settings.setdefault("led", {})
        if "camera_indicator" in led:
            cur["camera_indicator"] = bool(led["camera_indicator"])
            _status_led.camera_indicator = cur["camera_indicator"]
            # Re-sync the LED to the current camera state so enabling/disabling
            # this takes effect now, not just on the next camera start/stop.
            if _camera.available():
                _status_led.notify_camera(_camera.is_streaming())

    voice = data.get("voice")
    if isinstance(voice, dict):
        cur = _settings.setdefault("voice", {})
        if "gain" in voice:
            gain = max(1.0, min(8.0, float(voice["gain"])))   # 1x..8x; higher just clips
            cur["gain"] = gain
            _assistant.listener.gain = gain                   # live, no restart needed
        if "enabled" in voice:
            cur["enabled"] = bool(voice["enabled"])
            # Persist the preference, but don't grab the mic back while the
            # hardware is handed off to MyRobotLab — it takes effect on resume.
            if cur["enabled"] and _assistant.available() and not _handoff_released:
                _assistant.start()
            elif not cur["enabled"]:
                _assistant.stop()
        if "model" in voice:
            model = str(voice["model"]).strip()
            if not model:
                return jsonify({"error": "model must not be empty"}), 400
            cur["model"] = model
            _assistant.brain.model = model        # live: next reply uses it

    snd = data.get("sound")
    if isinstance(snd, dict) and "lead_in" in snd:
        try:
            lead = round(float(snd["lead_in"]), 2)
        except (TypeError, ValueError):
            return jsonify({"error": "lead_in must be numeric"}), 400
        if not 0.0 <= lead <= 3.0:
            return jsonify({"error": "lead_in must be between 0 and 3 seconds"}), 400
        _settings.setdefault("sound", {})["lead_in"] = lead
        _sound.lead_in = lead                    # apply to live playback immediately
    if isinstance(snd, dict) and "sync_offset" in snd:
        try:
            off = round(float(snd["sync_offset"]), 3)
        except (TypeError, ValueError):
            return jsonify({"error": "sync_offset must be numeric"}), 400
        if not -1.0 <= off <= 1.0:
            return jsonify({"error": "sync_offset must be between -1 and 1 seconds"}), 400
        _settings.setdefault("sound", {})["sync_offset"] = off
        _sound.sync_offset = off                 # apply to the next clip's jaw timing

    disp = data.get("display")
    if isinstance(disp, dict):
        cur = _settings.setdefault("display", {})
        if "host" in disp:
            host = str(disp["host"]).strip()
            # Bare host/IP only: a scheme or path here would silently produce a
            # broken URL later, and the failure would look like "Pi is offline".
            if host and not _HOST_RE.match(host):
                return jsonify({"error": "host must be an IP or hostname, "
                                         "with no http:// or path"}), 400
            cur["host"] = host
        if "port" in disp:
            try:
                port = int(disp["port"])
            except (TypeError, ValueError):
                return jsonify({"error": "port must be an integer"}), 400
            if not 1 <= port <= 65535:
                return jsonify({"error": "port must be between 1 and 65535"}), 400
            cur["port"] = port
        if "token" in disp:
            cur["token"] = str(disp["token"])
        # Apply live so a Save is enough to reach a newly-configured chest Pi.
        _display.configure(host=cur.get("host"), port=cur.get("port"),
                           token=cur.get("token"))

    greet = data.get("greet")
    if isinstance(greet, dict):
        cur = _settings.setdefault("greet", {})
        if "enabled" in greet:
            cur["enabled"] = bool(greet["enabled"])
        if "cooldown" in greet:
            try:
                cd = round(float(greet["cooldown"]), 1)
            except (TypeError, ValueError):
                return jsonify({"error": "greet cooldown must be numeric"}), 400
            if not 0.0 <= cd <= 3600.0:
                return jsonify({"error": "greet cooldown must be 0-3600 seconds"}), 400
            cur["cooldown"] = cd
        # Live, so Save is enough — no restart to stop him greeting people.
        _greeter.configure(enabled=cur.get("enabled"), cooldown=cur.get("cooldown"))

    cam = data.get("camera")
    if isinstance(cam, dict):
        cur = _settings.setdefault("camera", {})
        if "flip" in cam:
            cur["flip"] = bool(cam["flip"])
        if "af_mode" in cam:
            try:
                mode = int(cam["af_mode"])
            except (TypeError, ValueError):
                return jsonify({"error": "af_mode must be an integer"}), 400
            if mode not in (0, 2):
                return jsonify({"error": "af_mode must be 0 (manual) or 2 (continuous)"}), 400
            cur["af_mode"] = mode
        if "lens_position" in cam:
            try:
                cur["lens_position"] = round(float(cam["lens_position"]), 2)
            except (TypeError, ValueError):
                return jsonify({"error": "lens_position must be numeric"}), 400

        # Push camera defaults to the live camera so the change is visible now.
        if _camera.available():
            with _lock:
                _camera.set_rotation(bool(cur.get("flip", False)))
                _camera.set_focus(mode=cur.get("af_mode"),
                                  lens_position=cur.get("lens_position"))

    save_settings(_settings)
    return jsonify(_settings)


@app.get("/api/handoff")
def api_handoff_status():
    return jsonify(_handoff_state())


@app.post("/api/handoff")
@protected
def api_handoff():
    """Release the shared hardware to MyRobotLab, or take it back.

    Body: ``{"release": true|false}``. When released, the InMoov app drops the
    I2C/PCA9685 servos, the USB audio card, and the Pi camera so MRL (port 8888)
    can drive them; when taken back, the servos return to rest and audio/camera
    work again. The choice persists so an event set-up survives a reboot."""
    data = request.get_json(force=True) or {}
    if "release" in data:
        with _lock:
            _apply_handoff(bool(data["release"]))
        _settings.setdefault("hardware", {})["released"] = _handoff_released
        save_settings(_settings)
    return jsonify(_handoff_state())


@app.get("/api/brain")
def api_brain_status():
    """Which LLM backend answers open questions, and what's actually reachable."""
    return jsonify(_assistant.brain.status())


@app.post("/api/brain")
@protected
def api_brain():
    """Switch the LLM backend: ``{"backend": "auto"|"claude"|"local"}``.

    'auto' prefers Claude and falls back to the local model when the network is
    down — which is the case FRED is taken to events for. Persists."""
    data = request.get_json(force=True) or {}
    if "backend" in data:
        want = str(data["backend"])
        if want not in BACKENDS:
            return jsonify({"error": f"backend must be one of {list(BACKENDS)}"}), 400
        _assistant.brain.set_backend(want)
        _settings.setdefault("brain", {})["backend"] = _assistant.brain.backend
        save_settings(_settings)
        # Pull the model into RAM off the request path, so the first question
        # after a switch to local isn't slow.
        threading.Thread(target=_assistant.brain.warm_local,
                         name="local-warm", daemon=True).start()
    return jsonify(_assistant.brain.status())


@app.get("/api/audit")
def api_audit_status():
    return jsonify(_audit_state())


@app.post("/api/audit")
@protected
def api_audit():
    """Enter or leave audit (dry run) mode.

    Body: ``{"audit": true|false}``. When on, the panel stays fully usable —
    servo sliders, the chat box, the wake word, face tracking — but no audio
    leaves the speaker, no pulse reaches a servo, and the cart will not drive.
    Commanded angles are still tracked and reported, so the readouts show what
    FRED would have done. The choice persists across reboots."""
    data = request.get_json(force=True) or {}
    if "audit" in data:
        with _lock:
            _apply_audit(bool(data["audit"]))
        _settings.setdefault("audit", {})["enabled"] = _audit_mode
        save_settings(_settings)
    return jsonify(_audit_state())


@app.get("/api/sensors")
def api_sensors():
    """Latest state of every remote sensor node (readings + online flags)."""
    return jsonify(_sensors.state())


@app.post("/api/sensors/ingest")
def api_sensors_ingest():
    """Ingest a payload from a smart sensor node (a Pico W in the stomach).

    Body is the node's JSON: ``{node, uptime_ms, readings{}, events[]}``. If
    ``sensors.token`` is set in settings, the node must send it as the
    ``X-Sensor-Token`` header (or ``token`` in the body) — LAN-only either way.

    A node may name its own ``transport`` so the panel shows the real path: the
    stomach node reaches us as ``serial-relay`` (USB into the chest Pi, forwarded
    over the robot LAN by display_control.py), which is worth telling apart
    from a node posting directly over WiFi."""
    token = str(_sensor_cfg.get("token", "") or "")
    data = request.get_json(force=True, silent=True) or {}
    if token:
        sent = request.headers.get("X-Sensor-Token") or data.get("token") or ""
        if sent != token:
            return jsonify({"error": "bad or missing sensor token"}), 403
    transport = str(data.get("transport") or "wifi")[:32]
    if not _sensors.ingest(data, transport=transport):
        return jsonify({"error": "unusable payload"}), 400
    return jsonify({"ok": True})


@app.get("/api/display")
def api_display_status():
    """Chest display state: what it's showing and whether it's reachable.

    Never 5xx's — the chest Pi is a decoration, so an unreachable one is a
    normal, reportable condition (``online: false``), not an error."""
    if not _display.configured():
        return jsonify({"configured": False, "online": False,
                        "host": "", "port": _display.port})
    body = {"configured": True, "host": _display.host, "port": _display.port}
    try:
        body.update(_display.state())
        body["online"] = True
    except DisplayError as e:
        body.update({"online": False, "error": str(e),
                     "animation": _display_cfg.get("animation", "")})
    return jsonify(body)


@app.get("/api/cart")
def api_cart_status():
    """Drive base state for the panel: telemetry, PS2 priority, reachability.

    Like the display, an unreachable cart is a reportable condition rather than
    an error — the robot standing still does not depend on its wheels."""
    if not _cart_enabled():
        return jsonify({"configured": False, "online": False,
                        "enabled": bool(_cart_cfg.get("enabled"))})
    body = {"configured": True, "enabled": True,
            "limits": {"steer": cart_mod.STEER_LIMIT, "speed": cart_mod.SPEED_LIMIT}}
    try:
        body.update(_cart.state())
        body["online"] = True
    except CartError as e:
        body.update({"online": False, "error": str(e)})
    return jsonify(body)


@app.post("/api/cart/drive")
@protected
def api_cart_drive():
    """One drive command from the joystick. Body: ``{"steer": 0, "speed": 150}``.

    Authority expires: the chest Pi stops the cart unless this is called again
    inside its watchdog window, so the panel posts continuously while the stick
    is held and simply stops posting when it is released. Letting go, closing
    the tab, or losing WiFi are all the same event to the cart, and all of them
    stop it — which is why there is no "release" call to forget to send."""
    if not _cart_enabled():
        return jsonify({"error": "cart is disabled"}), 409
    data = request.get_json(force=True) or {}
    try:
        return jsonify(_cart.drive(data.get("steer", 0), data.get("speed", 0)))
    except CartError as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/whoami")
def api_whoami():
    """Names, addresses, versions, and what the brain is inferring on.

    Open, like /api/state and /api/health: it is identity, not control, and it
    carries nothing that is not already discoverable by anyone who can reach
    this port. The one genuinely useful fact is the inference device — the
    answer to "is the local brain on the GPU or has it silently been on the CPU
    since boot", which otherwise needs a journal grep over SSH.
    """
    return jsonify(whoami_mod.state(brain=_assistant.brain,
                                    hotspot=hotspot_mod.state()))


@app.get("/api/hotspot")
def api_hotspot_status():
    """The 'fred' access point: is it up, and who has joined."""
    return jsonify(hotspot_mod.state())


@app.post("/api/hotspot")
@protected
def api_hotspot_set():
    """Switch the access point on or off. Body: {"enabled": bool}.

    Reported back as the state *after* the change rather than the request, so a
    panel that asked for "on" and got a failure shows off, not a hopeful on."""
    data = request.get_json(force=True) or {}
    out = hotspot_mod.set_enabled(bool(data.get("enabled")))
    return jsonify(out), (500 if out.get("error") else 200)


@app.post("/api/hotspot/config")
@protected
def api_hotspot_config():
    """Set the AP's network name and password. Body: {"ssid", "passphrase"}.

    Gated like everything else that changes settings, and worth being gated: the
    AP password is the outermost credential on this robot now that the access
    point comes up at boot with a route to the internet behind it.

    The reply never contains the passphrase. It was just typed in; echoing it
    back would put a second copy on the wire for nobody's benefit.
    """
    data = request.get_json(force=True) or {}
    out = hotspot_mod.configure(str(data.get("ssid", "")),
                                str(data.get("passphrase", "")))
    return jsonify(out), (400 if out.get("error") else 200)


@app.post("/api/cart/controller")
@protected
def api_cart_controller():
    """Who may drive the cart: off | takeover | only.

    A standing decision about the robot rather than a per-session one, so the
    chest persists it. Answered even when the cart is disabled — knowing the
    setting is useful before you switch the base on."""
    if not _cart_enabled():
        return jsonify({"error": "cart is disabled"}), 409
    mode = (request.get_json(force=True) or {}).get("mode", "")
    try:
        return jsonify(_cart.set_controller_mode(mode))
    except CartError as e:
        return jsonify({"error": str(e)}), 502


@app.post("/api/cart/stop")
def api_cart_stop():
    """Stop the cart. ``{"estop": true}`` latches until explicitly cleared.

    Answers 200 even when the cart is disabled or unreachable: a stop button
    that reports failure invites a second, more panicked press, and the honest
    news is that an unreachable cart is already stopping itself.

    **Carries no @protected, and must not.** Driving is gated; stopping never
    is. Someone watching a 350 lb base head for a wall should not be typing a
    PIN, and there is no harm a stranger can do with this endpoint that is worse
    than the harm of the owner being unable to reach it."""
    data = request.get_json(force=True) or {}
    if not _cart_enabled():
        return jsonify({"ok": True, "stopped": True, "note": "cart is disabled"})
    try:
        if data.get("clear_estop"):
            return jsonify(_cart.clear_estop())
        return jsonify(_cart.stop(estop=bool(data.get("estop"))))
    except CartError as e:
        return jsonify({"ok": True, "stopped": True, "unreachable": str(e),
                        "note": "the chest Pi stops the cart on its own within "
                                "half a second of losing contact"})


@app.get("/api/display/animations")
def api_display_animations():
    """Preset list for the admin dropdown, fetched from the chest Pi itself."""
    try:
        return jsonify({"animations": _display.animations()})
    except DisplayError as e:
        return jsonify({"error": str(e), "animations": []}), 502


@app.post("/api/display")
@protected
def api_display_select():
    """Switch the chest animation. Body: ``{"animation": "reactor-copper"}``.

    The pick is persisted on both ends: the chest Pi restores it on boot, and we
    keep it so the admin dropdown shows the right entry even while that Pi is
    unreachable."""
    data = request.get_json(force=True) or {}
    animation = str(data.get("animation", "")).strip()
    if not animation:
        return jsonify({"error": "animation must not be empty"}), 400
    try:
        state = _display.select(animation)
    except DisplayError as e:
        return jsonify({"error": str(e)}), 502
    _display_cfg["animation"] = animation      # _display_cfg is _settings["display"]
    save_settings(_settings)
    return jsonify({"configured": True, "online": True,
                    "host": _display.host, "port": _display.port, **state})


@app.post("/api/display/metrics")
@protected
def api_display_metrics():
    """Show/hide the sensor readout on the chest panel. Body: ``{"enabled": true}``.

    Overlaid on whichever animation is playing, so this is orthogonal to the
    preset pick and doesn't restart it. The chest Pi remembers the flag across
    reboots, and mirrors it back in ``/api/state``, so it isn't duplicated here."""
    data = request.get_json(force=True) or {}
    try:
        state = _display.set_metrics(bool(data.get("enabled")))
    except DisplayError as e:
        return jsonify({"error": str(e)}), 502
    if state.get("error"):
        return jsonify(state), 400
    return jsonify({"configured": True, "online": True, **state})


@app.post("/api/led")
@protected
def api_led():
    """Manually turn the BCM16 red status LED on/off.

    Body: ``{"on": true|false}``. This is a live, non-persisted override; the
    camera may re-assert the LED on its next start/stop while the admin's
    ``led.camera_indicator`` setting is enabled."""
    if not _status_led.available():
        return jsonify({"error": "no LED"}), 503
    data = request.get_json(force=True) or {}
    if "on" not in data:
        return jsonify({"error": "on (bool) required"}), 400
    _status_led.set(bool(data["on"]))
    return jsonify(_status_led.status())


@app.get("/api/track")
def api_track_status():
    return jsonify(_tracker.status())


@app.post("/api/track")
@protected
def api_track():
    """Start/stop face tracking and/or live-tune it.

    Body: ``{"on": true|false, <tuning>...}``. Tuning keys (gain_x, gain_y,
    invert_x/_y/_neck/_tilt, deadzone, neck_gain, tilt_gain, sat_margin,
    eye_recenter, fps, seek_gain) are applied whether or not ``on`` is present,
    so you can tune while it runs."""
    if (blocked := _blocked_by_handoff()):
        return blocked
    if not _tracker.available():
        return jsonify({"error": "face tracking unavailable (needs OpenCV + camera)"}), 503
    data = request.get_json(force=True) or {}
    before = _tracker.tuning()
    _tracker.configure(**data)                   # ignores non-tuning / None keys
    after = _tracker.tuning()
    if after != before:                          # persist only real changes, so
        _settings["track"] = after               # toggling on/off doesn't rewrite
        save_settings(_settings)                 # the file every time
    if "on" in data:
        _tracker.start() if data["on"] else _tracker.stop()
    return jsonify(_tracker.status())


@app.get("/api/voice")
def api_voice_status():
    return jsonify(_assistant.status())


@app.post("/api/voice")
@protected
def api_voice():
    """Start/stop the 'Hey FRED' wake-word listener. Body: {"on": bool}."""
    if (blocked := _blocked_by_handoff()):
        return blocked                           # starting it would re-grab the mic
    data = request.get_json(force=True) or {}
    if "on" in data:
        if data["on"]:
            if not _assistant.available():
                return jsonify({"error": "voice unavailable (needs the Vosk model + a mic)"}), 503
            _assistant.start()
        else:
            _assistant.stop()
    return jsonify(_assistant.status())


@app.post("/api/command")
@protected
def api_command():
    """Send FRED a text command/question (types what you'd say). Runs the hybrid
    brain — local match or Claude — executes any action, and speaks the reply."""
    if (blocked := _blocked_by_handoff()):
        return blocked
    text = (request.get_json(force=True) or {}).get("text", "")
    if not str(text).strip():
        return jsonify({"error": "text required"}), 400
    result = _assistant.converse(str(text))
    return jsonify({**result, "status": _assistant.status()})


@app.post("/api/say")
def api_say():
    """Speak arbitrary text aloud with lip-sync (no brain) — for testing audio."""
    if (blocked := _blocked_by_handoff()):
        return blocked
    text = (request.get_json(force=True) or {}).get("text", "")
    if not str(text).strip():
        return jsonify({"error": "text required"}), 400
    ok = _assistant.speak(str(text))
    return jsonify({"spoke": ok})


@app.get("/api/log")
def api_log():
    """Transcript entries newer than ?since=<id> (0 = all). Poll incrementally."""
    try:
        since = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since = 0
    items = _log.since(since)
    return jsonify({"items": items, "last": items[-1]["id"] if items else since,
                    "head": _log.head()})


@app.post("/api/log/clear")
@protected
def api_log_clear():
    _log.clear()
    return jsonify({"cleared": True})


@app.get("/api/positions")
def api_positions():
    """Lightweight current servo angles + speaking flag, polled by the live head
    animation so the on-screen face mirrors what the robot physically does
    (voice commands, face tracking, and the jaw moving while it talks)."""
    pos = {name: _ctrl.get_angle(name) for name in _config["servos"]}
    return jsonify({"pos": pos, "speaking": _assistant.is_speaking(),
                    "mouth": _assistant.mouth_seq(),   # bumps per clip -> refetch /api/mouth
                    "led": _status_led.is_on,
                    # Rides along on the hot poll so the panel's audit banner stays
                    # truthful even when the mode is flipped from the admin page,
                    # another browser, or curl. It's one bool.
                    "audit": _audit_mode})


@app.get("/api/mouth")
def api_mouth():
    """The loudness envelope currently driving the jaw servo, so the on-screen
    face animates from the same data instead of a lookalike flutter. Fetched once
    per clip, when /api/positions reports a new ``mouth`` sequence number."""
    return jsonify(_assistant.mouth() or {"seq": 0, "levels": []})


@app.get("/api/state")
def api_state():
    return jsonify(_state())


def _vcgencmd(*args) -> str:
    """Run `vcgencmd ...` and return its stripped stdout, or '' on any failure."""
    try:
        out = subprocess.run(["vcgencmd", *args], capture_output=True,
                             text=True, timeout=2)
        return out.stdout.strip()
    except Exception:      # noqa: BLE001 - not on a Pi / no perms -> just report unknown
        return ""


# get_throttled bit meanings (Raspberry Pi firmware). Low bits = happening NOW,
# the +16 bits = has happened at least once since boot.
_THROTTLE_BITS = {
    0:  "undervolt_now",        16: "undervolt_since_boot",
    1:  "freq_capped_now",      17: "freq_capped_since_boot",
    2:  "throttled_now",        18: "throttled_since_boot",
    3:  "soft_temp_limit_now",  19: "soft_temp_limit_since_boot",
}


def _health() -> dict:
    """Pi power/thermal health. On Pi 4 the 5V rail voltage itself isn't
    exposed (that's a Pi 5 pmic_read_adc feature), so we report the firmware's
    under-voltage/throttle flags — the real signal that the 5V bus is sagging —
    plus SoC temperature and core voltage."""
    flags = {v: False for v in _THROTTLE_BITS.values()}
    raw = _vcgencmd("get_throttled")            # e.g. "throttled=0x50000"
    bits = None
    if "=" in raw:
        try:
            bits = int(raw.split("=", 1)[1], 16)
            for bit, name in _THROTTLE_BITS.items():
                flags[name] = bool(bits & (1 << bit))
        except ValueError:
            bits = None

    def _num(s, pre, suf):
        try:
            return float(s.split(pre, 1)[1].rstrip(suf))
        except (IndexError, ValueError):
            return None

    # One source of truth for temperature, shared with what FRED says out loud.
    # sysinfo reads sysfs directly, which also spares this 5-second poll a
    # subprocess (vcgencmd agrees to a tenth of a degree).
    temp = sysinfo.soc_temp_c()
    core = _num(_vcgencmd("measure_volts", "core"), "volt=", "V")

    return {
        "supported": bits is not None,
        "throttled_hex": (f"0x{bits:x}" if bits is not None else None),
        "temp_c": temp,
        "core_volt": core,
        "voltage_readable": False,   # Pi 4: no direct 5V rail readout
        **flags,
    }


@app.get("/api/health")
def api_health():
    return jsonify(_health())


@app.post("/api/move")
@protected
def api_move():
    if (blocked := _blocked_by_handoff()):
        return blocked
    data = request.get_json(force=True)
    name = data["name"]
    angle = float(data["angle"])
    raw = bool(data.get("raw", False))          # calibration: ignore soft limits
    with _lock:
        actual = _ctrl.set_angle(name, angle, enforce_limits=not raw)
    return jsonify({"name": name, "angle": actual})


@app.post("/api/rest")
@protected
def api_rest():
    if (blocked := _blocked_by_handoff()):
        return blocked
    with _lock:
        _ctrl.rest()
    return jsonify(_state())


@app.post("/api/relax")
@protected
def api_relax():
    if (blocked := _blocked_by_handoff()):
        return blocked
    name = (request.get_json(silent=True) or {}).get("name")
    with _lock:
        _ctrl.relax(name)
    return jsonify({"relaxed": name or "all"})


@app.post("/api/record")
@protected
def api_record():
    """Record the given angle as a servo's min/max/rest limit (in memory)."""
    data = request.get_json(force=True)
    name = data["name"]
    field = data["field"]                       # "min" | "max" | "rest"
    if field not in ("min", "max", "rest"):
        return jsonify({"error": "field must be min|max|rest"}), 400
    if name not in _config["servos"]:
        return jsonify({"error": f"unknown servo {name}"}), 404
    _config["servos"][name][f"{field}_angle"] = round(float(data["angle"]), 1)
    return jsonify(_state())


@app.post("/api/channel")
@protected
def api_channel():
    """Reassign which PCA9685 port drives a servo (in memory; persist via /api/save)."""
    data = request.get_json(force=True)
    name = data["name"]
    if name not in _config["servos"]:
        return jsonify({"error": f"unknown servo {name}"}), 404
    if (blocked := _blocked_by_handoff()):
        return blocked
    try:
        channel = int(data["channel"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "channel must be an integer"}), 400
    for other, s in _config["servos"].items():
        if other != name and s["channel"] == channel:
            return jsonify({"error": f"port {channel} is already used by '{other}'"}), 409
    with _lock:
        try:
            _ctrl.set_channel(name, channel)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
    return jsonify(_state())


@app.post("/api/identify")
@protected
def api_identify():
    """Wiggle one servo so the operator can see which physical port it's on."""
    if (blocked := _blocked_by_handoff()):
        return blocked
    data = request.get_json(force=True)
    name = data["name"]
    if name not in _config["servos"]:
        return jsonify({"error": f"unknown servo {name}"}), 404
    with _lock:
        _ctrl.identify(name)
    return jsonify({"identified": name, "channel": _config["servos"][name]["channel"]})


@app.post("/api/save")
@protected
def api_save():
    """Persist the current (possibly re-recorded) config to disk, and push it to
    the head Pi when the servos live there.

    Writing this file used to be the whole job, and on a Pi-hosted panel it was.
    With the servos remote it is only half: the head owns the hardware and its
    own servos.json wins on every RemoteServoController refresh, so a
    calibration saved only here never reaches a servo — Rest and the soft limits
    silently keep the values the head booted with.
    """
    with open(CONFIG_PATH, "w") as f:
        json.dump(_config, f, indent=2)
        f.write("\n")
    out = {"saved": str(CONFIG_PATH)}

    push = getattr(_ctrl, "push_config", None)   # absent on the local controller
    if push is None:
        return jsonify(out)
    try:
        out["pushed"] = push(_config)
    except Exception as exc:                     # noqa: BLE001 - report, don't crash
        # The file on this machine is written, but the calibration is NOT live.
        # Saying 200 here would recreate exactly the silent failure this push
        # was added to remove, so the status has to carry the bad news.
        out["push_error"] = f"{type(exc).__name__}: {exc}"
        return jsonify(out), 502
    return jsonify(out)


@app.post("/api/camera")
@protected
def api_camera():
    """Adjust live camera settings: focus (af_mode/lens_position) and 180 flip."""
    if (blocked := _blocked_by_handoff()):
        return blocked
    if not _camera.available():
        return jsonify({"error": "no camera"}), 503
    data = request.get_json(force=True)
    if "flip" in data:
        _camera.set_rotation(bool(data["flip"]))
    if data.get("af_once"):
        _camera.autofocus()                      # single autofocus scan
    elif "af_mode" in data or "lens_position" in data:
        try:
            _camera.set_focus(mode=data.get("af_mode"),
                              lens_position=data.get("lens_position"))
        except (TypeError, ValueError):
            return jsonify({"error": "af_mode/lens_position must be numeric"}), 400
    return jsonify(_camera.settings())


@app.get("/api/sounds")
def api_sounds():
    """List playable sound names and current audio status."""
    if not _sound.available():
        return jsonify({"error": "no audio"}), 503
    return jsonify(_sound.settings())


@app.post("/api/sound/play")
def api_sound_play():
    """Play a named sound (``sounds/<name>.wav``), non-blocking."""
    if (blocked := _blocked_by_handoff()):
        return blocked
    if not _sound.available():
        return jsonify({"error": "no audio"}), 503
    name = (request.get_json(force=True) or {}).get("name")
    if not name:
        return jsonify({"error": "name required"}), 400
    if not _sound.play(str(name)):
        return jsonify({"error": f"cannot play {name!r}",
                        "sounds": _sound.list()}), 404
    return jsonify({"playing": name})


@app.post("/api/sound/stop")
def api_sound_stop():
    """Stop any currently-playing sound."""
    _sound.stop()
    return jsonify({"stopped": True})


# ---- terminator clips: upload / list / preview / delete --------------------
def _safe_clip_stem(name: str) -> str:
    """A filesystem-safe stem from an uploaded filename (no path, no traversal)."""
    stem = Path(name).stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")
    return stem[:60] or "clip"


@app.get("/api/sounds/terminator")
def api_term_list():
    """List installed terminator clips and whether conversion is available."""
    return jsonify({"clips": _sound.list_category("terminator"),
                    "can_convert": shutil.which("ffmpeg") is not None})


@app.post("/api/sounds/terminator/upload")
@protected
def api_term_upload():
    """Accept an audio upload, convert to 22 kHz mono WAV, save under
    sounds/terminator/. Plays at random when terminator mode engages."""
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "no file uploaded"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in _UPLOAD_EXTS:
        return jsonify({"error": f"unsupported file type {ext or '?'}"}), 400
    TERMINATOR_DIR.mkdir(parents=True, exist_ok=True)
    dest = TERMINATOR_DIR / f"{_safe_clip_stem(f.filename)}.wav"
    if ext == ".wav":
        f.save(str(dest))
    else:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            return jsonify({"error": "server can't convert audio (ffmpeg missing) — "
                                     "upload a .wav file instead"}), 415
        fd, tmp = tempfile.mkstemp(suffix=ext)
        os.close(fd)
        try:
            f.save(tmp)
            r = subprocess.run(
                [ffmpeg, "-y", "-i", tmp, "-ac", "1", "-ar", "22050",
                 "-c:a", "pcm_s16le", str(dest)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if r.returncode != 0:
                return jsonify({"error": "audio conversion failed"}), 500
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return jsonify({"saved": dest.name, "clips": _sound.list_category("terminator")})


def _term_clip_path(name: str) -> Path | None:
    """Resolve a terminator clip filename to its path, rejecting traversal."""
    if not name or "/" in name or "\\" in name or not name.endswith(".wav"):
        return None
    p = TERMINATOR_DIR / name
    return p if p.is_file() else None


@app.post("/api/sounds/terminator/play")
def api_term_play():
    """Preview a specific terminator clip through the speaker."""
    if (blocked := _blocked_by_handoff()):
        return blocked
    p = _term_clip_path((request.get_json(force=True) or {}).get("name", ""))
    if p is None:
        return jsonify({"error": "clip not found"}), 404
    _sound.play_file(p)
    return jsonify({"playing": p.name})


@app.post("/api/sounds/terminator/delete")
@protected
def api_term_delete():
    """Delete a terminator clip."""
    p = _term_clip_path((request.get_json(force=True) or {}).get("name", ""))
    if p is None:
        return jsonify({"error": "clip not found"}), 404
    p.unlink()
    return jsonify({"deleted": p.name, "clips": _sound.list_category("terminator")})


@app.get("/camera/stream")
def camera_stream():
    """Live MJPEG feed (multipart/x-mixed-replace) — one shared camera, many viewers."""
    if not _camera.available():
        return jsonify({"error": "no camera"}), 503

    def gen():
        for frame in _camera.frames():
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                   + frame + b"\r\n")

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.get("/camera/snapshot")
def camera_snapshot():
    """Single still JPEG — handy as a fallback / for testing."""
    if not _camera.available():
        return jsonify({"error": "no camera"}), 503
    frame = _camera.snapshot()
    if not frame:
        return jsonify({"error": "no frame"}), 503
    return Response(frame, mimetype="image/jpeg")


if __name__ == "__main__":
    if _handoff_released:
        _apply_handoff(True)                     # boot straight into the released
        print("Hardware handoff: RELEASED to MyRobotLab (I2C/audio/camera held off).")
    if _audit_mode:
        # Sound already got it in its constructor; this arms the servos and cart.
        _apply_audit(True)
        print("AUDIT MODE: dry run — no audio out, no servo motion, cart held.")
    print(f"Serving InMoov control panel — mode: {'MOCK' if _ctrl.mock else 'LIVE'}")
    if _spot_cfg.get("enabled", True) and not _handoff_released:
        # Runs whether or not tracking is on: the bearing it produces is what
        # tells the tracker there is somebody to turn toward in the first place.
        if _spotter.start():
            s = _spotter.status()
            print(f"Wide spotter: PanaCast {s['size']} @ {s['detect_hz']} Hz")
        else:
            print(f"Wide spotter: unavailable — {_spotter.last_error}")
    # Load the local model AND read its prompt prefix now, off the conversation
    # path. Skipping this doesn't save the work, it just bills it to whoever asks
    # FRED the first question — as a ~10 s silence before he answers.
    if _assistant.brain.backend in ("auto", "local"):
        threading.Thread(target=_assistant.brain.warm_local,
                         name="local-warm", daemon=True).start()
    boot = _snd_cfg.get("boot_sound", "")
    if boot and _sound.available() and not _handoff_released:
        _sound.play(boot)                        # non-blocking chime on startup
    if (_settings.get("voice", {}).get("enabled") and _assistant.available()
            and not _handoff_released):
        _assistant.start(greet=True)             # "Hey FRED" listener on at boot
    if _sensor_cfg.get("serial_enabled"):
        _serial_sensors.start()                  # read a USB-serial sensor node (no-WiFi fallback)
    app.run(host="0.0.0.0", port=8080, threaded=True)

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
from pathlib import Path

# make the project root importable when run as `python web/app.py`
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask, Response, jsonify, render_template, request  # noqa: E402

from inmoov.servo_controller import ServoController, CONFIG_PATH, load_config  # noqa: E402
from inmoov.camera import Camera  # noqa: E402
from inmoov.face_tracker import FaceTracker, TUNABLE  # noqa: E402
from inmoov.assistant import Assistant  # noqa: E402
from inmoov import sysinfo  # noqa: E402
from inmoov.convlog import ConversationLog  # noqa: E402
from inmoov.led import Led  # noqa: E402
from inmoov.sound import Sound  # noqa: E402
from inmoov.sensors import SensorHub, SerialSensorReader  # noqa: E402
from inmoov.display import DisplayClient, DisplayError, VoicePusher  # noqa: E402
from inmoov.greeter import Greeter  # noqa: E402
from inmoov.settings import load_settings, save_settings  # noqa: E402

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
_ctrl = ServoController(config=_config,      # auto mock when /dev/i2c-1 absent
                        move_to_rest=not _boot_released)
_led_cfg = _settings.get("led", {})
_status_led = Led(pin=16,                     # BCM16 status LED (no-op if no GPIO)
                  camera_indicator=bool(_led_cfg.get("camera_indicator", True)))
_cam_cfg = _settings.get("camera", {})
_camera = Camera(indicator=_status_led,      # lazily starts on first stream viewer; lights the LED
                 rotate_180=bool(_cam_cfg.get("flip", False)),
                 af_mode=int(_cam_cfg.get("af_mode", 0)),
                 lens_position=float(_cam_cfg.get("lens_position", 2.0)))
_snd_cfg = _settings.get("sound", {})
_voice_cfg = _settings.get("voice", {})
_sound = Sound(device=_snd_cfg.get("device", "default"),  # aplay-based, no-op if no audio
               enabled=bool(_snd_cfg.get("enabled", True)),
               lead_in=float(_snd_cfg.get("lead_in", 0.0)),  # pad opening words on slow-to-wake devices
               sync_offset=float(_snd_cfg.get("sync_offset", 0.0)))  # lip-sync trim
# Load the piper voice now, off the request path: the first synthesis pays a
# ~1.7s model load, and we'd rather spend it at boot than on FRED's first reply.
threading.Thread(target=_sound.warm, name="tts-warm", daemon=True).start()
_log = ConversationLog()                     # live transcript: dialogue + detections/events
_track_cfg = {k: v for k, v in (_settings.get("track") or {}).items()
              if k in TUNABLE}             # drop stale/unknown keys: a hand-edited
                                           # settings.json must not break startup
_tracker = FaceTracker(_camera, _ctrl,       # face-follow: eyes + neck + head tilt
                       event_cb=_log.event,  # logs face-detected / lost / tracking on-off
                       **_track_cfg)         # bench tuning persisted from /api/track
# Remote smart-sensor nodes (a Pico in the stomach) push readings/events here,
# relayed by the chest Pi over the Bluetooth PAN. Events land in the transcript;
# the on_event hook is where behaviours (auto-greet on approach) will attach.
# Built before the assistant because the assistant hands the hub to Claude as a
# tool, so FRED can answer "is anyone there?" from real sensor data.
_sensor_cfg = _settings.get("sensors", {})
_sensors = SensorHub(on_event=None, log=_log,
                     offline_after=float(_sensor_cfg.get("offline_after", 10.0)))
_assistant = Assistant(_ctrl, _status_led, _tracker, _sound,  # voice: wake word + Claude + lip-sync
                       device=_snd_cfg.get("device", "plughw:0,0"), log=_log,
                       mic_gain=float(_voice_cfg.get("gain", 1.0)),
                       model=_voice_cfg.get("model") or None,
                       sensors=_sensors)
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
        _camera.suspend()          # force-stop the sensor, report unavailable
        _sound.suspend()           # stop playback, block new
        _ctrl.suspend()            # relax servos + release the I2C/PCA9685 bus
        _status_led.off()          # drop the status LED so nothing stays lit
    else:
        _ctrl.resume()             # re-open I2C, re-apply ranges, return to rest
        _sound.resume()
        _camera.resume()           # sensor restarts lazily on the next viewer
        # The voice listener and face tracker are left OFF — re-arm them from
        # their own toggles, as after any boot.
    _handoff_released = release


def _handoff_state() -> dict:
    return {"released": _handoff_released,
            "servo_suspended": _ctrl.is_suspended(),
            "sound_suspended": _sound.is_suspended(),
            "camera_suspended": _camera.is_suspended()}


def _blocked_by_handoff():
    """A 409 response when the hardware is released to MyRobotLab, else None. Used
    to reject endpoints that would otherwise grab the shared hardware back."""
    if _handoff_released:
        return jsonify({"error": "hardware is released to MyRobotLab — turn the "
                                 "handoff off in the admin panel first",
                        "handoff": _handoff_state()}), 409
    return None


def _state() -> dict:
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
    return {"mock": _ctrl.mock, "channels": channels, "camera": camera,
            "sound": sound, "led": _status_led.status(), "track": _tracker.status(),
            "voice": _assistant.status(), "servos": servos, "settings": _settings,
            "handoff": _handoff_state(), "sensors": _sensors.state(),
            "greet": _greeter.state()}


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/admin")
def admin():
    return render_template("admin.html")


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Bare IPv4/hostname for the chest display — deliberately no scheme, port or path.
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$")


@app.get("/api/settings")
def api_get_settings():
    return jsonify(_settings)


@app.post("/api/settings")
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
    over the Bluetooth PAN by display_control.py), which is worth telling apart
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


@app.get("/api/display/animations")
def api_display_animations():
    """Preset list for the admin dropdown, fetched from the chest Pi itself."""
    try:
        return jsonify({"animations": _display.animations()})
    except DisplayError as e:
        return jsonify({"error": str(e), "animations": []}), 502


@app.post("/api/display")
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
                    "led": _status_led.is_on})


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
def api_rest():
    if (blocked := _blocked_by_handoff()):
        return blocked
    with _lock:
        _ctrl.rest()
    return jsonify(_state())


@app.post("/api/relax")
def api_relax():
    if (blocked := _blocked_by_handoff()):
        return blocked
    name = (request.get_json(silent=True) or {}).get("name")
    with _lock:
        _ctrl.relax(name)
    return jsonify({"relaxed": name or "all"})


@app.post("/api/record")
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
def api_save():
    """Persist the current (possibly re-recorded) config to disk."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(_config, f, indent=2)
        f.write("\n")
    return jsonify({"saved": str(CONFIG_PATH)})


@app.post("/api/camera")
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
    print(f"Serving InMoov control panel — mode: {'MOCK' if _ctrl.mock else 'LIVE'}")
    boot = _snd_cfg.get("boot_sound", "")
    if boot and _sound.available() and not _handoff_released:
        _sound.play(boot)                        # non-blocking chime on startup
    if (_settings.get("voice", {}).get("enabled") and _assistant.available()
            and not _handoff_released):
        _assistant.start(greet=True)             # "Hey FRED" listener on at boot
    if _sensor_cfg.get("serial_enabled"):
        _serial_sensors.start()                  # read a USB-serial sensor node (no-WiFi fallback)
    app.run(host="0.0.0.0", port=8080, threaded=True)

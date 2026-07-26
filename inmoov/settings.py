"""Persisted app/UI settings for the InMoov web panel.

Separate from config/servos.json (which is hardware calibration): this holds
operator preferences the admin screen edits — the animation's iris colour and
the camera's *default* flip / focus state applied on boot. Missing keys fall
back to DEFAULT_SETTINGS, so an old or hand-trimmed file still loads.
"""
from __future__ import annotations

import json
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.json"

DEFAULT_SETTINGS = {
    "iris_color": "#143a97",          # hex; drives --iris-color in the SVG face
    "camera": {
        "flip": False,                # apply the 180 flip on page/app load
        "af_mode": 0,                 # 0 = manual, 2 = continuous AF (matches libcamera AfMode)
        "lens_position": 2.0,         # dioptres, used when af_mode is manual (~0.5 m)
    },
    "led": {
        "camera_indicator": True,     # light the BCM16 red LED while the camera streams
    },
    "hardware": {
        # When True, the app boots with the shared hardware RELEASED to another
        # owner (MyRobotLab): I2C/PCA9685 servos, the USB audio card, and the Pi
        # camera are all handed off. Toggle live from the admin panel; the choice
        # persists so an event set-up survives a reboot. See /api/handoff.
        "released": False,
    },
    "sensors": {
        # Remote smart-sensor nodes (a Pico W in the stomach reading ultrasonic +
        # PIR) push readings/events to POST /api/sensors/ingest over WiFi, or over
        # USB serial when there's no network. See inmoov/sensors.py.
        "token": "",                  # shared secret required on the HTTP ingest
                                      # (empty = no auth; set one for events — LAN-only regardless)
        "offline_after": 10.0,        # seconds without data before a node reads "offline"
        "serial_enabled": False,      # also read a USB-serial node (Pico over USB) as the
                                      # no-network fallback
        "serial_port": "/dev/ttyACM0",
        "serial_baud": 115200,
    },
    "display": {
        # The chest display Pi: a second Pi driving the 7" DSI panel, running
        # deploy/display/display_control.py. Empty host = feature off (the admin
        # panel's chest section just shows "not configured"). See inmoov/display.py.
        "host": "",                   # IP of the chest Pi, e.g. "192.168.68.81"
        "port": 8081,                 # display_control.py's control API port
        "token": "",                  # shared secret, sent as X-Display-Token
                                      # (empty = no auth; LAN-only regardless)
        "animation": "reactor",       # last preset picked, so the head's UI shows
                                      # the right selection before it can reach the Pi
    },
    "voice": {
        "enabled": False,             # start the "Hey FRED" wake-word listener at boot
        "gain": 1.0,                  # software mic boost (analog capture is already
                                      # maxed); 1.0 = off, ~2-3 for quiet/distant speech
        "model": "claude-haiku-4-5-20251001",   # Claude model behind FRED's replies.
                                      # Haiku answers in ~0.7s vs Opus's ~1.7s; for one- or
                                      # two-sentence spoken replies that trade is worth it.
    },
    "greet": {
        "enabled": True,              # say hello when someone walks up (approach event)
        "cooldown": 90.0,             # seconds before the same arrival can greet again;
                                      # stops a person lingering at the edge of the cone
                                      # from being greeted over and over
    },
    "track": {
        # Face-tracking tunables (gains, invert flags, deadzone...). Deliberately
        # empty: the defaults live in FaceTracker's signature, so there is one
        # source of truth rather than two that can drift. This starts empty and
        # POST /api/track writes the full tuning set here the first time you
        # change anything, so a value dialled in on the bench survives a restart.
        # Keys are filtered through face_tracker.TUNABLE on load.
    },
    "sound": {
        "enabled": True,              # master mute for the head's audio
        "device": "plughw:0,0",       # ALSA device for aplay/espeak -D; plug auto-converts
                                      # rate/format for card 0 (USB P10S). "default" = asound.conf.
        "boot_sound": "startup",      # sound name played on app start ("" to disable)
        "lead_in": 0.0,               # seconds of silence prepended to each clip; raise
                                      # (~0.4-0.6) for a device that clips the opening words
                                      # while its output stream spins up. 0 = off.
        "sync_offset": 0.0,           # seconds; lip-sync trim. lead_in is compensated
                                      # exactly, so this only covers residual device
                                      # latency. Positive = jaw waits longer (mouth was
                                      # ahead of the voice). Usually within ±0.2.
    },
}


def _merge(base: dict, over: dict) -> dict:
    """Deep-merge ``over`` onto a copy of ``base`` (one level of nesting)."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def load_settings(path: str | Path = SETTINGS_PATH) -> dict:
    """Return settings from disk merged over DEFAULT_SETTINGS. Any read/parse
    failure (missing file, bad JSON) falls back to the defaults."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return _merge(DEFAULT_SETTINGS, data if isinstance(data, dict) else {})


def save_settings(settings: dict, path: str | Path = SETTINGS_PATH) -> None:
    """Write settings to disk as pretty JSON (trailing newline)."""
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

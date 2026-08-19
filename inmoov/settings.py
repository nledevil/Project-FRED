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
        # Where frames come from. "auto" picks a local Pi camera if there is one,
        # else a stream URL if `source` is one, else a local USB camera. Set it
        # explicitly when a machine has more than one option — the NUC has both a
        # USB camera of its own and the head Pi's stream to choose between.
        #   "picamera2" - Pi CSI camera (source ignored)
        #   "mjpeg"     - another machine's MJPEG stream; source = its URL
        #   "v4l2"      - local USB/UVC camera; source = index or /dev/videoN
        "backend": "auto",
        "source": "",                 # e.g. "http://10.0.0.10:8081/stream.mjpg" or 0
    },
    "servo": {
        # Where the servos are. Empty host = drive the local PCA9685 over I2C,
        # which is what runs on the head Pi itself. Set a host and the servos are
        # driven over the network instead, via deploy/servo_server.py on the Pi
        # that actually owns the I2C bus — that is how the NUC moves FRED now
        # that the brain and the wiring live on different machines.
        "remote_host": "",            # e.g. "10.0.0.10" (the head Pi)
        "remote_port": 8082,
        "remote_token": "",           # must match SERVO_TOKEN on the server ("" = no auth)
    },
    "led": {
        "camera_indicator": True,     # light the BCM16 red LED while the camera streams
        # Where the LED is. Empty host = drive this machine's own BCM16, which is
        # what runs on the head Pi itself. Set a host and it is driven over the
        # network via deploy/led_server.py — x86 has no GPIO, so this is how the
        # NUC lights an LED that is soldered to a Raspberry Pi header.
        "remote_host": "",            # e.g. "10.0.0.10" (the head Pi)
        "remote_port": 8083,
        "remote_token": "",           # must match LED_TOKEN on the server ("" = no auth)
    },
    "spotter": {
        # The wide-angle face spotter (Jabra PanaCast on the chest touchscreen).
        # Gives the face tracker a real bearing across ~180 degrees for people
        # the head camera cannot see; falls back to the chest ultrasonics when
        # off or blind. See inmoov/wide_spotter.py.
        "enabled": True,
        "device": 0,                  # V4L2 index; the PanaCast's second node does not stream
        "detect_hz": 4.0,             # decode+detect rate — acquisition is a human-scale event
        "detect_width": 1920,         # downscale before detection (~10 ms/frame)
    },
    "hardware": {
        # When True, the app boots with the shared hardware RELEASED to another
        # owner (MyRobotLab): I2C/PCA9685 servos, the USB audio card, and the Pi
        # camera are all handed off. Toggle live from the admin panel; the choice
        # persists so an event set-up survives a reboot. See /api/handoff.
        "released": False,
    },
    "audit": {
        # Audit (dry run) mode: FRED stays fully interactive through the web
        # panel — the brain answers, the transcript fills, speech is synthesised
        # and the on-screen face lip-syncs to it — but nothing physical happens.
        # No audio reaches the speaker, no pulse reaches a servo, and the cart
        # will not drive. The servo readouts keep tracking the commanded angles,
        # so the panel shows exactly what he *would* have done.
        #
        # Distinct from "hardware.released" (which hands the devices to
        # MyRobotLab and disables FRED's own controls) and from "sound.enabled"
        # (a plain mute, which also kills the lip-sync). Persists across reboots
        # so a bench session can't be undone by a power cycle. See /api/audit.
        "enabled": False,
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
    "cart": {
        # The hoverboard drive base. Its Pico plugs into the chest Pi, so this
        # rides on the `display` connection above rather than repeating the
        # host/token — one chest Pi, one address to keep correct.
        #
        # Speeds are in the hoverboard firmware's own units (see the cart repo):
        # ~0.64 wheel RPM per unit, and the firmware caps host commands at 300
        # speed / 250 steer regardless of what is asked for here.
        "enabled": False,             # master switch; off until the cart is wired up
        "speed": 150,                 # forward/back speed for spoken commands (~73 RPM)
        "turn": 150,                  # steer magnitude for spoken turns
        "step_seconds": 1.5,          # how long one spoken "forward"/"back" runs;
                                      # capped at 5s by inmoov/cart.py regardless
    },
    "brain": {
        # Which LLM answers the open questions the command matcher didn't catch.
        #   "auto"   — Claude when reachable, the local model when it isn't. FRED
        #              goes to events without reliable WiFi, so this is the point.
        #   "claude" — cloud only; open questions fail with no internet.
        #   "local"  — never touches the network.
        # The local model runs on Ollama (127.0.0.1:11434) against the Intel Arc
        # iGPU via Vulkan. See inmoov/local_brain.py for why this model and not a
        # reasoning one. Live switch from the admin panel; persists.
        "backend": "auto",
        # Let Claude look things up when the answer depends on something current.
        # Claude-only: the tool runs on Anthropic's side, so the local model never
        # gets it. Billed per search, hence a switch. The location is what he
        # assumes when nobody names a place ("what's the weather?") — set it to
        # wherever he actually is.
        "web_search": True,
        "web_search_location": {"city": "", "region": "", "country": "US",
                                "timezone": ""},
        "local_model": "qwen2.5:3b",
        "local_host": "http://127.0.0.1:11434",
        # Whether FRED may look through his eye camera to answer a question.
        # Cloud-only: the local model is text-only, so this does nothing on the
        # "local" backend. A look costs ~1000 image tokens (about $0.0016 at
        # Haiku's input price) plus a camera start, so it is rate-limited rather
        # than free — vision_min_seconds is the shortest gap between two looks.
        "vision": True,
        "vision_min_seconds": 12.0,
    },
    "event": {
        # Flipped when FRED is in front of the public. One switch rather than
        # three, because "he is at an event" is one fact and capping his answer
        # length, his driving speed and his spending are all consequences of it.
        # See inmoov/event.py. Live from the panel; persists.
        "enabled": False,
        "max_words": 25,          # spoken answers; the mic is deaf while he talks
        "cart_speed": 120,        # of cart.py's 300, on a machine weighing 350 lb
    },
    "voice": {
        "enabled": False,             # start the "Fred" wake-word listener at boot
        "gain": 1.0,                  # software mic boost (analog capture is already
                                      # maxed); 1.0 = off, ~2-3 for quiet/distant speech
        # Which Vosk model transcribes what he hears. A directory name under
        # models/. The small one is 68 MB and fast; the larger ones are more
        # accurate on the case that actually fails — children, at distance, in a
        # noisy hall — at a real CPU cost on a machine already giving ~4 cores
        # to the wide camera. Measure with tools/bench_asr.py before switching.
        "asr_model": "vosk-model-en-us-0.22-lgraph",
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
        "gap_lead_in": 0.12,          # seconds of silence prepended to the *second and
                                      # later* clips of one reply. Each sentence is its
                                      # own aplay and so reopens the device; without
                                      # this they lose their opening syllable. Much
                                      # smaller than lead_in — it covers a reopen, not
                                      # a cold start, and it is silence between the
                                      # sentences of one answer. 0 = off.
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

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
import sys
import threading
from pathlib import Path

# make the project root importable when run as `python web/app.py`
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flask import Flask, jsonify, render_template, request  # noqa: E402

from inmoov.servo_controller import ServoController, CONFIG_PATH, load_config  # noqa: E402

app = Flask(__name__)

_config = load_config()
_ctrl = ServoController(config=_config)      # auto mock when /dev/i2c-1 absent
_lock = threading.Lock()                     # serialize hardware access


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
    return {"mock": _ctrl.mock, "servos": servos}


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    return jsonify(_state())


@app.post("/api/move")
def api_move():
    data = request.get_json(force=True)
    name = data["name"]
    angle = float(data["angle"])
    raw = bool(data.get("raw", False))          # calibration: ignore soft limits
    with _lock:
        actual = _ctrl.set_angle(name, angle, enforce_limits=not raw)
    return jsonify({"name": name, "angle": actual})


@app.post("/api/rest")
def api_rest():
    with _lock:
        _ctrl.rest()
    return jsonify(_state())


@app.post("/api/relax")
def api_relax():
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


@app.post("/api/save")
def api_save():
    """Persist the current (possibly re-recorded) config to disk."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(_config, f, indent=2)
        f.write("\n")
    return jsonify({"saved": str(CONFIG_PATH)})


if __name__ == "__main__":
    print(f"Serving InMoov control panel — mode: {'MOCK' if _ctrl.mock else 'LIVE'}")
    app.run(host="0.0.0.0", port=8080, threaded=True)

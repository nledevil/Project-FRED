"""Remote smart-sensor hub for FRED.

The design offloads the timing-critical work to the sensor node rather than the
Pi: a microcontroller (a Raspberry Pi Pico W in the robot's stomach) reads the
ultrasonic + PIR sensors, does the echo timing / debouncing / filtering itself,
and pushes *clean, high-level* readings and edge events to the Pi. The Pi never
touches a GPIO or times a pulse — Linux scheduling jitter would wreck an HC-SR04
echo measurement anyway; an RP2040 hardware timer does it accurately.

This module is transport-agnostic. The same JSON payload arrives either over
WiFi (``POST /api/sensors/ingest``) or, when the node has no network, over USB
serial — both funnel into :meth:`SensorHub.ingest`. The hub keeps the latest
reading per (node, sensor), tracks last-seen for offline detection, logs edge
events to the conversation log, and calls an optional callback so behaviours
(auto-greet on approach, wake-on-motion) can react.

Payload shape the node sends::

    {
      "node": "stomach",
      "uptime_ms": 123456,
      "readings": {
        "dist_center": {"type": "distance", "cm": 82.5},
        "pir_center":  {"type": "motion", "active": true}
      },
      "events": [
        {"sensor": "dist_center", "event": "approach", "cm": 82.5},
        {"sensor": "pir_center",  "event": "motion_start"}
      ]
    }

``readings`` is the current value of each sensor (published every cycle);
``events`` are edges the node decided are worth reporting. Either may be omitted.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque

try:
    import serial  # pyserial — only needed for the USB-serial fallback
    _SERIAL_ERR = None
except Exception as exc:  # noqa: BLE001 - absent just disables the serial reader
    serial = None
    _SERIAL_ERR = exc


class SensorHub:
    """Latest sensor state + event fan-out. Thread-safe; fed by any transport."""

    def __init__(self, on_event=None, log=None, offline_after: float = 10.0):
        # on_event(node: str, event: dict) -> None  (optional behaviour hook)
        self._on_event = on_event
        self._log = log                       # ConversationLog (optional)
        self._offline_after = float(offline_after)
        self._nodes: dict[str, dict] = {}
        # A short rolling history, so "did anyone come by?" is answerable at all.
        # ``_nodes`` only ever holds the *latest* value of each reading, which
        # says nothing about a person who walked past thirty seconds ago.
        self._recent: deque = deque(maxlen=64)      # (t, node, event dict)
        self._lock = threading.Lock()

    def ingest(self, payload: dict, transport: str = "wifi") -> bool:
        """Accept one payload from a node. Returns True if it was usable."""
        if not isinstance(payload, dict):
            return False
        node = str(payload.get("node") or "unknown")
        now = time.time()
        with self._lock:
            n = self._nodes.setdefault(node, {"readings": {}})
            n["last_seen"] = now
            n["transport"] = transport
            n["uptime_ms"] = payload.get("uptime_ms")
            readings = payload.get("readings")
            if isinstance(readings, dict):
                for name, r in readings.items():
                    if isinstance(r, dict):
                        r = dict(r)
                        r["t"] = now
                        n["readings"][str(name)] = r
        # Events call out to the log / behaviour hook — do it outside the lock.
        events = payload.get("events")
        if isinstance(events, list):
            for ev in events:
                if isinstance(ev, dict):
                    self._handle_event(node, ev)
        return True

    # ---- events -----------------------------------------------------------
    def _handle_event(self, node: str, ev: dict) -> None:
        with self._lock:
            self._recent.append((time.time(), node, dict(ev)))
        msg = self._describe(node, ev)
        if self._log is not None and msg:
            self._log.event(msg)
        if self._on_event is not None:
            try:
                self._on_event(node, ev)
            except Exception as exc:  # noqa: BLE001 - a bad hook must not drop the event
                print(f"[SensorHub] on_event failed: {exc}")

    @staticmethod
    def _describe(node: str, ev: dict) -> str:
        """A short human line for the transcript, or '' to skip logging."""
        kind = str(ev.get("event", "")).lower()
        sensor = ev.get("sensor", "?")
        if kind == "approach":
            cm = ev.get("cm")
            near = f" ({round(float(cm))} cm)" if cm is not None else ""
            return f"Someone approached the {node}{near}."
        if kind == "depart":
            return f"They stepped away from the {node}."
        if kind == "motion_start":
            return f"Motion detected ({node}/{sensor})."
        if kind == "motion_stop":
            return ""            # too chatty to log the end of every motion burst
        # Unknown/other event types: log generically so nothing is silently lost.
        return f"Sensor event: {node}/{sensor} {kind}".strip()

    def recent_events(self, within: float = 300.0) -> list[dict]:
        """Edge events from the last ``within`` seconds, oldest first.

        Each entry is the node's event dict plus ``node`` and ``ago`` (seconds).
        This is what makes "did someone walk past?" answerable — the readings
        alone only describe this instant.
        """
        cutoff = time.time() - float(within)
        now = time.time()
        with self._lock:
            hits = [(t, n, e) for (t, n, e) in self._recent if t >= cutoff]
        return [dict(e, node=n, ago=round(now - t, 1)) for (t, n, e) in hits]

    # ---- introspection ----------------------------------------------------
    def state(self) -> dict:
        """Snapshot for /api/sensors and /api/state, with computed online flags."""
        now = time.time()
        with self._lock:
            nodes = {}
            for node, n in self._nodes.items():
                age = now - n.get("last_seen", 0.0)
                nodes[node] = {
                    "online": age <= self._offline_after,
                    "last_seen_ago": round(age, 1),
                    "transport": n.get("transport"),
                    "uptime_ms": n.get("uptime_ms"),
                    "readings": n.get("readings", {}),
                }
        return {"nodes": nodes, "offline_after": self._offline_after}


class SerialSensorReader:
    """Reads newline-delimited JSON payloads from a USB-serial sensor node.

    This is the no-network fallback: a Pico (W) plugged into the Pi over USB
    prints one JSON object per line to its USB CDC; we read them off
    ``/dev/ttyACM*`` and feed the same :meth:`SensorHub.ingest`. Reconnects on
    unplug/replug. A no-op (with a printed hint) when pyserial isn't installed.
    """

    def __init__(self, hub: SensorHub, port: str = "/dev/ttyACM0",
                 baud: int = 115200, log=None):
        self._hub = hub
        self._port = port
        self._baud = int(baud)
        self._log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def available(self) -> bool:
        return serial is not None

    def start(self) -> bool:
        if serial is None:
            print(f"[SerialSensorReader] pyserial not installed "
                  f"({_SERIAL_ERR}); serial sensor node disabled "
                  f"(pip install pyserial to enable).")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sensor-serial",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with serial.Serial(self._port, self._baud, timeout=1.0) as ser:
                    print(f"[SerialSensorReader] reading {self._port} @ {self._baud}")
                    while not self._stop.is_set():
                        line = ser.readline()
                        if not line:
                            continue                 # timeout tick — loop and re-check stop
                        try:
                            payload = json.loads(line.decode("utf-8", "replace").strip())
                        except (ValueError, UnicodeError):
                            continue                 # boot banners / partial lines — ignore
                        self._hub.ingest(payload, transport="serial")
            except (OSError, serial.SerialException) as exc:  # unplugged / no such port
                if not self._stop.is_set():
                    print(f"[SerialSensorReader] {self._port} unavailable ({exc}); "
                          f"retrying in 3s")
                    self._stop.wait(3.0)

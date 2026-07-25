"""Live sensor readout overlaid on whatever animation is running.

Two halves of one seam, same shape as voice_state.py:

* ``MetricsPublisher`` lives in display_control.py. The sensor relay hands it
  each payload as it arrives off the Pico, and the admin toggle sets ``enabled``.
  It writes both into a /dev/shm doc.
* ``MetricsHud`` lives in the *animation child*, which polls the doc and paints
  the panel into its frame just before the blit.

It has to be split that way round: the animation child mmaps /dev/fb0 and owns
it exclusively, so the daemon cannot draw anything itself while a child is alive.
Every animation therefore gets the overlay by calling ``hud.draw(frame)``, which
is two lines each and keeps all the layout in one place.

/dev/shm rather than a pipe: the child can die and respawn without the daemon
knowing, and there's no buffer anyone can fail to drain and wedge. Writes are
atomic (write-temp-then-rename) so the child never paints a half-written frame.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np

from font5x7 import CHAR_H, draw_text, text_width

METRICS_PATH = Path("/dev/shm/inmoov-metrics.json")

STALE_AFTER = 10.0          # seconds without a payload before we say NO DATA
NO_ECHO_CM = 399.0          # at or above this the node means "nothing in range"

# Layout. The font is 5x7, so scale 2 gives 10x14 glyphs — legible on the 7"
# panel from across a room without eating the animation.
SCALE = 2
LINE_H = 18
PAD = 8
MARGIN = 12
DIM = 0.25                  # how far to knock back the animation behind the panel

TITLE_RGB = (90, 150, 190)
VALUE_RGB = (120, 210, 255)
ALERT_RGB = (230, 120, 90)


def publish(data: dict, path: Path = METRICS_PATH) -> None:
    """Atomically replace the shared metrics doc."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)          # readers see old or new, never half
    except OSError:
        pass                           # no /dev/shm: the overlay just never appears


class MetricsPublisher:
    """Owns the published doc. One instance, in the daemon."""

    def __init__(self, enabled: bool = False, path: Path = METRICS_PATH):
        self._path = path
        self._doc: dict = {"enabled": bool(enabled), "readings": {}, "t": 0.0}
        self._flush()

    def _flush(self) -> None:
        publish(self._doc, self._path)

    def set_enabled(self, enabled: bool) -> bool:
        self._doc["enabled"] = bool(enabled)
        self._flush()
        return self._doc["enabled"]

    @property
    def enabled(self) -> bool:
        return bool(self._doc.get("enabled"))

    def set_payload(self, payload: dict) -> None:
        """Called by the relay for every payload off the sensor node."""
        readings = payload.get("readings")
        self._doc["readings"] = readings if isinstance(readings, dict) else {}
        self._doc["node"] = payload.get("node") or ""
        self._doc["t"] = time.monotonic()
        self._flush()


class MetricsFeed:
    """Reads the shared doc. Cheap enough to poll every frame.

    Re-parses only when the mtime changes, so an idle panel costs one stat().
    """

    def __init__(self, path: Path = METRICS_PATH):
        self._path = path
        self._mtime = -1.0
        self._data: dict = {}

    def poll(self) -> dict:
        try:
            mtime = os.stat(self._path).st_mtime
        except OSError:
            return self._data          # daemon hasn't published yet
        if mtime != self._mtime:
            try:
                self._data = json.loads(self._path.read_text())
                self._mtime = mtime
            except (OSError, ValueError):
                pass                   # mid-rename or corrupt: keep the last good
        return self._data


def _label(name: str) -> str:
    """Sensor key as the 5x7 font can render it: no underscores in the glyph set.

    Deliberately not prettified beyond that — the names come from the firmware's
    ULTRASONICS/PIR_NAME config and can be renamed there, so echoing them
    verbatim keeps the panel honest about which reading is which.
    """
    return name.replace("_", "-").upper()[:12]


def _format(name: str, r: dict) -> tuple[str, tuple]:
    """One panel line for one reading, as (text, colour)."""
    kind = str(r.get("type", ""))
    label = _label(name)
    if kind == "distance":
        cm = r.get("cm")
        if cm is None:
            return f"{label} --", ALERT_RGB
        if float(cm) >= NO_ECHO_CM:
            return f"{label} ---", TITLE_RGB      # nothing in range: not a fault
        return f"{label} {round(float(cm))}CM", VALUE_RGB
    if kind == "motion":
        if r.get("warming"):
            return f"{label} WARMUP", TITLE_RGB
        active = bool(r.get("active"))
        return f"{label} {'ACTIVE' if active else 'IDLE'}", \
               ALERT_RGB if active else VALUE_RGB
    return f"{label} ?", TITLE_RGB


class MetricsHud:
    """Paints the sensor panel into an animation's frame. Never raises."""

    def __init__(self, feed: MetricsFeed | None = None):
        self._feed = feed or MetricsFeed()

    def lines(self) -> list[tuple[str, tuple]]:
        d = self._feed.poll()
        if not d.get("enabled"):
            return []
        readings = d.get("readings") or {}
        age = time.monotonic() - float(d.get("t") or 0.0)
        if not readings or age > STALE_AFTER:
            return [("SENSORS NO DATA", ALERT_RGB)]
        out = [("SENSORS", TITLE_RGB)]
        for name in sorted(readings):
            r = readings[name]
            if isinstance(r, dict):
                out.append(_format(name, r))
        return out

    def draw(self, frame: np.ndarray) -> None:
        """Overlay the panel bottom-left. Call once per frame, before the blit."""
        try:
            lines = self.lines()
            if not lines:
                return
            H, W = frame.shape[:2]
            w = max(text_width(t, SCALE) for t, _ in lines) + PAD * 2
            h = len(lines) * LINE_H - (LINE_H - CHAR_H * SCALE) + PAD * 2
            x0, y1 = MARGIN, H - MARGIN
            y0, x1 = max(y1 - h, 0), min(x0 + w, W)

            # Knock the animation back behind the panel: draw_text *adds* into a
            # glow-composited frame, so without this the text washes out over a
            # bright core instead of reading as a panel.
            frame[y0:y1, x0:x1] *= DIM
            for i, (text, rgb) in enumerate(lines):
                draw_text(frame, text, x0 + PAD, y0 + PAD + i * LINE_H, rgb, SCALE)
            # Clip only what we touched. Callers blit with .astype(uint8), and an
            # un-clipped add would wrap a bright pixel round to black.
            np.clip(frame[y0:y1, x0:x1], 0, 255, out=frame[y0:y1, x0:x1])
        except Exception:
            pass          # a broken overlay must never take the animation down

"""Drag a slider, a servo moves — the head panel's servo column, on the robot.

Deliberately the same behaviour as the web panel's sliders rather than a second
opinion about how servos should be driven, because the two are used in the same
session on the same hardware and any difference between them reads as a fault:

* **A 60 ms trailing throttle per servo, last value always sent.** Same number
  as `move()` in index.html. Without it a drag at 30fps is 30 requests a second,
  each one a hop to the head, and the servo lags the finger by the queue.
* **A touched slider ignores the poll for a moment.** The poller is 2 s behind
  and would otherwise yank the knob back to where the servo *was* mid-drag.
  index.html calls this `t.userTs`; same idea, same reason.

**Limits come from the brain, never from here.** Each slider spans that servo's
calibrated min..max out of `/api/state`, so this page cannot ask for an angle
calibration says is unsafe — and when you recalibrate, the sliders change with
it. There is no raw/ignore-limits mode on purpose: that belongs on the panel
where you can see what you are doing, not on a screen you are prodding blind
while leaning over the robot.

The two states that stop a move are shown rather than discovered by pressing:
a released **handoff** (the brain returns 409 — MyRobotLab owns the bus) and
**audit** mode (every call succeeds and nothing moves, which is exactly the
thing you would otherwise waste ten minutes on).
"""
from __future__ import annotations

import time


ROW_Y0 = 100                    # first slider row
ROW_H = 52
# Rows were drawn at ROW_Y0 + i * ROW_H with nothing stopping them: the seventh
# ran past the bottom of a 480px panel and the eighth was drawn entirely off it.
# Six wired servos hid that, but arms and hands are more, and a servo you cannot
# see is one you cannot centre.
PER_PAGE = 6
# Left of centre, because REST already owns the bottom right (648..776) and the
# pager landed exactly on top of it — REST is checked first, so the pager would
# have been undrawable *and* untappable in the same spot.
# Squeezed between the last row (ends 412) and REST, which owns 648..776 at
# 404..452. Right-aligned to 640 keeps it clear of REST horizontally; the hint
# below moves down to 456 so the two do not share a line — in the widest theme
# that hint runs to x=509 and would have sat under the buttons.
PAGER_Y = 418
PAGER_X_RIGHT = 640
LABEL_X = 24
TRACK_X0, TRACK_X1 = 232, 640   # the draggable span
VALUE_X = 776
KNOB_W = 18
TRACK_H = 10

POST_EVERY = 0.06               # 60 ms, matching index.html's throttle
HOLD_POLL_OFF = 1.5             # ignore the poller for this long after a touch

REST_BTN = (648, 404, 776, 452)

# Friendly names. The font has no underscore, and "HEAD TILT FB" is what the
# label would become anyway — better to choose the words than to mangle them.
LABELS = {
    "eye_x": "EYES L/R",
    "eye_y": "EYES U/D",
    "head_tilt_fb": "NOD",
    "head_tilt_lr": "TILT",
    "jaw": "JAW",
    "neck": "NECK",
}


class ServosPage:
    title = "SERVOS"

    def __init__(self):
        self._page = 0
        self._total = 0                          # servos the brain reported
        self._local: dict[str, float] = {}       # what the finger says, per servo
        self._touched_at: dict[str, float] = {}
        self._sent_at: dict[str, float] = {}
        self._pending: dict[str, float] = {}

    @staticmethod
    def _servos(snap: dict) -> dict:
        return ((snap.get("nuc") or {}).get("servos")) or {}

    @staticmethod
    def _blocked(snap: dict) -> str:
        """Why a move would not reach the hardware, or "" if it would."""
        nuc = snap.get("nuc") or {}
        if not nuc:
            return "NO LINK TO BRAIN"
        if (nuc.get("handoff") or {}).get("released"):
            return "HANDED OFF TO MYROBOTLAB"
        if (nuc.get("audit") or {}).get("servo_audit"):
            return "AUDIT MODE - NOTHING WILL MOVE"
        link = nuc.get("servo_link") or {}
        if link.get("online") is False:
            return "BRAIN CANT REACH THE SERVOS"
        if nuc.get("mock"):
            return "MOCK MODE - NO HARDWARE"
        return ""

    def _angle(self, name: str, s: dict) -> float:
        """What to show: the finger while it is moving, otherwise the robot."""
        held = time.monotonic() - self._touched_at.get(name, 0.0) < HOLD_POLL_OFF
        if held and name in self._local:
            return self._local[name]
        current = s.get("current")
        if current is None:
            current = s.get("rest_angle", 0.0)
        return float(current)

    def _throttled(self, name: str, angle: float, net) -> None:
        now = time.monotonic()
        if now - self._sent_at.get(name, 0.0) >= POST_EVERY:
            self._send(name, angle, net)
        else:
            self._pending[name] = angle      # flushed on 'up', or by the next move

    def _send(self, name: str, angle: float, net) -> None:
        self._sent_at[name] = time.monotonic()
        net.post_move(name, angle)

    # ---- drawing ----------------------------------------------------------
    def view(self, snap: dict) -> dict:
        """The page as data: one entry per servo on this page, plus why a move
        would not reach the hardware.

        Split out of draw() so the Qt panel shows the same rows without a second
        copy of the ordering rule (known servos in physical order, then anything
        the brain reports that this page has never heard of, so a new servo
        appears by itself rather than being invisible until someone edits a
        list) or of the reasons a drag would do nothing.
        """
        servos = self._servos(snap)
        blocked = self._blocked(snap)
        if not servos:
            return {"rows": [], "blocked": blocked or "BRAIN SENT AN EMPTY LIST",
                    "empty": True, "page": self._page, "pages": 1}

        order = [n for n in LABELS if n in servos] + \
                [n for n in sorted(servos) if n not in LABELS]
        self._total = len(order)
        rows = []
        start = self._page * PER_PAGE
        for name in order[start:start + PER_PAGE]:
            sv = servos[name]
            rows.append({
                "name": name,
                "label": LABELS.get(name, name.replace("_", "-")),
                "angle": float(self._angle(name, sv)),
                "lo": float(sv.get("min_angle", 0)),
                "hi": float(sv.get("max_angle", 180)),
                "rest": float(sv.get("rest_angle", self._angle(name, sv))),
            })
        pages = max(1, -(-self._total // PER_PAGE))
        return {"rows": rows, "blocked": blocked, "empty": False,
                "page": min(self._page, pages - 1), "pages": pages}

    def set_angle(self, name: str, angle: float, net, final: bool = False) -> None:
        """Move one servo, wherever the drag came from."""
        snap = net.snapshot()
        sv = self._servos(snap).get(name)
        if not sv or self._blocked(snap):
            return
        angle = round(float(angle))
        self._local[name] = angle
        self._touched_at[name] = time.monotonic()
        if final:
            # Send it outright rather than leaving it to the throttle: a slider
            # that stops short of where the finger left is the bug everyone
            # notices.
            self._send(name, angle, net)
            self._pending.pop(name, None)
        else:
            self._throttled(name, angle, net)

    def turn_page(self, delta: int) -> None:
        pages = max(1, -(-self._total // PER_PAGE))
        self._page = max(0, min(pages - 1, self._page + delta))


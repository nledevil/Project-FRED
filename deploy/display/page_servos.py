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

import menu_ui as ui

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
        self._drag: str | None = None       # servo captured by the current touch
        self._local: dict[str, float] = {}  # what the finger says, per servo
        self._touched_at: dict[str, float] = {}
        self._sent_at: dict[str, float] = {}
        self._pending: dict[str, float] = {}
        self._rest = ui.Button(*REST_BTN, "REST", scale=2)
        self._rows: list[tuple[str, int]] = []   # (name, row y) from the last draw
        self._pager = ui.Pager(PER_PAGE, PAGER_Y, x_right=PAGER_X_RIGHT)
        self._total = 0                          # servos the brain reported

    # ---- helpers ----------------------------------------------------------
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

    @staticmethod
    def _to_angle(s: dict, x: int) -> float:
        lo, hi = float(s["min_angle"]), float(s["max_angle"])
        frac = (x - TRACK_X0) / float(TRACK_X1 - TRACK_X0)
        return lo + max(0.0, min(1.0, frac)) * (hi - lo)

    @staticmethod
    def _to_x(s: dict, angle: float) -> int:
        lo, hi = float(s["min_angle"]), float(s["max_angle"])
        frac = 0.0 if hi <= lo else (float(angle) - lo) / (hi - lo)
        return int(TRACK_X0 + max(0.0, min(1.0, frac)) * (TRACK_X1 - TRACK_X0))

    # ---- input ------------------------------------------------------------
    def on_touch(self, kind: str, x: int, y: int, net) -> None:
        if kind == "down":
            if self._rest.hit(x, y):
                net.post_rest()
                self._local.clear()          # the robot is about to disagree
                return
            if self._paged(kind, x, y):
                return                       # paging is not the start of a drag
            self._drag = self._row_at(y)
        if self._drag is None:
            return                           # a move/up that never started on a row

        name = self._drag
        snap = net.snapshot()
        s = self._servos(snap).get(name)
        if s and not self._blocked(snap):
            # 'up' carries a position too, and it can be newer than the last
            # 'move' — the driver may update the coordinate in the same report
            # that lifts the pen. Applying it is what makes the servo end where
            # the finger left rather than a few degrees behind it.
            angle = round(self._to_angle(s, x))
            self._local[name] = angle
            self._touched_at[name] = time.monotonic()
            if kind == "up":
                self._pending[name] = angle
            else:
                self._throttled(name, angle, net)

        if kind == "up":
            # Always flush: the throttle may have been holding the last value,
            # and a slider that stops short of where the finger left is the bug
            # everyone notices.
            if name in self._pending:
                self._send(name, self._pending.pop(name), net)
            self._drag = None

    def _paged(self, kind: str, x: int, y: int) -> bool:
        """True if the pager took the tap. Checked before the sliders, because a
        press that starts on the pager must not also capture a slider."""
        return self._pager.on_touch(kind, x, y, self._total)

    def _row_at(self, y: int) -> str | None:
        for name, row_y in self._rows:
            if row_y <= y < row_y + ROW_H:
                return name
        return None

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
    def draw(self, frame, snap: dict) -> None:
        servos = self._servos(snap)
        blocked = self._blocked(snap)

        if not servos:
            ui.text(frame, "NO SERVOS REPORTED", LABEL_X, ROW_Y0, ui.BAD_INK, 2)
            ui.text(frame, blocked or "BRAIN SENT AN EMPTY LIST",
                    LABEL_X, ROW_Y0 + 28, ui.DIM_INK, 2)
            self._rows = []
            return

        # Known servos first, in a sensible physical order, then anything the
        # brain reports that this page has never heard of — a new servo should
        # appear by itself rather than be invisible until someone edits a list.
        order = [n for n in LABELS if n in servos] + \
                [n for n in sorted(servos) if n not in LABELS]

        self._total = len(order)
        self._rows = []
        for i, name in enumerate(self._pager.slice(order)):
            s = servos[name]
            y = ROW_Y0 + i * ROW_H
            self._rows.append((name, y))
            angle = self._angle(name, s)

            ui.text(frame, LABELS.get(name, name.replace("_", "-")),
                    LABEL_X, y + 12, ui.INK, 2)

            mid = y + ROW_H // 2
            ui.fill(frame, TRACK_X0, mid - TRACK_H // 2, TRACK_X1, mid + TRACK_H // 2,
                    ui.PANEL)
            ui.border(frame, TRACK_X0, mid - TRACK_H // 2, TRACK_X1,
                      mid + TRACK_H // 2, ui.EDGE)
            # A tick at rest, so "where should this be" is answerable at a glance.
            rest_x = self._to_x(s, s.get("rest_angle", angle))
            ui.fill(frame, rest_x - 1, mid - 14, rest_x + 1, mid + 14, ui.EDGE)

            kx = self._to_x(s, angle)
            ink = ui.DIM_INK if blocked else ui.INK
            ui.fill(frame, TRACK_X0, mid - TRACK_H // 2, kx, mid + TRACK_H // 2,
                    ui.PANEL_ON)
            ui.fill(frame, kx - KNOB_W // 2, mid - 16, kx + KNOB_W // 2, mid + 16, ink)

            label = f"{round(angle)}"
            ui.text(frame, label, VALUE_X - ui.text_width(label, 2), y + 12,
                    ui.DIM_INK if blocked else ui.INK, 2)

        self._rest.draw(frame, ink=ui.DIM_INK if blocked else ui.INK)
        self._pager.draw(frame, self._total)
        if blocked:
            ui.text(frame, blocked, LABEL_X, 424, ui.BAD_INK, 2)
        else:
            ui.text(frame, "DRAG TO MOVE - LIMITS ARE THE CALIBRATED ONES",
                    LABEL_X, 456, ui.DIM_INK, 1)

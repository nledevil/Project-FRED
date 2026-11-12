"""The TEST tab: one button that answers "is FRED all there?".

For the fair: the robot has just been carried in and plugged back together,
and this is the screen on him. RUN SELF TEST asks the brain to check every
part (inmoov/selftest.py on the NUC does the asking — this page only starts
it and shows the answer), then FRED says the verdict out loud.

Problems first: the list is sorted fails, then warnings, then passes, because
on a 7" screen the one thing worth reading must not be on page three.

MOTION TEST wiggles each joint and so takes two taps — the first arms it and
says what is about to happen, the second within CONFIRM_S sends it.

Like every page here, this decides the words and the colours; the QML only
lays them out (SelfTestPage.qml).
"""
from __future__ import annotations

import time

import theme

_P = theme.palette()
_HEX = lambda rgb: "#%02x%02x%02x" % tuple(int(c) for c in rgb)   # noqa: E731
INK, DIM = _HEX(_P["INK"]), _HEX(_P["DIM_INK"])
OK, WARN, BAD = _HEX(_P["OK_INK"]), _HEX(_P["WARN_INK"]), _HEX(_P["BAD_INK"])

_ORDER = {"fail": 0, "warn": 1, "pass": 2, "skip": 3}
_INK = {"fail": BAD, "warn": WARN, "pass": OK, "skip": DIM}


class SelfTestPage:
    title = "TEST"
    PER_PAGE = 5
    CONFIRM_S = 4.0         # how long a first MOTION TEST tap stays armed
    PENDING_S = 6.0         # how long "starting" may wait for the poll to agree

    def __init__(self):
        self._page = 0
        self._armed_at = 0.0
        self._pending_at = 0.0          # a run was asked for, not seen yet
        self._pending_motion = False

    # ---- taps --------------------------------------------------------------
    def run(self, net) -> None:
        self._page = 0
        self._armed_at = 0.0
        self._pending_at, self._pending_motion = time.monotonic(), False
        net.post_selftest()

    def motion_tap(self, net) -> None:
        now = time.monotonic()
        if now - self._armed_at <= self.CONFIRM_S:
            self._armed_at = 0.0
            self._page = 0
            self._pending_at, self._pending_motion = now, True
            net.post_motiontest()
        else:
            self._armed_at = now

    def turn_page(self, delta: int) -> None:
        self._page += int(delta)          # clamped in view(), against the rows

    # ---- what is on screen -------------------------------------------------
    def view(self, snap: dict) -> dict:
        now = time.monotonic()
        armed = now - self._armed_at <= self.CONFIRM_S
        base = {"motionLabel": "TAP AGAIN TO MOVE" if armed else "MOTION TEST",
                "armed": armed}
        if snap.get("nuc") is None:
            return {**base, "headline": "NO LINK TO THE BRAIN", "ink": BAD,
                    "sub": "THE TEST RUNS ON THE NUC", "running": False,
                    "rows": [], "page": 0, "pages": 1}

        st, mt = snap.get("selftest") or {}, snap.get("motiontest") or {}
        motion = (mt.get("started_at") or 0) > (st.get("started_at") or 0)
        cur = mt if motion else st
        # The poll is every 2 s, so for a moment after a tap the brain's answer
        # is still the previous run. Say STARTING rather than show stale rows.
        if self._pending_at and (cur.get("running") and motion == self._pending_motion
                                 or now - self._pending_at > self.PENDING_S):
            self._pending_at = 0.0
        running = bool(cur.get("running")) or bool(self._pending_at)

        c = cur.get("counts") or {}
        if self._pending_at and not cur.get("running"):
            headline, ink, sub = "STARTING...", DIM, ""
        elif running:
            what = "MOVING EACH JOINT" if motion else "TESTING"
            headline = f"{what}... {cur.get('done', 0)} OF {cur.get('total') or '?'}"
            ink, sub = DIM, "STAND CLEAR" if motion else ""
        elif not cur.get("results"):
            headline, ink = "NOT RUN YET", DIM
            sub = "PRESS RUN SELF TEST AFTER PUTTING HIM TOGETHER"
        elif motion:
            headline = "MOTION TEST: PROBLEMS" if c.get("fail") else "MOTION TEST DONE"
            ink = BAD if c.get("fail") else OK
            sub = "DID EVERY JOINT MOVE?"
        else:
            ready = cur.get("verdict") == "ready"
            headline, ink = ("READY", OK) if ready else ("NOT READY", BAD)
            sub = (f"{c.get('pass', 0)} PASS  {c.get('warn', 0)} WARN  "
                   f"{c.get('fail', 0)} FAIL")

        rows = sorted(cur.get("results") or [],
                      key=lambda r: (_ORDER.get(r.get("status"), 9), r.get("seq", 0)))
        rows = [{"name": str(r.get("name", "")).upper(),
                 "state": str(r.get("status", "")).upper(),
                 "ink": _INK.get(r.get("status"), DIM),
                 "detail": str(r.get("detail") or "").upper()}
                for r in rows]
        pages = max(1, -(-len(rows) // self.PER_PAGE))
        self._page = max(0, min(self._page, pages - 1))
        start = self._page * self.PER_PAGE
        return {**base, "headline": headline, "ink": ink, "sub": sub,
                "running": running, "rows": rows[start:start + self.PER_PAGE],
                "page": self._page, "pages": pages}

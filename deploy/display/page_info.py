"""What this robot is: names, addresses, versions, and what the brain runs on.

Deliberately not the STATUS tab. That one answers *is it working* — up, down,
stale, too quiet — and colours things when they are wrong. This answers *what is
it*, which is the set of facts you otherwise get by SSHing into one of three
machines and running four commands, usually while standing next to the robot
with no laptop in your hands.

The line that earns the tab is INFERENCE. Ollama probes for the iGPU exactly
once at startup and keeps that answer for the life of the process, so losing the
race at boot means running the whole uptime on CPU at about a 25th of the prompt
speed — FRED taking half a minute to answer, with nothing anywhere saying why.
That check has been a journal grep over SSH since it was written down. It is a
line on a screen now, and it is the first thing on the page.

Nothing here is a control, so nothing here is drawn as a button. On this panel
a filled box with a border means tappable; a readout has to go without one.
"""
from __future__ import annotations

import menu_ui as ui

X0, X1 = ui.X0, ui.X1
COL2 = 300
HEAD_Y = 104
ROW_Y = 136
LINE_H = 26




def _named(machine: dict) -> str:
    """The machine's own hostname, but only when it is worth reading.

    Both Pis ship as "DietPi", so printing it says nothing about which one this
    is. If someone has renamed one, that is worth seeing — so it appears then
    and stays out of the way otherwise.
    """
    name = str(machine.get("hostname") or "").strip()
    return f" - {name.upper()}" if name and name.lower() != "dietpi" else ""


def _uptime(seconds) -> str:
    """Short and glanceable: 3D 04H, 5H 12M, 47M. Never a bare number of seconds."""
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    if s < 3600:
        return f"{s // 60}M"
    if s < 86400:
        return f"{s // 3600}H {(s % 3600) // 60:02d}M"
    return f"{s // 86400}D {(s % 86400) // 3600:02d}H"


class InfoPage:
    title = "INFO"

    def on_touch(self, kind: str, x: int, y: int, net) -> None:
        """Nothing to press — the page is a readout. The poller refreshes it."""

    # How many lines fit between the heading and the bottom of the panel. The
    # numpy renderer stops drawing past y=470 and says nothing about what it
    # dropped; this page has twelve lines today and grows a line per network
    # interface, so it was one address away from losing something quietly.
    PER_PAGE = 13

    def view(self, snap: dict) -> dict:
        """The page, split into screenfuls, so nothing is silently truncated."""
        rows = self.rows(snap)
        pages = max(1, -(-len(rows) // self.PER_PAGE))
        self._page = max(0, min(getattr(self, "_page", 0), pages - 1))
        start = self._page * self.PER_PAGE
        return {"rows": rows[start:start + self.PER_PAGE],
                "page": self._page, "pages": pages}

    def turn_page(self, delta: int, total: int) -> None:
        pages = max(1, -(-total // self.PER_PAGE))
        self._page = max(0, min(pages - 1, getattr(self, "_page", 0) + delta))

    def rows(self, snap: dict) -> list:
        """The page as data: (label, value, ink) per line.

        Split out of draw() so the Qt panel shows the same page without a second
        copy of the logic that decides, for instance, that inference fell back
        to the CPU — a 25x slowdown that is silent everywhere else.
        """
        who = snap.get("whoami") or {}
        rows: list[tuple[str, str, tuple]] = []

        # --- the reason this page exists ------------------------------------
        inference = who.get("inference") or {}
        library = str(inference.get("library") or "")
        if not library:
            rows.append(("INFERENCE", "UNKNOWN", ui.DIM_INK))
        elif library.lower() == "cpu":
            # Not a cosmetic detail: this is the 25x slowdown, and it is silent.
            rows.append(("INFERENCE", "CPU - GPU WAS MISSED AT BOOT", ui.BAD_INK))
        else:
            gpu = str(inference.get("name") or library).upper()
            total = str(inference.get("total") or "")
            rows.append(("INFERENCE", f"{library.upper()} {gpu} {total}".strip(),
                         ui.OK_INK))

        brain = who.get("brain") or {}
        if brain:
            active = str(brain.get("active") or "?").upper()
            model = str(brain.get("model") or "")
            rows.append(("BRAIN", f"{active} - LOCAL {model}".strip(" -"), ui.INK))

        rev = who.get("revision") or {}
        if rev.get("commit"):
            line = f"{rev['commit']} ON {str(rev.get('branch') or '?')}"
            if rev.get("dirty"):
                line += " - UNCOMMITTED"
            rows.append(("VERSION", line,
                         ui.WARN_INK if rev.get("dirty") else ui.INK))

        rows.append(("", "", ui.INK))            # a blank line, not a rule

        # --- the three machines ---------------------------------------------
        rows.append((str(who.get("hostname") or "BRAIN").upper(),
                     f"UP {_uptime(who.get('uptime_s'))}", ui.INK))
        for addr in (who.get("addresses") or []):
            rows.append(("", f"{addr.get('interface', '?')} {addr.get('address', '?')}",
                         ui.DIM_INK))

        # Labelled by role, not by hostname. Both Pis answer to "DietPi", so the
        # hostname put the same word on two rows and identified neither — and
        # the useful label only appeared when the machine was unreachable, which
        # is exactly backwards. A renamed Pi still gets its name shown, on the
        # right, where it is information rather than a heading that repeats.
        head = snap.get("head") or {}
        rows.append(("HEAD PI",
                     f"10.0.0.10 - UP {_uptime(head.get('uptime_s'))}"
                     f"{_named(head)}" if head else "10.0.0.10 - NO LINK",
                     ui.INK if head else ui.BAD_INK))

        display = (snap.get("chest") or {}).get("display") or {}
        rows.append(("CHEST PI",
                     f"10.0.0.11 - UP {_uptime(display.get('uptime_s'))}"
                     f"{_named(display)}" if display else "10.0.0.11 - THIS PI",
                     ui.INK))

        ap = snap.get("hotspot") or {}
        if ap.get("configured"):
            on = bool(ap.get("enabled"))
            clients = ap.get("clients")
            detail = str(ap.get("ssid") or "?")
            if on and clients is not None:
                detail += f" - {clients} JOINED"
            elif not on:
                detail += " - OFF"
            rows.append(("ACCESS POINT", detail, ui.OK_INK if on else ui.DIM_INK))

        return rows

    def draw(self, frame, snap: dict) -> None:
        who = snap.get("whoami") or {}
        rows = self.rows(snap)

        ui.text(frame, "WHAT THIS ROBOT IS", X0, HEAD_Y, ui.INK, 3)
        if not who:
            ui.empty(frame, "NO LINK TO THE BRAIN", ROW_Y + 10)
            ui.text(frame, "NAMES AND VERSIONS COME FROM THE NUC", X0,
                    ROW_Y + 10 + LINE_H, ui.DIM_INK, 1)
            return

        y = ROW_Y
        for label, value, ink in rows:
            if label:
                ui.text(frame, ui.fit(label, COL2 - X0 - 8), X0, y, ui.DIM_INK, 2)
            if value:
                ui.text(frame, ui.fit(value, X1 - COL2), COL2, y, ink, 2)
            y += LINE_H
            if y > 470:
                break

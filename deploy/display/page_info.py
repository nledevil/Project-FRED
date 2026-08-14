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

X0, X1 = 24, 776
COL2 = 300
HEAD_Y = 104
ROW_Y = 136
LINE_H = 26


def _fit(s: str, width_px: int, scale: int = 2) -> str:
    if ui.text_width(s, scale) <= width_px:
        return s
    n = max(1, width_px // max(1, ui.text_width("M", scale)))
    return s[:n]


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

    def draw(self, frame, snap: dict) -> None:
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

        head = snap.get("head") or {}
        rows.append((str(head.get("hostname") or "HEAD").upper(),
                     f"10.0.0.10 - UP {_uptime(head.get('uptime_s'))}"
                     if head else "10.0.0.10 - NO LINK",
                     ui.INK if head else ui.BAD_INK))

        display = (snap.get("chest") or {}).get("display") or {}
        rows.append((str(display.get("hostname") or "CHEST").upper(),
                     f"10.0.0.11 - UP {_uptime(display.get('uptime_s'))}"
                     if display else "10.0.0.11 - THIS PI",
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

        ui.text(frame, "WHAT THIS ROBOT IS", X0, HEAD_Y, ui.INK, 3)
        if not who:
            ui.text(frame, "NO LINK TO THE BRAIN", X0, ROW_Y + 10, ui.BAD_INK, 2)
            ui.text(frame, "NAMES AND VERSIONS COME FROM THE NUC", X0,
                    ROW_Y + 10 + LINE_H, ui.DIM_INK, 1)
            return

        y = ROW_Y
        for label, value, ink in rows:
            if label:
                ui.text(frame, _fit(label, COL2 - X0 - 8), X0, y, ui.DIM_INK, 2)
            if value:
                ui.text(frame, _fit(value, X1 - COL2), COL2, y, ink, 2)
            y += LINE_H
            if y > 470:
                break

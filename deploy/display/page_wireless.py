"""The access point, switched on from the robot itself.

FRED goes to venues with no usable WiFi, and the panel is how you drive him — so
he can serve his own network. Join ``fred`` from a phone and the panel is at
192.168.50.1:8080.

The AP runs on the **brain**, not this Pi: only the NUC's onboard Intel radio can
host one (the USB card added for the job turned out to do managed/monitor only,
which is a client and nothing else). So this page is a remote control, and the
one thing it must do well is be honest when the brain cannot be reached — a
button that silently does nothing is worse than a button that says why.

This tab briefly also did Bluetooth pairing, for an Xbox controller. That was
removed on 2026-08-12 once the 8BitDo's 2.4 GHz dongle was settled on as the
drive controller: the Bluetooth link flapped badly (hundreds of connect cycles),
and a pairing UI for hardware nobody uses is a surface to maintain and a thing
to explain. xpadneo is uninstalled and the controller's gamepad.py profile went
with it; reviving any of this means starting from the notes in SERVICE.md.
"""
from __future__ import annotations

import menu_ui as ui
from font5x7 import text_width

X0, X1 = 24, 776
HEAD_Y = 104
AP_BTN = (X0, 150, X1, 240)
INFO_Y = 268
LINE_H = 30


def _fit(s: str, width_px: int, scale: int = 2) -> str:
    if text_width(s, scale) <= width_px:
        return s
    n = max(1, width_px // max(1, text_width("M", scale)))
    return s[:n]


class WirelessPage:
    title = "WIRELESS"

    def __init__(self):
        self._ap = ui.Button(*AP_BTN, "", scale=4)
        self._note = ""

    # ---- input ------------------------------------------------------------
    def on_touch(self, kind: str, x: int, y: int, net) -> None:
        if kind != "down" or not self._ap.hit(x, y):
            return
        want = not self._enabled(net.snapshot())
        net.post_hotspot(want)
        # The brain takes a few seconds to bring hostapd up (the radio walks
        # through COUNTRY_UPDATE first), so say something now rather than leave
        # the button looking ignored until the next poll.
        self._note = "TURNING THE ACCESS POINT " + ("ON..." if want else "OFF...")

    @staticmethod
    def _enabled(snap: dict) -> bool:
        return bool((snap.get("hotspot") or {}).get("enabled"))

    # ---- drawing ----------------------------------------------------------
    def draw(self, frame, snap: dict) -> None:
        ap = snap.get("hotspot")
        ui.text(frame, "ACCESS POINT", X0, HEAD_Y, ui.INK, 3)
        ui.text(frame, "HOSTED ON THE BRAIN", X0 + 290, HEAD_Y + 8, ui.DIM_INK, 1)

        if ap is None:
            self._ap.draw(frame, on=False, ink=ui.DIM_INK, label="NO LINK")
            ui.text(frame, "CANNOT REACH THE BRAIN", X0, INFO_Y, ui.BAD_INK, 2)
            ui.text(frame, "THE ACCESS POINT IS SERVED BY THE NUC, NOT THIS PI",
                    X0, INFO_Y + LINE_H, ui.DIM_INK, 1)
            return
        if not ap.get("configured", True):
            self._ap.draw(frame, on=False, ink=ui.DIM_INK, label="N/A")
            ui.text(frame, _fit(str(ap.get("error") or "NOT INSTALLED").upper(), X1 - X0),
                    X0, INFO_Y, ui.DIM_INK, 2)
            return

        on = bool(ap.get("enabled"))
        if self._note and ap.get("enabled") == (self._note.endswith("ON...")):
            self._note = ""                 # the brain caught up with the request
        self._ap.draw(frame, on=on, ink=ui.OK_INK if on else ui.INK,
                      label="ON" if on else "OFF")

        rows = [(f"SSID {str(ap.get('ssid') or '?').upper()}", ui.INK)]
        if on:
            rows.append((f"PANEL {ap.get('address', '?')}:8080", ui.INK))
            clients = ap.get("clients")
            if clients is not None:
                rows.append((f"{clients} CLIENT{'' if clients == 1 else 'S'} JOINED",
                             ui.DIM_INK))
        else:
            rows.append(("JOIN THIS FROM A PHONE WHEN THERE IS NO WIFI", ui.DIM_INK))
        if ap.get("error"):
            rows.append((_fit(str(ap["error"]).upper(), X1 - X0), ui.BAD_INK))
        for i, (text, ink) in enumerate(rows):
            ui.text(frame, text, X0, INFO_Y + i * LINE_H, ink, 2)

        ui.text(frame, "TAP TO TURN " + ("OFF" if on else "ON"), X0,
                INFO_Y + len(rows) * LINE_H + 6, ui.DIM_INK, 1)
        if self._note:
            ui.text(frame, self._note, X0, 448, ui.WARN_INK, 1)

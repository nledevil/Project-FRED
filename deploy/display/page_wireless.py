"""The access point, switched on and configured from the robot itself.

FRED goes to venues with no usable WiFi, and the panel is how you drive him — so
he can serve his own network. Join ``fred`` from a phone and the panel is at
192.168.50.1:8080.

The AP runs on the **brain**, not this Pi: only the NUC's onboard Intel radio can
host one (the USB card added for the job turned out to do managed/monitor only,
which is a client and nothing else). So this page is a remote control, and the
one thing it must do well is be honest when the brain cannot be reached — a
button that silently does nothing is worse than a button that says why.

**The name and password are editable here.** They used to mean an SSH session
and a text editor as root, which is exactly the situation this tab exists to
avoid — and it matters more since the AP started coming up at boot with a route
to the internet behind it: its password is now the outermost credential on the
robot, and one shipped as a published default is not a credential at all.

Editing needs the brain, unlike the rest of this menu, because the config and
the radio both live there. The buttons say so when it is unreachable rather than
opening a keyboard whose DONE could never land.

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
from keyboard import Keyboard

X0, X1 = 24, 776
HEAD_Y = 104
AP_BTN = (X0, 138, 380, 214)
SSID_BTN = (396, 138, X1, 172)
PSK_BTN = (396, 180, X1, 214)
INFO_Y = 240
LINE_H = 30

SSID_MAX = 32
PSK_MIN, PSK_MAX = 8, 63


def _fit(s: str, width_px: int, scale: int = 2) -> str:
    if text_width(s, scale) <= width_px:
        return s
    n = max(1, width_px // max(1, text_width("M", scale)))
    return s[:n]


class WirelessPage:
    title = "WIRELESS"

    def __init__(self):
        self._ap = ui.Button(*AP_BTN, "", scale=4)
        self._ssid_btn = ui.Button(*SSID_BTN, "NAME", scale=2)
        self._psk_btn = ui.Button(*PSK_BTN, "PASSWORD", scale=2)
        self._note = ""
        self._kb: Keyboard | None = None
        self._editing = ""              # "ssid" or "psk" while the keyboard is up
        # Held between the two edits so the pair can be sent together: the
        # helper on the brain writes both keys at once, and a save that sent one
        # would need the other anyway.
        self._pending_ssid = ""
        self._pending_psk = ""

    # ---- input ------------------------------------------------------------
    def on_touch(self, kind: str, x: int, y: int, net) -> None:
        if self._kb is not None:
            self._kb.on_touch(kind, x, y)
            if not self._kb.active:
                self._finish_edit(net)
            return
        if kind != "down":
            return
        snap = net.snapshot()
        ap = snap.get("hotspot")
        if self._ap.hit(x, y):
            want = not self._enabled(snap)
            net.post_hotspot(want)
            # The brain takes a few seconds to bring hostapd up (the radio walks
            # through COUNTRY_UPDATE first), so say something now rather than
            # leave the button looking ignored until the next poll.
            self._note = "TURNING THE ACCESS POINT " + ("ON..." if want else "OFF...")
            return
        if not ap:
            self._note = "NO LINK TO THE BRAIN - CANNOT EDIT"
            return
        if self._ssid_btn.hit(x, y):
            self._editing = "ssid"
            self._kb = Keyboard("NETWORK NAME (SSID)",
                                self._pending_ssid or str(ap.get("ssid") or ""),
                                max_len=SSID_MAX, min_len=1)
        elif self._psk_btn.hit(x, y):
            self._editing = "psk"
            # Never seeded with the current password: the brain does not send it
            # here, and it should not — a passphrase on the wire so a screen can
            # prefill a field is a copy made for a convenience nobody needs.
            self._kb = Keyboard(f"PASSWORD ({PSK_MIN} TO {PSK_MAX} CHARACTERS)",
                                self._pending_psk,
                                max_len=PSK_MAX, min_len=PSK_MIN)

    def _finish_edit(self, net) -> None:
        kb, self._kb = self._kb, None
        field, self._editing = self._editing, ""
        if kb is None or kb.cancelled:
            return
        if field == "ssid":
            self._pending_ssid = kb.text
        else:
            self._pending_psk = kb.text
        if self._pending_ssid and self._pending_psk:
            net.post_hotspot_config(self._pending_ssid, self._pending_psk)
            self._note = "SAVING - THE ACCESS POINT WILL RESTART"
            self._pending_psk = ""      # do not keep it in memory for the session
        elif field == "ssid":
            self._note = "NOW SET A PASSWORD TO SAVE THE CHANGE"
        else:
            self._note = "NOW CONFIRM THE NETWORK NAME TO SAVE"

    @staticmethod
    def _enabled(snap: dict) -> bool:
        return bool((snap.get("hotspot") or {}).get("enabled"))

    # ---- drawing ----------------------------------------------------------
    def draw(self, frame, snap: dict) -> None:
        if self._kb is not None:
            self._kb.draw(frame)
            return

        ap = snap.get("hotspot")
        ui.text(frame, "ACCESS POINT", X0, HEAD_Y, ui.INK, 3)
        ui.text(frame, "HOSTED ON THE BRAIN", X0 + 290, HEAD_Y + 8, ui.DIM_INK, 1)

        if ap is None:
            self._ap.draw(frame, on=False, ink=ui.DIM_INK, label="NO LINK")
            self._ssid_btn.draw(frame, ink=ui.DIM_INK)
            self._psk_btn.draw(frame, ink=ui.DIM_INK)
            ui.text(frame, "CANNOT REACH THE BRAIN", X0, INFO_Y, ui.BAD_INK, 2)
            ui.text(frame, "THE ACCESS POINT IS SERVED BY THE NUC, NOT THIS PI",
                    X0, INFO_Y + LINE_H, ui.DIM_INK, 1)
            return
        if not ap.get("configured", True):
            self._ap.draw(frame, on=False, ink=ui.DIM_INK, label="N/A")
            self._ssid_btn.draw(frame, ink=ui.DIM_INK)
            self._psk_btn.draw(frame, ink=ui.DIM_INK)
            ui.text(frame, _fit(str(ap.get("error") or "NOT INSTALLED").upper(), X1 - X0),
                    X0, INFO_Y, ui.DIM_INK, 2)
            return

        on = bool(ap.get("enabled"))
        if self._note.startswith("TURNING") and ap.get("enabled") == self._note.endswith("ON..."):
            self._note = ""                 # the brain caught up with the request
        self._ap.draw(frame, on=on, ink=ui.OK_INK if on else ui.INK,
                      label="ON" if on else "OFF")
        self._ssid_btn.draw(frame, ink=ui.INK,
                            label="NAME" + (" *" if self._pending_ssid else ""))
        self._psk_btn.draw(frame, ink=ui.INK, label="PASSWORD")

        rows = [(f"SSID {str(ap.get('ssid') or '?').upper()}", ui.INK)]
        if on:
            rows.append((f"PANEL {ap.get('address', '?')}:8080", ui.INK))
            clients = ap.get("clients")
            if clients is not None:
                rows.append((f"{clients} CLIENT{'' if clients == 1 else 'S'} JOINED",
                             ui.DIM_INK))
        else:
            rows.append(("JOIN THIS FROM A PHONE WHEN THERE IS NO WIFI", ui.DIM_INK))
        if self._pending_ssid:
            rows.append((f"PENDING NAME {self._pending_ssid.upper()}", ui.WARN_INK))
        if ap.get("error"):
            rows.append((_fit(str(ap["error"]).upper(), X1 - X0), ui.BAD_INK))
        for i, (text, ink) in enumerate(rows):
            ui.text(frame, text, X0, INFO_Y + i * LINE_H, ink, 2)

        ui.text(frame, "TAP ON/OFF TO SWITCH IT - NAME AND PASSWORD TO CHANGE THEM",
                X0, INFO_Y + len(rows) * LINE_H + 6, ui.DIM_INK, 1)
        if self._note:
            ui.text(frame, self._note, X0, 452, ui.WARN_INK, 1)

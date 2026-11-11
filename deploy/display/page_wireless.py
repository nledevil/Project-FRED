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


SSID_MAX = 32
PSK_MIN, PSK_MAX = 8, 63




class WirelessPage:
    # "WIFI", not "WIRELESS": seven tabs leaves 100px each, and WIRELESS
    # needed 94 of them. The short name is also the one people say.
    title = "WIFI"

    def __init__(self):
        self._pending_ssid = ""
        self._pending_psk = ""
        self._note = ""
    def _enabled(snap: dict) -> bool:
        return bool((snap.get("hotspot") or {}).get("enabled"))

    def view(self, snap: dict) -> dict:
        """The page as data: the switch, the lines under it, and the note.

        Split out of draw() so the Qt panel says the same things about an
        access point it cannot reach — and keeps the same rule that the
        password is never seeded from the brain.
        """
        ap = snap.get("hotspot")
        if ap is None:
            return {"live": False, "label": "NO LINK", "on": False, "ink": "dim",
                    "rows": [{"text": "CANNOT REACH THE BRAIN", "ink": "dim"},
                             {"text": "THE ACCESS POINT IS SERVED BY THE NUC, "
                                      "NOT THIS PI", "ink": "dim"}],
                    "hint": "", "note": self._note, "editable": False}
        if not ap.get("configured", True):
            return {"live": False, "label": "N/A", "on": False, "ink": "dim",
                    "rows": [{"text": str(ap.get("error") or "NOT INSTALLED").upper(),
                              "ink": "dim"}],
                    "hint": "", "note": self._note, "editable": False}

        on = bool(ap.get("enabled"))
        if self._note.startswith("TURNING") and on == self._note.endswith("ON..."):
            self._note = ""             # the brain caught up with the request

        rows = [{"text": f"SSID {str(ap.get('ssid') or '?').upper()}", "ink": "ink"}]
        if on:
            rows.append({"text": f"PANEL {ap.get('address', '?')}:8080", "ink": "ink"})
            clients = ap.get("clients")
            if clients is not None:
                rows.append({"text": f"{clients} CLIENT"
                                     f"{'' if clients == 1 else 'S'} JOINED",
                             "ink": "dim"})
        else:
            rows.append({"text": "JOIN THIS FROM A PHONE WHEN THERE IS NO WIFI",
                         "ink": "dim"})
        if self._pending_ssid:
            rows.append({"text": f"PENDING NAME {self._pending_ssid.upper()}",
                         "ink": "warn"})
        if ap.get("error"):
            rows.append({"text": str(ap["error"]).upper(), "ink": "bad"})

        return {"live": True, "on": on, "label": "ON" if on else "OFF",
                "ink": "ok" if on else "ink", "editable": True,
                "ssidLabel": "NAME" + (" *" if self._pending_ssid else ""),
                "rows": rows, "note": self._note,
                "hint": "TAP ON/OFF TO SWITCH IT - NAME AND PASSWORD TO CHANGE THEM"}

    def toggle(self, net) -> None:
        want = not self._enabled(net.snapshot())
        net.post_hotspot(want)
        # The brain takes a few seconds to bring hostapd up (the radio walks
        # through COUNTRY_UPDATE first), so say something now rather than leave
        # the button looking ignored until the next poll.
        self._note = "TURNING THE ACCESS POINT " + ("ON..." if want else "OFF...")

    def editor(self, field: str, snap: dict) -> dict:
        """What an editor for this field should start with and accept."""
        ap = snap.get("hotspot") or {}
        if field == "ssid":
            return {"field": "ssid", "title": "NETWORK NAME (SSID)",
                    "value": self._pending_ssid or str(ap.get("ssid") or ""),
                    "maxLen": SSID_MAX, "minLen": 1}
        # Never seeded with the current password: the brain does not send it
        # here, and it should not.
        return {"field": "psk", "title": f"PASSWORD ({PSK_MIN} TO {PSK_MAX} CHARACTERS)",
                "value": "", "maxLen": PSK_MAX, "minLen": PSK_MIN}

    def commit(self, field: str, text: str, net) -> None:
        """Apply an edited value, wherever it was typed."""
        if field == "ssid":
            self._pending_ssid = text
        else:
            self._pending_psk = text
        if self._pending_ssid and self._pending_psk:
            net.post_hotspot_config(self._pending_ssid, self._pending_psk)
            self._note = "SAVING - THE ACCESS POINT WILL RESTART"
            self._pending_psk = ""      # do not keep it in memory for the session
        elif field == "ssid":
            self._note = "NOW SET A PASSWORD TO SAVE THE CHANGE"
        else:
            self._note = "NOW CONFIRM THE NETWORK NAME TO SAVE"

    # ---- drawing ----------------------------------------------------------

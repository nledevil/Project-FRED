"""The NUC's "fred" access point — read its state, switch it on and off.

FRED goes to venues with no usable WiFi. The panel is how you drive him, so it
has to be reachable without one: switch this on and the robot serves its own
network, joinable from any phone.

**Why the onboard Intel radio and not the USB card.** The USB card's driver
(rtl8xxxu, RTL8188EUS) advertises ``managed`` and ``monitor`` only — it cannot
be an access point at all. The Intel advertises ``AP``. So on 2026-08-12 the two
cards swapped jobs: the USB one became the house-WiFi client, which is all a
client needs, and the Intel was freed to serve this.

Starting it needs root, which the panel does not have, so it goes through
``sudo systemctl`` under a rule (``/etc/sudoers.d/fred-hotspot``) that permits
exactly start/stop/restart of exactly this unit. Reading state needs no
privilege at all.

The AP is deliberately **not** enabled at boot: it parks a radio in AP mode, and
most of the time the house WiFi is there and this is not wanted.
"""
from __future__ import annotations

import re
import subprocess

UNIT = "fred-hotspot.service"
CONF = "/etc/hostapd/fred.conf"
INTERFACE = "wlo1"
ADDRESS = "192.168.50.1"


def _run(args: list[str], timeout: float = 10.0) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def configured() -> bool:
    """Is the AP installed at all? False on a machine without the unit."""
    code, _ = _run(["systemctl", "cat", UNIT], timeout=5.0)
    return code == 0


def ssid() -> str:
    try:
        with open(CONF) as fh:
            for line in fh:
                if line.startswith("ssid="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def enabled() -> bool:
    code, out = _run(["systemctl", "is-active", UNIT], timeout=5.0)
    return out.strip() == "active"


def clients() -> int | None:
    """How many stations have associated. None if we cannot tell.

    Read from the kernel rather than from hostapd's control socket: `iw` needs
    no privilege and no extra plumbing, and the count is all the panel shows.
    """
    code, out = _run(["/usr/sbin/iw", "dev", INTERFACE, "station", "dump"], timeout=5.0)
    if code != 0:
        return None
    return len(re.findall(r"^Station ", out, re.M))


def state() -> dict:
    if not configured():
        return {"configured": False, "enabled": False,
                "error": "hotspot not installed on this machine"}
    on = enabled()
    out = {"configured": True, "enabled": on, "ssid": ssid(),
           "address": ADDRESS, "interface": INTERFACE}
    if on:
        out["clients"] = clients()
    return out


def set_enabled(on: bool) -> dict:
    """Start or stop the AP. Returns the state afterwards, not the intent."""
    if not configured():
        return {"error": "hotspot not installed on this machine"}
    verb = "start" if on else "stop"
    code, out = _run(["sudo", "-n", "/usr/bin/systemctl", verb, UNIT], timeout=25.0)
    if code != 0:
        # Almost always the sudoers rule missing, which is worth saying plainly
        # rather than returning a bare non-zero.
        return {"error": f"could not {verb} the hotspot: {out.strip()[:120]}"}
    return state()

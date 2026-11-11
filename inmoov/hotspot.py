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
SSID_FILE = "/etc/hostapd/fred.ssid"   # the name only; see ssid()
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
    """The AP's network name, without needing root.

    Three sources, in order of how much they can be trusted to answer:

    * ``fred.ssid``, which fred-ap-config writes alongside the real config for
      exactly this purpose. The config itself is 0600 — it holds the passphrase
      — and the panel does not run as root.
    * the config, for a machine set up before that file existed, or when this
      happens to be running privileged.
    * the radio, which knows only while the AP is actually up.
    """
    for path in (SSID_FILE, CONF):
        try:
            with open(path) as fh:
                for line in fh:
                    if path == SSID_FILE:
                        return line.strip()
                    if line.startswith("ssid="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            continue
    code, out = _run(["/usr/sbin/iw", "dev", INTERFACE, "info"], timeout=5.0)
    if code == 0:
        for line in out.splitlines():
            if line.strip().startswith("ssid "):
                return line.strip().split(None, 1)[1]
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


HELPER = "/usr/local/sbin/fred-ap-config"
SSID_MAX = 32
PSK_MIN, PSK_MAX = 8, 63


def check_config(new_ssid: str, passphrase: str) -> str:
    """"" if these are usable, else why not. Same rules the helper enforces.

    Checked twice on purpose. Here so the panel and the touchscreen can say
    "that is too short" without a round trip through sudo, and again in the
    helper because the helper runs as root and must not trust its caller.
    """
    if not 1 <= len(new_ssid.encode()) <= SSID_MAX:
        return f"the network name must be 1 to {SSID_MAX} characters"
    if not PSK_MIN <= len(passphrase) <= PSK_MAX:
        return f"the password must be {PSK_MIN} to {PSK_MAX} characters"
    for name, value in (("network name", new_ssid), ("password", passphrase)):
        if any(ord(c) < 0x20 or ord(c) > 0x7E for c in value):
            return f"the {name} must be plain printable characters"
    return ""


def configure(new_ssid: str, passphrase: str) -> dict:
    """Set the AP's SSID and passphrase; restart it only if it is already up.

    The passphrase goes to the helper on stdin, never in argv — /proc/<pid>/cmdline
    is world readable, so an argument would expose it to every account on the box
    for the life of the call.
    """
    if not configured():
        return {"error": "hotspot not installed on this machine"}
    why = check_config(new_ssid, passphrase)
    if why:
        return {"error": why}
    try:
        p = subprocess.run(["sudo", "-n", HELPER],
                           input=f"{new_ssid}\n{passphrase}\n",
                           capture_output=True, text=True, timeout=30.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"could not run the hotspot helper: {exc}"}
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip()[:160]
        return {"error": f"could not save the hotspot settings: {detail}"}
    # Never echo the passphrase back. The caller just typed it and the panel is
    # reachable over the network; a reply carrying it is a copy nobody asked for.
    return {**state(), "saved": True, "restarted": "restarted" in (p.stdout or "")}

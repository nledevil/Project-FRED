"""FRED's own WiFi connection — the one that gets him onto a school's network.

Distinct from ``inmoov.hotspot``, which is the access point FRED *hosts* so a
phone can reach the panel when there is no WiFi at all. This is the other
radio: the one that joins somebody else's network and carries the internet the
brain needs for Claude.

Everything privileged happens in /usr/local/sbin/fred-uplink under one narrow
sudoers rule — see deploy/hotspot-nuc/fred-uplink for why it is shaped that
way. This module only calls it and hands back what it said.

The scan is deliberately not cached here. It takes a couple of seconds because
a radio has to sweep the band, and a stale list of networks at a venue is worse
than a slow one: you tap a name that is no longer there and get a failure you
cannot explain.
"""
from __future__ import annotations

import json
import subprocess

HELPER = "/usr/local/sbin/fred-uplink"


def _run(verb: str, payload: dict | None = None, timeout: float = 30.0):
    """Call the helper and parse its JSON, or return None if it could not run."""
    try:
        p = subprocess.run(["sudo", "-n", HELPER, verb],
                           capture_output=True, text=True, timeout=timeout,
                           input=json.dumps(payload) if payload is not None else None)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0 and not (p.stdout or "").strip():
        return None
    try:
        return json.loads(p.stdout or "")
    except ValueError:
        return None


def state() -> dict:
    """What FRED is connected to, and what he remembers how to join."""
    got = _run("status", timeout=15.0)
    if got is None:
        # Almost always the sudoers rule missing, which is worth saying plainly
        # rather than showing an empty panel that looks like no WiFi hardware.
        return {"available": False, "error": "cannot run fred-uplink (sudoers?)",
                "ssid": "", "saved": []}
    got["available"] = not got.get("error")
    return got


def scan() -> list:
    """Networks in range, strongest first, one row per name."""
    got = _run("scan", timeout=45.0)
    return got if isinstance(got, list) else []


def join(ssid: str, password: str) -> dict:
    """Add a network and switch to it, keeping the ones already saved."""
    ssid = (ssid or "").strip()
    if not ssid:
        return {"ok": False, "error": "no network name"}
    # Checked here as well as in the helper so the panel can say "that is too
    # short" without a round trip through sudo and a netplan apply.
    if password and not (8 <= len(password) <= 63):
        return {"ok": False, "error": "a WPA password is 8 to 63 characters"}
    got = _run("join", {"ssid": ssid, "password": password or ""}, timeout=90.0)
    if got is None:
        return {"ok": False, "error": "cannot run fred-uplink (sudoers?)"}
    return got


def forget(ssid: str) -> dict:
    """Drop a saved network. The helper refuses to drop the last one."""
    got = _run("forget", {"ssid": (ssid or "").strip()}, timeout=90.0)
    if got is None:
        return {"ok": False, "error": "cannot run fred-uplink (sudoers?)"}
    return got

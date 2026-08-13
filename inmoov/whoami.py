"""Who this robot is: names, addresses, versions, and what the brain is running on.

The status panels answer *is it working*. This answers *what is it* — the facts
you currently get by SSHing into one of three machines and running four commands,
usually while standing next to the robot with no laptop.

The one worth the page on its own is the inference device. Ollama probes for a
GPU exactly once at startup and keeps that answer for the life of the process,
so losing the race at boot means running the whole uptime on CPU at roughly a
25th of the prompt speed — which reads as FRED taking half a minute to answer and
nothing anywhere saying why. The check for it is a journal grep, and until now
that meant a terminal. It costs 28 ms, so it can just be on a screen.

Everything here is read-only and cheap. The two things that are neither cheap
nor changeable are cached: the git revision cannot change without a redeploy
restarting this process, and the inference device cannot change without Ollama
restarting.
"""
from __future__ import annotations

import re
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Full paths: /usr/sbin and /sbin are not on the default PATH for this user, and
# `which ip` reports missing while the binary is sitting right there.
IP = "/usr/sbin/ip"
JOURNALCTL = "/usr/bin/journalctl"

_DEVICE_TTL = 30.0
_cache: dict = {"device": None, "device_at": 0.0, "revision": None}


def _run(args: list[str], timeout: float = 5.0) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout or ""


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def uptime_s() -> float | None:
    """This machine's uptime. /proc/uptime rather than the `uptime` command —
    it is a two-number file, on both the NUC and the Pis."""
    try:
        with open("/proc/uptime") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def addresses() -> list[dict]:
    """Every interface with an address, as the robot's own wiring names them.

    Reported per interface rather than as one "the IP", because this machine
    genuinely has three that mean different things: the robot LAN it serves, the
    access point it hosts, and the house WiFi it borrows.
    """
    out = []
    for line in _run([IP, "-brief", "-4", "addr", "show"]).splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[0] == "lo":
            continue
        addr = parts[2].split("/")[0]
        out.append({"interface": parts[0], "address": addr, "up": parts[1] == "UP"})
    return out


def revision() -> dict:
    """The deployed git revision, so "is this the code I pushed?" is answerable.

    Cached: it cannot change without a redeploy, and a redeploy restarts this.
    """
    if _cache["revision"] is None:
        short = _run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"]).strip()
        dirty = bool(_run(["git", "-C", str(ROOT), "status", "--porcelain"]).strip())
        branch = _run(["git", "-C", str(ROOT), "rev-parse", "--abbrev-ref", "HEAD"]).strip()
        _cache["revision"] = {"commit": short, "branch": branch, "dirty": dirty}
    return dict(_cache["revision"])


def inference_device() -> dict:
    """What Ollama decided to run on, off its own startup log.

    ``library`` is "Vulkan" when it found the Arc iGPU and "cpu" when it lost the
    boot race to the driver. There is no API for this — Ollama logs the answer
    once and never mentions it again — so the journal is the only source.
    """
    now = time.monotonic()
    if _cache["device"] is not None and (now - _cache["device_at"]) < _DEVICE_TTL:
        return dict(_cache["device"])
    out = _run([JOURNALCTL, "-u", "ollama", "-b", "--no-pager"], timeout=8.0)
    device = {"library": "", "name": "", "total": ""}
    for line in out.splitlines():
        if "inference compute" not in line:
            continue
        # Regex rather than splitting on whitespace: the interesting values are
        # quoted and contain spaces -- total="22.7 GiB" would otherwise come
        # back as "22.7 and the unit would be a separate field.
        for key in device:
            m = re.search(rf'\b{key}=(?:"([^"]*)"|(\S+))', line)
            if m:
                device[key] = (m.group(1) or m.group(2) or "").strip()
        break
    _cache["device"], _cache["device_at"] = device, now
    return dict(device)


def state(brain=None, hotspot=None) -> dict:
    """Everything above in one reply, for the panels.

    ``brain`` and ``hotspot`` are passed in rather than imported so this module
    stays a plain reader with no opinion about the rest of the app.
    """
    out = {
        "hostname": hostname(),
        "uptime_s": uptime_s(),
        "addresses": addresses(),
        "revision": revision(),
        "inference": inference_device(),
    }
    if brain is not None:
        # brain.status() is what /api/brain already serves; only the identifying
        # parts are kept here, since health belongs on the STATUS tab.
        try:
            st = brain.status()
        except Exception:                       # noqa: BLE001 — identity, not health
            st = {}
        out["brain"] = {"backend": st.get("backend", ""),
                        "active": st.get("active", ""),
                        "model": st.get("local_model") or st.get("model", "")}
    if hotspot is not None:
        out["hotspot"] = hotspot
    return out

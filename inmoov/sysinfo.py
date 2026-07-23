"""System facts FRED can talk about — the current time/date, this host's network
identity, and how warm his processor is running.

Two consumers, mirroring the command/brain split:
  * the local matcher calls ``spoken_time`` / ``spoken_date`` / ``spoken_ip`` for
    instant, offline answers to "what time is it", "what's the date", "what's
    your IP";
  * ``context_block`` is injected into Claude's system prompt each turn so *any*
    phrasing routed to the AI ("do you know what day it is?") is answered with
    the real, current values instead of a guess.

Clock values are read fresh on every call (never cached at import), so the time
is always current. Pure standard library + ``netinfo`` — safe to import anywhere.
"""
from __future__ import annotations

import socket
from datetime import datetime
from pathlib import Path

from . import netinfo

# The SoC's own thermal sensor. Reading sysfs keeps this module subprocess-free
# (``vcgencmd measure_temp`` reports the same value to a tenth of a degree), so
# it stays importable anywhere. On a Pi this is thermal_zone0 / "cpu-thermal";
# elsewhere it may be absent or name a different sensor, hence the type check.
_THERMAL = Path("/sys/class/thermal")


def _ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 11 -> '11th', 23 -> '23rd'."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def spoken_time(now: datetime | None = None) -> str:
    """A natural, speakable time: 'It's 3:05 PM.'"""
    now = now or datetime.now()
    # %-I strips the leading zero from the hour ('3' not '03').
    return f"It's {now.strftime('%-I:%M %p')}."


def spoken_date(now: datetime | None = None) -> str:
    """A natural, speakable date: 'Today is Sunday, July 5th, 2026.'"""
    now = now or datetime.now()
    return (f"Today is {now.strftime('%A, %B')} {_ordinal(now.day)}, "
            f"{now.strftime('%Y')}.")


def spoken_datetime(now: datetime | None = None) -> str:
    """Date and time together, for a general 'what's the time and date' ask."""
    now = now or datetime.now()
    return f"{spoken_date(now)} {spoken_time(now)}"


def spoken_ip() -> str:
    """This host's primary IP, spelled out digit-by-digit so the TTS reads it
    clearly ('1 9 2 dot 1 6 8 ...') rather than as huge numbers."""
    ip = netinfo.primary_ip()
    if not ip:
        return "I don't have a network address right now."
    return f"My I.P. address is {netinfo.speakable_ip(ip)}."


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def soc_temp_c() -> float | None:
    """This machine's CPU/SoC temperature in °C, or None if it has no sensor.

    Prefers a zone whose ``type`` names the CPU; falls back to the first zone
    that reads. Values are in millidegrees. Never raises — a missing or
    unreadable sensor just means FRED can't answer the question.
    """
    try:
        zones = sorted(_THERMAL.glob("thermal_zone*"))
    except OSError:
        return None

    def read(zone: Path) -> float | None:
        try:
            return int((zone / "temp").read_text().strip()) / 1000.0
        except (OSError, ValueError):
            return None

    def is_cpu(zone: Path) -> bool:
        try:
            return "cpu" in (zone / "type").read_text().strip().lower()
        except OSError:
            return False

    for zone in sorted(zones, key=lambda z: not is_cpu(z)):   # CPU zones first
        temp = read(zone)
        if temp is not None:
            return round(temp, 1)
    return None


def spoken_temp() -> str:
    """A natural, speakable temperature. Both scales, because the panel shows °C
    but the operator thinks in °F."""
    c = soc_temp_c()
    if c is None:
        return "I can't read my temperature sensor right now."
    f = c * 9 / 5 + 32
    hot = " That's warmer than I'd like." if c >= 75 else ""
    return f"My processor is running at {c:.0f} degrees Celsius, {f:.0f} Fahrenheit.{hot}"


def context_block(now: datetime | None = None) -> str:
    """A compact 'current facts' block for Claude's system prompt. Gives the
    model ground truth for time-, date- and network-related questions."""
    # astimezone() makes a naive 'now' timezone-aware using the host's local
    # zone, so %Z renders the abbreviation ('CDT') instead of an empty string.
    now = (now or datetime.now()).astimezone()
    ips = netinfo.all_ipv4()
    ip_str = ", ".join(ips) if ips else "no network address"
    c = soc_temp_c()
    temp_str = (f"{c:.1f} °C ({c * 9 / 5 + 32:.0f} °F)" if c is not None
                else "no temperature sensor on this machine")
    return (
        "Current facts about right now (use these when asked; do not guess):\n"
        f"- Date: {now.strftime('%A, %B')} {_ordinal(now.day)}, {now.strftime('%Y')}\n"
        f"- Time: {now.strftime('%-I:%M %p')} ({now.strftime('%Z')})\n"
        f"- This robot's hostname: {hostname()}\n"
        f"- This robot's IP address(es): {ip_str}\n"
        # FRED has a real thermal sensor and used to deny it, because nothing
        # here told him about it. Anything he can sense belongs in this block.
        f"- This robot's own processor (SoC) temperature: {temp_str}\n"
        "When speaking an IP address aloud, say it digit by digit."
    )

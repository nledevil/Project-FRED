"""Figure out this host's IP address for the boot announcement.

The head runs headless with no monitor, so on boot it speaks its own IP over
the speaker (see deploy/announce_ip.py). ``primary_ip()`` returns the address
you'd actually remote into: the source IP of the default route (whichever of
wifi/ethernet currently carries traffic), with a couple of fallbacks.

Pure standard library — safe to import with no hardware or deps.
"""
from __future__ import annotations

import socket
import subprocess


def primary_ip() -> str | None:
    """Best-guess LAN/wifi IP, or None if the host has no usable address yet.

    Strategy, in order:
      1. Open a UDP socket "to" a public IP — sends nothing, but the kernel
         picks the source IP of the default-route interface. Works offline as
         long as a default route exists.
      2. Fall back to ``hostname -I`` (first non-loopback address).
    """
    ip = _default_route_ip()
    if ip:
        return ip
    return _hostname_ip()


def _default_route_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))          # no packets sent for UDP connect()
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return ip if ip and not ip.startswith("127.") else None


def _hostname_ip() -> str | None:
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for tok in out.split():
        if ":" not in tok and not tok.startswith("127."):   # skip IPv6/loopback
            return tok
    return None


def all_ipv4() -> list[str]:
    """All non-loopback IPv4 addresses (handy when both wifi and eth are up)."""
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [t for t in out.split() if ":" not in t and not t.startswith("127.")]


def speakable_ip(ip: str) -> str:
    """Render an IP so espeak says it digit-by-digit: '192.168.68.78' ->
    '1 9 2 dot 1 6 8 dot 6 8 dot 7 8'. Spacing the digits stops espeak from
    reading each octet as a number ('one hundred ninety two')."""
    octets = (" ".join(octet) for octet in ip.split("."))
    return " dot ".join(octets)

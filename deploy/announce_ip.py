#!/usr/bin/env python3
"""Speak this host's IP address over the head's speaker at boot.

The InMoov head runs headless with no display, so finding its (DHCP-drifting)
IP to remote in is a chore. This plays the startup chime and then speaks the
IP aloud — run once at boot by the ``inmoov-announce-ip`` systemd service,
after ``network-online.target``.

Standalone:  ./venv/bin/python deploy/announce_ip.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# make the project root importable when run directly
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov.netinfo import primary_ip, speakable_ip  # noqa: E402
from inmoov.settings import load_settings  # noqa: E402
from inmoov.sound import Sound  # noqa: E402


def wait_for_ip(timeout: float = 60.0, interval: float = 2.0) -> str | None:
    """Poll for an IP up to ``timeout`` seconds (DHCP may lag network-online)."""
    end = time.monotonic() + timeout
    while True:
        ip = primary_ip()
        if ip:
            return ip
        if time.monotonic() >= end:
            return None
        time.sleep(interval)


def main() -> int:
    snd_cfg = load_settings().get("sound", {})
    snd = Sound(device=snd_cfg.get("device", "plughw:0,0"),
                enabled=bool(snd_cfg.get("enabled", True)))

    ip = wait_for_ip()
    snd.play("startup", wait=True)                 # chime first, so it grabs attention

    if not ip:
        print("[announce_ip] no IP found after wait")
        snd.speak("InMoov is online, but I could not find a network address.",
                  wait=True)
        return 1

    spoken = speakable_ip(ip)
    print(f"[announce_ip] announcing {ip}")
    snd.speak(f"InMoov is online. My I P address is {spoken}. "
              f"Again, {spoken}.", wait=True, speed=145)
    return 0


if __name__ == "__main__":
    sys.exit(main())

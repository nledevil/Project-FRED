#!/usr/bin/env python3
"""Run the cart firmware's own PS2 probe and say what came back.

pico-hover.ino has a diagnostic built in: send it ``d`` over USB serial and it
walks the PS2 config protocol — poll, enter config, model, status, set analog,
exit — printing the raw bytes the receiver returned to each. This drives that,
and then interprets it, because the interpretation is the part you forget
between one bring-up and the next.

Runs **on the chest Pi** (the port is there, and reading it needs root). The
cart driver holds the only handle on that port, so this stops the display
service for the few seconds it needs and starts it again afterwards:

    ssh chest 'sudo python3 /home/dietpi/display/tools/ps2_probe.py'

Safe by construction. The probe zeroes the drive target before it starts, ``d``
is not a drive command, and nothing here writes a speed — but note that if the
hoverboard mainboard *is* connected and powered, the cart is live hardware and
you should have it on blocks anyway.

Reading the output:

* ``FF`` everywhere — nothing is driving DAT. GP16 is INPUT_PULLUP, so an open
  or silent line reads high and every byte comes back FF. This says the line is
  dead, not *why*: an unpowered receiver, a cut DAT wire, and a wireless
  receiver whose controller is off or flat are indistinguishable from here. It
  does rule out a *mode* problem — anything answering shows a 5A.
* ``5A`` in byte 2 — the receiver is alive and answering. Then byte 1 is the
  mode: ``73`` analog (what the firmware needs), ``41`` digital (press ANALOG),
  ``F3`` config mode.
* ``00`` everywhere — DAT held low; usually CLK/CMD swapped or shorted.
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import termios
import time

DEFAULT_GLOB = "/dev/serial/by-id/*Pico*"
SERVICE = "inmoov-display"


def find_port(spec: str) -> str:
    matches = sorted(glob.glob(spec))
    if not matches:
        raise SystemExit(f"no cart Pico matched {spec!r} — is it plugged in?")
    return matches[0]


def probe(port: str, wait: float = 6.0) -> tuple[list[str], list[str]]:
    """Send ``d`` and return (probe lines, anything else that turned up).

    Returns the two separately because conflating them is how this tool started
    lying: the port has a kernel buffer, the cart driver may have been mid-line
    when it let go, and whatever was left sits there waiting to be read. Folded
    into the output it looks like the probe grew a few rows; kept apart it is
    just what it is — leftovers, occasionally interesting in their own right.
    """
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        time.sleep(0.5)                      # let the tty settle before talking
        # Bin whatever was already queued, so this run reports only this run.
        termios.tcflush(fd, termios.TCIFLUSH)
        os.write(fd, b"d\n")
        buf, t0 = b"", time.time()
        while time.time() - t0 < wait:
            try:
                chunk = os.read(fd, 4096)
                if chunk:
                    buf += chunk
                    if b"PS2 PROBE END" in buf:
                        break
            except BlockingIOError:
                time.sleep(0.03)
    finally:
        os.close(fd)

    lines = [l.strip() for l in buf.decode(errors="replace").splitlines() if l.strip()]
    # Take the LAST complete BEGIN..END block: if an earlier probe's tail was
    # still in flight, the newest block is the one this run actually caused.
    starts = [i for i, l in enumerate(lines) if "PS2 PROBE BEGIN" in l]
    ends = [i for i, l in enumerate(lines) if "PS2 PROBE END" in l]
    if starts and ends and ends[-1] > starts[-1]:
        block = lines[starts[-1]:ends[-1] + 1]
        other = lines[:starts[-1]] + lines[ends[-1] + 1:]
        return block, other
    return [], lines


def verdict(lines: list[str]) -> str:
    rows = [l for l in lines if l.startswith("probe ")]
    if not rows:
        return ("No probe output at all. The Pico answered nothing — wrong port, or "
                "firmware that predates the 'd' command.")
    payloads = [l.split("RX:", 1)[1].split() for l in rows if "RX:" in l]
    flat = [b.upper() for row in payloads for b in row]
    if flat and all(b == "FF" for b in flat):
        return ("Every byte is FF: nothing is driving the DAT line at all.\n"
                "  GP16 is INPUT_PULLUP, so an open or silent line reads high and gives\n"
                "  exactly this. It does NOT tell you which end is at fault — a receiver\n"
                "  with no power, a broken DAT/GND wire, and a wireless receiver that\n"
                "  never woke because its controller is off or flat all look identical\n"
                "  from here. What it does rule out is a mode problem: a receiver that\n"
                "  is answering would show a 5A somewhere, even in digital mode.\n"
                "  Check in this order, easiest and most likely first:\n"
                "    1. controller batteries / power switch, and its pairing LED\n"
                "    2. receiver power — 3.3V and GND at the receiver itself\n"
                "    3. the DAT wire, receiver to GP16 (then CLK/CMD/ATT)")
    if flat and all(b == "00" for b in flat):
        return ("Every byte is 00: DAT is being held low. Usually CLK and CMD swapped,\n"
                "  or DAT shorted to ground.")
    ready = [row for row in payloads if len(row) > 2 and row[2].upper() == "5A"]
    if not ready:
        return ("The receiver answered, but never with the 5A ready byte — it is not\n"
                "  speaking the PS2 protocol properly. Suspect a half-connected receiver\n"
                "  or the wrong pin order.")
    modes = {row[1].upper() for row in ready if len(row) > 1}
    if "73" in modes:
        return "Analog mode (73) seen — the controller is talking. This should work."
    if "41" in modes:
        return ("Digital mode (41) only. The firmware needs analog: press the ANALOG\n"
                "  button on the controller (its LED should light).")
    return f"Receiver is answering (5A seen). Mode bytes: {sorted(modes)}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default=DEFAULT_GLOB, help="tty or glob")
    ap.add_argument("--keep-service", action="store_true",
                    help="don't stop/start the display service around the probe")
    ap.add_argument("--out", default="",
                    help="also write the result to this file. Worth using over a "
                         "browser terminal, which can drop or repeat lines and make "
                         "a clean probe look corrupt.")
    args = ap.parse_args()

    port = find_port(args.port)
    print(f"cart Pico: {port}\n")

    stopped = False
    if not args.keep_service:
        # The cart driver owns the port; it has to let go for a few seconds.
        stopped = subprocess.run(["systemctl", "stop", SERVICE]).returncode == 0
        if stopped:
            print(f"stopped {SERVICE} for the probe\n")
        time.sleep(1.0)
    try:
        lines, other = probe(port)
    finally:
        if stopped:
            subprocess.run(["systemctl", "start", SERVICE])
            print(f"\nrestarted {SERVICE}")

    rows = [l for l in lines if l.startswith("probe ")]
    report = ["-" * 62]
    report += ["  " + l for l in lines]
    if other:
        report.append(f"  ({len(other)} line(s) of other traffic on the port, ignored)")
        report += [f"    {l}" for l in other[:5]]
    report.append("-" * 62)
    # A count you can check at a glance: the firmware runs exactly 11 queries, so
    # anything else means you are not looking at one clean probe.
    report.append(f"{len(rows)} probe rows (expect 11)"
                  + ("" if len(rows) == 11 else "   <-- unexpected, see below"))
    report.append(verdict(lines))
    if len(rows) != 11:
        report.append(
            "\nA count other than 11 is worth a second look. If the rows repeat or\n"
            "arrive out of order, suspect the terminal rather than the robot —\n"
            "over a browser terminal this output can be dropped and redrawn. Re-run\n"
            "with --out /tmp/ps2.txt and read the file to get an honest copy.")

    text = "\n".join(report)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
        print(f"\nwritten to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

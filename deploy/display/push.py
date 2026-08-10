#!/usr/bin/env python3
"""Copy a file onto a MicroPython board over the raw REPL, then soft-reset it.

This stands in for ``mpremote cp``. The chest Pi — the machine the sensor node
is actually plugged into — has no pip and no ensurepip, and keeping it
stdlib-only is deliberate (see display_control.py), so pulling in a toolchain
just to copy 9KB of firmware isn't a good trade. This is that copy, in stdlib.

    sudo systemctl stop inmoov-display      # the relay holds the port
    python3 push.py main.py
    sudo systemctl start inmoov-display

The relay must be stopped first: it keeps the tty open, and while a reader is
attached the board's output would be split between two processes. (The relay
opens the port read-only, so it can never disturb the node itself — but it will
happily eat the raw REPL's replies.)

Protocol, for the curious: Ctrl-C interrupts whatever is running, Ctrl-A enters
the raw REPL, each block of code is terminated by Ctrl-D and answered with
``OK<stdout>\\x04<stderr>\\x04>``, and Ctrl-B returns to the normal REPL.
"""
from __future__ import annotations

import argparse
import glob
import os
import select
import sys
import termios
import time
from pathlib import Path

CHUNK = 256               # bytes of file per f.write() — small enough to never
                          # outrun the board's line buffer
RAW_PROMPT = b"raw REPL; CTRL-B to exit\r\n>"


def resolve_port(spec: str = "auto") -> str:
    """A concrete device path from a path, a glob, or 'auto'."""
    if spec and spec != "auto":
        matches = sorted(glob.glob(spec))
        return matches[0] if matches else spec
    for pattern in ("/dev/serial/by-id/*MicroPython*", "/dev/ttyACM*"):
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return ""


def open_serial(path: str, baud: int = 115200) -> int:
    """Open the board's CDC read-write in raw mode.

    Never pass baud=1200: on an RP2040 that's the magic touch that reboots the
    board into its UF2 bootloader, and you'd be reflashing MicroPython instead
    of copying a file.
    """
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        speed = getattr(termios, f"B{baud}")
        iflag, oflag, cflag, lflag, _i, _o, cc = termios.tcgetattr(fd)
        cc = list(cc)
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW,
                          [0, 0, termios.CS8 | termios.CREAD | termios.CLOCAL, 0,
                           speed, speed, cc])
        termios.tcflush(fd, termios.TCIOFLUSH)
    except Exception:
        os.close(fd)
        raise
    return fd


def _write_all(fd: int, data: bytes) -> None:
    i = 0
    while i < len(data):
        i += os.write(fd, data[i:i + CHUNK])
        time.sleep(0.004)


def _drain(fd: int, settle: float = 0.2) -> bytes:
    """Swallow whatever is already in flight."""
    buf = bytearray()
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            try:
                buf.extend(os.read(fd, 4096))
            except (BlockingIOError, OSError):
                break
    return bytes(buf)


def _read_until(fd: int, token: bytes, timeout: float = 10.0) -> bytes:
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            continue
        except OSError as exc:
            raise RuntimeError(f"serial read failed: {exc}") from exc
        buf.extend(chunk)
        if token in buf:
            return bytes(buf)
    raise TimeoutError(f"timed out waiting for {token!r}; "
                       f"last saw {bytes(buf)[-200:]!r}")


def enter_raw(fd: int) -> None:
    _write_all(fd, b"\r\x03\x03")          # two Ctrl-Cs: stop whatever is running
    _drain(fd)
    _write_all(fd, b"\r\x01")              # Ctrl-A: raw REPL
    _read_until(fd, RAW_PROMPT, timeout=5.0)


def raw_exec(fd: int, code: str, timeout: float = 15.0) -> bytes:
    """Run one block on the board; raise whatever it raised."""
    _write_all(fd, code.encode() + b"\x04")
    resp = _read_until(fd, b"\x04>", timeout)
    start = resp.find(b"OK")
    if start < 0:
        raise RuntimeError(f"raw REPL did not acknowledge: {resp[:200]!r}")
    body = resp[start + 2:]
    if body.endswith(b"\x04>"):
        body = body[:-2]
    out, _, err = body.partition(b"\x04")
    err = err.replace(b"\x04", b"").strip()
    if err:
        raise RuntimeError(err.decode("utf-8", "replace"))
    return out


def exit_raw(fd: int, reset: bool = True) -> None:
    _write_all(fd, b"\r\x02")              # Ctrl-B: back to the friendly REPL
    time.sleep(0.2)
    if reset:
        _drain(fd)
        _write_all(fd, b"\x04")            # Ctrl-D: soft reset, which runs main.py


def push(fd: int, local: Path, target: str) -> int:
    data = local.read_bytes()
    raw_exec(fd, f"f=open({target!r},'wb')")
    for i in range(0, len(data), CHUNK):
        raw_exec(fd, f"f.write({data[i:i + CHUNK]!r})")
    raw_exec(fd, "f.close()")
    written = raw_exec(fd, f"import os;print(os.stat({target!r})[6])")
    return int(written.strip() or -1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="local file to copy")
    ap.add_argument("--target", default="", help="name on the board (default: same)")
    ap.add_argument("--port", default="auto", help="tty path, glob, or 'auto'")
    ap.add_argument("--no-reset", action="store_true",
                    help="leave the board at the REPL instead of running main.py")
    args = ap.parse_args()

    local = Path(args.file)
    if not local.is_file():
        print(f"no such file: {local}", file=sys.stderr)
        return 1
    target = args.target or local.name

    path = resolve_port(args.port)
    if not path:
        print("no MicroPython board found (is it plugged in and not in BOOTSEL?)",
              file=sys.stderr)
        return 1

    print(f"pushing {local} -> {path}:{target}")
    fd = open_serial(path)
    try:
        enter_raw(fd)
        size = push(fd, local, target)
        if size != local.stat().st_size:
            print(f"size mismatch: board reports {size}, local is "
                  f"{local.stat().st_size}", file=sys.stderr)
            return 1
        exit_raw(fd, reset=not args.no_reset)
    except (RuntimeError, TimeoutError) as exc:
        print(f"push failed: {exc}", file=sys.stderr)
        return 1
    finally:
        os.close(fd)
    print(f"wrote {size} bytes"
          + ("" if args.no_reset else "; board soft-reset, main.py running"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

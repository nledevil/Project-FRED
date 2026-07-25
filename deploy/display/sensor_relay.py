#!/usr/bin/env python3
"""Relay the chest sensor node's USB-serial stream to the head's ingest API.

The Pico in FRED's stomach reads the ultrasonics + PIR and prints one JSON
object per line to USB serial (see firmware/pico_sensor_node/main.py). It's
plugged into *this* Pi, but the app that reacts to sensors runs on the head — so
something has to carry the lines across. That's this: read the tty, POST each
line to ``http://10.0.0.1:8080/api/sensors/ingest`` over the Bluetooth PAN.

The PAN address is the whole reason this design beats putting WiFi on the node:
the head is *always* 10.0.0.1 on ``pan0``. No DHCP lease to chase, no mDNS, no
venue network required. It rides inside display_control.py rather than being its
own service because there's already a supervised, always-on process on this Pi.

Stdlib only — this Pi has no pyserial and no requests, and a relay this small
doesn't justify adding them. The tty is configured through ``termios`` and the
POST goes out through ``urllib``.

Two threads, deliberately: the reader must never block on HTTP. If the head is
down, POSTs sit in their timeout, and a reader waiting on that would let the
kernel's tty buffer fill and lose the stream. Instead the reader parks payloads
in a bounded queue and the sender drains it — when the head is unreachable the
oldest payloads are dropped, which is the right loss: the node re-sends full
state every heartbeat, so recovery only needs the newest one.
"""
from __future__ import annotations

import glob
import json
import os
import select
import termios
import threading
import time
import urllib.error
import urllib.request
from collections import deque

DEFAULT_URL = "http://10.0.0.1:8080/api/sensors/ingest"
DEFAULT_PORT = "auto"
TRANSPORT = "serial-relay"        # how the head labels this node's path

_RECONNECT_DELAY = 3.0            # between attempts to open the tty
_POST_TIMEOUT = 4.0
_QUEUE_MAX = 32                   # payloads held while the head is unreachable
_MAX_LINE = 8192                  # a line longer than this is junk, not JSON
_ERROR_QUIET = 30.0               # seconds between repeats of the same complaint


def resolve_port(spec: str = DEFAULT_PORT) -> str:
    """Turn a port spec into a concrete device path ('' if nothing is plugged in).

    ``auto`` prefers a /dev/serial/by-id MicroPython node over a bare ttyACM:
    the by-id name is derived from the board's flash ID, so it's stable across
    reboots and unambiguous if anything else USB-serial ever shows up. A spec
    containing a glob is expanded; anything else is taken literally.
    """
    if spec and spec != "auto":
        matches = sorted(glob.glob(spec))
        return matches[0] if matches else spec
    for pattern in ("/dev/serial/by-id/*MicroPython*", "/dev/ttyACM*"):
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return ""


def open_serial(path: str, baud: int = 115200) -> int:
    """Open a USB-CDC tty read-only in raw mode. Returns a non-blocking fd.

    Read-only is a safety property, not an accident: the MicroPython REPL shares
    this CDC with the sensor stream, so a stray byte written here could drop the
    node into the REPL and stop the sensors. An fd we can't write to can't do
    that. (Never set 1200 baud on an RP2040 either — that's the magic the SDK
    uses to reboot the board into its bootloader.)
    """
    # O_NONBLOCK on open: a tty otherwise blocks waiting for carrier until
    # CLOCAL is set, and CLOCAL can't be set before the fd exists.
    fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        speed = getattr(termios, f"B{baud}")
        iflag, oflag, cflag, lflag, _ispeed, _ospeed, cc = termios.tcgetattr(fd)
        iflag = 0            # no CR/NL translation, no flow control, no parity munging
        oflag = 0            # no output post-processing
        lflag = 0            # non-canonical, no echo, no signal generation
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        cc = list(cc)
        cc[termios.VMIN] = 0    # pure non-blocking reads; select() does the waiting
        cc[termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])
        termios.tcflush(fd, termios.TCIFLUSH)   # drop whatever arrived pre-config
    except Exception:
        os.close(fd)
        raise
    return fd


class SensorRelay:
    """Reads the sensor node's tty and forwards its payloads to the head.

    Best-effort by construction: every failure path retries and nothing raises
    into the display app. A missing Pico or an unreachable head must not take
    the animation down with it.
    """

    def __init__(self, port: str = DEFAULT_PORT, url: str = DEFAULT_URL,
                 token: str = "", baud: int = 115200, log=print, on_payload=None):
        self._port_spec = port
        self._url = url
        self._token = token
        self._baud = int(baud)
        self._log = log
        # on_payload(payload: dict) -> None. Called for every payload off the
        # node, before it's queued for the head — that's what feeds the LCD
        # overlay, which must keep updating even when the head is unreachable.
        self._on_payload = on_payload

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._queue: deque = deque(maxlen=_QUEUE_MAX)
        self._wake = threading.Condition()

        self._lock = threading.Lock()
        self._stats = {"port": "", "connected": False, "last_line": 0.0,
                       "lines": 0, "posted": 0, "failed": 0, "dropped": 0,
                       "last_error": "", "last_payload": None}
        self._last_moan: dict[str, float] = {}

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._threads:
            return
        for target, name in ((self._read_loop, "sensor-read"),
                             (self._send_loop, "sensor-send")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        self._log(f"sensor relay: {self._port_spec} -> {self._url}"
                  f"{' (token set)' if self._token else ''}")

    def shutdown(self) -> None:
        self._stop.set()
        with self._wake:
            self._wake.notify_all()

    # -- status ------------------------------------------------------------
    def state(self) -> dict:
        """Snapshot for GET /api/sensors — this is the bring-up debugging view."""
        with self._lock:
            s = dict(self._stats)
        last = s.pop("last_line")
        s["last_line_ago"] = round(time.monotonic() - last, 1) if last else None
        s["queued"] = len(self._queue)
        return s

    def _moan(self, key: str, message: str) -> None:
        """Log a recurring problem at most every _ERROR_QUIET seconds."""
        now = time.monotonic()
        if now - self._last_moan.get(key, 0.0) < _ERROR_QUIET:
            return
        self._last_moan[key] = now
        self._log(message)

    def _note_error(self, text: str) -> None:
        with self._lock:
            self._stats["last_error"] = text[:200]

    # -- reader ------------------------------------------------------------
    def _read_loop(self) -> None:
        while not self._stop.is_set():
            path = resolve_port(self._port_spec)
            with self._lock:
                self._stats["port"] = path
            if not path:
                self._moan("noport", "sensor relay: no sensor node found "
                                     f"(spec {self._port_spec!r}); retrying")
                self._note_error("no serial device matched")
                self._stop.wait(_RECONNECT_DELAY)
                continue
            try:
                fd = open_serial(path, self._baud)
            except OSError as exc:
                self._moan("open", f"sensor relay: cannot open {path} ({exc}); retrying")
                self._note_error(f"open {path}: {exc}")
                self._stop.wait(_RECONNECT_DELAY)
                continue

            self._log(f"sensor relay: reading {path}")
            with self._lock:
                self._stats["connected"] = True
                self._stats["last_error"] = ""
            self._last_moan.clear()
            try:
                self._pump(fd)
            finally:
                os.close(fd)
                with self._lock:
                    self._stats["connected"] = False
            if not self._stop.is_set():
                self._stop.wait(_RECONNECT_DELAY)

    def _pump(self, fd: int) -> None:
        """Read lines off an open tty until it goes away or we're told to stop."""
        buf = bytearray()
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 1.0)
            except OSError as exc:
                self._note_error(f"select: {exc}")
                return
            if not ready:
                continue                     # idle tick — loop and re-check stop
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            except OSError as exc:           # unplugged mid-read
                self._note_error(f"read: {exc}")
                self._log(f"sensor relay: serial read failed ({exc}); reconnecting")
                return
            if not chunk:
                self._log("sensor relay: serial closed; reconnecting")
                return

            buf.extend(chunk)
            while b"\n" in buf:
                raw, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                self._on_line(bytes(raw))
            if len(buf) > _MAX_LINE:
                # No newline in 8KB: this isn't our JSON stream (boot noise, or
                # something else got hold of the port). Resync rather than grow.
                del buf[:-_MAX_LINE // 2]

    def _on_line(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return
        try:
            payload = json.loads(text)
        except ValueError:
            return                           # MicroPython banners, partial lines
        if not isinstance(payload, dict) or "node" not in payload:
            return
        payload["transport"] = TRANSPORT     # so the head shows the real path
        with self._lock:
            self._stats["lines"] += 1
            self._stats["last_line"] = time.monotonic()
            self._stats["last_payload"] = payload
        if self._on_payload is not None:
            try:
                self._on_payload(payload)
            except Exception as exc:      # noqa: BLE001 - a bad hook can't stop the relay
                self._moan("hook", f"sensor relay: on_payload failed ({exc})")
        with self._wake:
            if len(self._queue) == _QUEUE_MAX:
                with self._lock:
                    self._stats["dropped"] += 1
            self._queue.append(payload)      # bounded: appending evicts the oldest
            self._wake.notify()

    # -- sender ------------------------------------------------------------
    def _send_loop(self) -> None:
        while not self._stop.is_set():
            with self._wake:
                while not self._queue and not self._stop.is_set():
                    self._wake.wait(1.0)
                if self._stop.is_set():
                    return
                payload = self._queue.popleft()
            self._post(payload)

    def _post(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["X-Sensor-Token"] = self._token
        req = urllib.request.Request(self._url, data=body, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=_POST_TIMEOUT) as resp:
                resp.read()
            with self._lock:
                self._stats["posted"] += 1
            self._last_moan.pop("post", None)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            with self._lock:
                self._stats["failed"] += 1
            self._note_error(f"post: {exc}")
            self._moan("post", f"sensor relay: POST to {self._url} failed ({exc}); "
                               f"dropping payloads until the head is back")

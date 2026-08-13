#!/usr/bin/env python3
"""Exercise cart_driver against a simulated Pico, with no cart attached.

The cart's Pico is a moving robot with 350 lb of steel behind it, so "plug it in
and see" is a poor first test. This stands up a pty, speaks the firmware's real
protocol on the far end of it — the same clamps, the same telemetry format, the
same PS2-outranks-the-host rule from pico-hover.ino — and checks the driver
against it.

The thing most worth testing is the watchdog, because it is the safety property
that exists specifically for a Bluetooth PAN that has already failed once: if
the head stops talking, does the cart actually get told to stop, and how fast?

    python3 tools/test_cart_driver.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cart_driver                                    # noqa: E402


class FakePico:
    """The far end of the pty, behaving like pico-hover.ino.

    Deliberately mirrors the firmware rather than an idealised version of it:
    host drive commands are ignored while a controller is 'connected', 'x' is
    always honoured, and telemetry goes out as the same plain-text 'fb' line.
    """

    def __init__(self, master_fd: int):
        self._fd = master_fd
        self._stop = threading.Event()
        self.lock = threading.Lock()
        self.steer = 0
        self.speed = 0
        self.commands: list[str] = []
        self.ps2_present = False
        self.last_command_at = 0.0
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()

    def shutdown(self):
        self._stop.set()

    def _emit(self, text: str):
        try:
            os.write(self._fd, (text + "\r\n").encode())
        except OSError:
            pass

    def telemetry(self):
        """One 'fb' line, exactly as the firmware formats it."""
        with self.lock:
            src = "PS2" if self.ps2_present else "USB"
            sl, sr = self.speed, self.speed
        self._emit(f"fb  src: {src}  in1: {self.steer}  in2: {self.speed}  "
                   f"speedR: {sr}  speedL: {sl}  bat: 41.50V  temp: 24.3C")

    def set_ps2(self, present: bool):
        with self.lock:
            self.ps2_present = present
        self._emit("PS2 controller connected (analog mode)" if present
                   else "PS2 controller lost - stopping")
        if present:
            with self.lock:
                self.steer = self.speed = 0

    def _run(self):
        buf = bytearray()
        while not self._stop.is_set():
            try:
                import select as _select
                ready, _, _ = _select.select([self._fd], [], [], 0.1)
                if not ready:
                    continue
                chunk = os.read(self._fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            buf.extend(chunk)
            while b"\n" in buf:
                raw, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                self._line(raw.decode("utf-8", "replace").strip())

    def _line(self, line: str):
        if not line:
            return
        with self.lock:
            self.commands.append(line)
            self.last_command_at = time.monotonic()
        if line[:1] in ("x", "X"):                      # always obeyed
            with self.lock:
                self.steer = self.speed = 0
            self._emit("cmd -> STOP")
            return
        with self.lock:
            ps2 = self.ps2_present
        if ps2:
            self._emit("PS2 controller active - USB drive commands ignored (x still stops)")
            return
        try:
            s, v = line.split()[:2]
            s, v = int(s), int(v)
        except ValueError:
            return
        s = max(-cart_driver.STEER_LIMIT, min(cart_driver.STEER_LIMIT, s))
        v = max(-cart_driver.SPEED_LIMIT, min(cart_driver.SPEED_LIMIT, v))
        with self.lock:
            self.steer, self.speed = s, v
        self._emit(f"cmd -> steer: {s}  speed: {v}")


FAILURES: list[str] = []


class FakePad:
    """Stands in for gamepad.Gamepad — same state() contract, no hardware.

    The real one reads evdev on the chest Pi; what cart_driver cares about is
    only ever the four keys below, so the seam is small enough to fake honestly.
    """

    def __init__(self):
        self.connected = True
        self.deadman = False
        self.speed = 0.0
        self.steer = 0.0

    def state(self):
        return {"connected": self.connected, "deadman": self.deadman,
                "speed": self.speed, "steer": self.steer,
                "boost": False, "raw": {}, "idle_for": 0.0}


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def wait_until(predicate, timeout=3.0, tick=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(tick)
    return False


def main() -> int:
    master, slave = os.openpty()
    port = os.ttyname(slave)
    pico = FakePico(master)
    pico.start()

    pad = FakePad()
    drv = cart_driver.CartDriver(port=port, watchdog=0.5, log=lambda m: None,
                                 gamepad=pad, controller_mode="off")
    drv.start()

    print("cart_driver against a simulated Pico\n")
    try:
        check("connects to the tty",
              wait_until(lambda: drv.state()["connected"]),
              drv.state()["port"])

        # --- telemetry parsing ---
        pico.telemetry()
        ok = wait_until(lambda: drv.state()["battery_v"] == 41.5)
        s = drv.state()
        check("parses the fb telemetry line", ok,
              f"bat={s['battery_v']}V temp={s['board_temp_c']}C src={s['source']}")

        # --- normal drive ---
        r = drv.drive(0, 150)
        ok = wait_until(lambda: pico.speed == 150)
        check("drive() reaches the Pico", ok and r.get("ok"), f"pico speed={pico.speed}")

        # --- clamping to the firmware's limits ---
        r = drv.drive(9999, 9999)
        check("clamps to the firmware limits",
              r["steer"] == cart_driver.STEER_LIMIT
              and r["speed"] == cart_driver.SPEED_LIMIT and r["clamped"],
              f"steer={r['steer']} speed={r['speed']}")

        # --- the watchdog: the whole reason this module exists ---
        drv.drive(0, 200)
        wait_until(lambda: pico.speed == 200)
        t0 = time.monotonic()
        stopped = wait_until(lambda: pico.speed == 0, timeout=2.0)
        elapsed = time.monotonic() - t0
        check("watchdog stops the cart when the head goes quiet", stopped,
              f"stopped {elapsed:.2f}s after the last command")
        check("watchdog fires within its window, not the firmware's 2s",
              stopped and elapsed < 1.0, f"{elapsed:.2f}s < 1.0s")
        check("watchdog stop was counted", drv.state()["watchdog_stops"] >= 1,
              f"count={drv.state()['watchdog_stops']}")

        # --- authority is refreshed by repeated calls ---
        end = time.monotonic() + 1.5
        while time.monotonic() < end:               # keep petting it
            drv.drive(0, 120)
            time.sleep(0.1)
        check("stays alive while the head keeps commanding", pico.speed == 120,
              f"pico speed={pico.speed} after 1.5s of 10Hz commands")
        drv.stop()

        # --- PS2 takes priority ---
        pico.set_ps2(True)
        pico.telemetry()
        wait_until(lambda: drv.state()["ps2_active"])
        r = drv.drive(0, 200)
        check("reports that the PS2 controller has priority",
              "ignored" in r, r.get("ignored", "no notice given"))
        check("PS2 override leaves the cart stopped", pico.speed == 0,
              f"pico speed={pico.speed}")
        pico.set_ps2(False)
        pico.telemetry()
        wait_until(lambda: not drv.state()["ps2_active"])

        # --- emergency stop latches ---
        drv.drive(0, 150)
        wait_until(lambda: pico.speed == 150)
        drv.stop(estop=True)
        ok = wait_until(lambda: pico.speed == 0)
        r = drv.drive(0, 200)
        check("estop stops immediately", ok, f"pico speed={pico.speed}")
        check("estop latches against further drive commands",
              "error" in r and r.get("estop"), r.get("error", ""))
        time.sleep(0.3)
        check("estop keeps the cart stopped", pico.speed == 0, f"pico speed={pico.speed}")
        drv.clear_estop()
        drv.drive(0, 100)
        check("drive works again after clearing the estop",
              wait_until(lambda: pico.speed == 100), f"pico speed={pico.speed}")
        drv.stop()

        # --- refuses the MicroPython sensor node ---
        # --- hand controller arbitration (off / takeover / only) -------------
        # The PS2 receiver used to settle this in firmware. It is software now,
        # so it needs testing like the watchdog does.
        drv.set_controller_mode("off")
        pad.deadman, pad.speed, pad.steer = True, 1.0, 0.0
        drv.drive(0, 120)
        wait_until(lambda: pico.speed == 120)
        check("mode=off ignores the controller entirely", pico.speed == 120,
              f"host still driving (pico speed={pico.speed})")

        drv.set_controller_mode("takeover")
        wait_until(lambda: pico.speed == cart_driver.SPEED_LIMIT)
        check("mode=takeover: deadman held gives the controller the cart",
              pico.speed == cart_driver.SPEED_LIMIT,
              f"pico speed={pico.speed} (full stick = {cart_driver.SPEED_LIMIT})")
        check("host is told its command is being ignored",
              "ignored" in drv.drive(0, 50),
              drv.drive(0, 50).get("ignored", "NOT REPORTED"))

        pad.deadman = False
        drv.drive(0, 90)
        wait_until(lambda: pico.speed == 90)
        check("releasing the deadman hands control back to the host",
              pico.speed == 90, f"pico speed={pico.speed}")

        pad.deadman, pad.speed = True, -1.0
        wait_until(lambda: pico.speed == -cart_driver.SPEED_LIMIT)
        check("controller drives in reverse too",
              pico.speed == -cart_driver.SPEED_LIMIT, f"pico speed={pico.speed}")

        pad.deadman = False
        drv.set_controller_mode("only")
        drv.drive(0, 200)
        time.sleep(0.3)
        check("mode=only refuses host drive commands", pico.speed == 0,
              f"pico speed={pico.speed} (must stay 0 with no deadman held)")
        check("and says so", "ignored" in drv.drive(0, 200),
              drv.drive(0, 200).get("ignored", "NOT REPORTED"))

        pad.deadman, pad.speed = True, 0.5
        wait_until(lambda: pico.speed == int(round(0.5 * cart_driver.SPEED_LIMIT)))
        check("mode=only still drives from the controller",
              pico.speed == int(round(0.5 * cart_driver.SPEED_LIMIT)),
              f"pico speed={pico.speed}")

        # Changing mode while the controller is pushing must not leave the last
        # stick position latched into the host's target.
        drv.set_controller_mode("off")
        time.sleep(0.3)
        check("switching mode mid-push stops the cart", pico.speed == 0,
              f"pico speed={pico.speed}")

        pad.deadman = False
        pad.connected = False
        drv.set_controller_mode("takeover")
        drv.drive(0, 80)
        wait_until(lambda: pico.speed == 80)
        check("a disconnected controller never takes the cart", pico.speed == 80,
              f"host still driving (pico speed={pico.speed})")
        drv.set_controller_mode("off")
        drv.stop()

        check("resolve_port refuses a MicroPython device",
              cart_driver.resolve_port(
                  "/dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_x-if00") == "",
              "would otherwise write drive commands at the sensor node's REPL")

    finally:
        drv.shutdown()
        pico.shutdown()
        time.sleep(0.2)
        os.close(slave)
        os.close(master)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("OK: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

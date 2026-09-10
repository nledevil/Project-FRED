"""Write tunable parameters to the reSpeaker's XVF3800, and re-apply them at boot.

The array does its own AGC and gain, and both are one vendor write away — but
nothing in this codebase ever wrote an XVF3800 parameter, only read the DOA
registers (see mic_doa.py), and the device forgets any change across a power
cycle. So the one knob that matters for a loud hall — how much headroom the ASR
output keeps before it clips — could not be set at all, let alone set to survive
the pack-up. This is the write half of mic_doa's read half, plus an apply() that
puts a configured set back after every boot.

**The command map, measured against the device's own docs** (Seeed's I2C list;
the USB vendor protocol is the same resource/command pair mic_doa already uses):

  name                 resid cmd type   range           what it does
  AEC_ASROUTGAIN        33    36  float  0.0 .. 1000.0   gain on the ASR beam (default 1.0)
  AEC_ASROUTONOFF       33    35  int32  0 / 1           1 = ASR-processed output (as shipped)
  PP_AGCONOFF           17    10  int32  0 / 1           automatic gain control on/off
  PP_AGCMAXGAIN         17    11  float  1.0 .. 1000.0   ceiling the AGC may reach
  PP_AGCDESIREDLEVEL    17    12  float  1e-8 .. 1.0     power the AGC drives toward

**Why write-then-read-back, always.** Writing a wrong resource on this device is
not a harmless no-op — the DOA work cost two wrong turns reading the wrong
register, and writing is worse. So every write here is verified by reading the
value straight back, and apply() reports a mismatch rather than trusting the
write landed. Nothing here raises into the caller: a missing array or a refused
write is a logged skip, exactly like the rest of the hardware wrappers.

Protocol (matches mic_doa._read): a vendor control transfer, bRequest 0,
wIndex = resid. Read sets the command's high bit (0x80) and reads length+1 bytes
where byte 0 is the device status (0 ok, 64 = busy, retry). Write clears the high
bit and sends the little-endian value as the data stage.
"""
from __future__ import annotations

import struct
import time

try:
    import usb.core
    import usb.util
    _USB_ERR = None
except Exception as exc:  # noqa: BLE001 - no pyusb just means the array can't be tuned
    usb = None
    _USB_ERR = exc

VENDOR_ID = 0x2886                  # Seeed, same as mic_doa

# name -> (resource id, command id, type, count). Types: "float" (<f), "int32" (<i).
PARAMS: dict[str, tuple[int, int, str, int]] = {
    "AEC_ASROUTGAIN":    (33, 36, "float", 1),
    "AEC_ASROUTONOFF":   (33, 35, "int32", 1),
    "PP_AGCONOFF":       (17, 10, "int32", 1),
    "PP_AGCMAXGAIN":     (17, 11, "float", 1),
    "PP_AGCDESIREDLEVEL": (17, 12, "float", 1),
}

_FMT = {"float": "f", "int32": "i"}
_WIDTH = {"float": 4, "int32": 4}

# Same servicer-busy handling mic_doa measured: the device answers a control
# transfer it isn't ready for with status 64 and expects to be asked again after
# a short wait, not hammered.
_RETRY_STATUS = 64
_RETRY_BACKOFF_S = 0.010
_MAX_ATTEMPTS = 5
_CTRL_TIMEOUT_MS = 500


class XvfParams:
    """A short-lived handle for reading and writing named parameters."""

    def __init__(self, log=None):
        self._log = log
        self._dev = None
        self.error = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> bool:
        if usb is None:
            self.error = f"pyusb missing ({_USB_ERR})"
            return False
        try:
            dev = usb.core.find(idVendor=VENDOR_ID)
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            return False
        if dev is None:
            self.error = "no reSpeaker on the bus"
            return False
        self._dev = dev
        self.error = None
        return True

    def close(self) -> None:
        dev, self._dev = self._dev, None
        if dev is not None and usb is not None:
            try:
                usb.util.dispose_resources(dev)
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *a):
        self.close()
        return False

    # -- the two primitives ------------------------------------------------
    def read(self, name: str):
        """Current value(s) of a named parameter, or None on any failure."""
        spec = PARAMS.get(name)
        if spec is None or self._dev is None:
            return None
        resid, cmdid, kind, count = spec
        nbytes = _WIDTH[kind] * count
        for attempt in range(_MAX_ATTEMPTS):
            if attempt:
                time.sleep(_RETRY_BACKOFF_S)
            try:
                raw = self._dev.ctrl_transfer(
                    usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR
                    | usb.util.CTRL_RECIPIENT_DEVICE,
                    0, 0x80 | cmdid, resid, nbytes + 1, _CTRL_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001
                self._note(f"read {name}: {type(exc).__name__}: {exc}")
                return None
            data = bytes(raw)
            if not data:
                return None
            if data[0] == 0:
                vals = list(struct.unpack("<" + _FMT[kind] * count, data[1:1 + nbytes]))
                return vals if count > 1 else vals[0]
            if data[0] != _RETRY_STATUS:
                self._note(f"read {name}: device status {data[0]}")
                return None
        self._note(f"read {name}: busy after {_MAX_ATTEMPTS} tries")
        return None

    def write(self, name: str, value, verify: bool = True):
        """Write a named parameter. Returns the read-back value on success (so a
        caller can log what actually landed), or None on failure/mismatch."""
        spec = PARAMS.get(name)
        if spec is None or self._dev is None:
            return None
        resid, cmdid, kind, count = spec
        values = list(value) if isinstance(value, (list, tuple)) else [value]
        if len(values) != count:
            self._note(f"write {name}: expected {count} value(s), got {len(values)}")
            return None
        if kind == "int32":
            values = [int(v) for v in values]
        else:
            values = [float(v) for v in values]
        payload = struct.pack("<" + _FMT[kind] * count, *values)
        for attempt in range(_MAX_ATTEMPTS):
            if attempt:
                time.sleep(_RETRY_BACKOFF_S)
            try:
                sent = self._dev.ctrl_transfer(
                    usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR
                    | usb.util.CTRL_RECIPIENT_DEVICE,
                    0, cmdid, resid, payload, _CTRL_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001
                self._note(f"write {name}: {type(exc).__name__}: {exc}")
                return None
            if sent == len(payload):
                break
        else:
            self._note(f"write {name}: short write")
            return None
        if not verify:
            return values if count > 1 else values[0]
        back = self.read(name)
        want = values if count > 1 else values[0]
        if not _close_enough(back, want, kind):
            self._note(f"write {name}: read back {back!r}, wanted {want!r}")
            return None
        return back

    # -- the boot job ------------------------------------------------------
    def apply(self, params: dict) -> dict:
        """Write a whole configured set, verifying each. Returns a per-name report
        of what landed (or the reason it didn't) — for a boot log and a tool."""
        report: dict[str, object] = {}
        for name, value in (params or {}).items():
            if name not in PARAMS:
                report[name] = "unknown parameter"
                continue
            got = self.write(name, value, verify=True)
            report[name] = {"set": value, "readback": got, "ok": got is not None}
        return report

    def _note(self, msg: str) -> None:
        self.error = msg
        if self._log is not None:
            self._log(f"[xvf] {msg}")


def _close_enough(back, want, kind: str) -> bool:
    if back is None:
        return False
    if kind == "int32":
        return int(back) == int(want)
    # float round-trips through the device with some loss; accept a small relative gap
    try:
        return abs(float(back) - float(want)) <= max(1e-6, abs(float(want)) * 1e-3)
    except (TypeError, ValueError):
        return False


def apply_from_settings(settings: dict, log=None) -> dict | None:
    """Boot hook: apply settings['voice']['xvf_params'] if any are configured.

    Returns None when there is nothing to do (the default — an empty or absent
    block changes nothing, so a rig without the array or without a reason to tune
    it behaves exactly as before). Never raises."""
    params = ((settings.get("voice") or {}).get("xvf_params") or {})
    if not params:
        return None
    xp = XvfParams(log=log)
    if not xp.open():
        if log is not None:
            log(f"[xvf] cannot apply params: {xp.error}")
        return {"error": xp.error}
    try:
        report = xp.apply(params)
    finally:
        xp.close()
    if log is not None:
        for name, r in report.items():
            log(f"[xvf] {name} -> {r}")
    return report

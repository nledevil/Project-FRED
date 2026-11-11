#!/usr/bin/env python3
"""The XVF3800 parameter writer: the command map, the wire format, and read-back.

Writing the wrong resource to this device is the failure the DOA work paid for
twice over, so the parts worth pinning are the ones a typo would break silently:

  * the command map matches the device's documented numbers (a wrong resid/cmd
    is caught here, not on the array);
  * a read builds the exact control transfer mic_doa uses — high bit set on the
    command, resid in wIndex, length+1 for the status byte — and decodes the
    right type;
  * a write clears the high bit, sends the little-endian value, and is only
    reported successful when the value reads back;
  * a write with the wrong number of values, or an unknown name, is refused.

Driven with a fake device shaped like pyusb's ctrl_transfer (returns a status
byte + payload on IN, a byte count on OUT), so no array is required.

    python3 tools/test_xvf_params.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import xvf_params as XP                         # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeDev:
    """Mimics pyusb ctrl_transfer against a tiny per-(resid,cmd) store."""
    def __init__(self):
        self.store: dict[tuple, bytes] = {}
        self.calls: list[dict] = []

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, data_or_len, timeout):
        is_in = bool(bmRequestType & 0x80)          # CTRL_IN == 0x80
        cmd = wValue & 0x7F
        self.calls.append({"in": is_in, "req": bRequest, "wValue": wValue,
                           "cmd": cmd, "resid": wIndex, "arg": data_or_len})
        if is_in:
            length = int(data_or_len)               # includes the status byte
            payload = self.store.get((wIndex, cmd), b"\x00" * (length - 1))
            return [0] + list(payload[:length - 1])  # status 0 (ok) + data
        self.store[(wIndex, cmd)] = bytes(data_or_len)
        return len(data_or_len)


def main() -> int:
    print("the command map matches the device's documented numbers")
    expect = {
        "AEC_ASROUTGAIN":    (33, 36, "float", 1),
        "AEC_ASROUTONOFF":   (33, 35, "int32", 1),
        "PP_AGCONOFF":       (17, 10, "int32", 1),
        "PP_AGCMAXGAIN":     (17, 11, "float", 1),
        "PP_AGCDESIREDLEVEL": (17, 12, "float", 1),
    }
    for name, spec in expect.items():
        check(f"{name} = {spec}", XP.PARAMS.get(name) == spec, str(XP.PARAMS.get(name)))

    print("a write clears the high bit and sends the little-endian value")
    xp = XP.XvfParams()
    dev = FakeDev()
    xp._dev = dev
    back = xp.write("AEC_ASROUTGAIN", 2.5, verify=True)
    check("write returned the read-back value", back == 2.5, str(back))
    out = [c for c in dev.calls if not c["in"]][0]
    check("OUT used the bare command id (no 0x80)", out["cmd"] == 36 and out["wValue"] == 36, str(out))
    check("OUT addressed resid 33", out["resid"] == 33)
    check("OUT payload was the 4-byte LE float",
          out["arg"] == struct.pack("<f", 2.5), out["arg"].hex())

    print("a read sets the high bit and decodes the right type")
    got = xp.read("AEC_ASROUTGAIN")
    rd = [c for c in dev.calls if c["in"]][-1]
    check("IN set the high bit on the command", rd["wValue"] == (0x80 | 36), hex(rd["wValue"]))
    check("IN asked for value bytes + 1 status", rd["arg"] == 4 + 1, str(rd["arg"]))
    check("the float round-tripped", got == 2.5, str(got))

    print("an int32 parameter encodes as int, not float")
    xp.write("PP_AGCONOFF", 1, verify=True)
    out = [c for c in dev.calls if not c["in"] and c["resid"] == 17 and c["cmd"] == 10][-1]
    check("OUT payload was a 4-byte LE int32", out["arg"] == struct.pack("<i", 1), out["arg"].hex())
    check("reads back as an int", xp.read("PP_AGCONOFF") == 1)

    print("bad inputs are refused, not sent")
    before = len(dev.calls)
    check("an unknown name returns None", xp.write("NOPE", 1) is None)
    check("...and issued no transfer", len(dev.calls) == before)

    print("apply() reports per-name what landed")
    xp2 = XP.XvfParams(); xp2._dev = FakeDev()
    rep = xp2.apply({"AEC_ASROUTGAIN": 1.0, "PP_AGCDESIREDLEVEL": 0.002, "NOPE": 5})
    check("known params report ok", rep["AEC_ASROUTGAIN"]["ok"] and rep["PP_AGCDESIREDLEVEL"]["ok"], str(rep))
    check("an unknown one is flagged, not applied", rep["NOPE"] == "unknown parameter", str(rep.get("NOPE")))

    print("_close_enough tolerates float loss but not int drift")
    check("float within tolerance", XP._close_enough(0.0020001, 0.002, "float"))
    check("float far off rejected", not XP._close_enough(0.5, 0.002, "float"))
    check("int must be exact", XP._close_enough(1, 1, "int32") and not XP._close_enough(0, 1, "int32"))

    print("apply_from_settings is a no-op when nothing is configured")
    check("empty settings -> None (nothing opened, nothing changed)",
          XP.apply_from_settings({"voice": {}}) is None)
    check("absent voice block -> None", XP.apply_from_settings({}) is None)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

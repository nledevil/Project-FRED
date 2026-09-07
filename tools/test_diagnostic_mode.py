#!/usr/bin/env python3
"""What diagnostic mode will and will not do, without running any of it.

This is a mode that lets a robot execute commands because somebody talked to it,
so what is tested here is the policy rather than the plumbing: the switch starts
off, the tiers are separate, the unit list is closed, and the whole thing turns
itself off if forgotten.

Every check below is one sentence of the security argument in
inmoov/diagnostic.py, made executable. The one that matters most is the least
interesting to read: **nothing here can construct an enabled mode**. Diagnostic
mode is never loaded from settings and never restored at boot, because the
failure it guards against is not an attacker — it is flipping the switch in the
workshop on Tuesday and unpacking the robot at a school on Thursday.

``subprocess.run`` is stubbed throughout, so no command is ever really executed;
what is asserted is the argv that *would* have been.

    python3 tools/test_diagnostic_mode.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import diagnostic as D                          # noqa: E402

FAILURES: list[str] = []
RAN: list[list[str]] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def fake_run(argv, **kw):
    RAN.append(list(argv))
    return types.SimpleNamespace(stdout="ok", stderr="", returncode=0)


def refused(fn, *a, **kw):
    """Return the refusal message, or None if it was allowed through."""
    try:
        fn(*a, **kw)
        return None
    except D.Refused as why:
        return str(why)


def main() -> int:
    subprocess.run = fake_run                    # nothing here really runs

    print("the switch starts off and cannot be born on")
    m = D.DiagnosticMode()
    check("off at construction", m.enabled is False)
    check("unrestricted off too", m.unrestricted is False)
    check("no constructor argument can turn it on",
          "enabled" not in D.DiagnosticMode.__init__.__code__.co_varnames,
          str(D.DiagnosticMode.__init__.__code__.co_varnames))

    print("nothing runs while it is off")
    check("a check refuses", (refused(m.run_check, "disk") or "").startswith(
        "Diagnostic mode is off"))
    check("a shell refuses", "off" in (refused(m.run_shell, "ls") or ""))
    RAN.clear()
    check("...and neither reached a subprocess", RAN == [], str(RAN))

    print("the two tiers are separate switches")
    m.set(True)
    check("checks work once on", m.run_check("disk") == "ok")
    why = refused(m.run_shell, "ls")
    check("shell still refuses in the safe tier", why is not None and "panel" in why, str(why))
    m.set(True, unrestricted=True)
    check("shell works only once the second switch is on",
          m.run_shell("ls") == "ok")

    print("unrestricted will not turn on during an event")
    # A hall full of children is the exact situation where voice-driven shell is
    # a bad idea, so this is a refusal rather than a silent no-op.
    m2 = D.DiagnosticMode()
    why = refused(m2.set, True, unrestricted=True, at_event=True)
    check("refused, and says why", why is not None and "event mode" in why.lower(),
          str(why))
    check("...and it did not sneak on anyway", m2.unrestricted is False)
    check("the safe tier is still allowed at an event",
          m2.set(True, unrestricted=False)["enabled"] is True)

    print("the unit list is closed, and knows which machine it is on")
    m = D.DiagnosticMode()
    m.set(True)
    why = refused(m.run_check, "service_restart", "ssh")
    check("a unit outside the list is refused", why is not None and "not allowed" in why,
          str(why))
    for bad in ("networking", "systemd-resolved", "sshd", ""):
        check(f"...including {bad or '(empty)'!r}",
              refused(m.run_check, "service_status", bad) is not None)

    RAN.clear()
    m.run_check("service_status", "fred-panel")
    check("a local unit runs locally", RAN[-1][0] == "systemctl", str(RAN[-1]))
    RAN.clear()
    m.run_check("service_status", "inmoov-display")
    check("a chest unit goes over ssh to the chest",
          RAN[-1][0] == "ssh" and "chest" in RAN[-1], str(RAN[-1]))
    RAN.clear()
    m.run_check("logs", "servo-server")
    check("a head unit goes over ssh to the head",
          RAN[-1][0] == "ssh" and "head" in RAN[-1], str(RAN[-1]))
    check("...and BatchMode, so a missing key fails fast rather than hanging",
          "BatchMode=yes" in RAN[-1], str(RAN[-1]))

    print("the model's words never reach a shell in the safe tier")
    # The whole reason the checks are a table of argv builders rather than a
    # command string: there is no quoting bug to get wrong here because there is
    # no shell.
    RAN.clear()
    refused(m.run_check, "service_status", "fred-panel; rm -rf /")
    check("an injected unit name is refused, not quoted", RAN == [], str(RAN))
    check("no check builds a shell", all(
        argv[0] not in ("bash", "sh", "-c") for argv in RAN), str(RAN))

    print("the shell tier still refuses the obviously destructive")
    m.set(True, unrestricted=True)
    for cmd in ("rm -rf /", "sudo reboot", "dd if=/dev/zero of=/dev/sda",
                "mkfs.ext4 /dev/sda1", "systemctl stop ssh", "shutdown now"):
        check(f"refuses {cmd!r}", refused(m.run_shell, cmd) is not None)
    check("but allows an ordinary look around", m.run_shell("ls /tmp") == "ok")

    print("it forgets itself if left on")
    m = D.DiagnosticMode(idle_timeout=0.0)
    m.set(True, unrestricted=True)
    check("expired by its own clock", m.enabled is False)
    check("...and unrestricted goes with it", m.unrestricted is False)
    check("...and a check refuses again",
          refused(m.run_check, "disk") is not None)

    print("everything that happened is written down")
    m = D.DiagnosticMode()
    m.set(True)
    m.run_check("disk")
    refused(m.run_check, "service_restart", "ssh")
    refused(m.run_shell, "ls")
    kinds = [(a["tier"], a["allowed"]) for a in m.status()["audit"]]
    check("the allowed run is recorded", ("check", True) in kinds, str(kinds))
    check("the refused unit is recorded too", ("check", False) in kinds, str(kinds))
    check("and the refused shell", ("shell", False) in kinds, str(kinds))
    check("the audit is bounded", len(D.DiagnosticMode().audit) == 0)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

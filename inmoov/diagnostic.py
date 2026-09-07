"""Diagnostic mode — letting FRED debug himself out loud.

At a venue there is often no laptop, no second pair of hands, and a robot that
has gone quiet for a reason nobody standing there can see. This is the switch
that lets you ask him instead: "why is your chest display blank", "is the head
Pi up", "what does your log say". He runs the check and tells you.

**The switch is the security boundary, not the voice.** Once this is on, anyone
who can say "Fred" can use it — the wake word is not a secret and he is taken to
halls full of people. So the gate is the PIN-protected toggle on the admin
panel, which needs physical access to a screen on the robot, and everything
below exists to bound what happens if that switch is left on by mistake.

Three things bound it:

*Two tiers.* On its own the switch buys a fixed list of read-mostly checks —
service status, log tails, temperatures, whether the Pis answer. Arbitrary shell
is a second switch, off by default, that has to be reached for deliberately.

*It expires.* The mode turns itself off after ``IDLE_TIMEOUT`` with nothing
asked of it, and it is never restored at boot. The failure this is aimed at is
not an attacker, it is you flipping it on in the workshop on Tuesday and
unpacking the robot at a school on Thursday.

*It is written down.* Every command, its tier, and whether it was allowed goes
to the audit log whether it ran or not, because "what did he actually do" is not
a question anyone should have to reconstruct afterwards.

**Unrestricted mode additionally refuses to turn on during an event.** A hall
full of children is the exact situation where voice-driven shell is a bad idea,
and being told "not now" is better than remembering.

The object is read at call time rather than copied at construction, the same way
``EventMode`` is, so the panel can flip it without restarting anything.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import time

# How long the mode stays on with nothing asked of it. Long enough to walk to
# the other side of a robot and think; short enough that a switch left on in the
# workshop is off again before it matters.
IDLE_TIMEOUT = 30 * 60.0

# Only these units, and *where each one lives*. Not ssh, not networking, not
# anything whose restart would strand the machine he is running on.
#
# The host matters as much as the name: "why is your chest display blank" is the
# question this whole mode exists for, and inmoov-display runs on the chest Pi,
# not on the brain. Asking systemctl about it locally answers "could not be
# found", which is a confusing lie rather than an answer. So a unit carries the
# machine it belongs to, and remote ones go over the ssh aliases in the NUC's
# own config — the same "head"/"chest" the terminal services use, with key auth
# already set up as the user this runs as.
UNIT_HOSTS = {
    "fred-panel": "local",
    "fred-hotspot": "local", "fred-hotspot-dns": "local",
    "fred-terminal": "local", "fred-terminal-local": "local",
    "fred-terminal-head": "local", "fred-terminal-head-local": "local",
    "fred-terminal-chest": "local", "fred-terminal-chest-local": "local",
    "servo-server": "head", "camera-stream": "head",
    "inmoov-hotspot": "head", "inmoov-hotspot-check": "head",
    "inmoov-terminal": "head",
    "inmoov-display": "chest", "pan-client": "chest",
}
ALLOWED_UNITS = tuple(UNIT_HOSTS)


def _on_host(host: str, argv: list[str]) -> list[str]:
    """Wrap argv to run on one of the Pis, or leave it alone for the brain."""
    if host == "local":
        return argv
    # BatchMode so a missing key fails in two seconds rather than hanging on a
    # password prompt nobody can see, with FRED silent in front of a room.
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host,
            " ".join(shlex.quote(a) for a in argv)]

# The safe tier, as a table rather than a regex over free text: each entry names
# the check, how to build the argv from its one optional argument, and what the
# argument is allowed to be. Building argv here rather than parsing a string is
# the point — nothing the model says is ever handed to a shell in this tier.
#
# "read-mostly" rather than read-only: restarting a FRED service is the single
# most useful thing to be able to ask for, and it is bounded by ALLOWED_UNITS.
_CHECKS: dict[str, dict] = {
    "service_status": {
        "argv": lambda a: ["systemctl", "status", "--no-pager", "-n", "20", a],
        "units": True,
        "help": "Whether a service is running, and its last few log lines.",
    },
    "service_restart": {
        "argv": lambda a: ["sudo", "systemctl", "restart", a],
        "units": True, "writes": True,
        "help": "Restart one of FRED's services, on whichever machine it lives.",
    },
    "logs": {
        "argv": lambda a: ["journalctl", "-u", a, "-n", "40", "--no-pager", "-o", "cat"],
        "units": True,
        "help": "The last 40 log lines from one of FRED's services.",
    },
    "kernel_log": {
        "argv": lambda a: ["dmesg", "--level=err,warn", "-T", "--color=never"],
        "help": "Kernel errors and warnings — USB drops, undervoltage, resets.",
    },
    "disk": {"argv": lambda a: ["df", "-h", "/"], "help": "Free disk space."},
    "memory": {"argv": lambda a: ["free", "-h"], "help": "Free memory."},
    "uptime": {"argv": lambda a: ["uptime"], "help": "How long he has been up, and load."},
    "temperature": {
        "argv": lambda a: ["sensors"],
        "help": "Processor temperatures.",
    },
    "processes": {
        "argv": lambda a: ["ps", "-eo", "pcpu,pmem,comm", "--sort=-pcpu"],
        "help": "What is using the processor.",
    },
    "network": {"argv": lambda a: ["ip", "-brief", "addr"],
                "help": "Network interfaces and addresses."},
    "ping_head": {"argv": lambda a: ["ping", "-c", "3", "-W", "2", "10.0.0.10"],
                  "help": "Whether the head Pi answers."},
    "ping_chest": {"argv": lambda a: ["ping", "-c", "3", "-W", "2", "10.0.0.11"],
                   "help": "Whether the chest Pi answers."},
    "usb": {"argv": lambda a: ["lsusb"], "help": "What is plugged into USB."},
    "sound_cards": {"argv": lambda a: ["cat", "/proc/asound/cards"],
                    "help": "Which sound cards the machine can see."},
}

CHECKS = tuple(_CHECKS)

# Refused in the unrestricted tier no matter what. Not a security control —
# anything with a shell can work around a word list, and this tier is only ever
# reached deliberately. It is a guard against the model being *helpful*: an
# agent asked to free up disk space will reach for rm, and an agent asked to fix
# the network will reach for the interface it is talking to you over.
_SHELL_REFUSE = re.compile(
    r"\b(rm|mkfs|dd|shred|fdisk|parted|userdel|passwd|visudo)\b"
    r"|\b(shutdown|reboot|poweroff|halt)\b"
    r"|\bsystemctl\s+(stop|disable|mask)\s+(ssh|networking|systemd-networkd)"
    r"|>\s*/dev/[sn]",
    re.I)


class Refused(Exception):
    """A command that policy would not run. The message is spoken to the room."""


class DiagnosticMode:
    """Whether FRED may debug himself out loud, and how far.

    ``enabled`` is the switch; ``unrestricted`` is the second one. Both are
    False at construction and neither is ever loaded from settings — see the
    module docstring on why this does not persist.
    """

    def __init__(self, log=None, idle_timeout: float = IDLE_TIMEOUT):
        self._enabled = False
        self._unrestricted = False
        self._until = 0.0
        self._log = log
        self.idle_timeout = float(idle_timeout)
        self.audit: list[dict] = []

    # -- the switch --------------------------------------------------------
    def _expired(self) -> bool:
        return self._enabled and time.monotonic() > self._until

    @property
    def enabled(self) -> bool:
        if self._expired():
            self._off("idle timeout")
        return self._enabled

    @property
    def unrestricted(self) -> bool:
        return self.enabled and self._unrestricted

    def _off(self, why: str) -> None:
        was = self._enabled
        self._enabled = self._unrestricted = False
        self._until = 0.0
        if was and self._log:
            self._log(f"[diagnostic] off ({why})")

    def touch(self) -> None:
        """Push the expiry out. Called whenever the mode is actually used."""
        if self._enabled:
            self._until = time.monotonic() + self.idle_timeout

    def set(self, on: bool, *, unrestricted: bool | None = None,
            at_event: bool = False) -> dict:
        """Flip the switch. Returns ``status()``; raises Refused on a bad ask."""
        if not on:
            self._off("switched off")
            return self.status()
        self._enabled = True
        self.touch()
        if unrestricted is not None:
            if unrestricted and at_event:
                # Deliberately a refusal rather than a silent no-op: being told
                # is the whole value.
                raise Refused("Not while event mode is on — turn that off first "
                              "if you really want a shell.")
            self._unrestricted = bool(unrestricted)
        if self._log:
            self._log(f"[diagnostic] on"
                      f"{' (unrestricted)' if self._unrestricted else ''}")
        return self.status()

    def remaining(self) -> float:
        return max(0.0, self._until - time.monotonic()) if self.enabled else 0.0

    # -- running things ----------------------------------------------------
    def _record(self, tier: str, what: str, allowed: bool, note: str = "") -> None:
        self.audit.append({"t": time.time(), "tier": tier, "command": what,
                           "allowed": allowed, "note": note})
        del self.audit[:-200]                 # bounded; this is a breadcrumb trail
        if self._log:
            self._log(f"[diagnostic] {tier} {'ran' if allowed else 'REFUSED'}: "
                      f"{what}{(' — ' + note) if note else ''}")

    def run_check(self, name: str, unit: str = "", timeout: float = 20.0) -> str:
        """Run one of the named checks. Raises Refused if policy says no."""
        if not self.enabled:
            raise Refused("Diagnostic mode is off. Turn it on from the admin panel.")
        spec = _CHECKS.get((name or "").strip().lower())
        if spec is None:
            raise Refused(f"I don't have a check called that. I have: "
                          f"{', '.join(CHECKS)}.")
        arg = (unit or "").strip()
        if spec.get("units"):
            arg = arg.removesuffix(".service")
            if arg not in ALLOWED_UNITS:
                self._record("check", f"{name} {arg}", False, "unit not allowed")
                here = [u for u, h in UNIT_HOSTS.items() if h == "local"][:4]
                raise Refused(
                    f"I'm not allowed to touch {arg or 'that'}. On my brain I "
                    f"can do {', '.join(here)}; on my head, servo-server and "
                    f"camera-stream; on my chest, inmoov-display.")
            arg = f"{arg}.service"
        self.touch()
        argv = _on_host(UNIT_HOSTS.get(arg.removesuffix(".service"), "local"),
                        spec["argv"](arg)) if spec.get("units") else spec["argv"](arg)
        self._record("check", " ".join(argv), True)
        return _run(argv, timeout)

    def run_shell(self, command: str, timeout: float = 30.0) -> str:
        """Run an arbitrary command. Only in the unrestricted tier."""
        cmd = (command or "").strip()
        if not self.enabled:
            raise Refused("Diagnostic mode is off. Turn it on from the admin panel.")
        if not self._unrestricted:
            self._record("shell", cmd, False, "restricted tier")
            raise Refused("I can only run my fixed checks right now. Unrestricted "
                          "mode has to be switched on from the panel.")
        if not cmd:
            raise Refused("You didn't say what to run.")
        if _SHELL_REFUSE.search(cmd):
            self._record("shell", cmd, False, "refused pattern")
            raise Refused("I won't run that one — it's destructive or it would "
                          "cut off the machine I'm running on.")
        self.touch()
        self._record("shell", cmd, True)
        return _run(["bash", "-c", cmd], timeout)

    def status(self) -> dict:
        on = self.enabled                     # property: also applies the timeout
        return {"enabled": on, "unrestricted": self._unrestricted and on,
                "remaining": round(self.remaining()),
                "idle_timeout": self.idle_timeout,
                "checks": list(CHECKS), "units": list(ALLOWED_UNITS),
                "audit": self.audit[-20:]}


# Spoken aloud, so the output has to be short enough to be said and still useful
# to a model reading it. This is the cap on what comes *back*, not on the reply.
OUTPUT_MAX = 4000


def _run(argv: list[str], timeout: float) -> str:
    """Run argv and return combined output, trimmed. Never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, check=False)
    except FileNotFoundError:
        return f"(no such command: {argv[0]})"
    except subprocess.TimeoutExpired:
        return f"(timed out after {timeout:g}s)"
    except Exception as exc:  # noqa: BLE001 - a failed check must not kill the turn
        return f"(couldn't run it: {type(exc).__name__}: {exc})"
    out = (p.stdout or "") + (p.stderr or "")
    out = out.strip() or f"(no output, exit {p.returncode})"
    if len(out) > OUTPUT_MAX:
        # Keep both ends: the head says what it is, the tail usually says what
        # went wrong.
        half = OUTPUT_MAX // 2
        out = out[:half] + "\n...[trimmed]...\n" + out[-half:]
    if p.returncode != 0:
        out = f"(exit {p.returncode})\n{out}"
    return out

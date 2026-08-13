"""A 4-digit PIN in front of the panel's settings and everything that moves him.

The panel has always been zero-auth, which was defensible while reaching it
meant being on the wired robot LAN. The `fred` access point changed that: anyone
who joins the SSID reaches ``/api/move`` and ``/api/cart/drive``, and the AP's
password is the only thing in the way.

**Be clear about what this is worth.** Four digits is ten thousand guesses, the
panel speaks plain HTTP, and the PIN therefore crosses the network in the clear
to anyone already sniffing it. This is a barrier against the person who joined
the network and started poking, which is the actual threat at a STEM event with
thirty curious teenagers. It is not a secret, and it would not survive an
attacker who wanted in. Treat the AP password and physical access as the real
perimeter; this stops the casual case and makes the deliberate one obvious.

What it does do properly:

* **The PIN is never stored.** settings.json keeps a random salt and a
  PBKDF2-HMAC-SHA256 digest, so reading the file does not hand over the PIN.
  (It would still fall to an offline sweep of all 10 000 — hence the paragraph
  above. The hash is only ever served to the robot's own LAN, see material().)
* **Online guessing is rate limited**, which is the defence that actually
  matters for four digits: five wrong tries and that address waits, doubling to
  a quarter of an hour. Ten thousand guesses at that rate is not an afternoon.
* **The robot's own machines are exempt by address.** The head and chest Pis
  call these endpoints constantly and cannot type a PIN. Rather than mint them
  tokens, the wired robot LAN is trusted and everything else — the access point,
  the house WiFi — is not. That is exactly the line the threat model draws.

**No PIN is set out of the box, and nothing is gated until one is.** Shipping a
default would repeat the mistake the AP made with `inmoov-robot`: a published
credential is not a credential. The panel says loudly that it is unprotected
until someone sets one.
"""
from __future__ import annotations

import hashlib
import ipaddress
import secrets
import threading
import time

ITERATIONS = 120_000            # ~0.1 s here, ~0.3 s on the chest Pi
PIN_LENGTH = 4
SESSION_S = 12 * 3600           # a session lasts a working day, then re-ask

# Wrong tries before an address starts waiting, and how long it then waits.
FREE_TRIES = 5
LOCK_BASE_S = 60
LOCK_MAX_S = 900

# The robot's own machines. Loopback plus the wired LAN that br0 serves; the
# access point (192.168.50/24) and the house WiFi are deliberately absent.
DEFAULT_TRUSTED = ("127.0.0.0/8", "::1/128", "10.0.0.0/24")

_lock = threading.Lock()
_sessions: dict[str, float] = {}                 # token -> expires at
_fails: dict[str, tuple[int, float]] = {}        # addr -> (count, locked until)


# ---- the PIN itself ------------------------------------------------------
def normalise(pin) -> str:
    """The PIN as exactly PIN_LENGTH digits, or "" if it is not one.

    Deliberately strict. A PIN that is silently truncated or zero-padded is a
    PIN whose owner does not know what it is.
    """
    # isascii() as well as isdigit(): str.isdigit() is true for Arabic-Indic and
    # other digit forms, and a PIN nothing on the keypad can produce is a PIN
    # its owner cannot type back.
    s = str(pin or "").strip()
    return s if len(s) == PIN_LENGTH and s.isascii() and s.isdigit() else ""


def hash_pin(pin: str, salt: str, iterations: int = ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt),
                               iterations).hex()


def make_material(pin: str) -> dict:
    """A fresh salt and digest for ``pin``, shaped as it is stored."""
    salt = secrets.token_hex(16)
    return {"salt": salt, "hash": hash_pin(pin, salt), "iterations": ITERATIONS}


def material(settings: dict) -> dict:
    return dict((settings.get("auth") or {}).get("pin") or {})


def is_set(settings: dict) -> bool:
    m = material(settings)
    return bool(m.get("salt") and m.get("hash"))


def check_pin(settings: dict, pin: str) -> bool:
    """Constant-time compare against the stored digest. No rate limiting here —
    that is the caller's job, because it needs to know who is asking."""
    m = material(settings)
    pin = normalise(pin)
    if not pin or not m.get("salt") or not m.get("hash"):
        return False
    got = hash_pin(pin, str(m["salt"]), int(m.get("iterations", ITERATIONS)))
    return secrets.compare_digest(got, str(m["hash"]))


# ---- who is asking -------------------------------------------------------
def trusted_networks(settings: dict) -> tuple:
    raw = (settings.get("auth") or {}).get("trusted_cidrs") or DEFAULT_TRUSTED
    nets = []
    for cidr in raw:
        try:
            nets.append(ipaddress.ip_network(str(cidr), strict=False))
        except ValueError:
            continue                    # a typo must not lock everyone out
    return tuple(nets)


def is_trusted(addr: str, settings: dict) -> bool:
    """Is this address one of the robot's own machines?

    A bad address is untrusted rather than an error: the safe answer to "I
    cannot tell who this is" is to ask for the PIN.
    """
    try:
        ip = ipaddress.ip_address((addr or "").strip())
    except ValueError:
        return False
    return any(ip in net for net in trusted_networks(settings))


# ---- rate limiting -------------------------------------------------------
def locked_for(addr: str) -> float:
    """Seconds this address must wait before its next try. 0.0 if it may go."""
    with _lock:
        _count, until = _fails.get(addr, (0, 0.0))
    return max(0.0, until - time.monotonic())


def note_failure(addr: str) -> float:
    """Record a wrong PIN. Returns the wait now imposed on this address."""
    with _lock:
        count, _until = _fails.get(addr, (0, 0.0))
        count += 1
        wait = 0.0
        if count > FREE_TRIES:
            wait = min(LOCK_BASE_S * (2 ** (count - FREE_TRIES - 1)), LOCK_MAX_S)
        _fails[addr] = (count, time.monotonic() + wait)
        return wait


def note_success(addr: str) -> None:
    with _lock:
        _fails.pop(addr, None)


# ---- sessions ------------------------------------------------------------
def open_session() -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = time.monotonic() + SESSION_S
        _sweep()
    return token


def valid_session(token: str) -> bool:
    if not token:
        return False
    with _lock:
        expires = _sessions.get(token)
        if expires is None:
            return False
        if expires < time.monotonic():
            _sessions.pop(token, None)
            return False
        return True


def close_session(token: str) -> None:
    with _lock:
        _sessions.pop(token, None)


def close_all_sessions() -> None:
    """Used when the PIN changes: whoever was in on the old one is out."""
    with _lock:
        _sessions.clear()


def _sweep() -> None:
    """Drop expired tokens. Called under the lock, on the rare write path."""
    now = time.monotonic()
    for token in [t for t, exp in _sessions.items() if exp < now]:
        _sessions.pop(token, None)

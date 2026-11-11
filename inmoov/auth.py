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
import hmac
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
def redact_settings(settings: dict) -> dict:
    """A copy of settings that is safe to serve off the robot's own LAN.

    Exists because /api/state served the live settings dict whole — including
    ``auth.pin.{salt,hash,iterations}`` and every device token — to any caller,
    and /api/state is deliberately an open route. A guest on the access point
    could fetch it once and sweep all ten thousand PINs offline against the
    digest, walking straight past the rate limiter this module maintains. That
    defeated the design stated twice in this file and once at
    /api/auth/material: the hash is only ever served to the robot's own LAN.

    Dropped: the whole ``auth`` subtree, any key containing ``token`` or
    ``secret`` at any depth, and any key that *is* ``pin`` (substring matching
    would eat innocents like "mapping"). Everything else passes through, so
    the panel keeps its iris colour and the chest keeps its snapshot — both of
    those callers are LAN-trusted anyway and never see this copy.

    Returns a new structure; the live dict is never mutated.
    """
    def scrub(node):
        if isinstance(node, dict):
            return {k: scrub(v) for k, v in node.items()
                    if k not in ("auth", "pin")
                    and "token" not in k.lower()
                    and "secret" not in k.lower()}
        if isinstance(node, list):
            return [scrub(v) for v in node]
        return node
    return scrub(dict(settings or {}))


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


# ---- sessions: stateless, signed, restart-surviving ----------------------
# The panel used to keep sessions in a process dict, which meant every brain
# restart re-keypadded every phone mid-event — the exact thing you cannot afford
# when thirty people share one AP and the operator just power-cycled to clear a
# wedged servo. A session is now a signed statement the server can re-verify
# without remembering it: "this cookie is good until <t>", HMAC'd with a secret
# that lives in settings.json and therefore survives the restart the dict did
# not. No server-side table, so nothing to lose on reboot and nothing to grow.
#
# Revocation is the honest cost of statelessness, handled two ways: changing the
# PIN rotates the secret, which invalidates every outstanding token at once (the
# close_all the old code did explicitly); a single logout clears the browser's
# cookie and adds the token to a small in-memory denylist, best-effort defence
# that a restart empties — acceptable, because a logged-out browser no longer
# holds the token to replay anyway.
_TOKEN_SEP = "."
_revoked: set[str] = set()          # best-effort single-token logout, per process


def session_secret(settings: dict, save=None) -> str:
    """The HMAC key for session tokens, minted once and persisted.

    Pass ``save=save_settings`` so a freshly-minted secret is written back the
    first time; without it a volatile key is used and tokens die on restart —
    the old behaviour, kept only for callers that have nowhere to persist.
    """
    a = settings.setdefault("auth", {})
    key = str(a.get("session_secret") or "")
    if not key:
        key = secrets.token_hex(32)
        a["session_secret"] = key
        if save is not None:
            try:
                save(settings)
            except Exception:       # noqa: BLE001 - a read-only disk must not stop logins
                pass
    return key


def _sign(payload: str, key: str) -> str:
    return hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def mint_token(key: str, ttl: int = SESSION_S) -> str:
    """A signed token good for ``ttl`` seconds: ``<exp>.<nonce>.<sig>``. Wall-clock
    expiry, not monotonic, so it still means something after the clock the process
    was timing against is gone; the nonce makes two tokens minted in the same
    second distinct, so a single logout revokes one without touching the other."""
    exp = str(int(time.time()) + int(ttl))
    payload = exp + _TOKEN_SEP + secrets.token_urlsafe(9)
    return payload + _TOKEN_SEP + _sign(payload, key)


def token_valid(token: str, key: str) -> bool:
    """True if ``token`` is well-formed, correctly signed with ``key``, unexpired,
    and not locally revoked. Constant-time on the signature compare."""
    if not token or not key or token in _revoked:
        return False
    parts = token.split(_TOKEN_SEP)
    if len(parts) != 3 or not parts[0].isdigit():
        return False
    exp, nonce, sig = parts
    if not secrets.compare_digest(sig, _sign(exp + _TOKEN_SEP + nonce, key)):
        return False
    return int(exp) > int(time.time())


def revoke_token(token: str) -> None:
    """Best-effort single-token logout (see the block comment)."""
    if token:
        _revoked.add(token)


def rotate_secret(settings: dict, save=None) -> None:
    """Mint a new secret, invalidating every outstanding token. Used when the PIN
    changes or is cleared — whoever was in on the old PIN is now out."""
    settings.setdefault("auth", {})["session_secret"] = secrets.token_hex(32)
    _revoked.clear()                # the old tokens can't verify anyway now
    if save is not None:
        try:
            save(settings)
        except Exception:           # noqa: BLE001
            pass


# ---- which hostnames answer here: a Host allowlist against DNS rebinding ----
# The LAN-trust exemption plus plain HTTP is DNS-rebindable: a browser on a
# trusted machine visits evil.com, whose DNS is flipped to 10.0.0.1 mid-session,
# and every fetch it makes now lands on the panel carrying the trusted source
# address — walking straight through is_trusted(). The one thing the attacker
# cannot forge is the Host header: the browser sets it from the URL bar, so it
# reads "evil.com", never a name this robot answers to. Rejecting an unknown
# Host closes the hole for the cost of a list that already exists in spirit.
#
# IP literals are always allowed: a browser only sends an IP Host when the user
# typed an IP, which is direct access already covered by the LAN/PIN perimeter,
# not a rebind. Tailnet (*.ts.net) names are allowed because their DNS is not
# attacker-controllable. Everything else must be on the list.
DEFAULT_ALLOWED_HOSTS = ("fred", "localhost")


def _hostname_only(host: str) -> str:
    """``host`` down to its bare name: no port, no brackets, lowercased."""
    h = (host or "").strip()
    if h.startswith("["):                       # [::1]:8080
        return h[1:h.index("]")].lower() if "]" in h else h[1:].lower()
    return (h.rsplit(":", 1)[0] if ":" in h else h).lower()


def allowed_hostnames(settings: dict) -> tuple:
    extra = (settings.get("auth") or {}).get("allowed_hosts") or ()
    names = set(DEFAULT_ALLOWED_HOSTS)
    for n in extra:
        n = str(n).strip().lower()
        if n:
            names.add(n)
    try:
        import socket
        names.add(socket.gethostname().lower())
    except Exception:               # noqa: BLE001 - a nameless host still has its defaults
        pass
    return tuple(names)


def host_allowed(host_header: str, settings: dict) -> bool:
    """Is this request's Host one this robot answers to? See the block comment."""
    name = _hostname_only(host_header)
    if not name:
        return True                 # no Host at all: not a browser, not a rebind
    try:
        ipaddress.ip_address(name)  # a bare IP can't be rebound
        return True
    except ValueError:
        pass
    if name == "localhost" or name.endswith(".localhost") or name.endswith(".ts.net"):
        return True
    return name in allowed_hostnames(settings)

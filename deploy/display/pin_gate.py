"""The PIN keypad in front of the chest settings menu.

The panel's PIN gates the settings and anything that moves him. This is the same
gate on the touchscreen, because the menu behind it reaches the same controls —
servo positions, the voice, the hotspot, and who may drive the cart.

**The stop button is on this screen, not behind it.** Everything else on the
menu waits for four digits; the e-stop cannot, because the moment you need it is
the moment you are least able to type. Gating a stop would have undone the
reason it was put on the cart page in the first place.

The PIN is checked *here*, against a local copy, because this menu's whole
promise is that it keeps working with the brain switched off — that is when you
are most likely to be standing at the robot wanting it. So the brain's salt and
digest are cached to ``pin.json`` next to this file: pushed by the brain when
the PIN changes, and re-fetched on open when the brain happens to be reachable.
The digest never leaves the wired robot LAN (see the brain's /api/auth/material).

If no PIN has ever been cached and the brain cannot be asked, the menu opens.
Failing closed would mean a robot whose touchscreen bricks itself the first time
its own network hiccups, guarding a screen you must already be standing at.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from pathlib import Path

import menu_ui as ui

PIN_PATH = Path(__file__).resolve().parent / "pin.json"
PIN_LENGTH = 4

# Same shape as the brain's rate limiting, for the same reason: four digits is
# ten thousand guesses. Slower here on purpose — a wrong PIN on a touchscreen is
# a fat finger, not a sweep, so the first few tries stay free and cheap.
FREE_TRIES = 5
LOCK_BASE_S = 30
LOCK_MAX_S = 300


# ---- the cached material -------------------------------------------------
def load() -> dict:
    try:
        data = json.loads(PIN_PATH.read_text())
    except (OSError, ValueError):
        return {}
    pin = data.get("pin") if isinstance(data, dict) else None
    return dict(pin) if isinstance(pin, dict) else {}


def save(material: dict) -> bool:
    """Write the cache. An empty dict means "the PIN was removed"."""
    try:
        tmp = PIN_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pin": dict(material or {})}))
        tmp.replace(PIN_PATH)                  # atomic: a torn file locks nobody out
        return True
    except OSError:
        return False


def fetch(nuc: str, timeout: float = 1.0) -> dict | None:
    """Ask the brain for the current material and cache it. None if unreachable.

    Kept short: this runs when the menu opens, and a brain that is down must
    cost a beat, not a visible stall before the keypad appears.
    """
    try:
        with urllib.request.urlopen(f"{nuc}/api/auth/material", timeout=timeout) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    material = data.get("pin") if data.get("pin_set") else {}
    material = dict(material) if isinstance(material, dict) else {}
    save(material)
    return material


def material(nuc: str = "") -> dict:
    """The material to check against: fresh if the brain answers, else cached."""
    if nuc:
        got = fetch(nuc)
        if got is not None:
            return got
    return load()


def is_set(material: dict) -> bool:
    return bool(material.get("salt") and material.get("hash"))


def check(pin: str, material: dict) -> bool:
    if not is_set(material) or len(pin) != PIN_LENGTH or not pin.isdigit():
        return False
    try:
        got = hashlib.pbkdf2_hmac("sha256", pin.encode(),
                                  bytes.fromhex(str(material["salt"])),
                                  int(material.get("iterations", 120_000))).hex()
    except (ValueError, TypeError):
        return False
    return got == str(material["hash"])


# ---- the keypad ----------------------------------------------------------
KEY_X0, KEY_X1 = 24, 470
KEY_Y0, KEY_Y1 = 168, 462
KEY_GAP = 10
STOP_X0, STOP_X1 = 490, 776
STOP_Y0, STOP_Y1 = 168, 462
DOT_Y = 104
DOT_W, DOT_H, DOT_GAP = 46, 52, 14

KEYS = (("1", "2", "3"),
        ("4", "5", "6"),
        ("7", "8", "9"),
        ("CLEAR", "0", "DEL"))


class PinPad:
    """Four digits, a stop button, and nothing else to get wrong.

    Submits on the fourth digit rather than offering an ENTER key: the length is
    fixed and known, so an extra tap would only ever be a way to get it wrong.
    """

    def __init__(self, material: dict, log=print):
        self._material = dict(material or {})
        self._log = log
        self._entry = ""
        self._message = ""
        self._fails = 0
        self._locked_until = 0.0
        self.unlocked = not is_set(self._material)

        self._keys = []
        cols, rows = len(KEYS[0]), len(KEYS)
        kw = (KEY_X1 - KEY_X0 - KEY_GAP * (cols - 1)) // cols
        kh = (KEY_Y1 - KEY_Y0 - KEY_GAP * (rows - 1)) // rows
        for r, row in enumerate(KEYS):
            for c, label in enumerate(row):
                x0 = KEY_X0 + c * (kw + KEY_GAP)
                y0 = KEY_Y0 + r * (kh + KEY_GAP)
                self._keys.append((label, ui.Button(x0, y0, x0 + kw, y0 + kh,
                                                    label, scale=3 if len(label) > 1 else 5)))
        self._stop = ui.Button(STOP_X0, STOP_Y0, STOP_X1, STOP_Y1)

    # ---- input -----------------------------------------------------------
    def on_touch(self, kind: str, x: int, y: int, net) -> None:
        if kind != "down":
            return
        if self._stop.hit(x, y):
            # No confirmation, and no PIN. See the module docstring.
            net.post_cart_stop()
            self._message = "CART STOPPED"
            return
        if self._wait() > 0:
            return                              # locked out; keys are dead
        for label, button in self._keys:
            if not button.hit(x, y):
                continue
            if label == "CLEAR":
                self._entry, self._message = "", ""
            elif label == "DEL":
                self._entry, self._message = self._entry[:-1], ""
            elif len(self._entry) < PIN_LENGTH:
                self._entry += label
                if len(self._entry) == PIN_LENGTH:
                    self._submit()
            return

    def _submit(self) -> None:
        if check(self._entry, self._material):
            self.unlocked = True
            self._log("menu: PIN accepted")
            return
        self._fails += 1
        self._entry = ""
        wait = 0.0
        if self._fails > FREE_TRIES:
            wait = min(LOCK_BASE_S * (2 ** (self._fails - FREE_TRIES - 1)), LOCK_MAX_S)
            self._locked_until = time.monotonic() + wait
        self._message = "WRONG PIN" if wait <= 0 else ""
        self._log(f"menu: wrong PIN ({self._fails} wrong)")

    def _wait(self) -> float:
        return max(0.0, self._locked_until - time.monotonic())

    # ---- drawing ---------------------------------------------------------
    def draw(self, frame, snap: dict) -> None:
        wait = self._wait()
        ui.text(frame, "ENTER PIN TO OPEN SETTINGS", KEY_X0, 68, ui.DIM_INK, 2)

        # Filled boxes rather than asterisks: the font has no "*" and an unknown
        # character renders as a blank, which would read as a lost keypress.
        for i in range(PIN_LENGTH):
            x0 = KEY_X0 + i * (DOT_W + DOT_GAP)
            filled = i < len(self._entry)
            # The dots show how many digits are in; they are not keys.
            ui.readout(frame, x0, DOT_Y, x0 + DOT_W, DOT_Y + DOT_H)

        for label, button in self._keys:
            button.draw(frame, ink=ui.DIM_INK if wait > 0 else ui.INK)

        if wait > 0:
            ui.text(frame, f"TOO MANY TRIES - WAIT {wait:.0f}S", KEY_X0 + 300,
                    DOT_Y + 18, ui.BAD_INK, 2)
        elif self._message:
            ink = ui.OK_INK if self._message == "CART STOPPED" else ui.BAD_INK
            ui.text(frame, self._message, KEY_X0 + 300, DOT_Y + 18, ink, 2)

        self._draw_stop(frame, snap)

    def _draw_stop(self, frame, snap: dict) -> None:
        """Same face and the same meaning as the cart page's, so it reads as one
        control that happens to be reachable from two screens."""
        cart = (snap.get("chest") or {}).get("cart") or {}
        latched = bool(cart.get("estop"))
        ink = ui.WARN_INK if latched else ui.BAD_INK
        # See page_cart._draw_stop: own colours, theme's shape, and it
        # acknowledges the tap like every other control.
        ui.draw_face(frame, STOP_X0, STOP_Y0, STOP_X1, STOP_Y1, ui.STOP_PANEL,
                     ink, self._stop.phase(), weight=3.0)
        ui.text_centred(frame, "STOP", STOP_X0, STOP_X1, STOP_Y0 + 92, ink, 8)
        ui.text_centred(frame, "E-STOP LATCHED" if latched else "NO PIN NEEDED",
                        STOP_X0, STOP_X1, STOP_Y0 + 172, ink, 2)

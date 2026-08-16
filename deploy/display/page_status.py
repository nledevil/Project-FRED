""""Is everything talking to each other?" — the page this menu exists for.

One row per part of FRED, and the honest answer for each. The rows are about
*links*, not boxes: FRED is three computers and a drive base, and the
interesting failure is almost never "a Pi is off", it is "the Pi is fine and
nobody can reach it".

Each row therefore reports two different things where they exist:

* **NUC** — can this chest reach the brain's panel at all.
* **HEAD** — can *we* reach the head's servo server, and separately, can the
  **brain** reach it (``servo_link`` in the brain's own state). Those come apart:
  the head can be up and answering us while the brain's link to it is down, and
  that is precisely the case where FRED looks fine and does nothing.
* **CHEST** — local, so no link to test; what matters is whether the sensor node
  in the stomach is still feeding the relay.
* **CART** — the drive base's Pico, the hoverboard behind it, and the two states
  (e-stop, PS2 remote) that make a healthy cart ignore the brain.
* **AUDIO** — both directions. Speech out is easy; the ear is the one worth
  building, because a mic muted at the hardware looks perfect from the host.

Anything unknown says so rather than guessing. A blank where a fault should be
is the one thing a status page must never do.
"""
from __future__ import annotations

import theme

# The inks these rows label their answers with. Read at import: the panel
# re-execs on a theme change, so import time is always current.
_P = theme.palette()
INK, DIM_INK = _P["INK"], _P["DIM_INK"]
OK_INK, WARN_INK, BAD_INK = _P["OK_INK"], _P["WARN_INK"], _P["BAD_INK"]

# How long a capturing mic may produce nothing but exact zeros before the row
# stops calling it fine. Not a mute detector: the PowerConf gates a quiet room
# to exact zeros as well (30 s of them, measured, with the mic live), so this
# is "nothing has been heard in a while" and the cause is left to the human.
# A minute is long enough that an ordinary quiet spell doesn't raise it, short
# enough to catch a mute in the same visit.
MIC_SILENCE_WARN = 60.0




def _duration(seconds: float) -> str:
    """A span of time, as opposed to _age's "when did that happen"."""
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}S"
    if seconds < 5400:
        return f"{seconds // 60}M"
    return f"{seconds // 3600}H"


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "NEVER"
    if seconds < 1:
        return "NOW"
    if seconds < 90:
        return f"{int(seconds)}S AGO"
    return f"{int(seconds / 60)}M AGO"


class StatusPage:
    title = "STATUS"

    def on_touch(self, kind: str, x: int, y: int, net) -> None:
        """Nothing to press — the page is a readout. The poller refreshes it."""

    # ---- rows -------------------------------------------------------------
    def _nuc_row(self, snap: dict) -> tuple[str, tuple, list[str]]:
        s, err = snap.get("nuc"), snap.get("nuc_error")
        if not s:
            return "NO LINK", BAD_INK, [err or "UNREACHABLE"]
        detail = []
        brain = (s.get("brain") or {}).get("active")
        if brain:
            detail.append(f"BRAIN {brain}".upper())
        temp = snap.get("nuc_temp")
        if temp is not None:
            detail.append(f"{round(float(temp))}C")
        detail.append("MOCK" if s.get("mock") else "LIVE")
        return "OK", OK_INK, detail

    def _head_row(self, snap: dict) -> tuple[str, tuple, list[str]]:
        direct, err = snap.get("head"), snap.get("head_error")
        # The brain's own view of the same link, which is the one that decides
        # whether FRED can actually move.
        link = ((snap.get("nuc") or {}).get("servo_link")) or {}
        brain_sees = link.get("online")

        if not direct:
            return "DOWN", BAD_INK, [err or "UNREACHABLE"]
        if brain_sees is False:
            # We can reach the head and the brain cannot. This is the failure
            # worth shouting about: everything looks alive and FRED still won't
            # move, because the link that matters is the brain's, not ours.
            return "NO BRAIN LINK", BAD_INK, [
                "HEAD UP - BRAIN CANT REACH IT",
                str(link.get("error") or "").upper()]
        detail = ["SERVOS MOCK" if direct.get("mock") else "SERVOS LIVE"]
        if direct.get("suspended"):
            detail.append("SUSPENDED - HANDED OFF")
        elif brain_sees is None and snap.get("nuc"):
            detail.append("BRAIN LINK UNKNOWN")
        return "OK", OK_INK, detail

    def _cart_row(self, snap: dict) -> tuple[str, tuple, list[str]]:
        """The drive base: its Pico, the hoverboard mainboard behind it, and the
        two states that silently stop it obeying the brain.

        **An unattached cart is dim, not red.** The cart is optional and spends
        most of its life off the robot; a row that is permanently red is a row
        you stop reading, and then it is red for a real reason one day and you
        miss it. Red here means "attached and wrong".

        E-stop and the PS2 remote get their own states because both look exactly
        like a broken cart from the brain's side: commands accepted, nothing
        moves. The remote takes priority by design, and e-stop is latched until
        it is cleared.
        """
        cart = (snap.get("chest") or {}).get("cart")
        brain = snap.get("nuc_cart") or {}
        # Two independent switches, and either one alone stops the cart: the
        # chest owns the serial link, the brain owns whether it will ever send a
        # command. A row that only watched the chest would say OK for a cart
        # nothing is allowed to drive.
        brain_off = bool(snap.get("nuc")) and not brain.get("enabled", True)

        if cart is None:
            return "UNKNOWN", DIM_INK, ["NO ANSWER FROM CHEST"]
        if cart.get("enabled") is False:
            return "OFF", DIM_INK, ["DRIVER DISABLED ON CHEST"]
        if not cart.get("connected"):
            detail = [str(cart.get("last_error") or "NO PICO").upper()]
            if brain_off:
                detail.append("AND DISABLED IN BRAIN SETTINGS")
            return "NOT ATTACHED", DIM_INK, detail
        if brain_off:
            # Plugged in and healthy, but the brain will refuse to drive it.
            return "BRAIN OFF", WARN_INK, ["CART DISABLED IN SETTINGS"]
        if cart.get("estop"):
            return "ESTOP", BAD_INK, ["LATCHED - CLEAR IT TO DRIVE"]
        if cart.get("ps2_active"):
            return "PS2 REMOTE", WARN_INK, ["REMOTE HAS PRIORITY OVER BRAIN"]
        if not cart.get("mainboard_seen"):
            return "NO MAINBOARD", BAD_INK, ["PICO OK - HOVERBOARD SILENT"]

        age = cart.get("telemetry_age")
        detail = []
        volts, temp = cart.get("battery_v"), cart.get("board_temp_c")
        if volts is not None:
            detail.append(f"BATT {float(volts):.1f}V")
        if temp is not None:
            detail.append(f"{round(float(temp))}C")
        line = " ".join(detail) or "NO TELEMETRY YET"
        if cart.get("moving"):
            line += " MOVING"
        if age is not None and age > 5:
            return "STALE", BAD_INK, [f"NO TELEMETRY {_age(age)}"]
        return "OK", OK_INK, [line]

    def _audio_row(self, snap: dict) -> tuple[str, tuple, list[str]]:
        """Both directions, because "audio is broken" is two different faults.

        Out is easy: the brain says whether it can speak and which synthesiser
        it has. In is the one worth building — the USB speakerphone has a mute
        button of its own, and when it is pressed nothing on the host looks
        wrong. ALSA reports the capture control unmuted at 100%, arecord runs
        and returns data, and the data is exact zeros. FRED looks like he is
        listening and hears nothing, for hours.

        The row reports **the fact, not the diagnosis**: "no signal for N".
        Tempting as it is to call a zero run a mute, this microphone gates a
        quiet room to exact zeros as well, so that claim would be wrong every
        time nobody is talking — and a false alarm in red is how a status panel
        stops being read. The mute button gets a mention as the first thing to
        check, which is the useful half of the guess without the certainty.
        """
        nuc = snap.get("nuc") or {}
        if not nuc:
            return "UNKNOWN", DIM_INK, ["NO LINK TO BRAIN"]
        sound = nuc.get("sound") or {}
        voice = nuc.get("voice") or {}
        mic = voice.get("mic") or {}

        if not sound:
            return "NO OUTPUT", BAD_INK, ["BRAIN HAS NO SOUND DEVICE"]
        if sound.get("suspended"):
            return "HANDED OFF", DIM_INK, ["AUDIO RELEASED TO MYROBOTLAB"]
        if sound.get("audit"):
            return "AUDIT", WARN_INK, ["SPEECH RENDERED - NOT PLAYED"]
        if not sound.get("can_speak"):
            return "MUTE OUT", BAD_INK, ["CANNOT SPEAK - NO TTS OR DEVICE"]

        tts = str(sound.get("tts") or "?").upper()
        if not voice.get("available"):
            # Output is fine; there is simply no ear fitted (no mic, no model).
            return "OUT ONLY", DIM_INK, [f"TTS {tts} - NO VOICE INPUT"]
        if not mic.get("capturing"):
            state = "SPEAKING" if voice.get("speaking") else "NOT LISTENING"
            return "IDLE", DIM_INK, [f"TTS {tts} - MIC {state}"]

        silent = mic.get("silent_for")
        if silent is not None and silent >= MIC_SILENCE_WARN:
            return "NO MIC SIGNAL", WARN_INK, [
                f"NOTHING HEARD FOR {_duration(silent)}",
                "CHECK THE MUTE BUTTON ON THE MIC"]
        return "OK", OK_INK, [f"TTS {tts} - MIC LIVE"]

    def _chest_row(self, snap: dict) -> tuple[str, tuple, list[str]]:
        # We are running on the chest, so there is no link to test — the useful
        # question is whether the stomach node is still feeding the relay.
        sensors = (snap.get("chest") or {}).get("sensors") or {}
        if not sensors:
            return "OK", DIM_INK, ["RELAY OFF"]
        if not sensors.get("connected"):
            return "NO NODE", BAD_INK, ["PICO UNPLUGGED"]
        age = sensors.get("age")
        node = (sensors.get("node") or "").upper()[:10]
        if age is None:
            return "NO DATA", BAD_INK, ["NOTHING FROM PICO"]
        fresh = age < 15
        return ("OK" if fresh else "STALE"), (OK_INK if fresh else BAD_INK), \
               [f"{node} {_age(age)}".strip()]

    # ---- drawing ----------------------------------------------------------
    def rows(self, snap: dict) -> list:
        """The five rows as data: (name, where, state, ink, detail).

        Split out of draw() so a second renderer can show the same page without
        a second copy of the logic that decides whether the head is up. What is
        *drawn* can differ between renderers; what the panel claims about the
        robot must not.
        """
        return [("NUC", "10.0.0.1", *self._nuc_row(snap)),
                ("HEAD", "10.0.0.10", *self._head_row(snap)),
                ("CHEST", "LOCAL", *self._chest_row(snap)),
                ("CART", "DRIVE BASE", *self._cart_row(snap)),
                ("AUDIO", "MIC / SPEAKER", *self._audio_row(snap))]


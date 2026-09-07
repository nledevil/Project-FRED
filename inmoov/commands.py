"""FRED's command vocabulary — the bridge between spoken/typed language and the
robot's actuators.

One registry, two consumers:
  * the **local matcher** (``match_local``) recognises the common commands with
    plain regex — instant, offline, no API call;
  * **Claude** (see ``brain.py``) gets the same actions as tool definitions
    (``CLAUDE_TOOLS``) so it can carry out natural-language commands the matcher
    missed, and mix them with a spoken answer.

Both funnel into ``execute_action(ctx, name, **args)``, which drives the shared
hardware singletons and returns a short line for FRED to speak back.

``ctx`` is any object exposing ``controller`` (ServoController), ``led`` (Led),
``tracker`` (FaceTracker) and ``sound`` (Sound) — see ``assistant.py``.
"""
from __future__ import annotations

import io
import random
import re

from . import diagnostic, sysinfo, whoami

# The one tool whose result is a picture rather than a sentence. brain.py
# special-cases it (see Brain._look) because only there is it known which
# backend is answering — the local model is text-only and would be handed a
# wall of base64. Named here so the two files can't drift apart.
VISION_TOOL = "look_at_what_you_see"

# What gets sent to the vision model. Image cost is roughly pixels/750 tokens,
# so the long edge is the price dial: 1024 is ~1050 tokens against the ~1600 a
# full 1568px frame costs, and is still ample for "how many people are there?"
# or "what colour is my shirt?". The API's own ceiling is 1568 on the long edge;
# anything larger is downscaled server-side and billed for the trip anyway.
VIEW_MAX_EDGE = 1024
VIEW_QUALITY = 80

# Spoken when terminator mode is engaged — one at random, for flavour.
TERMINATOR_PHRASES = (
    "I'll be back.",
    "Hasta la vista, baby.",
    "Come with me if you want to live.",
    "Get down!",
    "I need your clothes, your boots, and your motorcycle.",
)


def _jaw(ctx, opened: bool) -> float:
    """Command the jaw to its open (max) or closed (rest) calibrated angle."""
    s = ctx.controller.servos.get("jaw")
    if not s:
        return 0.0
    return ctx.controller.set_angle("jaw", s["max_angle"] if opened else s["rest_angle"])


# Eye/neck presets for "look ..." — min/max/rest of the calibrated travel. The
# left/right sense depends on wiring; flip here if it comes out mirrored.
def _look(ctx, direction: str) -> str:
    c = ctx.controller
    ex, ey = c.servos.get("eye_x"), c.servos.get("eye_y")
    d = direction.lower()
    if d == "center":
        if ex:
            c.set_angle("eye_x", ex["rest_angle"])
        if ey:
            c.set_angle("eye_y", ey["rest_angle"])
        return "Looking straight ahead."
    if d in ("left", "right") and ex:
        c.set_angle("eye_x", ex["min_angle"] if d == "left" else ex["max_angle"])
        return f"Looking {d}."
    if d in ("up", "down") and ey:
        # Vertical is the other way round: on this build a *larger* eye_y angle
        # points the eyes DOWN. That isn't a guess — FaceTracker._track_face has
        # always relied on it. It signs its error as "+ ey = face low" and then
        # adds that straight onto the current angle, so a face below the lens
        # raises eye_y. Tracking has worked all along, which makes increasing =
        # down the build's real convention; this preset was the odd one out, and
        # "look up" duly looked down.
        c.set_angle("eye_y", ey["min_angle"] if d == "up" else ey["max_angle"])
        return f"Looking {d}."
    return "I can look left, right, up, down, or center."


# Head/neck rotation for "turn your head ..." — the whole head, not just the
# eyes. Uses move_smooth so the turn looks deliberate rather than snapping. As
# with _look, the left/right sense follows the wiring; flip here if mirrored.
def _turn_head(ctx, direction: str) -> str:
    c = ctx.controller
    nk = c.servos.get("neck")
    if not nk:
        return "My neck isn't wired up."
    d = direction.lower()
    if d == "center":
        c.move_smooth("neck", nk["rest_angle"], duration=0.6)
        return "Facing forward."
    if d in ("left", "right"):
        c.move_smooth("neck", nk["min_angle"] if d == "left" else nk["max_angle"],
                      duration=0.6)
        return f"Turning my head {d}."
    return "I can turn my head left, right, or center."


# The two servos nothing could reach until now. head_tilt_fb nods (chin down and
# up); head_tilt_lr tips the head sideways, ear toward shoulder. Between them
# and neck/eyes/jaw, every servo on the robot has a tool.
#
# Which way is "down" is not a guess for head_tilt_fb. FaceTracker adds
# ``dir_t * tilt_gain * cmd_y`` to it using the same signed command it gives
# eye_y, and _look above records that a *larger* eye_y points DOWN — so a larger
# head_tilt_fb tilts down too, and tracking has worked all along on that basis.
#
# head_tilt_lr genuinely is a guess: FaceTracker never touches it, so there is
# nothing to infer a sense from. Larger is taken to mean tipping right. If he
# leans the wrong way, swap the two angles here — the same note _look and
# _turn_head carry about their own wiring.
def _tilt_head(ctx, direction: str) -> str:
    c = ctx.controller
    fb, lr = c.servos.get("head_tilt_fb"), c.servos.get("head_tilt_lr")
    d = (direction or "").lower()
    if d in ("center", "centre", "level", "straight"):
        moved = []
        for name, s in (("head_tilt_fb", fb), ("head_tilt_lr", lr)):
            if s:
                c.move_smooth(name, s["rest_angle"], duration=0.5)
                moved.append(name)
        return "Head level." if moved else "My head tilt isn't wired up."
    if d in ("up", "back"):
        if not fb:
            return "I can't tilt up and down — that servo isn't wired up."
        c.move_smooth("head_tilt_fb", fb["min_angle"], duration=0.6)
        return "Looking up."
    if d in ("down", "forward"):
        if not fb:
            return "I can't tilt up and down — that servo isn't wired up."
        c.move_smooth("head_tilt_fb", fb["max_angle"], duration=0.6)
        return "Looking down."
    if d in ("left", "right"):
        if not lr:
            return "I can't tip my head sideways — that servo isn't wired up."
        c.move_smooth("head_tilt_lr",
                      lr["min_angle"] if d == "left" else lr["max_angle"],
                      duration=0.6)
        return f"Tilting my head {d}."
    return "I can tilt my head up, down, left, right, or level."


def _nod(ctx, times: int = 2) -> str:
    """Yes. Chin down and back up, and it must end where it started.

    A gesture rather than a position: asked to nod, nobody wants the head parked
    chin-down. Each move blocks (see ServoController.move_smooth), so this is
    simply a sequence, and the last one always returns to rest — including when
    the loop is cut short by a bad count.
    """
    c = ctx.controller
    fb = c.servos.get("head_tilt_fb")
    if not fb:
        return "I can't nod — my head tilt servo isn't wired up."
    n = max(1, min(5, int(times or 2)))     # five is already a long time to watch
    rest = fb["rest_angle"]
    # Part of the way, not the end stops: a nod is a gesture, and slamming to the
    # limit and back looks like a fault rather than agreement.
    down = rest + (fb["max_angle"] - rest) * 0.55
    up = rest - (rest - fb["min_angle"]) * 0.35
    for _ in range(n):
        c.move_smooth("head_tilt_fb", down, duration=0.35)
        c.move_smooth("head_tilt_fb", up, duration=0.35)
    c.move_smooth("head_tilt_fb", rest, duration=0.3)
    return "Nodding."


def _shake_head(ctx, times: int = 2) -> str:
    """No. Left and right on the neck, ending centred for the same reason."""
    c = ctx.controller
    nk = c.servos.get("neck")
    if not nk:
        return "I can't shake my head — my neck isn't wired up."
    n = max(1, min(5, int(times or 2)))
    rest = nk["rest_angle"]
    left = rest - (rest - nk["min_angle"]) * 0.45
    right = rest + (nk["max_angle"] - rest) * 0.45
    for _ in range(n):
        c.move_smooth("neck", left, duration=0.3)
        c.move_smooth("neck", right, duration=0.3)
    c.move_smooth("neck", rest, duration=0.3)
    return "Shaking my head."


def _speak_distance(cm: float) -> str:
    """A distance the way a person would say it out loud."""
    if cm >= 399:
        return "clear"                       # no echo came back: nothing in range
    if cm >= 100:
        m = cm / 100.0
        return "1 metre" if round(m, 1) == 1.0 else f"{m:.1f} metres"
    return f"{round(cm)} centimetres"


def _speak_ago(sec: float) -> str:
    if sec < 5:
        return "just now"
    return f"{round(sec)} seconds ago" if sec < 90 else f"{round(sec / 60)} minutes ago"


# Displays that show FRED's voice state — listening, thinking, speaking — as
# opposed to the ones that are only decorative. At an event this is the panel a
# child reads to know whether he heard them, so event mode will not let him
# swap it for a flourish. See _set_chest_display.
VOICE_STATE_DISPLAYS = ("voice-hud", "voice-hud-c", "face", "face-talk")


def _set_chest_display(ctx, animation: str) -> str:
    """Put something on the screen in FRED's chest, as part of an answer."""
    display = getattr(ctx, "display", None)
    if display is None or not display.configured():
        return "I don't have a chest screen wired up."
    try:
        available = display.animations()
    except Exception as exc:  # noqa: BLE001 - an unreachable panel is a spoken answer
        return f"I can't reach my chest screen: {exc}"

    ids = [str(a.get("id") or "") for a in available if a.get("id")]
    want = (animation or "").strip().lower()
    if want not in ids:
        # Enumerated rather than guessed at: the presets live on the chest Pi and
        # a hardcoded list here would go stale the moment one is added.
        return ("I can't show that. I can show: " + ", ".join(ids) + ".") if ids \
            else "My chest screen isn't offering anything to show."

    event = getattr(ctx, "event", None)
    if getattr(event, "enabled", False) and want not in VOICE_STATE_DISPLAYS:
        # The turn-taking signal outranks the flourish. A child in a queue reads
        # that panel to know whether he is listening to them or to someone else,
        # and swapping it for an arc reactor takes that away at exactly the
        # moment it is load-bearing.
        return ("Not while there's a queue — my chest is showing whether I'm "
                "listening, and people need to see that.")

    try:
        display.select(want)
    except Exception as exc:  # noqa: BLE001
        return f"That didn't take: {exc}"
    label = next((str(a.get("label") or want) for a in available
                  if a.get("id") == want), want)
    return f"Chest screen is showing {label} now."


def _play_sound(ctx, name: str) -> str:
    """Play one of the clips on the robot, as punctuation to an answer."""
    sound = getattr(ctx, "sound", None)
    if sound is None or not sound.available():
        return "I don't have any audio to play it through."
    names = []
    try:
        names = [str(n) for n in sound.list()]
    except Exception:  # noqa: BLE001 - fall through to attempting it anyway
        pass
    # Matched loosely on purpose. Asked for "your startup sound", Claude passes
    # "startup sound", and an exact compare against the file stem missed it —
    # it then tried twice and told the person it did not know which one they
    # meant, about a clip it had.
    want = (name or "").strip().lower()
    for suffix in (" sound", " clip", ".wav"):
        if want.endswith(suffix):
            want = want[: -len(suffix)].strip()
    match = next((n for n in names if n.lower() == want), None)
    if match is None:
        match = next((n for n in names if want and want in n.lower()), None)
    if match is None:
        return ("I don't have that one. I have: " + ", ".join(names) + "."
                if names else "I don't have any sounds loaded.")
    if not sound.play(match):
        return f"I couldn't play {match}."
    # Terse rather than empty: an empty tool result read as a failure and got
    # the tool called a second time.
    return f"Played {match}."


# How loud FRED may make himself when *asked out loud*, as against from the
# panel. The panel keeps the full 0-100; this band is narrower on purpose, and
# the reasoning is already written down in tools/test_auth.py, which gates the
# panel's volume behind the PIN: "Zero is a mute that looks like broken hardware
# to whoever is standing there, and full scale in a hall is its own disruption."
#
# A spoken command has no PIN and cannot have one — anybody standing in front of
# him at an event can talk to him, including children who will absolutely try
# "Fred, mute yourself". So the voice path gets a floor it cannot mute below and
# a ceiling it cannot deafen a room with, and the operator keeps the real range
# on the PIN-gated slider.
VOICE_VOLUME_MIN = 20
VOICE_VOLUME_MAX = 85
VOICE_VOLUME_STEP = 10


def _set_volume(ctx, direction: str = "", percent=None) -> str:
    """Change how loud FRED speaks, on request. Never persisted — see below.

    Deliberately does not write settings.json, which is the other half of the
    same split: the level an operator chose from the panel is the one the robot
    should come back to. If a visitor turns him down at an event, that lasts
    until the next restart rather than becoming the robot's new idea of itself.
    """
    sound = getattr(ctx, "sound", None)
    if sound is None or not sound.available():
        return "I don't have any audio to set the level on."
    now = sound.volume()
    if now is None:
        return "I can't change my volume — this sound card has no level control."

    if percent is not None:
        try:
            want = int(round(float(percent)))
        except (TypeError, ValueError):
            return "I didn't get a level I could use."
    elif direction:
        step = VOICE_VOLUME_STEP if direction.lower().startswith("u") else -VOICE_VOLUME_STEP
        want = now + step
    else:
        return f"I'm at {now} percent."

    capped = min(max(want, VOICE_VOLUME_MIN), VOICE_VOLUME_MAX)
    if capped == now:
        # Saying "done" when nothing moved is how a robot gets asked four times.
        # But "already at my limit" and "already at the level you asked for" are
        # different facts, and reporting the first for the second told somebody
        # 60 percent was his ceiling when it is 85.
        if capped <= VOICE_VOLUME_MIN or capped >= VOICE_VOLUME_MAX:
            edge = "quiet" if capped <= VOICE_VOLUME_MIN else "loud"
            return f"That's already as {edge} as I go by voice — I'm at {now} percent."
        return f"I'm already at {now} percent."
    if not sound.set_volume(capped):
        return "I couldn't change my volume."
    if capped != want:
        # Say that it *moved*, not just where the limit is. "That's as far as I
        # go, I'm at 20 percent" got read back as "already at my quietest", which
        # tells the person nothing changed when it just had.
        return (f"I've set it to {capped} percent, which is as far as I go by "
                "voice. Use the panel if you need more.")
    return f"Volume {capped} percent."


def _diagnostic(ctx, check: str = "", unit: str = "", command: str = "") -> str:
    """Run a diagnostic on request. Off unless the panel switch is on.

    Both tiers land here so there is one place that turns a Refused into
    something FRED can say. The refusal text is deliberately spoken rather than
    swallowed: "I can't do that right now, the switch is off" is an answer, and
    silence is a robot that looks broken.
    """
    mode = getattr(ctx, "diagnostic", None)
    if mode is None:
        return "I don't have diagnostic mode on this build."
    try:
        if command:
            return mode.run_shell(command)
        return mode.run_check(check, unit)
    except diagnostic.Refused as why:
        return str(why)


def _speak_uptime(sec: float) -> str:
    """Uptime as something sayable. Nobody wants "27143 seconds"."""
    if sec < 60:                 # only ever true just after a reboot
        return "less than a minute"
    mins = sec / 60
    if mins < 60:
        return "a minute" if round(mins) == 1 else f"{round(mins)} minutes"
    hours = mins / 60
    if hours < 36:
        return "an hour" if round(hours) == 1 else f"{round(hours)} hours"
    days = hours / 24
    return "a day" if round(days) == 1 else f"{round(days)} days"


def _health_report(ctx) -> str:
    """How FRED is doing, phrased to be spoken aloud.

    The facts block already hands Claude the date, the addresses and the
    processor temperature on every turn, so the point of this is the rest: how
    long he has been up, and what the drive base's own board says about its
    battery and its heat — which nothing else in the conversation can see.

    Everything degrades to a clause rather than an absence. "How are you
    feeling?" should never come back empty, and a missing battery reading is
    itself worth saying: it usually means the base is switched off.
    """
    parts: list[str] = []

    up = whoami.uptime_s()
    if up:
        parts.append(f"I've been awake for {_speak_uptime(up)}")

    c = sysinfo.soc_temp_c()
    if c is not None:
        # Warm rather than a number he'd have to interpret; the exact figure is
        # in the facts block already if someone actually asks for degrees.
        parts.append(f"my processor is at {c:.0f} degrees Celsius"
                     + (" and running hot" if c >= 80 else ""))

    cart = getattr(ctx, "cart", None)
    if cart is None or not cart.configured():
        parts.append("I don't have a drive base wired up")
        return ", ".join(parts) + "."

    try:
        st = cart.state()
    except Exception:  # noqa: BLE001 - CartError or a network blip; both are "can't ask"
        parts.append("I can't reach my drive base to check its battery")
        return ", ".join(parts) + "."

    volts, board = st.get("battery_v"), st.get("board_temp_c")
    if volts:
        # A 10S hoverboard pack: ~42 V charged, ~33 V empty. Rough on purpose —
        # voltage sags under load, so a percentage would be false precision.
        level = ("well charged" if volts >= 39 else
                 "getting low" if volts >= 35 else "nearly flat")
        parts.append(f"my battery is {level} at {volts:.1f} volts")
    else:
        # The board only reports while it is powered; the panel can be talking
        # to the chest Pi quite happily and still hear nothing from the wheels.
        parts.append("my base isn't reporting a battery, so it's probably switched off")
    if board:
        parts.append(f"the board in my base is at {board:.0f} degrees")
    if st.get("estop"):
        parts.append("and my emergency stop is engaged, so I can't drive until it's cleared")
    return ", ".join(parts) + "."


def _sensor_label(name: str) -> str:
    """``dist_left`` -> ``left``. Names come from the node's own config, so strip
    the type prefix rather than assuming a fixed set."""
    for prefix in ("dist_", "pir_", "sensor_"):
        if name.startswith(prefix):
            return name[len(prefix):].replace("_", " ")
    return name.replace("_", " ")


def _sensor_report(ctx, which: str = "all") -> str:
    """What the proximity sensors see, phrased to be spoken aloud.

    Deliberately answers "is anyone there?" rather than dumping numbers: the
    readings alone are an instant, so movement is reported from the hub's recent
    event history, which is the only thing that can say someone *walked past*.
    """
    hub = getattr(ctx, "sensors", None)
    if hub is None:
        return "I don't have any proximity sensors wired up."
    nodes = (hub.state() or {}).get("nodes") or {}
    online = {k: v for k, v in nodes.items() if v.get("online")}
    if not nodes:
        return "My proximity sensors haven't reported in yet."
    if not online:
        return "My proximity sensors have gone quiet — I'm not hearing from them."

    dists, motions = [], []
    for n in online.values():
        for name, r in (n.get("readings") or {}).items():
            if not isinstance(r, dict):
                continue
            if r.get("type") == "distance" and r.get("cm") is not None:
                dists.append((_sensor_label(name), float(r["cm"])))
            elif r.get("type") == "motion":
                motions.append((bool(r.get("active")), bool(r.get("warming"))))

    parts = []
    if which in ("all", "distance") and dists:
        dists.sort()
        if all(cm >= 399 for _, cm in dists):
            parts.append("nothing is within range of my distance sensors")
        else:
            parts.append(", ".join(
                f"{lbl} is clear" if cm >= 399 else f"{lbl} reads {_speak_distance(cm)}"
                for lbl, cm in dists))

    if which in ("all", "motion"):
        if any(w for _, w in motions):
            parts.append("my motion sensor is still warming up")
        elif any(a for a, _ in motions):
            parts.append("there's movement right now")
        elif motions:
            seen = [e for e in hub.recent_events(300.0)
                    if e.get("event") in ("motion_start", "approach")]
            if seen:
                parts.append(f"no movement right now, but something went past "
                             f"{_speak_ago(seen[-1].get('ago', 0.0))}")
            else:
                parts.append("nothing has moved in the last few minutes")

    if not parts:
        return "My sensors aren't telling me anything useful right now."
    # Capitalise each clause, not just the first — they are separate sentences.
    return ". ".join(p[0].upper() + p[1:] for p in parts) + "."


_DRIVE_WORDS = {
    "forward":  ("forward", 1, 0),
    "back":     ("backwards", -1, 0),
    "left":     ("left", 0, -1),
    "right":    ("right", 0, 1),
    "around":   ("around", 0, 1),      # spin in place; steer only, no forward speed
}


def _drive(ctx, direction: str, seconds=None) -> str:
    """Move the cart a bounded distance and say so.

    Everything programmatic goes through CartClient.nudge(), which runs for a
    fixed time and stops itself — there is deliberately no "start driving and
    keep going" path for Claude or the matcher to reach.
    """
    cart = getattr(ctx, "cart", None)
    if cart is None or not cart.configured():
        return "I don't have a drive base connected."

    key = str(direction or "").lower()
    spec = _DRIVE_WORDS.get(key)
    if spec is None:
        return "I can go forward, backwards, left, right, or turn around."
    label, fwd, turn = spec

    cfg = getattr(ctx, "cart_cfg", None) or {}
    speed = int(cfg.get("speed", 150)) * fwd
    steer = int(cfg.get("turn", 150)) * turn
    if seconds is None:
        seconds = float(cfg.get("step_seconds", 1.5))
    if key == "around":
        seconds = max(float(seconds), 2.0)   # a spin needs longer than a nudge

    try:
        result = cart.nudge(steer, speed, seconds)
    except Exception as exc:                  # noqa: BLE001 - CartError or transport
        return f"I couldn't move: {exc}"
    if result.get("ignored"):
        # The firmware hands priority to the PS2 controller. Say so plainly —
        # otherwise this looks like the robot just ignoring an instruction.
        return "Someone's holding the controller, so it has priority over me."
    if key == "around":
        return "Turning around."
    return f"Moving {label}."


def _cart_stop(ctx) -> str:
    cart = getattr(ctx, "cart", None)
    if cart is None or not cart.configured():
        return "I don't have a drive base connected."
    try:
        cart.stop()
    except Exception as exc:                  # noqa: BLE001
        # Worth being explicit: the chest Pi stops the cart on its own if it
        # stops hearing from us, so a failed stop request is not a runaway.
        return f"I couldn't reach the drive base ({exc}) — it stops itself in half a second."
    return "Stopped."


def _shrink(jpeg: bytes, max_edge: int, quality: int) -> bytes:
    """Downscale a frame to ``max_edge`` on its long side, re-encoded as JPEG.

    Best-effort: a missing or unhappy Pillow returns the original bytes rather
    than failing the look. Oversized frames still work — the API downscales
    them itself — they just cost more than they need to.
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - Pillow is not a hard dependency here
        return jpeg
    try:
        img = Image.open(io.BytesIO(jpeg))
        if max(img.size) <= max_edge:
            return jpeg
        img = img.convert("RGB")
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - a truncated frame shouldn't kill the turn
        return jpeg


def capture_view(ctx, *, source: str = "eyes", max_edge: int = VIEW_MAX_EDGE,
                 quality: int = VIEW_QUALITY) -> tuple[bytes | None, str]:
    """Grab one still for the vision model, from the eye or the wide camera.

    Returns ``(jpeg, "")`` on success or ``(None, reason)`` when there is no
    picture — the reason is a sentence FRED can say. Never returns both empty:
    the caller must always have something to tell the person, because the one
    unacceptable outcome is FRED silently pretending he looked.

    Two cameras, and they are not interchangeable:

    * ``eyes`` — the head camera, behind his eyes, the one the face tracker
      follows with. It sees where he is facing, and ``snapshot()`` starts the
      sensor if it is idle and stops it again after, so a look always works.
    * ``wide`` — the PanaCast on his chest, which takes in the whole room. It
      is only usable **while the spotter is already running**: its snapshot()
      returns nothing when stopped, and starting it costs about four CPU cores
      — far too much to spend on one glance. So this never starts it.
    """
    if source == "wide":
        spotter = getattr(ctx, "spotter", None)
        if spotter is None:
            return None, "I don't have a wide camera."
        try:
            if not spotter.is_running():
                # Deliberately not started here — see the docstring. Naming the
                # other camera keeps this a useful answer rather than a dead end.
                return None, ("My wide camera isn't running, so I can only look "
                              "straight ahead with my eyes.")
            frame = spotter.snapshot()
        except Exception as exc:  # noqa: BLE001 - a wedged sensor is a spoken answer
            return None, f"I couldn't get a picture from my wide camera: {exc}"
        if not frame:
            return None, "My wide camera didn't give me a picture just then."
        return _shrink(frame, max_edge, quality), ""

    cam = getattr(ctx, "camera", None)
    if cam is None:
        return None, "I don't have a camera wired up."
    try:
        if not cam.available():
            return None, "My camera isn't working right now, so I can't look."
        frame = cam.snapshot()
    except Exception as exc:  # noqa: BLE001 - a wedged sensor is a spoken answer
        return None, f"I couldn't get a picture: {exc}"
    if not frame:
        return None, "My camera didn't give me a picture just then."
    return _shrink(frame, max_edge, quality), ""


def execute_action(ctx, name: str, **args) -> str:
    """Run one action and return FRED's spoken confirmation."""
    if name == "drive":
        return _drive(ctx, str(args.get("direction", "")), args.get("seconds"))
    if name == "cart_stop":
        return _cart_stop(ctx)
    if name == "check_health":
        return _health_report(ctx)
    if name == "set_chest_display":
        return _set_chest_display(ctx, str(args.get("animation", "")))
    if name == "play_sound":
        return _play_sound(ctx, str(args.get("sound", "")))
    if name == "set_volume":
        return _set_volume(ctx, str(args.get("direction", "")),
                           args.get("percent"))
    if name == "tilt_head":
        return _tilt_head(ctx, str(args.get("direction", "")))
    if name == "nod":
        return _nod(ctx, args.get("times", 2))
    if name == "shake_head":
        return _shake_head(ctx, args.get("times", 2))
    if name == "diagnostic":
        return _diagnostic(ctx, str(args.get("check", "")),
                           str(args.get("unit", "")),
                           str(args.get("command", "")))

    if name == "open_mouth":
        _jaw(ctx, True)
        return "Ahhh."
    if name == "close_mouth":
        _jaw(ctx, False)
        return "Closing my mouth."

    if name == "set_tracking":
        on = bool(args.get("on"))
        if not ctx.tracker.available():
            return "I can't track faces right now — my camera or vision isn't ready."
        if on:
            ctx.tracker.start()
            return "Okay, I'm watching your face now."
        ctx.tracker.stop()
        return "I've stopped tracking."

    if name == "set_led":
        on = bool(args.get("on"))
        if not ctx.led.available():
            return "My red light isn't wired up."
        ctx.led.set(on)
        if not on:
            return "Terminator mode off."
        # Terminator mode: play a real movie clip from sounds/terminator/ if any
        # are installed (an empty "" reply means "don't also speak over it").
        # Fall back to a spoken catchphrase when no clips are present.
        sound = getattr(ctx, "sound", None)
        if sound is not None and sound.play_random("terminator"):
            return ""
        return random.choice(TERMINATOR_PHRASES)

    if name == "look":
        return _look(ctx, str(args.get("direction", "center")))

    if name == "turn_head":
        return _turn_head(ctx, str(args.get("direction", "center")))

    if name == "read_sensors":
        return _sensor_report(ctx, str(args.get("which", "all")))

    if name == "say_time":
        return sysinfo.spoken_time()
    if name == "say_date":
        return sysinfo.spoken_date()
    if name == "say_datetime":
        return sysinfo.spoken_datetime()
    if name == "say_ip":
        return sysinfo.spoken_ip()
    if name == "say_temp":
        return sysinfo.spoken_temp()

    if name == "reset":
        ctx.controller.rest()
        return "Back to my resting position."

    if name == "relax":
        ctx.controller.relax()
        return "Relaxing my servos."

    return "I'm not sure how to do that."


# --- local (offline) matching ------------------------------------------------
# First match wins. Each entry: (compiled regex, action name, static args).
# `_ON`/`_OFF` let one phrase family cover both directions of a toggle.
_OFF = re.compile(r"\b(off|stop|disable|cancel|quit|end|no longer|don'?t)\b", re.I)


def _toggle(text: str) -> bool:
    """True unless the phrase clearly asks to turn something off."""
    return not _OFF.search(text)


# FRED only knows *his own* processor temperature. Matching bare "temperature"
# made him answer "what's the temperature outside?" with his SoC reading, so the
# phrase must name him (or a part of him) and must not name something external —
# a question about the weather belongs to Claude, not to this matcher. Written as
# lookaheads because all three conditions are unordered.
#
# Bare "degrees" is deliberately absent: it belongs to "turn 90 degrees".
_TEMP_RX = re.compile(
    r"^(?=.*\b(?:you|your|yours|cpu|soc|processor|chip|core)\b)"          # about FRED
    r"(?=.*\b(?:temperature|temp|hot|warm|overheating|overheated)\b)"     # about heat
    r"(?!.*\b(?:outside|weather|room|air|water|oven|coffee|in here|out there)\b)",  # not the world
    re.I)


_PATTERNS = [
    (re.compile(r"\b(open|drop)\b.*\b(mouth|jaw)\b", re.I), "open_mouth", {}),
    (re.compile(r"\b(close|shut)\b.*\b(mouth|jaw)\b", re.I), "close_mouth", {}),
    # Volume, before the LED rules: "turn it up" is a phrasing people reach for
    # constantly and it costs nothing to answer without a Claude round trip.
    # "louder"/"quieter" are their own rule because they carry the direction in
    # one word, with no verb to match on.
    (re.compile(r"\b(louder|turn (it |yourself |your voice |the volume )?up|"
                r"speak up|volume up)\b", re.I), "set_volume", {"direction": "up"}),
    (re.compile(r"\b(quieter|softer|turn (it |yourself |your voice |the volume )?down|"
                r"volume down|keep it down)\b", re.I), "set_volume", {"direction": "down"}),
    (re.compile(r"\b(how loud are you|what('?s| is) your volume|volume level)\b", re.I),
     "set_volume", {}),
    (re.compile(r"\bterminator\b", re.I), "set_led", "toggle"),
    (re.compile(r"\b(red )?(led|light)\b", re.I), "set_led", "toggle"),
    (re.compile(r"\btrack(ing)?\b.*\b(face|me|my face)\b", re.I), "set_tracking", "toggle"),
    (re.compile(r"\bwatch (me|my face)\b", re.I), "set_tracking", {"on": True}),
    # Gestures first: "nod" is a thing to do, not a direction to hold.
    (re.compile(r"\bnod\b", re.I), "nod", {}),
    (re.compile(r"\b(shake (your |the )?head|shake it off)\b", re.I), "shake_head", {}),
    # Head tilt, ahead of the eye rules: "tilt your head up" must move the head,
    # not just the eyes, and "look up" alone still means the eyes.
    (re.compile(r"\b(tilt|tip|lean)\b.*\b(head)?\b.*\b(?P<direction>up|down|left|right)\b", re.I), "tilt_head", "group"),
    (re.compile(r"\b(tilt|tip|lean)\b.*\b(level|straight|center|centre)\b", re.I), "tilt_head", {"direction": "center"}),
    (re.compile(r"\b(head|chin)\b.*\b(up|down)\b.*\b(tilt|tip)\b", re.I), "tilt_head", "group"),
    (re.compile(r"\blook (to (the )?)?(?P<direction>left|right|up|down)\b", re.I), "look", "group"),
    (re.compile(r"\blook (straight|ahead|forward|center|centre)\b", re.I), "look", {"direction": "center"}),
    # Head/neck rotation — distinct verbs ("turn", "face") so they don't clash
    # with the eye "look" commands above.
    # "move" and "point" belong here as much as "turn": people say "move your
    # head to the right" constantly, and before this it fell through to the cart.
    (re.compile(r"\b(turn|move|point|swing|face) (your |the )?head (back )?(to (the )?)?(straight|forward|center|centre|front)\b", re.I), "turn_head", {"direction": "center"}),
    (re.compile(r"\b(turn|move|point|swing|face) (your |the )?head (to (the )?)?(?P<direction>left|right)\b", re.I), "turn_head", "group"),
    (re.compile(r"\bface (forward|front|straight ahead)\b", re.I), "turn_head", {"direction": "center"}),
    (re.compile(r"\bturn (to (the )?)?(?P<direction>left|right)\b", re.I), "turn_head", "group"),
    (re.compile(r"\bface (to (the )?)?(?P<direction>left|right)\b", re.I), "turn_head", "group"),
    # Driving the cart. These sit *after* the head-turn rules on purpose: bare
    # "turn left" has always meant his neck, and quietly changing that to mean
    # "roll the whole robot left" would be a surprising thing for a wheeled base
    # to start doing. Rolling needs a movement verb ("drive/move/go/roll left")
    # or "turn around"; Claude's drive tool covers the phrasings this misses.
    (re.compile(r"\bstop\b.*\b(moving|driving|rolling|the cart|your wheels)\b", re.I), "cart_stop", {}),
    (re.compile(r"^\s*(stop|halt|whoa|freeze)\s*[.!]?\s*$", re.I), "cart_stop", {}),
    (re.compile(r"\bturn (yourself |the cart |a)?(a)?round\b", re.I), "drive", {"direction": "around"}),
    (re.compile(r"\b(drive|move|go|roll|head)\b.*\b(forward|forwards|ahead|straight on)\b", re.I), "drive", {"direction": "forward"}),
    (re.compile(r"\b(back up|backup|reverse)\b", re.I), "drive", {"direction": "back"}),
    (re.compile(r"\b(drive|move|go|roll)\b.*\b(back|backward|backwards)\b", re.I), "drive", {"direction": "back"}),
    (re.compile(r"\b(drive|move|go|roll|steer)\b.*\b(to (the )?)?(?P<direction>left|right)\b", re.I), "drive", "group"),
    (re.compile(r"\bcome (here|closer|to me|towards me|forward)\b", re.I), "drive", {"direction": "forward"}),
    # Proximity sensors. Ahead of the system-facts block below because "what do
    # you see" style phrasings are more specific than the catch-all fact rules.
    # "did anyone walk by" first: the generic presence rule below would also
    # match it (on "someone ... by") and answer with distances nobody asked for.
    (re.compile(r"\b(walk(ed)?|go(ne)?|came?|pass(ed)?|went)\b.*\b(by|past|through|in front)\b", re.I), "read_sensors", {"which": "motion"}),
    (re.compile(r"\b(did|has|have)\b.*\b(any\s?(one|body)|some\s?(one|body))\b.*\b(walk|walked|go|gone|come|came|pass|passed|move|moved)\b", re.I), "read_sensors", {"which": "motion"}),
    # Bare presence only. Since FRED can actually see (VISION_TOOL), a question
    # that asks him to *count*, *describe* or *look at* something must not be
    # short-circuited to distance readings — the sensors cannot count people or
    # tell you what colour a shirt is, and answering from them is how "how many
    # people are in front of you?" got "I don't have any proximity sensors".
    # Anything with a seeing verb goes to Claude instead, which holds both this
    # tool and the camera and can pick the right one. "Is anyone there?" stays
    # here: instant, offline, and right in the dark.
    (re.compile(r"^(?!.*\b(how many|count|colou?r|wearing|holding|describe|"
                r"look(s|ing)? like|see|seeing|watch(ing)?|show(ing)?|read)\b)"
                r".*\b(any\s?(one|body)|some\s?(one|body)|people)\b"
                r".*\b(there|here|around|near(by)?|close|in front)\b", re.I),
     "read_sensors", {}),
    (re.compile(r"\bmotion (sensor|detect)", re.I), "read_sensors", {"which": "motion"}),
    (re.compile(r"\bhow far\b", re.I), "read_sensors", {"which": "distance"}),
    (re.compile(r"\b(distance|proximity|ultrasonic)\b.*\b(sensor|reading|say|read)", re.I), "read_sensors", {"which": "distance"}),
    (re.compile(r"\bwhat\b.*\byour sensors?\b", re.I), "read_sensors", {}),
    # System facts — instant, offline answers (Claude also gets these via its
    # injected context block for other phrasings).
    (re.compile(r"\b(what('?s| is)?\s+(the\s+)?(current\s+)?time|time is it|what time)\b", re.I), "say_time", {}),
    (re.compile(r"\b(what('?s| is)?\s+(the\s+|today'?s\s+)?date|what day (is it|is today)|what'?s today)\b", re.I), "say_date", {}),
    (_TEMP_RX, "say_temp", {}),
    (re.compile(r"\b((what('?s| is)?\s+)?(your |the )?)?i\.?p\.?(\s+address)?\b", re.I), "say_ip", {}),
    (re.compile(r"\b(reset|rest|home|neutral|straighten up)\b", re.I), "reset", {}),
    (re.compile(r"\brelax\b", re.I), "relax", {}),
]


# Parts of him that are not the wheels. If one of these is named, the utterance
# is about moving a servo, and the driving rules below must not answer it.
#
# This is a safety guard, not tidiness. "Move your head to the right" matched
# the cart's "(drive|move|go|roll) ... right" rule and answered "driving right";
# "move your head back to centre" matched "... back" and drove the robot
# backwards. Both reached the cart without ever going near the model. The base
# weighs 350 lb and the person saying it is standing in front of him.
_ABOUT_HIS_BODY = re.compile(
    r"\b(head|neck|eyes?|eyeballs?|face|jaw|mouth|chin|gaze|nod|tilt)\b", re.I)
# Only "drive" is guarded. cart_stop never is: a stop must not fail on a
# technicality, which is the same rule _cart() follows for the same reason.
_DRIVES_THE_ROBOT = ("drive",)


# Somebody asking for more than one thing. The matcher answers exactly one
# action and stops, so on "move right, then back to centre, then left" it did
# the first step, reported it, and looked like a robot that had not listened.
# There is no fixing that here — one pattern is one action — so a sequence is
# handed to the model instead, which can call the tool as many times as it
# takes. Costs a round trip on a request that was always going to take several
# seconds of servo movement anyway.
_A_SEQUENCE = re.compile(
    r"\b(then|after that|followed by|and back|next,)\b"
    r"|,\s*(then|and then)\b"
    r"|\b(twice|three times|a few times|back and forth)\b", re.I)


def match_local(text: str):
    """Return ``(action_name, args)`` for the first matching pattern, or None.

    None for anything that asks for several things in order — see _A_SEQUENCE.
    """
    if _A_SEQUENCE.search(text):
        return None
    about_body = bool(_ABOUT_HIS_BODY.search(text))
    for rx, name, spec in _PATTERNS:
        if about_body and name in _DRIVES_THE_ROBOT:
            continue                    # see _ABOUT_HIS_BODY
        m = rx.search(text)
        if not m:
            continue
        if spec == "toggle":
            args = {"on": _toggle(text)}
        elif spec == "group":
            args = {"direction": m.group("direction").lower()}
        else:
            args = dict(spec)
        return name, args
    return None


# --- Claude tool definitions -------------------------------------------------
# Same actions, exposed so Claude can invoke them for phrasings the matcher
# missed and combine an action with a spoken reply. run_tool() maps back to
# execute_action().
CLAUDE_TOOLS = [
    {"name": "set_mouth", "description": "Open or close FRED's jaw/mouth.",
     "input_schema": {"type": "object", "properties": {
         "state": {"type": "string", "enum": ["open", "closed"]}}, "required": ["state"]}},
    {"name": "set_face_tracking", "description": "Turn face tracking on or off (FRED follows a face with his eyes and neck).",
     "input_schema": {"type": "object", "properties": {
         "enabled": {"type": "boolean"}}, "required": ["enabled"]}},
    {"name": "set_terminator_mode", "description": "Turn the red status LED (a.k.a. 'terminator mode') on or off.",
     "input_schema": {"type": "object", "properties": {
         "enabled": {"type": "boolean"}}, "required": ["enabled"]}},
    {"name": "set_volume", "description":
        "Change how loudly FRED speaks, or report the current level. Only use "
        "this when someone asks — never adjust it on your own initiative. Give "
        "'direction' for 'turn it up'/'louder'/'quieter', or 'percent' for an "
        "absolute level like 'set your volume to 40'. Call it with neither to "
        "answer 'how loud are you?'. By voice he is limited to a band well "
        f"short of silent and of full scale ({VOICE_VOLUME_MIN}-{VOICE_VOLUME_MAX} "
        "percent); if someone needs beyond that, tell them to use the control "
        "panel. The change lasts until he restarts.",
     "input_schema": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["up", "down"],
                       "description": "Step the level up or down."},
         "percent": {"type": "number",
                     "description": "Absolute level, 0-100, clamped to the "
                                    "band above."}}}},
    # Two names rather than one tool with a mode flag: the safe tier and the
    # shell are different decisions, and a model that has to pick the *name*
    # "run_shell" has been made to notice which one it is reaching for.
    {"name": "diagnostic", "description":
        "Run a diagnostic check on yourself and report what it says. It needs "
        "diagnostic mode switched on from the admin panel; the facts on this "
        "turn tell you when it is on, and when they do, call this rather than "
        "saying you cannot check. Use this for 'why is "
        "your chest display blank', 'is the head Pi up', 'what does your log "
        "say', 'are you running hot', 'have you got disk space'. Checks: "
        + ", ".join(diagnostic.CHECKS) +
        ". 'unit' is required for service_status, service_restart and logs, and "
        "must be one of FRED's own services. Read the output and tell the person "
        "the answer in a sentence — do not read the log out loud.",
     "input_schema": {"type": "object", "properties": {
         "check": {"type": "string", "enum": list(diagnostic.CHECKS)},
         "unit": {"type": "string",
                  "description": "Service name, for the checks that need one."}},
         "required": ["check"]}},
    {"name": "run_shell", "description":
        "Run an arbitrary shell command on FRED's brain and report the output. "
        "This needs BOTH diagnostic mode and unrestricted mode switched on from "
        "the admin panel; if it refuses, tell the person which switch is off "
        "rather than trying the diagnostic tool instead. Prefer the diagnostic "
        "tool for anything it already covers — this is for the case it does "
        "not. Keep commands short and read-only unless you have been asked for "
        "a change; you are speaking the result aloud to somebody standing in "
        "front of you, so summarise rather than reciting.",
     "input_schema": {"type": "object", "properties": {
         "command": {"type": "string",
                     "description": "The command line to run."}},
         "required": ["command"]}},
    {"name": "look", "description": "Point FRED's eyes in a direction (moves the eyes only).",
     "input_schema": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["left", "right", "up", "down", "center"]}},
         "required": ["direction"]}},
    {"name": "tilt_head", "description":
        "Tilt FRED's head on its two tilt joints: up/down pivots his chin (a "
        "nodding axis, held in position), and left/right tips his head sideways "
        "toward a shoulder. Distinct from turn_head, which rotates the neck, and "
        "from look, which moves only the eyes. Use 'center' to bring the head "
        "level again.",
     "input_schema": {"type": "object", "properties": {
         "direction": {"type": "string",
                       "enum": ["up", "down", "left", "right", "center"]}},
         "required": ["direction"]}},
    {"name": "nod", "description":
        "Nod — chin down and up, ending level. This is the gesture for yes or "
        "for agreeing, not a position to hold; use tilt_head if someone wants "
        "his head left pointing up or down.",
     "input_schema": {"type": "object", "properties": {
         "times": {"type": "integer",
                   "description": "How many nods, 1-5. Defaults to 2."}}}},
    {"name": "shake_head", "description":
        "Shake his head left and right, ending centred — the gesture for no or "
        "for disagreeing. Uses the neck; distinct from turn_head, which leaves "
        "the head where it is sent.",
     "input_schema": {"type": "object", "properties": {
         "times": {"type": "integer",
                   "description": "How many shakes, 1-5. Defaults to 2."}}}},
    {"name": "turn_head", "description": "Physically rotate FRED's whole head/neck left, right, or back to center. Use this for 'turn your head', 'face left', etc. — distinct from 'look', which only moves the eyes.",
     "input_schema": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["left", "right", "center"]}},
         "required": ["direction"]}},
    {"name": "read_sensors", "description":
        "Read FRED's proximity sensors: ultrasonic distance sensors and a motion "
        "detector in his chest. Use this for 'is anyone there?', 'how far away am "
        "I?', 'did someone walk by?'. Reports current "
        "distances plus whether anything has moved in the last few minutes, so it "
        "answers questions about the recent past as well as right now. It works "
        "in the dark and sees behind him, which the camera cannot — but it only "
        "reports distance and movement. To count people, or to say anything "
        f"about what someone or something looks like, use {VISION_TOOL} instead.",
     "input_schema": {"type": "object", "properties": {
         "which": {"type": "string", "enum": ["all", "distance", "motion"],
                   "description": "Limit the reading to one kind of sensor. Defaults to all."}}}},
    {"name": "drive", "description":
        "Drive FRED's wheeled base to move his whole body across the room. Use "
        "this for 'come here', 'back up', 'move forward', 'turn around'. This "
        "MOVES THE ENTIRE ROBOT — it is not turn_head, which only rotates his "
        "neck, and not look, which only moves his eyes. Each call moves for a "
        "short fixed time and then stops on its own, so to travel further, call "
        "it again. Prefer short moves and check the sensors if unsure of the "
        "space. If someone is holding the manual controller, it has priority and "
        "this will report that it did nothing.",
     "input_schema": {"type": "object", "properties": {
         "direction": {"type": "string",
                       "enum": ["forward", "back", "left", "right", "around"],
                       "description": "'around' spins him in place to face the other way."},
         "seconds": {"type": "number",
                     "description": "How long to move, 0.1-5. Defaults to a short "
                                    "nudge of about 1.5s. Keep it small indoors."}},
         "required": ["direction"]}},
    {"name": "stop_moving", "description":
        "Stop the wheeled base immediately. Use for 'stop', 'halt', 'whoa'.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": VISION_TOOL, "description":
        "Look through FRED's eye camera and see what is actually in front of "
        "him right now. Call this whenever answering depends on what he can "
        "see — 'what are you looking at?', 'how many people are here?', 'what "
        "colour is my shirt?', 'what am I holding?', 'is anyone in the room?', "
        "'read this for me' — and whenever you are about to describe his "
        "surroundings. You cannot see anything until you call this, so never "
        "guess at or invent what is in front of him; if the look fails, say so "
        "plainly. It returns the picture itself for you to examine. This is "
        "sight, not proximity: read_sensors reports distances and movement in "
        "the dark, and answers 'did someone walk past?' better than looking.",
     "input_schema": {"type": "object", "properties": {
         "camera": {"type": "string", "enum": ["eyes", "wide"],
                    "description":
                        "'eyes' (the default) is the camera in his head — it "
                        "sees whoever he is facing, and always works. 'wide' is "
                        "the camera on his chest, which takes in the whole room "
                        "at once: better for counting people or finding "
                        "something off to one side, but it only works when it "
                        "is already running, and says so if it isn't."}}}},
    {"name": "set_chest_display", "description":
        "Change what the screen in FRED's chest is showing, as part of an "
        "answer. Use it when the picture is the point — someone asks what else "
        "he can display, or the moment wants a flourish. Ask for an animation "
        "by its id; if you name one he does not have, he will tell you what he "
        "does have, so guess and correct rather than refusing. Note the chest "
        "screen is also how a person in a queue sees whether he is listening, "
        "thinking or speaking, so do not leave it on a decorative one at an "
        "event — he will refuse there anyway.",
     "input_schema": {"type": "object", "properties": {
         "animation": {"type": "string",
                       "description": "The animation id, e.g. reactor, flux, "
                                      "face, voice-hud, off."}},
         "required": ["animation"]}},
    {"name": "play_sound", "description":
        "Play one of the sound clips on the robot. Punctuation for an answer — "
        "not speech, which you do by replying normally. Name a clip; if it is "
        "not one he has, he will list the ones he does.",
     "input_schema": {"type": "object", "properties": {
         "sound": {"type": "string", "description": "The clip name, without .wav"}},
         "required": ["sound"]}},
    {"name": "check_health", "description":
        "Check how FRED himself is doing: how long he has been running, how hot "
        "his processor is, and what his drive base reports about its battery "
        "and temperature. Call this for 'how are you feeling?', 'how's your "
        "battery?', 'are you tired?', 'how long have you been on?', 'are you "
        "okay?'. This is about his own body, not the room — read_sensors is for "
        "what is near him and look_at_what_you_see is for what he can see. The "
        "date, time, network addresses and processor temperature are already in "
        "the facts you are given each turn; call this for the rest.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "reset_pose", "description": "Return all servos to their neutral resting position.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "relax", "description": "Release the servos so they stop holding torque.",
     "input_schema": {"type": "object", "properties": {}}},
]


# --- Claude-only server tools ------------------------------------------------
# Deliberately NOT in CLAUDE_TOOLS. That list is handed to *both* backends, and
# a server tool carries no input_schema — local_brain._to_ollama would turn this
# into a function with an empty schema and no description, which the local model
# can happily call and nothing on this machine can execute. Claude's tool list is
# CLAUDE_TOOLS plus this; the local model's is CLAUDE_TOOLS alone.
#
# Server-side means Anthropic runs the search: there is no run_tool() entry here
# and nothing to execute on the robot. What comes back is already the answer.
WEB_SEARCH_TOOL = "web_search"
WEB_SEARCH_MAX_USES = 3     # a spoken answer is never worth ten searches


def web_search_tool(location: dict | None = None) -> dict:
    """The web search tool definition, optionally localised.

    ``web_search_20250305`` — the basic version — on purpose. The newer
    ``_20260209`` filters results through code execution first, which needs
    programmatic tool calling; FRED runs Haiku 4.5, which does not have it, and
    the newer version answers a request from a model without it with a 400.

    `location` narrows results for questions that name no place ("what's the
    weather?"). It is approximate by design — city and region, never a fix.
    """
    tool: dict = {"type": "web_search_20250305", "name": WEB_SEARCH_TOOL,
                  "max_uses": WEB_SEARCH_MAX_USES}
    if location:
        tool["user_location"] = {"type": "approximate", **location}
    return tool


def run_tool(ctx, tool_name: str, tool_input: dict) -> str:
    """Map a Claude tool call to execute_action and return the spoken result."""
    ti = tool_input or {}
    if tool_name == "set_mouth":
        return execute_action(ctx, "open_mouth" if ti.get("state") == "open" else "close_mouth")
    if tool_name == "set_face_tracking":
        return execute_action(ctx, "set_tracking", on=bool(ti.get("enabled")))
    if tool_name == "set_terminator_mode":
        return execute_action(ctx, "set_led", on=bool(ti.get("enabled")))
    if tool_name == "tilt_head":
        return execute_action(ctx, "tilt_head", direction=ti.get("direction", "center"))
    if tool_name == "nod":
        return execute_action(ctx, "nod", times=ti.get("times", 2))
    if tool_name == "shake_head":
        return execute_action(ctx, "shake_head", times=ti.get("times", 2))
    if tool_name == "diagnostic":
        return execute_action(ctx, "diagnostic", check=ti.get("check", ""),
                              unit=ti.get("unit", ""))
    if tool_name == "run_shell":
        return execute_action(ctx, "diagnostic", command=ti.get("command", ""))
    if tool_name == "set_volume":
        return execute_action(ctx, "set_volume",
                              direction=ti.get("direction", ""),
                              percent=ti.get("percent"))
    if tool_name == "look":
        return execute_action(ctx, "look", direction=ti.get("direction", "center"))
    if tool_name == "turn_head":
        return execute_action(ctx, "turn_head", direction=ti.get("direction", "center"))
    if tool_name == "read_sensors":
        return execute_action(ctx, "read_sensors", which=ti.get("which", "all"))
    if tool_name == "drive":
        return execute_action(ctx, "drive", direction=ti.get("direction", ""),
                              seconds=ti.get("seconds"))
    if tool_name == "stop_moving":
        return execute_action(ctx, "cart_stop")
    if tool_name == "check_health":
        return execute_action(ctx, "check_health")
    if tool_name == "set_chest_display":
        return execute_action(ctx, "set_chest_display",
                              animation=ti.get("animation", ""))
    if tool_name == "play_sound":
        # Not `name=`: execute_action's own first parameter is called name, and
        # passing the clip under that key raised "multiple values for argument
        # 'name'" — which reached Claude as a tool failure, so it retried with
        # different "parameter formats" for a tool whose schema was fine.
        return execute_action(ctx, "play_sound", sound=ti.get("sound", ""))
    if tool_name == "reset_pose":
        return execute_action(ctx, "reset")
    if tool_name == "relax":
        return execute_action(ctx, "relax")
    if tool_name == VISION_TOOL:
        # Only Brain._look can answer this properly — it is the one caller that
        # knows whether the backend can receive an image. Reaching here means a
        # text-only path asked to see, so say so rather than returning a
        # confirmation that would read as "I looked".
        return "I can't look at anything from here."
    return "I don't have that ability."

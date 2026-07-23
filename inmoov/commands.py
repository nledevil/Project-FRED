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

import random
import re

from . import sysinfo

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
        c.set_angle("eye_y", ey["max_angle"] if d == "up" else ey["min_angle"])
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


def execute_action(ctx, name: str, **args) -> str:
    """Run one action and return FRED's spoken confirmation."""
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
    (re.compile(r"\bterminator\b", re.I), "set_led", "toggle"),
    (re.compile(r"\b(red )?(led|light)\b", re.I), "set_led", "toggle"),
    (re.compile(r"\btrack(ing)?\b.*\b(face|me|my face)\b", re.I), "set_tracking", "toggle"),
    (re.compile(r"\bwatch (me|my face)\b", re.I), "set_tracking", {"on": True}),
    (re.compile(r"\blook (to (the )?)?(?P<direction>left|right|up|down)\b", re.I), "look", "group"),
    (re.compile(r"\blook (straight|ahead|forward|center|centre)\b", re.I), "look", {"direction": "center"}),
    # Head/neck rotation — distinct verbs ("turn", "face") so they don't clash
    # with the eye "look" commands above.
    (re.compile(r"\bturn (your |the )?head (back )?(to (the )?)?(straight|forward|center|centre|front)\b", re.I), "turn_head", {"direction": "center"}),
    (re.compile(r"\b(turn|face) (your |the )?head (to (the )?)?(?P<direction>left|right)\b", re.I), "turn_head", "group"),
    (re.compile(r"\bface (forward|front|straight ahead)\b", re.I), "turn_head", {"direction": "center"}),
    (re.compile(r"\bturn (to (the )?)?(?P<direction>left|right)\b", re.I), "turn_head", "group"),
    (re.compile(r"\bface (to (the )?)?(?P<direction>left|right)\b", re.I), "turn_head", "group"),
    # System facts — instant, offline answers (Claude also gets these via its
    # injected context block for other phrasings).
    (re.compile(r"\b(what('?s| is)?\s+(the\s+)?(current\s+)?time|time is it|what time)\b", re.I), "say_time", {}),
    (re.compile(r"\b(what('?s| is)?\s+(the\s+|today'?s\s+)?date|what day (is it|is today)|what'?s today)\b", re.I), "say_date", {}),
    (_TEMP_RX, "say_temp", {}),
    (re.compile(r"\b((what('?s| is)?\s+)?(your |the )?)?i\.?p\.?(\s+address)?\b", re.I), "say_ip", {}),
    (re.compile(r"\b(reset|rest|home|neutral|straighten up)\b", re.I), "reset", {}),
    (re.compile(r"\brelax\b", re.I), "relax", {}),
]


def match_local(text: str):
    """Return ``(action_name, args)`` for the first matching pattern, or None."""
    for rx, name, spec in _PATTERNS:
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
    {"name": "look", "description": "Point FRED's eyes in a direction (moves the eyes only).",
     "input_schema": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["left", "right", "up", "down", "center"]}},
         "required": ["direction"]}},
    {"name": "turn_head", "description": "Physically rotate FRED's whole head/neck left, right, or back to center. Use this for 'turn your head', 'face left', etc. — distinct from 'look', which only moves the eyes.",
     "input_schema": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["left", "right", "center"]}},
         "required": ["direction"]}},
    {"name": "reset_pose", "description": "Return all servos to their neutral resting position.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "relax", "description": "Release the servos so they stop holding torque.",
     "input_schema": {"type": "object", "properties": {}}},
]


def run_tool(ctx, tool_name: str, tool_input: dict) -> str:
    """Map a Claude tool call to execute_action and return the spoken result."""
    ti = tool_input or {}
    if tool_name == "set_mouth":
        return execute_action(ctx, "open_mouth" if ti.get("state") == "open" else "close_mouth")
    if tool_name == "set_face_tracking":
        return execute_action(ctx, "set_tracking", on=bool(ti.get("enabled")))
    if tool_name == "set_terminator_mode":
        return execute_action(ctx, "set_led", on=bool(ti.get("enabled")))
    if tool_name == "look":
        return execute_action(ctx, "look", direction=ti.get("direction", "center"))
    if tool_name == "turn_head":
        return execute_action(ctx, "turn_head", direction=ti.get("direction", "center"))
    if tool_name == "reset_pose":
        return execute_action(ctx, "reset")
    if tool_name == "relax":
        return execute_action(ctx, "relax")
    return "I don't have that ability."

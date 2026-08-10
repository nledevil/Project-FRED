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

import base64
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


# Robot-frame yaws for the spoken/LLM direction words (0 = cart-forward).
_LOOK_YAWS = {"front": 0.0, "right": 90.0, "back": 180.0, "left": -90.0}


def _surround_summary(ctx) -> str:
    """What the 360 camera sees, falling back to the chest sensors when the
    surround stack isn't running — one spoken answer either way."""
    sv = getattr(ctx, "surround", None)
    if sv is not None and sv.is_running():
        return sv.spoken_summary()
    fallback = _sensor_report(ctx)
    if sv is not None:
        return "My surround camera isn't watching right now. " + fallback
    return fallback


def look_around_blocks(ctx, direction: str = "all"):
    """Claude's eyes: dewarped 360-camera views as image content blocks.

    Returns a list of tool_result content blocks (text labels + base64 JPEGs),
    or a plain string when there's nothing to see — brain.py passes either
    through to the API unchanged. Views are kept small (512px, q70) because
    they ride inside a *spoken* conversation: tokens are latency here.
    """
    cam = getattr(ctx, "camera360", None)
    if cam is None or not cam.available():
        return "My surround camera isn't connected."
    d = str(direction or "all").lower()
    wanted = list(_LOOK_YAWS.items()) if d == "all" else [(d, _LOOK_YAWS[d])] \
        if d in _LOOK_YAWS else None
    if wanted is None:
        return "I can look front, left, right, back, or all around."
    blocks = []
    for label, yaw in wanted:
        jpg = cam.jpeg_view(yaw=yaw, pitch=0.0, fov=90.0,
                            out_size=(512, 384), quality=70)
        if not jpg:
            continue
        blocks.append({"type": "text",
                       "text": f"View {label} (yaw {yaw:+.0f}°):"})
        blocks.append({"type": "image",
                       "source": {"type": "base64", "media_type": "image/jpeg",
                                  "data": base64.b64encode(jpg).decode("ascii")}})
    if not blocks:
        return "I couldn't get a picture just now — my camera may still be starting."
    return blocks


def _drive_cart(ctx, direction: str, duration_s: float) -> str:
    """LLM cart motion — deliberately coarse (a direction word and a bounded
    duration, never raw speed numbers) and double-gated: the cart must be
    enabled AND its ``ai_drive`` flag set. The vision governor and the
    command TTL still apply underneath, same as any other caller."""
    cart = getattr(ctx, "cart", None)
    if cart is None or not cart.enabled:
        return "My cart isn't connected."
    if not getattr(cart, "ai_drive", False):
        return "I'm not allowed to drive myself — my operator has that switched off."
    d = str(direction or "").lower()
    dur = max(0.5, min(3.0, float(duration_s or 1.0)))
    speed = int(cart.max_speed * 0.6)          # gentle: AI moves are never at cap
    steer = int(cart.max_steer * 0.6)
    moves = {"forward": (0, speed), "backward": (0, -speed),
             "left": (-steer, 0), "right": (steer, 0)}
    if d not in moves:
        return "I can drive forward, backward, or turn left or right."
    st = cart.drive(*moves[d], ttl=dur)
    g = st.get("guard") or {}
    if st.get("governed_speed") == 0 and moves[d][1] != 0:
        why = g.get("why") or "someone is too close"
        return f"I'd better not — {why}."
    verb = "Rolling" if d in ("forward", "backward") else "Turning"
    return f"{verb} {d} for {dur:.0f} second{'s' if dur >= 2 else ''}."


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

    if name == "read_sensors":
        return _sensor_report(ctx, str(args.get("which", "all")))

    if name == "scan_surroundings":
        return _surround_summary(ctx)

    if name == "cart_stop":
        cart = getattr(ctx, "cart", None)
        if cart is None or not cart.enabled:
            return "My cart isn't connected."
        cart.stop()
        return "Stopping the cart."

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
    # Cart stop first: a safety phrase must never lose to a fuzzier match.
    (re.compile(r"\b(stop|halt|freeze)\b.*\b(cart|driving|moving|wheels?|rolling)\b", re.I), "cart_stop", {}),
    (re.compile(r"\bdon'?t move\b", re.I), "cart_stop", {}),
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
    # Proximity sensors. Ahead of the system-facts block below because "what do
    # you see" style phrasings are more specific than the catch-all fact rules.
    # "did anyone walk by" first: the generic presence rule below would also
    # match it (on "someone ... by") and answer with distances nobody asked for.
    (re.compile(r"\b(walk(ed)?|go(ne)?|came?|pass(ed)?|went)\b.*\b(by|past|through|in front)\b", re.I), "read_sensors", {"which": "motion"}),
    (re.compile(r"\b(did|has|have)\b.*\b(any\s?(one|body)|some\s?(one|body))\b.*\b(walk|walked|go|gone|come|came|pass|passed|move|moved)\b", re.I), "read_sensors", {"which": "motion"}),
    # 360-camera surroundings — richer than the ultrasonics for "who's around",
    # so these phrasings go to the surround summary (which itself falls back to
    # the chest sensors when the camera isn't watching).
    (re.compile(r"\b(look|scan|check)\b.*\b(around|surroundings|behind you)\b", re.I), "scan_surroundings", {}),
    (re.compile(r"\bwho('?s| is| do you see)\b.*\b(around|near(by)?|behind|there)\b", re.I), "scan_surroundings", {}),
    (re.compile(r"\bwhat\b.*\b(do you )?see\b.*\b(around|behind)?", re.I), "scan_surroundings", {}),
    (re.compile(r"\b(any\s?(one|body)|some\s?(one|body)|people)\b.*\b(there|here|around|near(by)?|close|in front)\b", re.I), "scan_surroundings", {}),
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
    {"name": "read_sensors", "description":
        "Read FRED's proximity sensors: ultrasonic distance sensors and a motion "
        "detector in his chest. Use this for 'is anyone there?', 'how far away am "
        "I?', 'did someone walk by?', 'can you see anyone?'. Reports current "
        "distances plus whether anything has moved in the last few minutes, so it "
        "answers questions about the recent past as well as right now.",
     "input_schema": {"type": "object", "properties": {
         "which": {"type": "string", "enum": ["all", "distance", "motion"],
                   "description": "Limit the reading to one kind of sensor. Defaults to all."}}}},
    {"name": "reset_pose", "description": "Return all servos to their neutral resting position.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "relax", "description": "Release the servos so they stop holding torque.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "look_around", "description":
        "See through FRED's 360-degree surround camera (mounted on a mast above "
        "his head). Returns actual photos: a dewarped view toward the chosen "
        "direction, or all four directions at once. Use this when someone asks "
        "what you can see, what something looks like, what's happening around "
        "you, or to read/describe anything in the environment. Directions are "
        "relative to the cart: 'front' is the way the cart faces.",
     "input_schema": {"type": "object", "properties": {
         "direction": {"type": "string",
                       "enum": ["front", "left", "right", "back", "all"],
                       "description": "Which way to look. Defaults to all."}}}},
    {"name": "scan_surroundings", "description":
        "A quick spoken summary of where people are around FRED right now, from "
        "the 360 camera's person/motion detection (falls back to the chest "
        "ultrasonics when the camera is off). Use for 'is anyone around?', "
        "'who's behind you?'. Much faster than look_around; no images.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "drive_cart", "description":
        "Drive FRED's wheeled cart base a short, bounded move: forward, "
        "backward, or turn left/right for up to 3 seconds. A vision safety "
        "governor will slow or refuse the move if a person is in the way. Only "
        "works when the operator has enabled AI driving. For any request to "
        "stop, use stop_cart.",
     "input_schema": {"type": "object", "properties": {
         "direction": {"type": "string",
                       "enum": ["forward", "backward", "left", "right"]},
         "duration_s": {"type": "number",
                        "description": "How long to move, 0.5-3 seconds. Default 1."}},
         "required": ["direction"]}},
    {"name": "stop_cart", "description":
        "Immediately stop the cart's wheels. Always allowed, always safe.",
     "input_schema": {"type": "object", "properties": {}}},
]


def run_tool(ctx, tool_name: str, tool_input: dict):
    """Map a Claude tool call to execute_action and return the result.

    Usually a spoken string; ``look_around`` instead returns a list of content
    blocks (text + images) that brain.py hands to the API verbatim, so Claude
    genuinely sees the pictures rather than a description of them.
    """
    ti = tool_input or {}
    if tool_name == "look_around":
        return look_around_blocks(ctx, ti.get("direction", "all"))
    if tool_name == "scan_surroundings":
        return execute_action(ctx, "scan_surroundings")
    if tool_name == "drive_cart":
        return _drive_cart(ctx, ti.get("direction", ""), ti.get("duration_s", 1.0))
    if tool_name == "stop_cart":
        return execute_action(ctx, "cart_stop")
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
    if tool_name == "read_sensors":
        return execute_action(ctx, "read_sensors", which=ti.get("which", "all"))
    if tool_name == "reset_pose":
        return execute_action(ctx, "reset")
    if tool_name == "relax":
        return execute_action(ctx, "relax")
    return "I don't have that ability."

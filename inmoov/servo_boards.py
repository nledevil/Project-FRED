"""Arm servo boards — PCA9685s behind RP2040-Zeros on the NUC's USB.

The head's servos sit on a Pi's own I2C bus. x86 has no such bus worth using,
so each arm's PCA9685 hangs off a microcontroller instead and the NUC talks to
it over USB serial. Every board runs the same firmware
(``firmware/servo_board_node/main.py``) and does not know which arm it is: it
reports the RP2040's unique id, and ``config/servo_boards.json`` is where that
id is given a side and where each joint is given a channel. Plug in a second
board and it shows up here as a new, unassigned one.

The division of labour is deliberate. The firmware knows channels and
microseconds and nothing else; names, limits, inversion and calibration live
here, so tuning a joint never means reflashing a board.

Servos are addressed by name — ``r_elbow``, ``l_thumb`` — built from the
board's side and the joint, and the public methods mirror ``ServoController``.

**Enable is a separate thing from position.** A board boots with its outputs
cut (OE high) and cuts them again if this process goes quiet for a few seconds
— the heartbeat thread is what keeps an idle arm holding. The first move after
boot or after such a trip re-enables by itself, because only the commanded
channel has a pulse so nothing else can jump. ``disable()`` is different: it is
a stop, and it stays stopped until ``enable()`` is called on purpose.

**When a board is unreachable** its moves are clamped and recorded locally, it
reads offline, and the next scan picks it up again when it comes back.
"""
from __future__ import annotations

import glob
import json
import threading
import time
from pathlib import Path

try:
    import serial  # pyserial
    _SERIAL_ERR = None
except Exception as exc:  # noqa: BLE001 - absent just means nothing is detected
    serial = None
    _SERIAL_ERR = exc

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "servo_boards.json"

NODE_TYPE = "servo_board"       # what the firmware calls itself
CHANNELS = 16
# The firmware cuts the outputs after 3 s of silence; ping well inside that.
HEARTBEAT_S = 1.0
# A healthy reply is back in ~10 ms. Past this the board is not running the
# firmware (sat at the REPL, some other MicroPython board, unplugged).
REPLY_TIMEOUT = 0.5
DEFAULT_PORT_GLOB = "/dev/serial/by-id/usb-MicroPython_*"

SIDES = {"right": "r", "left": "l"}

# What can be plugged into a channel, in shoulder-to-fingertip order.
JOINTS = {
    "shoulder":        "Shoulder up/down",
    "shoulder_rotate": "Shoulder rotate",
    "bicep_rotate":    "Bicep rotate",
    "elbow":           "Elbow up/down",
    "wrist":           "Wrist rotate",
    "thumb":           "Thumb open/close",
    "index":           "Index finger open/close",
    "middle":          "Middle finger open/close",
    "ring":            "Ring finger open/close",
    "pinky":           "Pinky finger open/close",
}

# A joint nobody has calibrated yet: the middle 90 degrees of a standard servo
# (1000-2000 us). Narrow on purpose — the real limits are found on the arm.
NEW_SERVO = {"min_angle": 45.0, "max_angle": 135.0, "rest_angle": 90.0,
             "actuation_range": 180, "pulse_min_us": 500, "pulse_max_us": 2500,
             "invert": False}

# How fast a joint travels when driven from the tuning panel, in degrees per
# second (a joint's own "tune_speed" overrides it). The board walks the pulse
# there at this rate rather than jumping, and that is what gives Kill something
# to stop: a servo completes the move to the last pulse it was sent even after
# the signal is cut — a digital one holds there, too — so a kill can only ever
# stop the *next* step. At 60 deg/s that is about a degree away.
TUNE_SPEED_DPS = 60.0

# rest_board(): pause between joints, so ten servos do not all draw their
# start-up current from the supply in the same instant.
REST_STAGGER_S = 0.15

# identify(): a small wiggle around centre on a raw channel, in microseconds.
IDENTIFY_CENTRE_US, IDENTIFY_SWING_US = 1500, 150


def load_config(path: str | Path = CONFIG_PATH) -> dict:
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    cfg.setdefault("port_glob", DEFAULT_PORT_GLOB)
    cfg.setdefault("boards", {})
    return cfg


class ServoBoardError(Exception):
    """A board is unreachable, unknown, or refused a command; or an assignment
    that cannot be made (a taken channel, a side already in use)."""


class BoardLink:
    """One board's serial port: a command out, a status line back."""

    def __init__(self, port: str, ser):
        self.port = port
        self._ser = ser
        self._lock = threading.Lock()
        self.uid = ""
        self.online = False
        self.enabled = False
        self.pca = False
        self.us: list[int] = [0] * CHANNELS     # the board's word: 0 = no pulse
        self.tripped: str | None = None
        self.error: str | None = None
        self.last_tx = 0.0

    def request(self, cmd: dict) -> dict:
        with self._lock:
            if self._ser is None:
                raise ServoBoardError(f"board offline ({self.error})")
            try:
                # Anything already waiting is an unsolicited line (the board
                # announcing a trip) or the tail of a reply we gave up on.
                # Either way it is not the answer to this command.
                self._ser.reset_input_buffer()
                self._ser.write((json.dumps(cmd) + "\n").encode())
                self.last_tx = time.monotonic()
                reply = self._read_reply()
            except ServoBoardError:
                raise
            except Exception as e:  # noqa: BLE001 - unplugged mid-exchange
                self._drop(f"serial error ({e})")
                raise ServoBoardError(self.error) from e
            if reply.get("node") != NODE_TYPE:
                self._drop(f"answers as {reply.get('node')!r}, not a servo board")
                raise ServoBoardError(self.error)
            self.uid = str(reply.get("uid") or self.uid)
            self.online = True
            self.error = None
            self.enabled = bool(reply.get("enabled"))
            self.pca = bool(reply.get("pca"))
            self.us = list(reply.get("us") or self.us)
            self.tripped = reply.get("tripped")
            if not reply.get("ok"):
                raise ServoBoardError(reply.get("error") or "board refused the command")
            return reply

    def _read_reply(self) -> dict:
        deadline = time.monotonic() + REPLY_TIMEOUT
        while time.monotonic() < deadline:
            line = self._ser.readline().strip()
            if not line.startswith(b"{"):
                continue                    # blank, or REPL chatter
            try:
                return json.loads(line)
            except ValueError:
                continue
        self._drop("no reply — not running the servo firmware?")
        raise ServoBoardError(self.error)

    def _drop(self, msg: str) -> None:
        self.online = False
        self.enabled = False
        self.us = [0] * CHANNELS
        self.error = msg
        try:
            self._ser.close()
        except Exception:  # noqa: BLE001
            pass
        self._ser = None

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                self._drop("closed")


class ServoBoards:
    """Every servo board on this machine's USB, addressed by servo name.

    Parameters
    ----------
    config : dict, optional
        Parsed servo_boards.json. Loaded from `path` if omitted.
    path : str or Path
        Where save() writes.
    heartbeat : bool
        Run the keep-alive thread (default True). Tests turn it off.
    opener, lister : callable, optional
        ``opener(port) -> serial-like`` and ``lister() -> [port, ...]``; seams
        for tests.
    """

    def __init__(self, config: dict | None = None, path: str | Path = CONFIG_PATH,
                 heartbeat: bool = True, opener=None, lister=None):
        self.path = Path(path)
        self.config = config if config is not None else load_config(self.path)
        self.config.setdefault("boards", {})
        self._opener = opener or (lambda port: serial.Serial(
            port, 115200, timeout=REPLY_TIMEOUT, write_timeout=REPLY_TIMEOUT))
        self._lister = lister or (lambda: sorted(glob.glob(
            self.config.get("port_glob", DEFAULT_PORT_GLOB))))
        self.mock = serial is None and opener is None
        self._links: dict[str, BoardLink] = {}      # uid -> link
        self._strangers: dict[str, str] = {}        # port -> why it is not a board
        self._scan_lock = threading.Lock()
        self._current: dict[tuple[str, str], float] = {}   # (uid, joint) -> angle
        # Joints switched on from the admin page's tuning panel. A kill takes a
        # joint out, and a tuning move for a joint that is not in here is
        # refused — so a slider request still in flight when Kill is pressed
        # cannot switch the servo back on behind it.
        self._live: set[tuple[str, str]] = set()
        self._letgo = 0                             # bumped by every kill/relax/stop
        self._audit = False
        self._stopped = False                       # disable() was called
        self._closing = threading.Event()

        if self.mock:
            print(f"[ServoBoards] no pyserial ({_SERIAL_ERR}); arm boards disabled.")
            return
        self.scan()
        if heartbeat:
            threading.Thread(target=self._heartbeat, name="servo-boards-heartbeat",
                             daemon=True).start()

    # ---- detection --------------------------------------------------------
    def scan(self) -> None:
        """Look for boards on USB. Ports already held by a live link are left
        alone; everything else matching the glob is opened and asked who it is."""
        if self.mock:
            return
        with self._scan_lock:
            held = {l.port for l in self._links.values() if l.online}
            self._strangers = {}
            for port in self._lister():
                if port in held:
                    continue
                try:
                    link = BoardLink(port, self._opener(port))
                except Exception as e:  # noqa: BLE001 - busy, no permission, gone
                    self._strangers[port] = f"cannot open ({e})"
                    continue
                try:
                    link.request({"op": "ping"})
                except ServoBoardError as e:
                    self._strangers[port] = str(e)
                    continue
                if not link.uid:
                    link.close()
                    self._strangers[port] = "firmware too old to report an id — reflash it"
                    continue
                old = self._links.get(link.uid)
                if old is not None:
                    old.close()
                else:
                    print(f"[ServoBoards] found board {link.uid} on {port}")
                self._links[link.uid] = link
                self.config["boards"].setdefault(link.uid, {"side": "", "servos": {}})
                self._adopt(link)

    def _adopt(self, link: BoardLink) -> None:
        """Take over a board that is already holding joints.

        A panel restart is quicker than the firmware's 3 s silence timeout, so
        the board usually never notices: its outputs stay on and the servos
        keep holding. Starting from "everything is off" would then be a lie
        with a powered arm behind it. So joints with a pulse are taken as live,
        at the angle that pulse means; a pulse on a channel nothing is assigned
        to is cut, since nobody could otherwise switch it off.
        """
        servos = self.config["boards"][link.uid].get("servos", {})
        by_ch = {s["channel"]: (j, s) for j, s in servos.items()
                 if s.get("channel") is not None}
        orphans = []
        for ch, us in enumerate(link.us):
            if not us:
                continue
            if ch not in by_ch:
                orphans.append(ch)
                continue
            joint, s = by_ch[ch]
            self._live.add((link.uid, joint))
            self._current[(link.uid, joint)] = self._angle_of(s, us)
        if orphans:
            try:
                link.request({"op": "relax", "ch": orphans})
            except ServoBoardError:
                pass

    def _heartbeat(self) -> None:
        """Keep enabled boards from tripping, and notice replugged ones."""
        ticks = 0
        while not self._closing.wait(HEARTBEAT_S / 2):
            ticks += 1
            for link in list(self._links.values()):
                if link.online and time.monotonic() - link.last_tx >= HEARTBEAT_S:
                    try:
                        link.request({"op": "ping"})
                    except ServoBoardError as e:
                        print(f"[ServoBoards] board {link.uid} offline — {e}")
            # A board that dropped (or one plugged in since) is a rescan away.
            # Only when something is missing or new: opening a port that is not
            # ours costs a reply timeout each time.
            if ticks % 10 == 0:
                ports = set(self._lister())
                known = ({l.port for l in self._links.values() if l.online}
                         | set(self._strangers))
                if ports - known:
                    self.scan()

    # ---- lookup -----------------------------------------------------------
    def _board_cfg(self, uid: str) -> dict:
        if uid not in self.config["boards"]:
            raise ServoBoardError(f"unknown board {uid!r}")
        b = self.config["boards"][uid]
        b.setdefault("side", "")
        b.setdefault("servos", {})
        return b

    @staticmethod
    def _name(side: str, joint: str) -> str:
        return f"{SIDES[side]}_{joint}"

    @property
    def servos(self) -> dict[str, dict]:
        """name -> servo config, for every joint that has a side and a channel."""
        return {name: s for name, (_uid, s) in self._named().items()}

    def _named(self) -> dict[str, tuple[str, dict]]:
        return {name: (uid, s) for name, (uid, _joint, s) in self._keyed().items()}

    def _keyed(self) -> dict[str, tuple[str, str, dict]]:
        out = {}
        for uid, b in self.config["boards"].items():
            side = b.get("side", "")
            if side not in SIDES:
                continue
            for joint, s in b.get("servos", {}).items():
                if s.get("channel") is not None:
                    out[self._name(side, joint)] = (uid, joint, s)
        return out

    def _require(self, name: str) -> tuple[str, dict]:
        named = self._named()
        if name not in named:
            raise KeyError(f"Unknown servo {name!r}. Known: {sorted(named)}")
        return named[name]

    def _joint(self, uid: str, joint: str) -> dict:
        """The config of a joint that is on a channel of board `uid`."""
        s = self._board_cfg(uid)["servos"].get(joint)
        if s is None or s.get("channel") is None:
            raise ServoBoardError(f"{joint!r} is not assigned to a channel on this board")
        return s

    def _send(self, uid: str, cmd: dict) -> dict:
        link = self._links.get(uid)
        if link is None:
            raise ServoBoardError(f"board {uid} is not connected")
        return link.request(cmd)

    # ---- assignment (the admin page) --------------------------------------
    def set_side(self, uid: str, side: str) -> None:
        """Say which arm this board is: "right", "left", or "" for neither."""
        b = self._board_cfg(uid)
        if side and side not in SIDES:
            raise ServoBoardError(f"side must be one of {sorted(SIDES)} or empty")
        if side:
            for other, ob in self.config["boards"].items():
                if other != uid and ob.get("side") == side:
                    raise ServoBoardError(f"board {other} is already the {side} arm")
        b["side"] = side

    def assign(self, uid: str, channel: int, joint: str | None) -> None:
        """Put `joint` on `channel` of board `uid`; None/"" empties the channel.

        A joint moved from another channel keeps its calibration. The channel
        it leaves is relaxed, and its cached angle is dropped — where the servo
        on the new port sits is unknown until the next move.
        """
        b = self._board_cfg(uid)
        channel = int(channel)
        if not 0 <= channel < CHANNELS:
            raise ServoBoardError(f"channel must be 0..{CHANNELS - 1}")
        if joint and joint not in JOINTS:
            raise ServoBoardError(f"unknown joint {joint!r}")
        servos = b["servos"]
        freed = []
        for j, s in servos.items():             # whoever holds the channel now
            if s.get("channel") == channel and j != joint:
                s["channel"] = None
                freed.append(channel)
        if joint:
            s = servos.setdefault(joint, dict(NEW_SERVO, channel=None,
                                              description=JOINTS[joint] + " (uncalibrated)"))
            if s.get("channel") not in (None, channel):
                freed.append(s["channel"])
            s["channel"] = channel
        # Whatever moved or was displaced: its position is unknown now, and it
        # is off until somebody switches it on again.
        for j in servos:
            if servos[j].get("channel") is None or j == joint:
                self._current.pop((uid, j), None)
                self._live.discard((uid, j))
        if freed and not self._audit:
            try:
                self._send(uid, {"op": "relax", "ch": sorted(set(freed))})
            except ServoBoardError:
                pass

    # ---- tuning (the admin page) ------------------------------------------
    def set_limits(self, uid: str, joint: str, **fields) -> dict:
        """Change a joint's min_angle / max_angle / rest_angle. Moves nothing —
        which is the point: rest can be set before the servo is ever enabled."""
        s = self._joint(uid, joint)
        new = {k: s[k] for k in ("min_angle", "max_angle", "rest_angle")}
        for k, v in fields.items():
            if k not in new:
                raise ServoBoardError(f"cannot set {k!r}")
            try:
                new[k] = round(float(v), 1)
            except (TypeError, ValueError):
                raise ServoBoardError(f"{k} must be a number") from None
        rng = s.get("actuation_range", 180)
        if not 0 <= new["min_angle"] <= new["max_angle"] <= rng:
            raise ServoBoardError(f"need 0 <= min <= max <= {rng}")
        if not new["min_angle"] <= new["rest_angle"] <= new["max_angle"]:
            raise ServoBoardError(
                f"rest ({new['rest_angle']:g}) must be between min "
                f"({new['min_angle']:g}) and max ({new['max_angle']:g})")
        s.update(new)
        return s

    def tune(self, uid: str, joint: str, angle: float | None = None,
             enable: bool = False, raw: bool = False) -> float:
        """Drive one joint from the tuning panel. Returns the applied angle.

        `enable=True` switches the joint on; with no angle it goes to its rest
        angle. Without `enable` the joint must already be on, so a move that
        was in flight when it was killed is refused rather than obeyed.

        Held to the joint's min/max like any other move, unless `raw` — which
        is how the real limits are found, by going past the placeholder ones.
        Raw is bounded only by the servo's own range, so it is the call that
        can push a joint into its end stop; kill() is the way out.

        The board walks the joint there at TUNE_SPEED_DPS instead of jumping
        (see that constant for why a kill depends on it). The one move that
        cannot be walked is switching on a joint whose position nobody knows —
        never moved since the board was powered — which jumps, as any servo
        does when it first gets a signal.
        """
        s = self._joint(uid, joint)
        key = (uid, joint)
        if self._audit:
            raise ServoBoardError("audit mode is on — nothing moves")
        if self._stopped:
            raise ServoBoardError("the arms are stopped — allow motion first")
        if enable:
            self._live.add(key)
        elif key not in self._live:
            raise ServoBoardError(f"{joint} is off — enable it first")
        if angle is None:
            angle = s["rest_angle"]
        if raw:
            target = max(0.0, min(float(s.get("actuation_range", 180)), float(angle)))
        else:
            target = self._clamp(s, float(angle))
        ch = str(s["channel"])
        rng = s.get("actuation_range", 180)
        dps = float(s.get("tune_speed", TUNE_SPEED_DPS))
        cmd = {"op": "set", "us": {ch: self._pulse_us(s, target)},
               "rate": max(1, round(dps * abs(s["pulse_max_us"] - s["pulse_min_us"]) / rng))}
        if key in self._current:            # where it was last left, if we know
            cmd["from"] = {ch: self._pulse_us(s, self._current[key])}
        try:
            reply = self._send(uid, cmd)
            if key not in self._live:       # killed while that was on the wire
                self._send(uid, {"op": "relax", "ch": [s["channel"]]})
                raise ServoBoardError(f"{joint} was killed")
            if not reply.get("enabled"):
                self._send(uid, {"op": "enable", "on": True})
        except ServoBoardError:
            self._live.discard(key)
            raise
        self._current[key] = target
        return target

    def rest_board(self, uid: str) -> list[str]:
        """Switch on every joint assigned on board `uid` and send it to its
        rest angle, one after another. Returns the joints that were sent.

        Gives up part-way if anything is killed, relaxed or stopped meanwhile:
        whoever pressed Kill does not want the remaining joints coming on.
        """
        servos = self._board_cfg(uid)["servos"]
        joints = sorted((j for j, s in servos.items() if s.get("channel") is not None),
                        key=lambda j: servos[j]["channel"])
        if not joints:
            raise ServoBoardError("no joints are assigned on this board")
        started = self._letgo
        done = []
        for i, joint in enumerate(joints):
            if i:
                time.sleep(REST_STAGGER_S)
            if self._letgo != started:
                break
            self.tune(uid, joint, enable=True)
            done.append(joint)
        return done

    def kill(self, uid: str, joint: str | None = None) -> None:
        """Cut the pulse to one joint — or, with no joint, to the whole board —
        right now. Never refused and never queued behind a move: not gated on
        audit or stop, and callers must not hold a lock around it."""
        self._letgo += 1
        cfg = self._board_cfg(uid)
        if joint is None:
            self._live = {k for k in self._live if k[0] != uid}
            self._stopped_at(uid, self._send(uid, {"op": "relax"}))
            return
        self._live.discard((uid, joint))
        ch = cfg["servos"].get(joint, {}).get("channel")
        if ch is None:
            raise ServoBoardError(f"{joint!r} is not assigned to a channel on this board")
        self._stopped_at(uid, self._send(uid, {"op": "relax", "ch": [ch]}))

    def _stopped_at(self, uid: str, reply: dict) -> None:
        """Record where a relax caught each joint. A joint killed mid-walk is
        not at the angle it was sent toward; the board says which pulse it had
        reached, and the next Enable walks on from there."""
        prev, now = reply.get("prev"), reply.get("us")
        if not prev or not now:
            return
        for j, s in self._board_cfg(uid)["servos"].items():
            ch = s.get("channel")
            if ch is not None and ch < len(prev) and prev[ch] and not now[ch]:
                self._current[(uid, j)] = self._angle_of(s, prev[ch])

    def identify_channel(self, uid: str, channel: int, cycles: int = 2) -> None:
        """Wiggle whatever is plugged into a raw channel, then let it go — how
        you find out which connector is which before anything is assigned.
        Raises ServoBoardError if the board cannot do it."""
        channel = int(channel)
        if not 0 <= channel < CHANNELS:
            raise ServoBoardError(f"channel must be 0..{CHANNELS - 1}")
        if self._audit:
            raise ServoBoardError("audit mode is on — nothing moves")
        if self._stopped:
            raise ServoBoardError("the arms are stopped — enable them first")
        link = self._links.get(uid)
        was_enabled = bool(link and link.enabled)
        ch = str(channel)
        self._send(uid, {"op": "set", "us": {ch: IDENTIFY_CENTRE_US}})
        self._send(uid, {"op": "enable", "on": True})
        try:
            time.sleep(0.4)
            for _ in range(max(1, cycles)):
                for us in (IDENTIFY_CENTRE_US + IDENTIFY_SWING_US,
                           IDENTIFY_CENTRE_US - IDENTIFY_SWING_US):
                    self._send(uid, {"op": "set", "us": {ch: us}})
                    time.sleep(0.3)
            self._send(uid, {"op": "set", "us": {ch: IDENTIFY_CENTRE_US}})
            time.sleep(0.3)
        finally:
            try:
                self._send(uid, {"op": "relax", "ch": [channel]})
                if not was_enabled:         # leave the board as we found it
                    self._send(uid, {"op": "enable", "on": False})
            except ServoBoardError:
                pass

    def save(self) -> None:
        with open(self.path, "w") as f:
            json.dump(self.config, f, indent=2)
            f.write("\n")

    def describe(self) -> dict:
        """Everything the admin page draws: each known board and its channels,
        ports that are not servo boards, and the joints on offer."""
        boards = []
        for uid, b in self.config["boards"].items():
            link = self._links.get(uid)
            side = b.get("side", "")
            channels: list = [None] * CHANNELS
            servos = {}
            for joint, s in b.get("servos", {}).items():
                ch = s.get("channel")
                if ch is None or not 0 <= ch < CHANNELS:
                    continue
                channels[ch] = joint
                servos[joint] = {
                    "channel": ch,
                    "name": self._name(side, joint) if side in SIDES else None,
                    "min_angle": s["min_angle"], "max_angle": s["max_angle"],
                    "rest_angle": s["rest_angle"],
                    "actuation_range": s.get("actuation_range", 180),
                    "current": self._current.get((uid, joint)),
                    # On = switched on here AND the board really has a pulse
                    # there (it clears them all when it trips or reboots).
                    "live": bool((uid, joint) in self._live and link
                                 and link.online and link.us[ch]),
                }
            boards.append({
                "uid": uid, "side": side,
                "port": link.port if link else None,
                "online": bool(link and link.online),
                "enabled": bool(link and link.enabled),
                "pca": bool(link and link.pca),
                "tripped": link.tripped if link else None,
                "error": link.error if link else "not connected",
                "channels": channels, "servos": servos,
            })
        boards.sort(key=lambda x: (not x["online"], x["side"] or "~", x["uid"]))
        return {"boards": boards,
                "strangers": [{"port": p, "error": e}
                              for p, e in sorted(self._strangers.items())],
                "joints": JOINTS, "sides": list(SIDES), "channels": CHANNELS,
                "stopped": self._stopped, "audit": self._audit, "mock": self.mock}

    # ---- angle maths ------------------------------------------------------
    @staticmethod
    def _clamp(s: dict, angle: float) -> float:
        return max(s["min_angle"], min(s["max_angle"], angle))

    @staticmethod
    def _pulse_us(s: dict, angle: float) -> int:
        """Logical angle -> pulse width, applying the invert flag."""
        rng = s.get("actuation_range", 180)
        phys = rng - angle if s.get("invert") else angle
        lo, hi = s["pulse_min_us"], s["pulse_max_us"]
        return round(lo + (hi - lo) * phys / rng)

    @staticmethod
    def _angle_of(s: dict, us: int) -> float:
        """Pulse width -> logical angle; the inverse of _pulse_us."""
        rng = s.get("actuation_range", 180)
        lo, hi = s["pulse_min_us"], s["pulse_max_us"]
        phys = (us - lo) * rng / (hi - lo)
        angle = rng - phys if s.get("invert") else phys
        return round(max(0.0, min(float(rng), angle)), 1)

    # ---- public API (mirrors ServoController) -----------------------------
    def set_angle(self, name: str, angle: float, enforce_limits: bool = True) -> float:
        """Move `name` to `angle` degrees. Returns the actual (clamped) angle."""
        self._require(name)
        return self.set_angles({name: angle}, enforce_limits=enforce_limits)[name]

    def set_angles(self, angles: dict[str, float],
                   enforce_limits: bool = True) -> dict[str, float]:
        """Move several servos, one exchange per board. Returns applied angles."""
        applied: dict[str, float] = {}
        per_board: dict[str, dict[str, int]] = {}
        keyed = self._keyed()
        for name, angle in angles.items():
            uid, s = self._require(name)
            if enforce_limits:
                target = self._clamp(s, float(angle))
            else:
                target = max(0.0, min(float(s.get("actuation_range", 180)), float(angle)))
            applied[name] = target
            per_board.setdefault(uid, {})[str(s["channel"])] = self._pulse_us(s, target)
        if not self._audit:
            for uid, pulses in per_board.items():
                try:
                    reply = self._send(uid, {"op": "set", "us": pulses})
                    if not reply.get("enabled") and not self._stopped:
                        self._send(uid, {"op": "enable", "on": True})
                except ServoBoardError:
                    pass                    # recorded locally, like the head link
        for name, target in applied.items():
            self._current[keyed[name][:2]] = target
        return applied

    def get_angle(self, name: str) -> float | None:
        """Last commanded angle for `name` (None if never moved)."""
        k = self._keyed().get(name)
        return self._current.get(k[:2]) if k else None

    def move_smooth(self, name: str, angle: float, duration: float = 0.5,
                    steps: int = 25) -> float:
        """Ease `name` from its current angle to `angle` over `duration` seconds."""
        _uid, s = self._require(name)
        target = self._clamp(s, angle)
        start = self.get_angle(name)
        if start is None:
            start = s["rest_angle"]
        steps = max(1, steps)
        for i in range(1, steps + 1):
            self.set_angle(name, start + (target - start) * i / steps)
            time.sleep(duration / steps)
        return target

    def rest(self) -> None:
        """Move every assigned servo to its configured rest position."""
        self.set_angles({n: s["rest_angle"] for n, s in self.servos.items()})

    def relax(self, name: str | None = None) -> None:
        """Cut the pulse so the servo(s) stop holding torque. The arm will sag."""
        if self._audit:
            return
        self._letgo += 1
        if name:
            uid, joint, s = self._keyed()[self._require(name) and name]
            self._live.discard((uid, joint))
            targets = [(uid, {"op": "relax", "ch": [s["channel"]]})]
        else:
            self._live.clear()
            targets = [(uid, {"op": "relax"}) for uid in self._links]
        for uid, cmd in targets:
            try:
                self._send(uid, cmd)
            except ServoBoardError:
                pass

    def enable(self) -> None:
        """Clear a previous disable(). Outputs come back on the next move."""
        self._stopped = False

    def disable(self) -> None:
        """Stop: cut every board's outputs at its OE pin, and stay stopped until
        enable(). Not gated on audit — a stop must always be allowed through."""
        self._stopped = True
        self._letgo += 1
        self._live.clear()
        for uid in list(self._links):
            try:
                self._send(uid, {"op": "enable", "on": False})
            except ServoBoardError:
                pass

    def identify(self, name: str, sweep: float = 12.0, cycles: int = 3,
                 dwell: float = 0.13) -> None:
        """Wiggle `name` around its rest angle so you can spot which servo it is."""
        _uid, s = self._require(name)
        rest = s["rest_angle"]
        lo = max(s["min_angle"], rest - sweep)
        hi = min(s["max_angle"], rest + sweep)
        for _ in range(max(1, cycles)):
            self.set_angle(name, lo)
            time.sleep(dwell)
            self.set_angle(name, hi)
            time.sleep(dwell)
        self.set_angle(name, rest)

    # ---- audit (dry run) --------------------------------------------------
    def is_audit(self) -> bool:
        return self._audit

    def set_audit(self, on: bool) -> None:
        """Turn audit mode on/off. Idempotent. Does not relax: the arms hold the
        pose they are in (the heartbeat keeps running) and take no commands."""
        self._audit = bool(on)

    def close(self) -> None:
        """Stop the heartbeat and cut every output. The arms go limp."""
        self._closing.set()
        self.disable()
        for uid, link in list(self._links.items()):
            try:
                link.request({"op": "relax"})   # so a later enable replays nothing
            except ServoBoardError:
                pass
            link.close()

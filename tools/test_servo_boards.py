#!/usr/bin/env python3
"""Arm servo boards: found by id, joints assigned to channels, a stop stays stopped.

Each arm is a PCA9685 behind an RP2040 that boots with its outputs cut and cuts
them again when the NUC goes quiet. Both arms run the same firmware, so the
only thing telling them apart is the RP2040's unique id — the admin page gives
that id a side and gives each joint a channel. This pins the parts that would
hurt if they regressed: a second board must not inherit the first one's joints,
an assignment must not leave two joints on one channel, the first move after
boot re-enables by itself while the first move after a stop must not, and
nothing moves during an audit. All against fake boards speaking the firmware's
protocol.

    python3 tools/test_servo_boards.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import servo_boards as sb                      # noqa: E402
from inmoov.servo_boards import ServoBoards, ServoBoardError   # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def refused(fn) -> bool:
    try:
        fn()
    except ServoBoardError:
        return True
    return False


class FakeBoard:
    """A serial port with the firmware's protocol behind it."""

    def __init__(self, uid, node="servo_board"):
        self.uid, self.node = uid, node
        self.enabled = False
        self.us = [0] * 16
        self.cmds: list[dict] = []
        self.dead = False
        self._out: list[bytes] = []

    def reset_input_buffer(self):
        self._out.clear()

    def write(self, data: bytes):
        if self.dead:
            raise OSError("unplugged")
        cmd = json.loads(data)
        self.cmds.append(cmd)
        if cmd["op"] == "enable":
            self.enabled = bool(cmd["on"])
        elif cmd["op"] == "set":
            for ch, us in cmd["us"].items():
                self.us[int(ch)] = us       # the real board walks; this one arrives
        reply = {"node": self.node, "uid": self.uid, "ok": True}
        if cmd["op"] == "relax":
            reply["prev"] = list(self.us)
            for ch in cmd.get("ch", range(16)):
                self.us[ch] = 0
        reply.update(enabled=self.enabled, pca=True, us=self.us)
        self._out.append(json.dumps(reply).encode() + b"\n")

    def readline(self) -> bytes:
        return self._out.pop(0) if self._out else b""

    def close(self):
        pass


sb.time.sleep = lambda s: None          # identify's dwell is not under test

right, left, other = FakeBoard("aaaa"), FakeBoard("bbbb"), FakeBoard("cccc", node="stomach")
ports = {"/dev/A": right}
tmp = Path(tempfile.mkdtemp()) / "servo_boards.json"
boards = ServoBoards(config={"boards": {}}, path=tmp, heartbeat=False,
                     opener=lambda p: ports[p], lister=lambda: sorted(ports))


def board(uid):
    return next(b for b in boards.describe()["boards"] if b["uid"] == uid)


print("A plugged-in board is found and starts empty")
check("one board, by its unique id", [b["uid"] for b in boards.describe()["boards"]] == ["aaaa"])
check("no side, no joints", board("aaaa")["side"] == "" and not any(board("aaaa")["channels"]))
check("finding it moved nothing", not right.enabled and not any(right.us))
check("joints with no side have no servo name", boards.servos == {})

print("Assigning a side and joints names the servos")
boards.set_side("aaaa", "right")
boards.assign("aaaa", 3, "elbow")
boards.assign("aaaa", 7, "index")
check("r_elbow and r_index exist", sorted(boards.servos) == ["r_elbow", "r_index"])
check("the channel table shows them", board("aaaa")["channels"][3] == "elbow"
      and board("aaaa")["channels"][7] == "index")
check("assigning moved nothing", not right.enabled and not any(right.us))

print("Angles become pulses, inside the limits")
got = boards.set_angle("r_elbow", 90)
check("90 deg -> 1500 us on channel 3", right.us[3] == 1500, str(right.us[3]))
check("first move enables the outputs", right.enabled)
got = boards.set_angle("r_elbow", 999)
check("999 deg clamps to the placeholder max", got == 135 and right.us[3] == 2000,
      f"{got} deg, {right.us[3]} us")
check("get_angle reports the clamped angle", boards.get_angle("r_elbow") == 135)

print("Reassigning keeps one joint per channel and one channel per joint")
boards.config["boards"]["aaaa"]["servos"]["elbow"]["max_angle"] = 120.0   # "calibrated"
boards.assign("aaaa", 5, "elbow")
t = board("aaaa")["channels"]
check("elbow moved from ch 3 to ch 5", t[3] is None and t[5] == "elbow")
check("the channel it left is relaxed", right.us[3] == 0)
check("its calibration came with it",
      boards.config["boards"]["aaaa"]["servos"]["elbow"]["max_angle"] == 120.0)
check("its position is unknown again", boards.get_angle("r_elbow") is None)
boards.assign("aaaa", 5, "thumb")
t = board("aaaa")["channels"]
check("a joint assigned over another replaces it", t[5] == "thumb" and "elbow" not in t)
check("the displaced joint is no longer drivable", "r_elbow" not in boards.servos)
boards.assign("aaaa", 5, None)
check("a channel can be emptied", board("aaaa")["channels"][5] is None)
check("an unknown joint is refused", refused(lambda: boards.assign("aaaa", 1, "tentacle")))
check("channel 16 is refused", refused(lambda: boards.assign("aaaa", 16, "thumb")))

print("A second board is its own arm")
ports["/dev/B"] = left
ports["/dev/C"] = other
boards.scan()
d = boards.describe()
check("the new board appears, unassigned", board("bbbb")["side"] == ""
      and not any(board("bbbb")["channels"]))
check("the first board kept its assignments", board("aaaa")["channels"][7] == "index")
check("a board that is not a servo board is listed apart",
      [s["port"] for s in d["strangers"]] == ["/dev/C"])
check("two boards cannot both be the right arm",
      refused(lambda: boards.set_side("bbbb", "right")))
boards.set_side("bbbb", "left")
boards.assign("bbbb", 7, "index")
boards.set_angles({"r_index": 90, "l_index": 60})
check("each name reaches its own board", right.us[7] == 1500 and left.us[7] == 1167,
      f"right {right.us[7]}, left {left.us[7]}")

print("Identify wiggles one raw channel and puts the board back")
boards.disable(); boards.enable()       # both boards' outputs off, motion allowed
boards.identify_channel("bbbb", 12)
sets = [c["us"] for c in left.cmds if c["op"] == "set" and "12" in c["us"]]
check("the channel was driven either side of centre",
      {s["12"] for s in sets} == {1350, 1500, 1650})
check("it ends relaxed", left.us[12] == 0)
check("outputs are off again, as they were", not left.enabled)
check("the other board never heard about it",
      not any("12" in c.get("us", {}) for c in right.cmds))

print("Rest can be set before the servo is ever switched on")
n = len(left.cmds)
boards.set_limits("bbbb", "index", rest_angle=60)
check("changing limits sends nothing to the board", len(left.cmds) == n)
check("a move before enable is refused", refused(lambda: boards.tune("bbbb", "index", 80)))
check("and nothing was sent to the board", len(left.cmds) == n)
got = boards.tune("bbbb", "index", enable=True)
check("enable goes to the rest angle that was set", got == 60 and left.us[7] == 1167,
      f"{got} deg, {left.us[7]} us")
check("the page sees it as live", board("bbbb")["servos"]["index"]["live"])
check("rest outside min..max is refused",
      refused(lambda: boards.set_limits("bbbb", "index", rest_angle=170)))
check("min above max is refused",
      refused(lambda: boards.set_limits("bbbb", "index", min_angle=150)))

print("Tuning moves are walked, so a kill has something to stop")
boards.tune("bbbb", "index", 70)
sent = left.cmds[-1]
check("the move carries a rate (60 deg/s = 667 us/s)", sent.get("rate") == 667, str(sent))
left.us[7] = 1200                       # the board had only walked this far...
boards.kill("bbbb", "index")            # ...when Kill landed
check("kill records where the joint actually stopped",
      board("bbbb")["servos"]["index"]["current"] == 63.0,
      str(board("bbbb")["servos"]["index"]["current"]))
boards.tune("bbbb", "index", enable=True)
sent = left.cmds[-2] if left.cmds[-1]["op"] == "enable" else left.cmds[-1]
check("the next enable walks on from there, not from nowhere",
      sent.get("from") == {"7": 1200} and sent.get("rate") == 667, str(sent))

print("Tuning keeps to min and max unless told to go past them")
got = boards.tune("bbbb", "index", 170)
check("a tuning move stops at max_angle", got == 135 and left.us[7] == 2000,
      f"{got} deg, {left.us[7]} us")
got = boards.tune("bbbb", "index", 0)
check("and at min_angle", got == 45 and left.us[7] == 1000, f"{got} deg, {left.us[7]} us")
got = boards.tune("bbbb", "index", 170, raw=True)
check("a raw move goes past max_angle", got == 170 and left.us[7] == 2389,
      f"{got} deg, {left.us[7]} us")
got = boards.tune("bbbb", "index", 999, raw=True)
check("but not past the servo's own range", got == 180)
boards.kill("bbbb", "index")
check("kill cuts the pulse", left.us[7] == 0)
check("the page sees it as off", not board("bbbb")["servos"]["index"]["live"])
check("a move still in flight after the kill is refused",
      refused(lambda: boards.tune("bbbb", "index", 90)) and left.us[7] == 0)
boards.set_limits("bbbb", "index", min_angle=50, max_angle=140)
check("limits found while tuning are kept",
      boards.config["boards"]["bbbb"]["servos"]["index"]["max_angle"] == 140)
boards.set_audit(True)
boards.kill("bbbb", "index")
check("kill is allowed during an audit", left.cmds[-1]["op"] == "relax")
check("tuning is refused during an audit",
      refused(lambda: boards.tune("bbbb", "index", enable=True)))
boards.set_audit(False)
boards.tune("bbbb", "index", enable=True)
boards.kill("bbbb")
check("kill with no joint relaxes the whole board", not any(left.us))

print("All to rest switches on every assigned joint, at its own rest angle")
boards.relax()
boards.assign("bbbb", 2, "elbow")
boards.set_limits("bbbb", "elbow", rest_angle=120)
done = boards.rest_board("bbbb")
check("every assigned joint was sent, in channel order", done == ["elbow", "index"], str(done))
check("each went to its own rest angle", left.us[2] == 1833 and left.us[7] == 1167,
      f"elbow {left.us[2]}, index {left.us[7]}")
check("unassigned channels got nothing",
      [c for c, us in enumerate(left.us) if us] == [2, 7])
check("the other board was left alone", not any(right.us))
check("both read live", all(s["live"] for s in board("bbbb")["servos"].values()))
boards.relax()
real_tune = boards.tune
def tune_then_kill(uid, joint, **kw):       # somebody hits Kill after the first joint
    out = real_tune(uid, joint, **kw)
    boards.kill(uid)
    return out
boards.tune = tune_then_kill
done = boards.rest_board("bbbb")
boards.tune = real_tune
check("a kill part-way stops the rest of them coming on",
      done == ["elbow"] and not any(left.us), f"{done}, {left.us}")
boards.set_audit(True)
check("refused during an audit", refused(lambda: boards.rest_board("bbbb")))
boards.set_audit(False)
boards.assign("bbbb", 2, None)

print("Relax lets go of everything and locks nothing")
boards.tune("bbbb", "index", enable=True)
boards.set_angle("r_index", 90)
boards.relax()
check("no pulse on either board", not any(left.us) and not any(right.us))
check("a tuned joint reads off, and needs enabling again",
      not board("bbbb")["servos"]["index"]["live"]
      and refused(lambda: boards.tune("bbbb", "index", 90)))
boards.set_angle("r_index", 90)
check("a commanded joint simply holds again", right.us[7] == 1500 and right.enabled)

print("A stop stays stopped")
boards.disable()
check("disable cuts both boards", not right.enabled and not left.enabled)
boards.set_angle("r_index", 100)
check("a move after disable does not re-enable", not right.enabled)
check("identify is refused while stopped", refused(lambda: boards.identify_channel("aaaa", 0)))
boards.enable()
boards.set_angle("r_index", 100)
check("after enable(), the next move brings the outputs back", right.enabled)

print("Audit moves the readout, not the arm")
boards.set_audit(True)
n = len(right.cmds)
boards.set_angle("r_index", 120)
check("nothing sent to the board", len(right.cmds) == n)
check("readout tracks the intent", boards.get_angle("r_index") == 120)
check("identify is refused", refused(lambda: boards.identify_channel("aaaa", 0)))
boards.set_audit(False)

print("An unplugged board degrades, and comes back on a rescan")
right.dead = True
got = boards.set_angle("r_index", 200)
check("the move still returns the clamped angle", got == 135)
check("the board reads not connected", not board("aaaa")["online"])
check("its assignments are still shown", board("aaaa")["channels"][7] == "index")
right.dead = False
boards.scan()
check("a rescan finds it again", board("aaaa")["online"])

print("Assignments survive a restart")
boards.save()
again = ServoBoards(path=tmp, heartbeat=False,
                    opener=lambda p: ports[p], lister=lambda: sorted(ports))
check("sides and joints are read back", sorted(again.servos) == ["l_index", "r_index"])

print("A restart takes over joints the board is still holding")
boards.tune("bbbb", "index", 100, enable=True)
left.us[12] = 1500                      # and a pulse on a channel nothing is assigned to
again = ServoBoards(path=tmp, heartbeat=False,
                    opener=lambda p: ports[p], lister=lambda: sorted(ports))
b = next(x for x in again.describe()["boards"] if x["uid"] == "bbbb")
check("the held joint reads live, not off", b["servos"]["index"]["live"])
check("at the angle its pulse means", abs(b["servos"]["index"]["current"] - 100) < 0.5,
      str(b["servos"]["index"]["current"]))
check("taking over moved nothing", left.us[7] == 1611, str(left.us[7]))
check("so it can be killed", again.kill("bbbb", "index") is None and left.us[7] == 0)
check("a pulse on an unassigned channel is cut", left.us[12] == 0)

print("The shipped config is usable")
cfg = sb.load_config()
for uid, b in cfg["boards"].items():
    chans = [s["channel"] for s in b["servos"].values() if s.get("channel") is not None]
    check(f"board {uid}: no two joints share a channel", len(set(chans)) == len(chans))
    check(f"board {uid}: every joint is a known one", set(b["servos"]) <= set(sb.JOINTS))
    check(f"board {uid}: rest is inside the limits",
          all(s["min_angle"] <= s["rest_angle"] <= s["max_angle"]
              for s in b["servos"].values()))

if FAILURES:
    print(f"\n{len(FAILURES)} check(s) FAILED")
    sys.exit(1)
print("\nall checks passed")

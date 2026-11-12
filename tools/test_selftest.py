#!/usr/bin/env python3
"""Check the one-button self-test against a fake robot, without the robot.

The button exists for a fair: a robot just carried in and plugged back
together. So the failures that matter are the ones that would tell that
person "ready" when he is not — a camera frozen on one frame, an arm board
not plugged in, a dead machine making the whole run hang — and the one that
would hurt somebody: the motion test switching an arm joint on, which sends
it to rest at full speed.

    python3 tools/test_selftest.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import selftest as st                       # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


class Cam:
    def __init__(self, frozen=False):
        self.frozen, self.n = frozen, 0

    def available(self):
        return True

    def snapshot(self, timeout=5.0):
        self.n += 1
        return b"jpeg" if self.frozen else b"jpeg%d" % self.n


class Spotter(Cam):
    def is_running(self):
        return True


class Boards:
    def __init__(self, sides=("left", "right"), live=False):
        self.moves: list = []
        self.pinged: list = []
        self.boards = [{"uid": f"b{i}", "side": side, "online": True, "pca": True,
                        "tripped": None, "enabled": True,
                        "servos": {"elbow": {"channel": 1, "name": f"{side}_elbow",
                                             "min_angle": 40, "max_angle": 140,
                                             "rest_angle": 90,
                                             "current": 90.0 if live else None,
                                             "live": live}}}
                       for i, side in enumerate(sides)]

    def describe(self):
        return {"boards": self.boards, "strangers": [], "mock": False,
                "stopped": False, "audit": False}

    def ping(self, uid):
        self.pinged.append(uid)
        return {}

    def set_angle(self, name, angle):
        self.moves.append((name, angle))
        return angle


class Brain:
    def status(self):
        return {"claude_ready": True, "local_ready": True, "local_model": "qwen"}


class Assistant:
    brain = Brain()

    def available(self):
        return True

    def status(self):
        return {"listening": True, "mic": {"capturing": True}}


def healthy_parts(**over):
    ok = SimpleNamespace(status=lambda: {"online": True, "available": True,
                                         "mock": False, "locked": []})
    parts = SimpleNamespace(
        ctrl=ok, led=ok, camera=Cam(), boards=Boards(),
        display=SimpleNamespace(configured=lambda: True,
                                state=lambda: {"running": True, "animation": "reactor"}),
        sensors=SimpleNamespace(state=lambda: {"nodes": {"stomach": {"online": True}}}),
        cart=None, spotter=Spotter(),
        mic_doa=SimpleNamespace(status=lambda: {"available": True, "running": True,
                                                "reads": 9}),
        assistant=Assistant(), transcriber=None,
        sound=SimpleNamespace(available=lambda: True, can_speak=lambda: True,
                              settings=lambda: {"tts": "piper"}),
        settings={}, lock=threading.Lock(),
        hotspot_state=lambda: {"configured": True, "enabled": True, "ssid": "fred",
                               "clients": []},
        uplink_state=lambda: {"ssid": "home"},
        inference_device=lambda: {"library": "Vulkan"})
    for k, v in over.items():
        setattr(parts, k, v)
    return parts


# The network-facing checks reach out of this machine; swap them for answers.
OFFLINE = {"Internet": lambda p: (st.PASS, "stub"),
           "Tailscale": lambda p: (st.PASS, "stub")}


def checks():
    return [st.Check(c.group, c.name, OFFLINE.get(c.name, c.fn), c.timeout)
            for c in st.CHECKS]


def run(parts, **kw):
    said: list[str] = []
    out = st.SelfTest(parts, speak=said.append, checks=kw.get("checks") or checks()).run()
    return out, said


def by_name(out):
    return {r["name"]: r for r in out["results"]}


def main() -> int:
    print("a healthy robot reads READY, and he says so")
    out, said = run(healthy_parts())
    fails = [r["name"] for r in out["results"] if r["status"] == st.FAIL]
    check("verdict is ready", out["verdict"] == "ready", ", ".join(fails))
    check("every check reported", out["done"] == len(st.CHECKS),
          f"{out['done']}/{len(st.CHECKS)}")
    check("he speaks the verdict, once", said == [st.spoken_verdict(out)]
          and "All systems working" in said[0], str(said))
    check("results come back head first", out["results"][0]["group"] == "Head")

    print("the failures that read as healthy")
    out, said = run(healthy_parts(camera=Cam(frozen=True)))
    r = by_name(out)["Eye camera"]
    check("a frozen camera fails", r["status"] == st.FAIL and "frozen" in r["detail"],
          r["detail"])
    check("...and makes the run NOT READY", out["verdict"] == "not_ready")
    check("...and he names it out loud", "eye camera" in said[0], said[0])

    out, _ = run(healthy_parts(boards=Boards(sides=("left",))))
    r = by_name(out)["Right arm board"]
    check("a missing arm board fails, and says how to fix it",
          r["status"] == st.FAIL and "Arm servos" in r["detail"], r["detail"])

    b = Boards()
    run(healthy_parts(boards=b))
    check("arm boards are pinged, not trusted from a cached flag",
          sorted(b.pinged) == ["b0", "b0", "b1", "b1"] or set(b.pinged) == {"b0", "b1"},
          str(b.pinged))

    out, _ = run(healthy_parts(inference_device=lambda: {"library": "cpu"}))
    r = by_name(out)["Local brain"]
    check("ollama on the CPU fails (the 28-second answers)",
          r["status"] == st.FAIL, r["detail"])

    out, _ = run(healthy_parts(sensors=SimpleNamespace(state=lambda: {"nodes": {}})))
    check("unplugged front sensors fail",
          by_name(out)["Front sensors"]["status"] == st.FAIL)

    print("one dead part cannot hang or crash the run")
    def hangs(p):
        time.sleep(30)
    def crashes(p):
        raise RuntimeError("boom")
    t0 = time.monotonic()
    out, _ = run(healthy_parts(), checks=[st.Check("Head", "Hangs", hangs, timeout=0.5),
                                          st.Check("Head", "Crashes", crashes)])
    took = time.monotonic() - t0
    r = by_name(out)
    check("a hung check is cut off at its time limit", took < 5
          and r["Hangs"]["status"] == st.FAIL, f"{took:.1f}s, {r['Hangs']['detail']}")
    check("a crashing check is a FAIL, not a crash",
          r["Crashes"]["status"] == st.FAIL and "boom" in r["Crashes"]["detail"])

    print("warnings do not stop the show")
    out, said = run(healthy_parts(hotspot_state=lambda: {"configured": True,
                                                          "enabled": False}))
    check("hotspot off is a warning, still READY", out["verdict"] == "ready"
          and by_name(out)["FRED's hotspot"]["status"] == st.WARN)
    check("...and he mentions it", "1 warning" in said[0], said[0])

    print("the motion test never switches an arm joint on")
    b = Boards(live=False)
    m = st.MotionTest(healthy_parts(ctrl=None, boards=b))
    m.start()
    while m.status()["running"]:
        time.sleep(0.02)
    rows = m.status()["results"]
    check("joints that are off are skipped", rows and
          all(r["status"] == st.SKIP for r in rows), str(rows))
    check("...and nothing was sent to them", b.moves == [], str(b.moves))

    b = Boards(live=True)
    st.MotionTest.DWELL = 0.0
    m = st.MotionTest(healthy_parts(ctrl=None, boards=b))
    m.start()
    while m.status()["running"]:
        time.sleep(0.02)
    angles = [a for name, a in b.moves if name == "left_elbow"]
    check("a live joint wiggles a few degrees around where it is, and returns",
          angles == [85.0, 95.0, 90.0], str(angles))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

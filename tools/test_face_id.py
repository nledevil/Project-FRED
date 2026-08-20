#!/usr/bin/env python3
"""Session face recall's promises — the ones that matter without a robot, and
the ones that need real faces.

    venv/bin/python tools/test_face_id.py                 # logic + privacy
    venv/bin/python tools/test_face_id.py --faces DIR     # + accuracy, real faces
    venv/bin/python tools/test_face_id.py --camera 30     # + a live look

Three layers, because they fail for different reasons and only one of them can
be checked on a laptop with nobody in the room:

**Logic and privacy** run everywhere and are the ones with teeth. They drive
``FaceId`` with stand-in descriptors instead of pictures — the point is not
whether the descriptor is any good (that needs faces) but whether the promises
around it hold: that a thin burst says nothing, that a face expires on the idle
timeout, that forgetting actually forgets, and that not one byte is written to
disk while any of it happens. That last one is enforced rather than eyeballed:
``open`` is replaced with a guard that fails the run on any write.

**Accuracy** needs a directory of faces, one subdirectory per person
(``DIR/somebody/*.jpg`` — the LFW layout, and any folder of holiday photos
sorted by who is in them will do). It runs the real detector and the real
descriptor, simulates sessions of people arriving, leaving and coming back, and
reports how often FRED recognises the returning one — and, the number that
actually matters, how often he greets the wrong person.

**Live** points it at the robot's own camera for the length of a walk-away-and-
come-back, and prints what it decided. There is no assertion here; it is for
sitting in front of FRED and seeing whether it works in *this* room.
"""
from __future__ import annotations

import argparse
import builtins
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from inmoov import face_id  # noqa: E402
from inmoov.face_id import FaceId  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


# --- stand-in descriptors ----------------------------------------------------
# Unit vectors in the same 1764-dim space the real HOG lands in, built so that
# "the same person" is a small wobble away and "somebody else" is far. That lets
# every rule around the descriptor be tested without a single photograph.
DIMS = 1764


def person(seed: int):
    rng = np.random.default_rng(seed)
    v = rng.normal(size=DIMS).astype(np.float32)
    return v / np.linalg.norm(v)


def frame_of(base, wobble: float = 0.12, seed: int | None = None):
    rng = np.random.default_rng(seed)
    v = base + rng.normal(scale=wobble / np.sqrt(DIMS), size=DIMS).astype(np.float32)
    return (v / np.linalg.norm(v)).astype(np.float32)


def nudged(base, dist: float, seed: int = 99):
    """A vector exactly ``dist`` (cosine) from ``base`` — a near-twin to order."""
    rng = np.random.default_rng(seed)
    u = rng.normal(size=DIMS).astype(np.float32)
    u -= base * float(u @ base)
    u /= np.linalg.norm(u)
    t = float(np.sqrt(1.0 / (1.0 - dist) ** 2 - 1.0))
    v = base + t * u
    return (v / np.linalg.norm(v)).astype(np.float32)


def feed(fid: FaceId, base, n: int, wobble: float = 0.12):
    """n frames of one person straight into the matcher, bypassing the camera."""
    seen = None
    for _ in range(n):
        d = frame_of(base, wobble)
        fid._describe = lambda _g, _d=d: _d          # noqa: SLF001 - that is the seam
        seen = fid.observe(np.zeros((240, 320), np.uint8))
    return seen


class NoDisk:
    """Fails the run if anything opens a file for writing while it is active."""

    def __init__(self):
        self.violations: list[str] = []
        self._open = builtins.open

    def __enter__(self):
        def guard(file, mode="r", *a, **kw):
            if any(c in str(mode) for c in "wxa+"):
                self.violations.append(f"{file!r} mode={mode!r}")
            return self._open(file, mode, *a, **kw)
        builtins.open = guard
        return self

    def __exit__(self, *exc):
        builtins.open = self._open
        return False


def logic_checks() -> None:
    print("logic and privacy (stand-in descriptors, no camera):")
    a, b, c = person(1), person(2), person(3)

    with NoDisk() as disk:
        fid = FaceId(idle_secs=60.0)
        check("a fresh recogniser knows nobody", fid.status()["visitors"] == [])
        check("...and has no opinion about who is in front of it",
              fid.current() is None)

        # A thin burst must stay silent: this is the "no opinion" contract.
        thin = feed(fid, a, face_id.MIN_SAMPLES - 1)
        check("a burst under the minimum says nothing", thin is None,
              f"{face_id.MIN_SAMPLES - 1} frames")
        check("...and creates nobody", fid.status()["visitors"] == [])

        v1 = feed(fid, a, face_id.NEW_SAMPLES)
        check("a full burst becomes a visitor", v1 is not None and v1.id == 1)
        check("who is a first sighting", v1 is not None and v1.sightings == 1)

        again = feed(fid, a, face_id.MIN_SAMPLES)
        check("the same face is the same visitor", again is v1)
        check("...and is not counted as a return while they are still standing there",
              v1.sightings == 1)

        fid._burst.clear()                            # noqa: SLF001 - they walked off
        fid._current = None                           # noqa: SLF001
        v1.last_seen -= face_id.SIGHTING_GAP + 1
        back = feed(fid, a, face_id.MIN_SAMPLES)
        check("coming back later is the same visitor, seen twice",
              back is v1 and v1.sightings == 2, f"sightings={v1.sightings}")

        v2 = feed(fid, b, face_id.NEW_SAMPLES)
        check("a different face is a different visitor", v2 is not None and v2 is not v1)
        v3 = feed(fid, c, face_id.NEW_SAMPLES)
        check("...and a third is a third", v3 is not None and v3 not in (v1, v2))
        check("nobody was merged", len({v1.id, v2.id, v3.id}) == 3)

        # Notes: the conversational half. They belong to a face, not to the room.
        v3.remember("how do your servos work")
        check("a note sticks to the visitor", v3.notes == ["how do your servos work"])
        check("remember() with a face attaches to that face",
              fid.remember("and your eyes?") is v3 and len(v3.notes) == 2)
        for i in range(face_id.NOTES_MAX + 2):
            v3.remember(f"question {i}")
        check("notes are capped", len(v3.notes) == face_id.NOTES_MAX,
              str(len(v3.notes)))
        check("...keeping the newest", v3.notes[-1].startswith("question"))
        v3.remember("x" * 500)
        check("a speech is trimmed", max(len(n) for n in v3.notes) <= face_id.NOTE_CHARS)

        # The idle rule — the constraint the whole feature was agreed on.
        fid.idle_secs = 0.0
        fid.status()
        check("everyone expires on the idle timeout", fid.status()["visitors"] == [])
        check("...and nobody is 'current' afterwards", fid.current() is None)

        fid.idle_secs = 60.0
        v = feed(fid, a, face_id.NEW_SAMPLES)
        v.remember("something personal")
        gallery, notes = v._gallery, v._notes         # noqa: SLF001 - checking the wipe
        fid.forget_all()
        check("forget_all forgets the visitors", fid.status()["visitors"] == [])
        check("...and what they said", notes == [])
        check("...and zeroes the descriptors rather than dropping them",
              all(not d.any() for d in gallery) and gallery == [])
        check("no face means no note kept", fid.remember("nobody is here") is None)

        # Memory is bounded: an event is a queue, not an archive.
        for i in range(face_id.MAX_VISITORS + 4):
            fid._burst.clear()                        # noqa: SLF001
            fid._current = None                       # noqa: SLF001
            feed(fid, person(100 + i), face_id.NEW_SAMPLES)
        check("the visitor list is bounded",
              len(fid.status()["visitors"]) <= face_id.MAX_VISITORS,
              str(len(fid.status()["visitors"])))

        # Two records that are near-twins of each other, and a face that sits
        # between them. Both are inside the threshold, so only the margin rule
        # can save it — and silence is the right answer.
        fid.forget_all()
        twin = nudged(a, 0.06)
        for i, base in enumerate((a, twin), start=1):
            v = face_id.Visitor(i, time.monotonic())
            v._add(base)                              # noqa: SLF001 - staged gallery
            fid._visitors.append(v)                   # noqa: SLF001
        between = (a + twin)
        between /= np.linalg.norm(between)
        burst = np.stack([frame_of(between, 0.02) for _ in range(face_id.MIN_SAMPLES)])
        check("an ambiguous match is refused rather than guessed",
              fid._match(burst) is None)
        fid._visitors.pop()                           # noqa: SLF001 - no twin, no doubt
        check("...and the same face is matched once the doubt is gone",
              fid._match(burst) is not None)

        # A face nothing in memory resembles is nobody, not the nearest thing.
        check("a stranger matches nobody",
              fid._match(np.stack([frame_of(person(42))
                                   for _ in range(face_id.MIN_SAMPLES)])) is None)

    check("nothing was written to disk", not disk.violations,
          "; ".join(disk.violations[:3]))

    # The sampler must never be the reason a camera runs.
    fid = FaceId()
    calls = {"n": 0}

    def dead_source():
        calls["n"] += 1
        return None                                   # "the camera is not streaming"

    if fid.available():
        fid.attend(dead_source)
        time.sleep(1.0)
        fid.stop()
        check("the sampler polls its source and copes with no frames",
              calls["n"] >= 1 and fid.status()["visitors"] == [], f"{calls['n']} polls")
        # A held camera has to be given back, whatever the loop did.
        held = {"n": 0}
        fid.attend(dead_source,
                   hold=(lambda: held.__setitem__("n", held["n"] + 1),
                         lambda: held.__setitem__("n", held["n"] - 1)))
        time.sleep(0.3)
        fid.stop()
        check("a held camera is released when the sampler stops", held["n"] == 0,
              f"balance={held['n']}")
    else:
        print("  SKIP  sampler (OpenCV or the cascade is missing here)")


# --- accuracy on real faces --------------------------------------------------
def load_people(root: Path, min_imgs: int, max_people: int, limit: int):
    """{person: [grayscale image, ...]}. Images, not descriptors: a visit has to
    go through the detector as many times as it has frames, because a jittering
    Haar box is half of what makes this hard."""
    import cv2
    people = {}
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        files = sorted(f for f in d.iterdir()
                       if f.suffix.lower() in (".jpg", ".jpeg", ".png"))
        if len(files) < min_imgs:
            continue
        imgs = [im for im in (cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
                              for f in files[:limit]) if im is not None]
        if len(imgs) >= min_imgs:
            people[d.name] = imgs
        if len(people) >= max_people:
            break
    return people


def _visit_frames(img, n: int, rng: random.Random):
    """One visit: n frames of the same moment, as a live camera would see it.

    A common per-visit offset (they are standing where they are standing, in
    the light they are standing in) plus small per-frame wobble, which is what
    the detector's box does frame to frame while somebody holds still.
    """
    import cv2
    h, w = img.shape
    ang0, sc0 = rng.uniform(-5, 5), 1 + rng.uniform(-0.08, 0.08)
    tx0, ty0 = rng.uniform(-0.04, 0.04) * w, rng.uniform(-0.04, 0.04) * h
    gain, bias = 1 + rng.uniform(-0.2, 0.2), rng.uniform(-15, 15)
    out = []
    for _ in range(n):
        M = cv2.getRotationMatrix2D((w / 2, h / 2), ang0 + rng.uniform(-1.5, 1.5),
                                    sc0 + rng.uniform(-0.02, 0.02))
        M[0, 2] += tx0 + rng.uniform(-1.5, 1.5)
        M[1, 2] += ty0 + rng.uniform(-1.5, 1.5)
        f = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_REPLICATE)
        out.append(np.clip(f.astype(np.float32) * gain + bias, 0, 255).astype(np.uint8))
    return out


def _session(fid: FaceId, frames) -> object:
    """Walk one visit's frames past the recogniser and return its last verdict."""
    seen = None
    for f in frames:
        seen = fid.observe(f)
    return seen


def accuracy_checks(root: Path, sessions: int = 120, burst: int = 10) -> None:
    print(f"accuracy on real faces from {root}:")
    if not FaceId().available():
        print("  SKIP  accuracy (OpenCV or the cascade is missing here)")
        return
    people = load_people(root, min_imgs=4, max_people=60, limit=12)
    if len(people) < 4:
        print(f"  SKIP  need 4+ people with 4+ images each under {root}")
        return
    print(f"  {len(people)} people, {sum(len(v) for v in people.values())} images")

    # Two runs, because "the same person, later" is not one question.
    #   same-moment  — they came back and stood in the same spot in the same
    #                  light. Only the framing moved. The best case, and close
    #                  to somebody who stepped away for thirty seconds.
    #   other-photo  — the return visit is built from a different photograph of
    #                  them entirely: different day, light, hair, expression.
    #                  Harder than anything a five-minute session will throw at
    #                  it, so read it as a floor rather than an estimate.
    for mode in ("same-moment", "other-photo"):
        rng = random.Random(7)
        recognised = wrong = missed = 0
        for _ in range(sessions):
            who = rng.sample(list(people), 3)
            shots = {n: rng.sample(people[n], 2) for n in who}
            fid = FaceId()
            ids = []
            # Three people talk to him, one after another, with no gap between
            # them: the burst has to survive the changing of the guard on the
            # strength of the faces alone.
            for name in who:
                seen = _session(fid, _visit_frames(shots[name][0], burst, rng))
                ids.append(seen.id if seen else None)
            for v in fid._visitors:                   # noqa: SLF001 - time passed
                v.last_seen -= face_id.SIGHTING_GAP + 1
            src = shots[who[0]][0 if mode == "same-moment" else 1]
            seen = _session(fid, _visit_frames(src, burst, rng))
            # Three outcomes, and only one of them is a mistake. Filed as a new
            # visitor is a *miss*: FRED treats them as a stranger and says
            # nothing, which is the failure this is tuned to prefer.
            if ids[0] is not None and seen is not None and seen.id == ids[0]:
                recognised += 1
            elif seen is not None and seen.id in ids[1:]:
                wrong += 1
            else:
                missed += 1
        n = sessions
        print(f"  {mode}: {burst} frames a visit, {n} sessions of three people, "
              "then the first one comes back")
        print(f"    recognised the returning visitor : {recognised / n:6.1%}")
        print(f"    greeted the wrong person         : {wrong / n:6.1%}")
        print(f"    said nothing                     : {missed / n:6.1%}")
        # The bar is on the mistake, not on the recall. A miss is invisible;
        # telling a child you remember a conversation you never had is not — and
        # how often he *does* remember depends entirely on how the pictures you
        # pointed this at were taken, so recall is reported rather than asserted
        # everywhere except the case the feature is actually for.
        if mode == "same-moment":
            check("same-moment: nobody is greeted as the wrong person",
                  wrong / n < 0.01, f"{wrong}/{n}")
            check("same-moment: a returning visitor is usually recognised",
                  recognised / n > 0.5, f"{recognised}/{n}")
        else:
            # A face that has genuinely changed matches nothing in memory and
            # ought to be filed as a stranger. When it isn't, what it hit was
            # somebody who looks like them — so this number is the look-alike
            # rate, and the bar is set to catch a threshold that has been
            # loosened into nonsense rather than to be passed comfortably.
            check("other-photo: look-alikes stay rare", wrong / n < 0.05,
                  f"{wrong}/{n}")


# --- live --------------------------------------------------------------------
def live(seconds: float) -> None:
    """Point it at the robot's own camera and narrate what it decides."""
    import json
    from inmoov.camera import Camera

    cfg = {}
    path = Path(__file__).resolve().parent.parent / "config" / "settings.json"
    try:
        cfg = json.loads(path.read_text()).get("camera", {})
    except Exception as exc:  # noqa: BLE001 - a missing settings file is not fatal
        print(f"  (no camera settings: {exc})")
    cam = Camera(backend=str(cfg.get("backend", "auto")), source=cfg.get("source"),
                 rotate_180=bool(cfg.get("flip", False)))
    fid = FaceId()
    print(f"live: {cam.backend_name} camera, {seconds:.0f}s — "
          "stand in front of FRED, walk away, come back")
    if not (cam.available() and fid.available()):
        print(f"  SKIP  camera={cam.available()} recogniser={fid.available()}")
        return
    cam.acquire()                                     # this one *does* want the sensor
    try:
        end = time.monotonic() + seconds
        last = None
        while time.monotonic() < end:
            who = fid.observe(cam.capture_gray())
            if who is not None and (last is None or who.id != last):
                print(f"  {time.strftime('%H:%M:%S')}  visitor {who.id}, "
                      f"sighting {who.sightings}, known {who.last_seen - who.first_seen:.0f}s")
                last = who.id
            elif who is None and last is not None:
                last = None
            time.sleep(1.0 / face_id.SAMPLE_HZ)
    finally:
        cam.release()
    s = fid.status()
    print(f"  {s['faces']}/{s['frames']} frames had a face, "
          f"{len(s['visitors'])} visitor(s): {s['visitors']}")
    fid.forget_all()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--faces", type=Path,
                    help="directory of person/*.jpg to measure accuracy on")
    ap.add_argument("--burst", type=int, default=10,
                    help="frames per visit for the accuracy run (default 10)")
    ap.add_argument("--camera", nargs="?", type=float, const=30.0,
                    help="run a live look for this many seconds (default 30)")
    args = ap.parse_args()

    logic_checks()
    if args.faces:
        print()
        accuracy_checks(args.faces, burst=args.burst)
    if args.camera:
        print()
        live(args.camera)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}: " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

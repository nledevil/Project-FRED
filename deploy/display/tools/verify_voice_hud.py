#!/usr/bin/env python3
"""Prove the native voice HUD renders exactly what the Python one does.

voice_hud.c is a port, not a rewrite, so the only interesting question about it
is whether the picture changed. This drives both renderers over one scripted
timeline — every state, both state transitions, the playhead sweeping right
across an utterance, and the sensor overlay with each kind of reading — and
compares the frames pixel for pixel.

Both are made deterministic the same way: the clock becomes a function of the
frame index, so "now" is identical on each side and neither can drift.

    python3 tools/verify_voice_hud.py            # or: make verify

Exits non-zero on any difference, and dumps the first offending frame from both
renderers so the failure can actually be looked at.

Memory note: a frame is 1.1 MB, so several hundred of them will not fit in the
Pi's tmpfs twice over. The C renderer therefore dumps to a pipe and both sides
are reduced to per-frame hashes as they stream; only a mismatch pays to
materialise anything.
"""
from __future__ import annotations

import hashlib
import json
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Two layouts: tools/ is a subdirectory in the repo, and everything lands in
# one directory on the chest Pi because that manifest flattens. Pick whichever
# actually holds the renderer — resolving to HERE.parent unconditionally meant
# this tool could never run on the machine it exists to check.
DISPLAY = next((d for d in (HERE.parent, HERE)
                if (d / "voice_hud.py").is_file()), HERE.parent)
sys.path.insert(0, str(DISPLAY))
import theme                                          # noqa: E402

BINARY = DISPLAY / "voice_hud"

W, H = 800, 480
FRAME_BYTES = W * H * 3
FPS = 30.0
PER_PHASE = 60
PHASES = ("idle", "listening", "thinking", "speaking")
N = PER_PHASE * len(PHASES)


def build_fixtures(tmp: Path) -> tuple[Path, Path]:
    """One timeline both renderers consume: per-frame voice docs + a metrics doc."""
    rng = random.Random(7)
    # Short envelope so the playhead completes its sweep inside one phase.
    levels = [round(abs(rng.gauss(0, 0.35)) % 1.0, 6) for _ in range(40)]
    frame_dt = 0.02
    speak_from = PHASES.index("speaking") * PER_PHASE
    # frac runs -0.5 -> 1.25 across the phase, so lead-in and tail are covered.
    play_at = speak_from / FPS + 0.5 * len(levels) * frame_dt

    docs = tmp / "docs"
    docs.mkdir()
    for n in range(N):
        state = PHASES[n // PER_PHASE]
        doc: dict = {"state": state}
        if state == "speaking":
            doc |= {"levels": levels, "play_at": play_at, "frame_dt": frame_dt}
        (docs / f"{n}.json").write_text(json.dumps(doc))

    metrics = tmp / "metrics.json"
    metrics.write_text(json.dumps({
        "enabled": True,
        "t": 1e9,                      # far future: never stale, so it's stable
        "node": "chest",
        "readings": {
            "left_us":  {"type": "distance", "cm": 142.3},   # normal
            "right_us": {"type": "distance", "cm": 399.0},   # at the no-echo mark
            "aux_us":   {"type": "distance", "cm": None},    # missing reading
            "pir_main": {"type": "motion", "active": True},  # motion
        },
    }))
    return docs, metrics


def python_hashes(docs: Path, metrics: Path, keep: int | None,
                  theme_name: str) -> tuple[list[str], bytes | None]:
    """Run voice_hud.py over the timeline, hashing each frame as it is blitted."""
    keep_path = docs.parent / "py_frame.raw"
    script = f'''
import sys, json, hashlib
sys.path.insert(0, {str(DISPLAY)!r})
import numpy as np, time as _time

DOCS = {str(docs)!r}
METRICS = json.load(open({str(metrics)!r}))
FPS, N, KEEP = {FPS!r}, {N!r}, {keep!r}
PERIOD = 1.0 / FPS

frame_no = [0]
_time.monotonic = lambda: frame_no[0] * PERIOD     # clock == frame index
_time.sleep = lambda *a: None                      # the limiter must not block

# Every module that draws resolves its palette when it is imported, so the
# theme has to be forced before the first of them — metrics_hud and
# cog_hud do it as well as voice_hud, which is why patching it later
# passed on the default theme and failed on the other two.
import theme
theme.load_name = lambda *a, **k: {theme_name!r}
import fb as fbmod, voice_state, metrics_hud
hashes, kept = [], [None]

class FakeFB:
    def __init__(self, dev="/dev/fb0"): self.w, self.h = {W}, {H}
    def show(self, rgb):
        buf = np.ascontiguousarray(rgb).tobytes()
        if KEEP is not None and frame_no[0] == KEEP: kept[0] = buf
        hashes.append(hashlib.md5(buf).hexdigest())
        frame_no[0] += 1
    def clear(self): pass
    def close(self): pass

fbmod.Framebuffer = FakeFB
fbmod.hide_cursor = lambda: None

_cache = {{}}
def doc(n):
    if n not in _cache:
        _cache[n] = json.load(open(f"{{DOCS}}/{{n}}.json"))
    return _cache[n]

class Feed(voice_state.VoiceFeed):
    def __init__(self): pass
    def poll(self): return doc(min(frame_no[0], N - 1))
    def state(self):
        s = self.poll().get("state", "idle")
        return s if s in voice_state.STATES else "idle"
    def level(self, now=None):
        d = self.poll(); levels = d.get("levels")
        play_at, frame_dt = d.get("play_at"), d.get("frame_dt")
        if not levels or play_at is None or not frame_dt: return 0.0
        i = int(((now if now is not None else _time.monotonic()) - play_at) / frame_dt)
        return float(levels[i]) if 0 <= i < len(levels) else 0.0

class MFeed:
    def poll(self): return METRICS

metrics_hud.time = _time                # staleness must use the fake clock too

import voice_hud
voice_hud.Framebuffer = FakeFB
voice_hud.hide_cursor = lambda: None
voice_hud.VoiceFeed = Feed
voice_hud.MetricsHud = lambda: metrics_hud.MetricsHud(MFeed())

sys.argv = ["voice_hud.py", "--seconds", str(N * PERIOD), "--fps", str(FPS)]
try: voice_hud.main()
except SystemExit: pass

sys.stdout.write("\\n".join(hashes))
if kept[0] is not None:
    open({str(keep_path)!r}, "wb").write(kept[0])
'''

    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"python renderer failed:\n{r.stderr}")
    return r.stdout.split(), (keep_path.read_bytes() if keep_path.exists() else None)


def c_hashes(docs: Path, metrics: Path, keep: int | None,
             theme_name: str) -> tuple[list[str], bytes | None]:
    """Stream the native renderer's frames off a pipe, hashing as they arrive."""
    proc = subprocess.Popen(
        [str(BINARY), "--sim", "--sim-docs", str(docs), "--frames", str(N),
         "--fps", str(FPS), "--metrics-path", str(metrics),
         "--theme", theme_name, "--dump", "/dev/stdout"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    hashes, kept = [], None
    while True:
        buf = proc.stdout.read(FRAME_BYTES)
        if not buf or len(buf) < FRAME_BYTES:
            break
        if keep is not None and len(hashes) == keep:
            kept = buf
        hashes.append(hashlib.md5(buf).hexdigest())
    err = proc.stderr.read().decode()
    if proc.wait() != 0:
        sys.exit(f"native renderer failed:\n{err}")
    return hashes, kept


def main() -> int:
    if not BINARY.exists():
        sys.exit(f"{BINARY} not built — run `make` first")

    tmp = Path(tempfile.mkdtemp(prefix="voice-hud-verify-"))
    try:
        docs, metrics = build_fixtures(tmp)
        # Every theme, not just the running one: the palettes are the newest
        # thing that exists twice, and a colour that disagrees between the two
        # renderers would otherwise only show up on the theme nobody tested.
        ok = True
        for theme_name in theme.ORDER:
            py, _ = python_hashes(docs, metrics, None, theme_name)
            c, _ = c_hashes(docs, metrics, None, theme_name)

            if len(py) != N or len(c) != N:
                print(f"FAIL [{theme_name}]: frame counts differ — "
                      f"python {len(py)}, native {len(c)}")
                return 1

            bad = [i for i, (a, b) in enumerate(zip(py, c)) if a != b]
            if not bad:
                print(f"  OK [{theme_name}]: {N}/{N} frames identical")
                continue
            ok = False
            break
        if ok:
            print(f"OK: {N}/{N} frames identical in all {len(theme.ORDER)} themes "
                  f"({', '.join(PHASES)}; {len(PHASES) - 1} state changes, "
                  f"playhead sweep, sensor overlay)")
            return 0

        first = bad[0]
        print(f"FAIL [{theme_name}]: {len(bad)}/{N} frames differ, first at {first} "
              f"({PHASES[first // PER_PHASE]})")
        # Re-run just far enough to materialise the offending frame from both.
        _, pyf = python_hashes(docs, metrics, first, theme_name)
        _, cf = c_hashes(docs, metrics, first, theme_name)
        if pyf and cf:
            out = Path.cwd()
            (out / "verify-py.raw").write_bytes(pyf)
            (out / "verify-c.raw").write_bytes(cf)
            diff = sum(1 for a, b in zip(pyf, cf) if a != b)
            worst = max((abs(a - b) for a, b in zip(pyf, cf)), default=0)
            print(f"  {diff} bytes differ, largest by {worst}")
            print(f"  wrote verify-py.raw / verify-c.raw ({W}x{H} RGB888)")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

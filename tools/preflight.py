#!/usr/bin/env python3
"""The show-morning check: is FRED actually ready, or does he only look it?

Every failure this probes for has happened, and most of them share a shape:
nothing crashed, every light was green, and the robot was quietly degraded —
Ollama probing before the GPU driver loaded (28 s answers all day, "nothing
looked broken"), the wide camera enumerated but delivering no frames, the WiFi
dongle failing calibration at boot, a panel that came back from a restart
deaf. Catching them used to mean remembering five journalctl incantations
spread across SERVICE.md, TODO.md and memory. This runs them all and answers
in one green/red column, before an audience arrives. (This checks the LIVE
robot; tools/test_all.sh runs the code tests. Different jobs — run both.)

    ./venv/bin/python tools/preflight.py            # the lot (plays a clip)
    ./venv/bin/python tools/preflight.py --quiet    # skip the audible speaker test

Three verdicts: PASS, WARN, FAIL. Only FAIL sets the exit code — a WARN is
"know this before the show", not "the show is off". The speaker check can only
prove aplay exited cleanly; whether sound actually left the speaker is a thing
only ears can verify, and the line says so.

Read-only against the robot, with two deliberate exceptions: the speaker test
plays sounds/test.wav (skippable), and the mic probe records half a second
only when the listener is off (when it is on, the running capture is the
evidence and the device is left alone).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NUC = "http://127.0.0.1:8080"
CHEST = "http://10.0.0.11:8081"
HEAD_HEALTH = "http://10.0.0.10:8082/api/health"

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
RESULTS: list[tuple[str, str, str]] = []


def note(verdict: str, label: str, detail: str = ""):
    RESULTS.append((verdict, label, detail))
    pad = {"PASS": "  ", "WARN": " !", "FAIL": "!!"}[verdict]
    print(f" {pad} {verdict}  {label}" + (f"  — {detail}" if detail else ""))


def sh(argv, timeout=10) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"no such command: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def ssh(host: str, cmd: str, timeout=12) -> tuple[int, str]:
    return sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
               host, cmd], timeout=timeout)


def http_json(url: str, timeout=8.0, headers=None) -> dict | None:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001 - a dead endpoint is a finding, not a crash
        return None


def http_bytes(url: str, timeout=12.0) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except Exception:  # noqa: BLE001
        return None


def settings() -> dict:
    try:
        return json.loads((ROOT / "config" / "settings.json").read_text())
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true",
                    help="skip the audible speaker clip (venue courtesy)")
    args = ap.parse_args()
    cfg = settings()

    print("services")
    # ------------------------------------------------------------------ NUC
    for unit in ("fred-panel", "ollama"):
        rc, out = sh(["systemctl", "is-active", unit])
        note(PASS if out.strip() == "active" else FAIL,
             f"NUC {unit}", out.strip())
    rc, out = sh(["systemctl", "--failed", "--no-legend", "--plain"])
    failed = [l.split()[0] for l in out.splitlines() if l.strip()]
    note(PASS if not failed else WARN, "NUC failed units",
         ", ".join(failed) if failed else "none")

    # ------------------------------------------------------------------ Pis
    rc, out = ssh("head", "systemctl is-active servo-server camera-stream led-server")
    states = out.split()
    if rc != 0 and not states:
        note(FAIL, "head Pi reachable over ssh", out.strip()[:80])
    else:
        for unit, st in zip(("servo-server", "camera-stream", "led-server"), states):
            note(PASS if st == "active" else FAIL, f"head {unit}", st)
    rc, out = ssh("chest", "systemctl is-active inmoov-display")
    if rc != 0 and not out.strip():
        note(FAIL, "chest Pi reachable over ssh", out.strip()[:80])
    else:
        note(PASS if out.strip() == "active" else FAIL,
             "chest inmoov-display", out.strip())

    print("the failure that hides: Ollama on CPU")
    # SERVICE.md: check after ANY reboot. CPU-only Ollama answers in ~28 s and
    # nothing else looks wrong. The journal line is the authority.
    rc, out = sh(["journalctl", "-u", "ollama", "-b", "--no-pager", "-o", "cat"])
    libs = [l for l in out.splitlines() if "library=" in l]
    if not libs:
        note(WARN, "Ollama GPU library", "no library= line this boot — ask it a "
                                         "question and time it")
    elif any("library=Vulkan" in l for l in libs):
        note(PASS, "Ollama running on the Arc iGPU (library=Vulkan)")
    else:
        note(FAIL, "Ollama fell back to CPU",
             libs[-1].strip()[:70] + " — expect ~28 s answers; restart ollama "
             "after the GPU driver settles")

    print("the brain, in one poll")
    state = http_json(f"{NUC}/api/state")
    if state is None:
        note(FAIL, "/api/state answers", "panel down or wedged — nothing below "
                                         "this can be trusted")
        state = {}
    else:
        note(PASS, "/api/state answers")
    link = state.get("servo_link") or {}
    note(PASS if link.get("online") else FAIL, "servo link to the head",
         link.get("error") or f"mock={link.get('mock')}")
    brain = state.get("brain") or {}
    note(PASS if brain.get("claude_ready") else WARN, "Claude API key loaded",
         "" if brain.get("claude_ready") else
         (brain.get("claude_error") or "no key — local model only"))
    note(PASS if brain.get("local_ready") else WARN, "local model reachable",
         brain.get("local_model", ""))
    spotter = state.get("spotter") or {}
    note(PASS if spotter.get("running") else WARN, "wide spotter running",
         spotter.get("error") or spotter.get("hint") or "")
    doa = state.get("mic_doa") or {}
    note(PASS if doa.get("running") else WARN, "mic direction-of-arrival running",
         doa.get("error") or "")
    diag = state.get("diagnostic") or {}
    if diag.get("enabled"):
        note(WARN, "diagnostic mode is ON",
             "unrestricted!" if diag.get("unrestricted") else
             "checks tier — expires on idle, but know it is armed")
    else:
        note(PASS, "diagnostic mode off")

    print("eyes: a fresh frame from each lens, twice")
    # The stale-snapshot bug's live twin. snapshot() now waits for a frame
    # captured after the request, so two pulls MUST hash differently — the
    # same bytes twice means a frozen source however healthy it looks.
    for name, url in (("head camera", f"{NUC}/camera/snapshot"),
                      ("wide camera", f"{NUC}/camera/wide/snapshot")):
        a = http_bytes(url)
        if not a:
            note(FAIL, f"{name} delivers a frame", "no snapshot — see the "
                 "panel's camera hint (USB power-cycle button for the wide)")
            continue
        time.sleep(0.4)
        b = http_bytes(url)
        if not b:
            note(FAIL, f"{name} second frame", "first worked, second did not")
        elif hashlib.md5(a).hexdigest() == hashlib.md5(b).hexdigest():
            note(FAIL, f"{name} is FROZEN",
                 "two pulls, identical bytes — the child/father failure")
        else:
            note(PASS, f"{name} live", f"{len(a)} bytes, frames differ")

    print("ears")
    voice = state.get("voice") or {}
    mic = voice.get("mic") or {}
    if voice.get("listening"):
        cap = mic.get("capturing")
        peak = mic.get("peak_recent")
        note(PASS if cap else FAIL, "listener capturing", f"peak_recent={peak}")
        if cap and (peak is None or peak == 0):
            note(WARN, "mic hears only digital zero",
                 "quiet room gates to zero on this card — talk at him and "
                 "watch the meter before trusting it")
        note(PASS, "wake listener is ON")
    else:
        note(WARN, "wake listener is OFF",
             "by design after a boot — turn it on from either panel")
        # The listener is not holding the device, so a half-second probe is safe.
        dev = (cfg.get("sound") or {}).get("device", "plughw:0,0")
        ch = str((cfg.get("voice") or {}).get("mic_channels", 1))
        rc, out = sh(["arecord", "-q", "-D", dev, "-f", "S16_LE", "-r", "16000",
                      "-c", ch, "-d", "1", "-t", "raw", "/dev/null"], timeout=8)
        note(PASS if rc == 0 else FAIL, "mic capture probe",
             "" if rc == 0 else out.strip().splitlines()[-1][:70] if out.strip() else f"rc={rc}")

    print("voice out")
    sound = state.get("sound") or {}
    note(PASS if voice.get("can_speak") else FAIL, "TTS + audio device ready",
         f"device={sound.get('device')}")
    if args.quiet:
        note(WARN, "speaker clip skipped (--quiet)",
             "aplay untested this run — ears untested every run")
    else:
        rc, out = sh(["aplay", "-q", "-D", str(sound.get("device") or "default"),
                      str(ROOT / "sounds" / "test.wav")], timeout=10)
        note(PASS if rc == 0 else FAIL, "speaker accepts audio",
             "did you HEAR it? this check cannot" if rc == 0
             else out.strip()[:70])

    print("disk, on all three")
    checks = [("NUC", None), ("head", "head"), ("chest", "chest")]
    for label, host in checks:
        if host is None:
            usage = shutil.disk_usage("/")
            free_pct = 100 * usage.free / usage.total
        else:
            rc, out = ssh(host, "df --output=pcent / | tail -1")
            if rc != 0:
                note(WARN, f"{label} disk", "unreachable")
                continue
            free_pct = 100 - float(out.strip().rstrip("%") or 100)
        v = PASS if free_pct > 15 else (WARN if free_pct > 5 else FAIL)
        note(v, f"{label} disk free", f"{free_pct:.0f}%")

    print("chest panel")
    tok = (cfg.get("display") or {}).get("token", "")
    chest = http_json(f"{CHEST}/api/state",
                      headers={"X-Display-Token": tok} if tok else {})
    if chest is None:
        note(FAIL, "chest display API", "not answering (token wrong, or daemon down)")
    else:
        note(PASS, "chest display API",
             f"showing {chest.get('animation')!r}"
             + (" — CRASHED+LATCHED: " + str(chest.get("error"))[:50]
                if chest.get("error") else ""))
        if chest.get("animation") == "settings":
            note(WARN, "chest is sitting in the settings menu",
                 "it self-closes in minutes now, but a show wants a look up")
    head_h = http_json(HEAD_HEALTH)
    note(PASS if head_h and head_h.get("ok") else FAIL, "head servo API health",
         f"mock={head_h.get('mock')}" if head_h else "no answer")

    print("watchdogs")
    # R3: a wedged-but-running service is restarted only if its watchdog timer
    # is armed. A preflight that did not check the watchdogs would miss exactly
    # the safety net it is meant to lean on mid-show.
    rc, out = sh(["systemctl", "is-active", "fred-panel-watchdog.timer"])
    note(PASS if out.strip() == "active" else WARN, "NUC panel watchdog armed",
         out.strip())
    rc, out = ssh("head", "systemctl is-active servo-watchdog.timer camera-watchdog.timer")
    note(PASS if out.split() == ["active", "active"] else WARN,
         "head watchdogs armed", out.replace("\n", " "))
    rc, out = ssh("chest", "systemctl is-active display-watchdog.timer")
    note(PASS if out.strip() == "active" else WARN, "chest watchdog armed",
         out.strip())

    print("settings sanity")
    track = cfg.get("track")
    if track == {} or track is None:
        note(WARN, "face-tracking tunables never saved",
             "gains are defaults — fine, but bench-tune before relying on it")
    else:
        note(PASS, "face-tracking tunables present")
    if float((cfg.get("mic_doa") or {}).get("mount_offset", 0.0)) == 0.0:
        note(WARN, "DOA forward never calibrated",
             "stand in front, talk, press Capture forward on the admin panel")
    else:
        note(PASS, "DOA forward calibrated",
             f"{(cfg.get('mic_doa') or {}).get('mount_offset')} deg")
    ev = state.get("event") or {}
    note(PASS, "event mode", "ON — short answers, slow cart"
         if ev.get("enabled") else "off — remember to flip it for a hall")

    print("backups")
    # tools/backup.sh writes dated archives; a robot whose PIN, API key and
    # device tuning exist on exactly one disk is one dead SSD from a lost day.
    bdir = Path.home() / "fred-backups"
    newest = max(bdir.glob("fred-backup-*.tar.gz"),
                 key=lambda p: p.stat().st_mtime, default=None) if bdir.is_dir() else None
    if newest is None:
        note(WARN, "no backup archive found",
             "run tools/backup.sh — settings.json holds the PIN, tokens and tuning")
    else:
        age_d = (time.time() - newest.stat().st_mtime) / 86400
        note(PASS if age_d <= 30 else WARN, "latest backup",
             f"{newest.name}, {age_d:.0f} days old"
             + ("" if age_d <= 30 else " — over a month; run tools/backup.sh"))

    # ---------------------------------------------------------------- verdict
    fails = [r for r in RESULTS if r[0] == FAIL]
    warns = [r for r in RESULTS if r[0] == WARN]
    print()
    print(f"{'NOT READY' if fails else 'READY'}: "
          f"{len([r for r in RESULTS if r[0] == PASS])} pass, "
          f"{len(warns)} warn, {len(fails)} fail")
    for _, label, detail in fails:
        print(f"  FAIL: {label}" + (f" — {detail}" if detail else ""))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

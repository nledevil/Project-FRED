"""Sound playback for the InMoov head — a thin wrapper around ALSA's ``aplay``.

Design goals mirror ``led.py`` / ``camera.py`` / ``servo_controller.py``:
  * Work with NO audio hardware (degrade to a silent no-op) so control logic can
    run on any machine. ``available()`` is False when ``aplay`` is missing or no
    ALSA card is present, in which case play() calls are silent no-ops.
  * No extra Python dependencies — playback shells out to ``aplay`` (the same
    tool we verified the USB P10S card with), so there's nothing to pip-install.
  * Non-blocking: playback runs in a background thread so a web request returns
    immediately. Starting a new sound stops the previous one (one voice at a
    time), matching how a talking head should behave.

Sounds are ``.wav`` files under ``sounds/`` (project root), addressed by name
without the extension: ``play("startup")`` plays ``sounds/startup.wav``.
"""
from __future__ import annotations

import atexit
import hashlib
import os
import re
import shutil
import select
import subprocess
import tempfile
import random
import threading
import time
import wave
from collections import OrderedDict
from pathlib import Path

SOUNDS_DIR = Path(__file__).resolve().parent.parent / "sounds"
# Optional Piper neural TTS: self-contained binary + voice model, preferred over
# espeak when present. Convention: models/piper/bin/piper + models/piper/voices/*.onnx.
PIPER_DIR = Path(__file__).resolve().parent.parent / "models" / "piper"
# Preferred voice: the first voice whose filename contains this substring wins
# (case-insensitive); otherwise the first voice alphabetically is used.
# Was "ryan", but that model garbles longer utterances on this rig — it renders
# a valid, correct-length, un-clipped WAV whose *phonemes* are mush past a
# sentence or so (short replies were fine, which masked it). Verified against
# espeak, lessac, bryce, and northern_english_male (all clean) through the
# identical playback path, so the fault is the ryan model's synthesis, not
# resampling/gain/the USB codec.
PIPER_VOICE = "northern_english_male"

# How many rendered utterances to keep on disk. FRED repeats a handful of lines
# constantly ("Yes?" on every wake word, the boot greeting), and a cache hit
# turns a ~0.8 s render into a file lookup. Long Claude replies never repeat,
# but they age out harmlessly.
_TTS_CACHE_MAX = 64


# Respellings applied on the way into the synthesiser. Piper phonemises through
# espeak-ng, which reads a few strings the way they're spelled rather than the
# way they're said, and the only lever we have is the spelling we hand it.
#
# Applied in _synth() alone, so this changes how a line *sounds* and nothing
# else: the transcript, the API replies and FRED's conversation memory all keep
# the words he actually chose.
#
# Verified against piper's own bundled libespeak-ng with the voice this build
# uses (en-gb-x-rp), rather than by ear — add to this list the same way:
#
#     "Pis"   -> pˈɪs      (i.e. "piss")
#     "Pi's"  -> pˈaɪz     correct, and "Pi" on its own is already pˈaɪ
_SAY_AS = (
    # "I have two Raspberry Pis for..." landed as "two raspberry piss". Only the
    # bare plural is wrong, so that is all this touches.
    (re.compile(r"\bpis\b", re.I), "Pi's"),
)


def _say_as(text: str) -> str:
    """Respell ``text`` so the synthesiser pronounces it correctly."""
    for pattern, replacement in _SAY_AS:
        text = pattern.sub(replacement, text)
    return text


def _silent_unlink(path: str | Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class _PiperDaemon:
    """A long-lived ``piper`` process, so the voice model loads only once.

    Piper loads its ONNX voice on the first synthesis — measured at ~1.65 s on
    this Pi 4 — then renders a short sentence in ~0.75 s. Re-exec'ing it per
    utterance paid that load *every time*, and it dominated FRED's reply
    latency. Keeping one process alive turns it into a one-off cost, paid at
    boot by ``Sound.warm()``.

    Protocol (piper's own, with ``--output_dir``): write one line of text on
    stdin, read one line back on stdout — the path of the rendered WAV. One
    utterance per line, so embedded newlines are collapsed to spaces.

    Any failure (dead process, wedged synthesis, unparseable reply) kills the
    child and returns None; ``Sound._synth`` then falls back to a one-shot
    ``piper`` run, and the next call respawns the daemon. The daemon is a
    latency optimisation, never a dependency.
    """

    _TIMEOUT = 30.0     # generous: a long sentence on a busy Pi, incl. model load

    def __init__(self, binary: str, voice: str, env: dict, out_dir: Path):
        self._binary, self._voice, self._env = binary, voice, env
        self._out_dir = out_dir
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()       # serialises the stdin/stdout exchange
        self._closed = False                # set by close(): never spawn again

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _spawn(self) -> bool:
        if self._closed:                    # shutting down — don't resurrect piper
            return False
        try:
            # bufsize=0: raw pipes, so select() on stdout can't be fooled by
            # bytes already sitting in a Python-side read buffer.
            self._proc = subprocess.Popen(
                [self._binary, "--model", self._voice,
                 "--output_dir", str(self._out_dir), "-q"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=self._env, bufsize=0)
            return True
        except OSError as exc:
            print(f"[Sound] piper daemon spawn failed: {exc}")
            self._proc = None
            return False

    def synth(self, text: str) -> str | None:
        """Render one utterance; return the WAV path, or None to fall back."""
        line = " ".join(text.split())       # one utterance per line
        if not line or self._closed:
            return None
        with self._lock:
            for _ in range(2):              # respawn once if the child had died
                if not self._alive() and not self._spawn():
                    return None
                path = self._exchange(line)
                if path is not None:
                    return path
                self.stop()                 # wedged or dead — retry on a fresh one
        return None

    def _exchange(self, line: str) -> str | None:
        """Write one line, read back the first non-empty line of output.

        Tolerates the pipes being closed underneath us — close() can land here
        while a render is in flight (e.g. the process exits during the boot warm-up),
        and that's a None, not a crash on a background thread.
        """
        proc = self._proc
        try:
            proc.stdin.write(line.encode("utf-8") + b"\n")
            proc.stdin.flush()
            buf = b""
            deadline = time.monotonic() + self._TIMEOUT
            while True:
                if b"\n" in buf:
                    head, buf = buf.split(b"\n", 1)
                    path = head.decode("utf-8", "replace").strip()
                    if not path:
                        continue            # piper sometimes emits a blank line
                    return path if os.path.isfile(path) else None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    print("[Sound] piper daemon timed out — restarting it")
                    return None
                if not select.select([proc.stdout], [], [], remaining)[0]:
                    continue
                chunk = proc.stdout.read(4096)
                if not chunk:               # EOF: the child died
                    return None
                buf += chunk
        except (BrokenPipeError, OSError, ValueError):
            return None                     # dead child, or close() pulled the pipes

    def stop(self) -> None:
        """Kill the child. The next synth() respawns it (unless close()d)."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:      # noqa: BLE001
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:  # noqa: BLE001
                pass
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        """Shut down for good — no respawn, even if a synth() is mid-flight."""
        self._closed = True
        self.stop()


class Sound:
    """Plays named ``.wav`` files through an ALSA device via ``aplay``.

    Parameters
    ----------
    device : str
        ALSA PCM device passed to ``aplay -D`` (default ``"plughw:0,0"`` — the
        USB P10S via the ``plug`` plugin, which auto-converts sample rate/format
        so both 48 kHz chimes and 22 kHz espeak speech play on it). Use
        ``"default"`` to follow /etc/asound.conf instead.
    sounds_dir : str | Path
        Directory of ``.wav`` files. Defaults to ``<project>/sounds``.
    enabled : bool
        Master mute. When False, play() is a no-op even if hardware exists.
    lead_in : float
        Seconds of silence to prepend to every clip before playback (default
        0.0 = off). Some audio devices (certain USB/Bluetooth codecs) drop the
        first fraction of a second while their output stream spins up, clipping
        the first word or two of speech; padding the front with silence lets the
        device wake during the silence instead. Leave 0 for devices that start
        instantly; try ~0.4–0.6 for one that swallows the opening words.
    sync_offset : float
        Seconds to add to the estimated moment the first audio sample becomes
        audible (see ``audio_epoch``). ``lead_in`` is already accounted for
        exactly; this trims the *residual* latency between ``aplay`` starting
        and sound leaving the speaker (ALSA period buffering, USB codec
        wake-up). Positive = the jaw waits longer before it starts moving, for
        when the mouth runs ahead of the voice; negative = the jaw moves sooner.
        Tune by ear, usually within ±0.2.
    audit : bool
        Audit (dry-run) mode. Speech is still *rendered* and still occupies the
        clock for exactly as long as it would have taken to play, but no audio
        device is ever opened — see ``set_audit``.
    """

    def __init__(self, device: str = "plughw:0,0",
                 sounds_dir: str | Path = SOUNDS_DIR, enabled: bool = True,
                 lead_in: float = 0.0, sync_offset: float = 0.0,
                 audit: bool = False):
        self.device = device
        self.sounds_dir = Path(sounds_dir)
        self.enabled = enabled
        self.lead_in = max(0.0, float(lead_in))
        self.sync_offset = float(sync_offset)
        self._audit = bool(audit)
        # In audit mode nothing is spawned, so there is no process to poll for
        # "still playing". Instead we remember when the clip *would* have ended
        # and let is_playing() answer from the clock.
        self._virtual_end: float | None = None
        self._lock = threading.Lock()
        # Suspended = the audio card has been handed off to another owner (e.g.
        # MyRobotLab). While suspended, playback is a no-op. (The mic/arecord is
        # held by the Listener, which is stopped separately during a handoff.)
        self._suspended = False
        self._proc: subprocess.Popen | None = None
        # Monotonic time at which the current clip's first sample is expected to
        # be audible. The jaw animation schedules itself against this, not
        # against "now" — see audio_epoch().
        self._audio_t0: float | None = None
        self._ok = shutil.which("aplay") is not None
        self._espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        # Piper is the preferred TTS (far more natural voice); espeak is the
        # fallback. render_tts()/speak() try piper first, then espeak.
        self._piper, self._piper_voice, self._piper_env = self._discover_piper()
        # Rendered speech lives here for the life of the process: the piper
        # daemon writes straight into it, and render_tts() reuses hits.
        self._cache_dir = Path(tempfile.mkdtemp(prefix="inmoov-tts-"))
        self._tts_cache: OrderedDict[str, str] = OrderedDict()
        self._tts_lock = threading.Lock()
        self._daemon = (_PiperDaemon(self._piper, self._piper_voice,
                                     self._piper_env, self._cache_dir)
                        if self._piper else None)
        atexit.register(self.close)
        if not self._ok:
            print("[Sound] aplay not found — audio disabled (no-op mode).")

    # ---- capability -------------------------------------------------------
    def available(self) -> bool:
        """True when playback is possible (aplay present, or audit mode, which
        needs no device at all). Independent of the ``enabled`` mute flag, which
        callers can toggle at runtime."""
        return self._ok or self._audit

    # ---- audit (dry run) --------------------------------------------------
    def is_audit(self) -> bool:
        return self._audit

    def set_audit(self, on: bool) -> None:
        """Turn audit mode on/off. Idempotent; stops anything currently playing.

        Audit mode is deliberately *not* a mute. A mute (``enabled=False``) makes
        play_file() fail, which tells the lip-sync layer no audio is coming and
        stops the jaw and the on-screen mouth dead. Audit instead simulates a
        successful playback of the correct duration, so the whole speech
        pipeline — render, envelope, mouth publish, jaw schedule — runs in real
        time against a silent speaker. That is the point: you get to watch
        exactly what FRED would have done.
        """
        on = bool(on)
        if on == self._audit:
            return
        self.stop()                # never leave a real aplay running behind us
        self._audit = on

    def list(self) -> list[str]:
        """Sorted names of playable sounds (``.wav`` stems in sounds_dir)."""
        if not self.sounds_dir.is_dir():
            return []
        return sorted(p.stem for p in self.sounds_dir.glob("*.wav"))

    def list_category(self, subdir: str) -> list[str]:
        """Sorted ``.wav`` filenames (with extension) in ``sounds/<subdir>/`` —
        e.g. the terminator clips. Empty list if the folder is missing."""
        d = self.sounds_dir / subdir
        if not d.is_dir():
            return []
        return sorted(p.name for p in d.glob("*.wav"))

    def is_playing(self) -> bool:
        with self._lock:
            if self._virtual_end is not None:      # audit: the clock is the clip
                return time.monotonic() < self._virtual_end
            return self._proc is not None and self._proc.poll() is None

    def can_speak(self) -> bool:
        """True when text-to-speech is possible (piper or espeak, + aplay)."""
        return self._ok and (self._piper is not None or self._espeak is not None)

    def tts_engine(self) -> str | None:
        """Name of the active TTS backend: ``"piper"``, ``"espeak"``, or None."""
        return "piper" if self._piper else "espeak" if self._espeak else None

    def settings(self) -> dict:
        return {"device": self.device, "enabled": self.enabled,
                "suspended": self._suspended, "audit": self._audit,
                "lead_in": self.lead_in, "sync_offset": self.sync_offset,
                "playing": self.is_playing(), "can_speak": self.can_speak(),
                "tts": self.tts_engine(), "sounds": self.list()}

    # ---- hardware handoff -------------------------------------------------
    def is_suspended(self) -> bool:
        return self._suspended

    def suspend(self) -> None:
        """Release the audio card: stop any current playback and refuse new
        playback until resume(). Idempotent."""
        self._suspended = True
        self.stop()

    def resume(self) -> None:
        """Take the audio card back; playback works again. Idempotent."""
        self._suspended = False

    # ---- text-to-speech backends -----------------------------------------
    @staticmethod
    def _discover_piper() -> tuple:
        """Locate the bundled Piper binary + first voice model.

        Returns ``(binary_path, voice_path, env)`` or ``(None, None, None)`` if
        piper isn't installed. The binary ships its own shared libraries, so we
        prepend its dir to LD_LIBRARY_PATH in the returned env.
        """
        binary = PIPER_DIR / "bin" / "piper"
        vdir = PIPER_DIR / "voices"
        voices = sorted(vdir.glob("*.onnx")) if vdir.is_dir() else []
        if not binary.is_file() or not os.access(binary, os.X_OK) or not voices:
            return None, None, None
        # Prefer PIPER_VOICE (e.g. "ryan"); fall back to the first voice.
        pick = next((v for v in voices if PIPER_VOICE.lower() in v.name.lower()),
                    voices[0])
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = f"{binary.parent}:{env.get('LD_LIBRARY_PATH', '')}".rstrip(":")
        return str(binary), str(pick), env

    def _synth(self, text: str, out_path: str, speed: int, voice: str) -> bool:
        """Render ``text`` to ``out_path`` (WAV) via piper, else espeak. Both
        produce 22 kHz mono WAV. Returns True on success. ``speed``/``voice``
        apply to the espeak fallback only (piper uses its model's natural voice).

        Piper goes through the warm daemon when it's healthy (it renders into
        the cache dir, so we just move the file into place); a daemon failure
        falls back to a cold one-shot run, which is slow but always works.

        Spelling is respelled for the synthesiser here (see ``_say_as``) — this
        is the one place all three backends funnel through.
        """
        text = _say_as(text)
        if self._daemon is not None:
            rendered = self._daemon.synth(text)
            if rendered is not None:
                try:
                    os.replace(rendered, out_path)
                    return True
                except OSError as exc:
                    print(f"[Sound] piper daemon output unusable: {exc}")
                    _silent_unlink(rendered)
        if self._piper is not None:
            try:
                subprocess.run(
                    [self._piper, "--model", self._piper_voice,
                     "--output_file", out_path],
                    input=text.encode("utf-8"), env=self._piper_env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=True)
                return True
            except (subprocess.CalledProcessError, OSError) as exc:
                print(f"[Sound] piper render failed: {exc} — trying espeak")
        if self._espeak is not None:
            try:
                subprocess.run([self._espeak, "-s", str(int(speed)), "-v", voice,
                                "-w", out_path, text],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               check=True)
                return True
            except (subprocess.CalledProcessError, OSError) as exc:
                print(f"[Sound] espeak render failed: {exc}")
        return False

    def warm(self) -> None:
        """Pre-load the piper voice so the first real utterance isn't slow.

        Renders a throwaway word through the daemon. Costs ~2 s once; call it in
        a background thread at boot. Safe to call when piper isn't installed, and
        never raises — it runs on a thread nobody joins, and a warm-up failure
        just means the first real utterance pays the model load.
        """
        if self._daemon is None:
            return
        try:
            path = self._daemon.synth("Ready.")
        except Exception as exc:  # noqa: BLE001
            print(f"[Sound] TTS warm-up failed: {exc}")
            return
        if path:
            _silent_unlink(path)

    # ---- playback ---------------------------------------------------------
    def resolve(self, name: str) -> Path | None:
        """Map a sound name to its .wav path, or None if it doesn't exist.

        Rejects names that would escape sounds_dir (path separators / '..').
        """
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            return None
        path = self.sounds_dir / f"{name}.wav"
        return path if path.is_file() else None

    def play(self, name: str, wait: bool = False) -> bool:
        """Play the named sound (``sounds/<name>.wav``). Returns True if started.

        Any currently-playing sound is stopped first. With ``wait=True`` this
        blocks until playback finishes (handy for CLI/boot chimes); the default
        returns immediately so web handlers don't stall.
        """
        path = self.resolve(name)
        if path is None:
            print(f"[Sound] unknown sound {name!r} (have: {self.list()})")
            return False
        return self.play_file(path, wait=wait)

    def play_random(self, subdir: str, wait: bool = False) -> bool:
        """Play a random ``.wav`` clip from ``sounds/<subdir>/``. Returns True if
        a clip was found and playback started, False if the folder is missing or
        empty — so callers can fall back to TTS when no clips are installed."""
        d = self.sounds_dir / subdir
        clips = sorted(d.glob("*.wav")) if d.is_dir() else []
        if not clips:
            return False
        return self.play_file(random.choice(clips), wait=wait)

    def _with_lead_in(self, path: Path) -> tuple[Path, bool]:
        """Return ``(path_to_play, owned)``. When ``lead_in`` > 0, write a temp
        copy of the WAV with that many seconds of silence prepended and return it
        with ``owned=True`` (the caller deletes it after playback); this masks the
        stream-startup latency of devices that clip the opening words. Returns
        ``(path, False)`` (play in place) when no lead-in is set or padding fails.

        Callers that care about lip-sync must ask ``audio_epoch()`` when the real
        audio starts rather than assume it's immediate — the padding this adds
        delays it, and getting that wrong makes the jaw run ahead of the voice.
        """
        if self.lead_in <= 0:
            return path, False
        try:
            with wave.open(str(path), "rb") as w:
                params = w.getparams()
                frames = w.readframes(w.getnframes())
            silence = bytes(int(self.lead_in * params.framerate)
                            * params.sampwidth * params.nchannels)
            fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="inmoov-lead-")
            os.close(fd)
            with wave.open(tmp, "wb") as w:
                w.setparams(params)
                w.writeframes(silence + frames)
            return Path(tmp), True
        except (wave.Error, OSError, EOFError) as exc:
            print(f"[Sound] lead-in pad failed ({exc}) — playing without it")
            return path, False

    def audio_epoch(self) -> float | None:
        """Monotonic time at which the current clip's *first real sample* is
        expected to be audible, or None when nothing is playing.

        This is the clock a lip-sync animation must schedule against. It is the
        moment ``aplay`` was spawned, plus the ``lead_in`` silence we prepended,
        plus ``sync_offset`` for the residual device latency. Animating from
        "now" instead makes the jaw lead the voice by exactly ``lead_in``.
        """
        return self._audio_t0

    def play_file(self, path: str | Path, wait: bool = False,
                  pad: bool = True) -> bool:
        """Play a specific .wav file. Returns True if playback was started.

        ``pad`` applies the ``lead_in`` silence. Pass False for the second and
        later clips of a continuous utterance: the device is already awake by
        then, and re-padding every clip would insert a ``lead_in``-long gap
        between the sentences of one reply.

        This blocks briefly (see ``probe`` below) to catch an aplay that fails on
        startup, which delays the caller's lip-sync by that much when there's no
        lead-in to hide it — hence the shorter probe on continuation clips.
        """
        if not self.available() or not self.enabled or self._suspended:
            why = ("no aplay" if not self._ok
                   else "handed off" if self._suspended else "muted")
            print(f"[Sound] (silent) would play {path} [{why}]")
            return False
        path = Path(path)
        if not path.is_file():
            print(f"[Sound] file not found: {path}")
            return False
        self.stop()                                  # one voice at a time
        if self._audit:
            # Dry run: no device, no aplay, no padded temp file — just the clock.
            return self._play_virtual(path, pad=pad, wait=wait)
        if pad:
            play_path, owned = self._with_lead_in(path)   # prepend device-warmup silence
            pre_roll = self.lead_in if owned else 0.0     # padding may have failed
        else:
            play_path, owned, pre_roll = path, False, 0.0
        cleanup = (lambda: _silent_unlink(play_path)) if owned else (lambda: None)
        # How long to wait for a failing aplay to exit before we call it started.
        # The first clip of an utterance follows arecord releasing the card, which
        # is when a busy-device failure is likely, so give it the full window; the
        # lead-in silence hides the wait. A continuation clip only races our own
        # previous aplay, fails within a few ms if it's going to, and has no
        # padding to hide behind — a long probe there just makes the jaw start late.
        probe = 0.1 if pad else 0.04
        # Retry once: capture (the wake-word listener) and playback share the one
        # USB card, and aplay can transiently fail to open it (device busy) right
        # as the listener's arecord restarts. We check aplay's exit code — a
        # failure used to be silent, so the jaw would mime with no sound.
        for attempt in (1, 2):
            with self._lock:
                self._proc = subprocess.Popen(
                    ["aplay", "-q", "-D", self.device, str(play_path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                # Stamp the clock at spawn, before any sleeps below, so the jaw
                # is scheduled from when audio really starts (retries restamp).
                self._audio_t0 = time.monotonic() + pre_roll + self.sync_offset
                proc = self._proc
            if wait:
                proc.wait()
                if proc.returncode == 0:
                    cleanup()
                    return True
            else:
                time.sleep(probe)                    # let aplay fail fast if it's going to
                if proc.poll() is None or proc.returncode == 0:
                    if owned:                        # delete the padded temp once aplay ends
                        threading.Thread(
                            target=lambda p=proc: (p.wait(), cleanup()),
                            daemon=True).start()
                    return True                      # still playing, or a very short clip finished cleanly
            if attempt == 2:
                print(f"[Sound] aplay failed (rc={proc.returncode}) on {self.device}")
                self._audio_t0 = None                 # nothing is audible; don't flap the jaw
                cleanup()
                return False
            time.sleep(0.15)                          # brief settle before the single retry
        self._audio_t0 = None
        cleanup()
        return False

    @staticmethod
    def _wav_duration(path: Path) -> float:
        """Length of a WAV in seconds, or 0.0 if it can't be read."""
        try:
            with wave.open(str(path), "rb") as w:
                rate = w.getframerate()
                return w.getnframes() / float(rate) if rate else 0.0
        except (wave.Error, OSError, EOFError):
            return 0.0

    def _play_virtual(self, path: Path, pad: bool, wait: bool) -> bool:
        """Audit mode's stand-in for ``aplay``: report a successful playback of
        the right length without opening the audio device.

        The epoch is stamped exactly as the real path stamps it — including the
        ``lead_in`` pre-roll and ``sync_offset`` — so ``audio_epoch()`` stays
        truthful and the jaw/mouth animation is timed identically to a real
        utterance. We don't bother writing the padded temp WAV: nothing reads it,
        and the only thing the padding contributed was that delay.
        """
        duration = self._wav_duration(path)
        pre_roll = self.lead_in if pad else 0.0
        with self._lock:
            self._proc = None
            self._audio_t0 = time.monotonic() + pre_roll + self.sync_offset
            self._virtual_end = self._audio_t0 + duration
            end = self._virtual_end
        if wait:
            time.sleep(max(0.0, end - time.monotonic()))
        return True

    def render_tts(self, text: str, speed: int = 150, voice: str = "en") -> str | None:
        """Render ``text`` to a WAV and return its path, or None if no TTS backend
        is present / it fails. Unlike speak(), this plays nothing — used by the
        lip-sync speaker, which needs the audio envelope before playback.

        The returned path is **owned by this Sound and must not be deleted by the
        caller**. Renders are cached (keyed on engine + voice + text) and reused,
        which makes FRED's stock lines — "Yes?" on every wake word — free the
        second time. Entries age out LRU; ``close()`` removes them all.
        """
        text = (text or "").strip()
        if not text or (self._piper is None and self._espeak is None):
            return None
        engine = self.tts_engine()
        key = hashlib.sha1(
            f"{engine}|{self._piper_voice or voice}|{speed}|{text}".encode()
        ).hexdigest()
        with self._tts_lock:
            hit = self._tts_cache.get(key)
            if hit is not None and os.path.isfile(hit):
                self._tts_cache.move_to_end(key)      # freshly used
                return hit
        # Render outside the lock: piper takes ~0.8 s and serialises internally,
        # so holding _tts_lock here would stall the render-ahead thread.
        out = str(self._cache_dir / f"{key}.wav")
        if not self._synth(text, out, speed, voice):
            _silent_unlink(out)
            return None
        with self._tts_lock:
            self._tts_cache[key] = out
            self._tts_cache.move_to_end(key)
            while len(self._tts_cache) > _TTS_CACHE_MAX:
                _, stale = self._tts_cache.popitem(last=False)
                _silent_unlink(stale)                 # safe: aplay holds an fd, not a name
        return out

    def speak(self, text: str, wait: bool = False, speed: int = 150,
              voice: str = "en") -> bool:
        """Speak ``text`` aloud via piper (or espeak). Returns True if speech started.

        Renders to a temp WAV (with any active voice effect) and plays it through
        play_file(), so it shares the same device, one-voice-at-a-time behaviour,
        and stop(). No-op (logs the text) when no TTS backend is present or audio
        is muted. ``speed`` is words/min for the espeak fallback.
        """
        text = (text or "").strip()
        if not text:
            return False
        if not self.can_speak() or not self.enabled:
            why = "no tts" if not self.can_speak() else "muted"
            print(f"[Sound] (silent) would speak: {text!r} [{why}]")
            return False
        path = self.render_tts(text, speed=speed, voice=voice)
        if path is None:
            return False
        return self.play_file(path, wait=wait)       # cache owns the file; don't delete

    def stop(self) -> None:
        """Stop the current sound if one is playing."""
        with self._lock:
            proc, self._proc = self._proc, None
            self._audio_t0 = None
            self._virtual_end = None       # audit: the clip stops being "playing"
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()

    def close(self) -> None:
        """Stop playback, shut the piper daemon down, and clear the TTS cache.

        Registered with atexit, and idempotent, so a short-lived script (e.g.
        deploy/announce_ip.py) doesn't leak the daemon or the cache directory.
        """
        self.stop()
        if self._daemon is not None:
            self._daemon.close()           # for good: an in-flight synth won't respawn it
        with self._tts_lock:
            self._tts_cache.clear()
        shutil.rmtree(self._cache_dir, ignore_errors=True)

    # context-manager sugar: stop playback on exit
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

"""A second opinion on what was said — Whisper, for the sentence after his name.

FRED hears with Vosk, and Vosk stays: its grammar API is what makes the wake
detector and barge-in possible, and Whisper has no such thing. But the
sentence *after* "Fred" is a free transcription, and that is where the heard
log shows Vosk losing: "terminator mode" as "terminate or mode", his name as
"read", a child's phrasing as a paragraph of the room. Whisper is markedly
better on noisy and unusual speech, at two costs this module is shaped
around:

**Latency.** Whisper works on a whole utterance, so it runs when Vosk's
endpointing says the sentence is over, and its time is added straight to what
the person waits before FRED starts thinking. faster-whisper on this CPU, int8,
a few threads: tools/bench_transcribers.py measures it. The listener runs it
on a worker thread so the microphone keeps being read meanwhile, and if the
worker is still busy when the next sentence ends, that sentence goes out on
Vosk's words at once rather than queueing — a late answer to the wrong
question is worse than Vosk.

**Hallucination on noise.** Whisper will confidently transcribe silence or
babble as "Thank you." The listener only asks for a second opinion when Vosk
already heard *words* in the utterance, so the endpointed audio is speech;
and an empty answer falls back to Vosk's. Its text is normalised to the shape
the matcher and the wake-word strip expect from Vosk — lower case, no
punctuation, apostrophes kept — so nothing downstream can tell which engine
spoke.

Off unless ``voice.transcriber`` is ``"whisper"``. Two ways to run it, chosen
by ``voice.whisper_device``:

``"cpu"`` (the default) is faster-whisper (``venv/bin/pip install
faster-whisper``); ``whisper_model`` names a size, downloaded to the Hugging
Face cache on first load, so the first boot after enabling it needs the
internet once. small.en costs 0.7 s a sentence on four threads.

``"npu"`` or ``"gpu"`` is the same model through OpenVINO GenAI
(``venv/bin/pip install openvino-genai``) on the NUC's Intel silicon:
``whisper_ov_model`` is a directory of a pre-converted model (one of the
OpenVINO/whisper-*-ov repos on Hugging Face; models/whisper-small.en-int8-ov
by default). The bench put small.en at 0.11 s a sentence on the NPU and
0.07 s on the Arc iGPU, the same words as the CPU, and the NPU is otherwise
idle where Ollama owns the GPU — so the NPU is the one to pick. Two things it
needs: the Intel user-space drivers (see TODO.md, "Speech recognition"), and
``/dev/accel/accel0`` (NPU) or ``/dev/dri/renderD128`` (GPU), which are group
``render`` — the service unit grants ``SupplementaryGroups=render``; without
it OpenVINO simply does not list the device, and this module says so and
leaves Vosk's words in use. The NPU compiles the model at load (~35 s cold,
seconds once ``models/.ov-cache`` holds the blobs) — on the background
thread, like everything else here.

One more thing the OpenVINO path needs, once per user: ``venv/bin/opt_in_out
--opt_out``. Importing openvino_genai otherwise fires Intel's usage telemetry,
which sends its ping from a ``multiprocessing.Process`` — and on Python 3.14
that child re-imports the main module, so the whole app (camera, servos, the
listener, this transcriber) came up three times over on 2026-09-23. The
opt-out is a consent file under the user's home, so a rebuilt machine needs
it again; the count of "[RemoteServo] online" lines at boot is the check.

Loading happens on a background thread at start, exactly like the local
brain's warm-up, and until it is ready Vosk's words are used.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import numpy as np

RATE = 16000
DEFAULT_MODEL = "base.en"
DEFAULT_THREADS = 4
DEFAULT_DEVICE = "cpu"
MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
DEFAULT_OV_MODEL = "whisper-small.en-int8-ov"      # under models/, or an absolute path
# The longest utterance worth a second opinion. Whisper's time grows with
# the audio; past this the listener has heard a speech, not a question.
MAX_SECONDS = 20.0

# What Vosk hands the rest of the robot: lower case words, apostrophes, no
# punctuation. Whisper writes prose; this makes it Vosk-shaped.
_KEEP = re.compile(r"[^a-z0-9' ]+")


def normalise(text: str) -> str:
    t = (text or "").lower().replace("’", "'")
    t = _KEEP.sub(" ", t)
    return " ".join(t.split())


class _Transcriber:
    """The lifecycle both backends share: load once in the background, count.

    A backend supplies ``_build()`` (the loaded, warmed model), ``_run(model,
    audio)`` (its prose for float32 16 kHz audio) and a ``describe()`` line
    for the log; everything about threads, errors, counting and Vosk-shaping
    lives here.
    """

    name = "whisper"

    def __init__(self, log=print):
        self._log = log
        self._model = None
        self._error = ""
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._count = 0            # transcriptions done
        self._secs = 0.0           # wall time spent in them
        self._thread = threading.Thread(target=self._load, name="whisper-load",
                                        daemon=True)

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if not self._thread.is_alive() and self._model is None and not self._error:
            self._thread.start()

    def _load(self) -> None:
        t0 = time.monotonic()
        try:
            model = self._build()
            # One short pass so the first real sentence does not pay the
            # graph's own warm-up — the same reason the local brain warms.
            self._run(model, np.zeros(RATE // 2, np.float32))
        except Exception as exc:  # noqa: BLE001 - no whisper just means Vosk's words
            self._error = f"{type(exc).__name__}: {exc}"
            self._log(f"[Transcriber] whisper unavailable: {self._error}")
            return
        with self._lock:
            self._model = model
            self._loaded_at = time.monotonic()
        self._log(f"[Transcriber] whisper {self.describe()} ready in "
                  f"{time.monotonic() - t0:.1f}s")

    def ready(self) -> bool:
        return self._model is not None

    def status(self) -> dict:
        with self._lock:
            n, secs = self._count, self._secs
        return {"engine": self.name, "model": self.model_name, "device": self.device,
                "ready": self.ready(), "error": self._error, "transcribed": n,
                "avg_seconds": round(secs / n, 2) if n else None}

    # ---- the work --------------------------------------------------------
    def transcribe(self, pcm16: bytes) -> str:
        """Vosk-shaped text for the utterance, or "" (use Vosk's words)."""
        model = self._model
        if model is None or not pcm16:
            return ""
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        limit = int(MAX_SECONDS * RATE)
        if len(audio) > limit:
            audio = audio[-limit:]
        t0 = time.monotonic()
        try:
            text = self._run(model, audio)
        except Exception as exc:  # noqa: BLE001 - a bad frame must not lose the sentence
            self._log(f"[Transcriber] whisper failed: {exc}")
            return ""
        with self._lock:
            self._count += 1
            self._secs += time.monotonic() - t0
        return normalise(text)

    # ---- what a backend supplies -----------------------------------------
    model_name = ""
    device = ""

    def describe(self) -> str:
        return f"{self.model_name} on {self.device}"

    def _build(self):
        raise NotImplementedError

    def _run(self, model, audio: np.ndarray) -> str:
        raise NotImplementedError


class WhisperTranscriber(_Transcriber):
    """faster-whisper (CTranslate2, int8) on CPU threads."""

    def __init__(self, model: str = DEFAULT_MODEL, threads: int = DEFAULT_THREADS,
                 log=print):
        super().__init__(log=log)
        self.model_name = model
        self.threads = max(1, int(threads))
        self.device = f"cpu x{self.threads}"

    def describe(self) -> str:
        return f"{self.model_name} on {self.threads} threads"

    def _build(self):
        from faster_whisper import WhisperModel
        return WhisperModel(self.model_name, device="cpu", compute_type="int8",
                            cpu_threads=self.threads, num_workers=1)

    def _run(self, model, audio: np.ndarray) -> str:
        segments, _ = model.transcribe(
            audio, language="en", beam_size=1, best_of=1, temperature=0.0,
            condition_on_previous_text=False, vad_filter=False,
            without_timestamps=True)
        return " ".join(s.text.strip() for s in segments)


class OpenVINOTranscriber(_Transcriber):
    """The same Whisper through OpenVINO GenAI, on the NPU or the Arc iGPU."""

    def __init__(self, model_dir: str | Path = DEFAULT_OV_MODEL, device: str = "npu",
                 log=print):
        super().__init__(log=log)
        path = Path(model_dir).expanduser()
        self.model_dir = path if path.is_absolute() else MODELS_DIR / path
        self.model_name = self.model_dir.name
        self.device = device.upper()

    def _build(self):
        import openvino as ov
        import openvino_genai as ov_genai
        if not (self.model_dir / "openvino_encoder_model.xml").exists():
            raise FileNotFoundError(f"no OpenVINO whisper model at {self.model_dir}")
        seen = ov.Core().available_devices
        if self.device not in seen:
            raise RuntimeError(
                f"OpenVINO sees {seen}, not {self.device} — the driver is missing, "
                f"or this process is not in group render (/dev/accel, /dev/dri)")
        cache = MODELS_DIR / ".ov-cache"
        cache.mkdir(parents=True, exist_ok=True)
        pipe = ov_genai.WhisperPipeline(str(self.model_dir), self.device,
                                        CACHE_DIR=str(cache))
        # The pipeline's own config knows the model is English-only; a fresh
        # WhisperGenerationConfig() assumes multilingual and demands lang_to_id.
        gen = pipe.get_generation_config()
        gen.max_new_tokens = 120
        gen.return_timestamps = False
        return (pipe, gen)

    def _run(self, model, audio: np.ndarray) -> str:
        pipe, gen = model
        out = pipe.generate(audio.astype(np.float32).tolist(), gen)
        return " ".join(t.strip() for t in out.texts)


def make(cfg: dict, log=print):
    """The transcriber ``voice`` settings ask for, or None for Vosk alone."""
    kind = str((cfg or {}).get("transcriber") or "vosk").lower()
    if kind != "whisper":
        return None
    device = str(cfg.get("whisper_device") or DEFAULT_DEVICE).lower()
    if device in ("npu", "gpu"):
        return OpenVINOTranscriber(model_dir=str(cfg.get("whisper_ov_model") or DEFAULT_OV_MODEL),
                                   device=device, log=log)
    return WhisperTranscriber(model=str(cfg.get("whisper_model") or DEFAULT_MODEL),
                              threads=int(cfg.get("whisper_threads") or DEFAULT_THREADS),
                              log=log)


def save_wav(path: Path, pcm16: bytes) -> None:
    """A debugging convenience: the bytes the transcriber was given, as a file."""
    import wave
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(pcm16)

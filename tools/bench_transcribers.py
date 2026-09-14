#!/usr/bin/env python3
"""Vosk against Whisper, on the audio that fails, with the cost that matters.

FRED hears with Vosk: a grammar-driven detector for his name (which Whisper
cannot do — no grammar API) and, once woken, a free transcription of the
sentence. The heard log says the second half is what fails: "terminator mode"
arrives as "terminate or mode", his name as "read" or "fed", and a sentence
from across the room as a paragraph of noise. The review's verdict was to
keep Vosk for the wake and *trial* Whisper for the command pass — benched on
real recordings first. There are none yet (voice.capture.enabled is off), so
this benches on the two nearest things there are, and says which is which:

  hall     LibriSpeech test-other — read speech from speakers the models were
           not tuned on, with ground truth — clean, and under *babble* made
           by mixing other speakers over it at 10, 5 and 0 dB. Babble is the
           right noise for a fair: a hall of four hundred is other voices.
  fred     the sentences people actually say to him (from heard.jsonl and the
           matcher's tests), spoken by the four piper voices on this machine,
           clean and under 5 dB babble. Synthetic, so easier than a child —
           but it is his vocabulary, and "terminator mode" is in it.

Per engine: word error rate per condition, and the cost on this CPU with the
threads the listener could give it — median seconds per utterance (the
latency a person waits after they stop talking) and the real-time factor.
Whisper runs through faster-whisper (CTranslate2, int8, CPU); the GPU is
Ollama's and the brain's.

The earcon plays at 1.5 s after the turn is handed to a model, and the
transcription happens *before* that hand-off, so a candidate's per-utterance
time is added straight to what a child waits. Under a second is the bar.

One honesty note on the Vosk rows: the listener streams audio into Vosk as
it arrives, so live it answers almost the moment the sentence ends — its
per-utterance time here is the *CPU* it spends on the sentence, not what a
person waits. Whisper's number is both, because it runs after the sentence.

    venv/bin/python tools/bench_transcribers.py --libri DIR   # DIR holds LibriSpeech/test-other
    venv/bin/python tools/bench_transcribers.py --libri DIR --engines vosk:lgraph,whisper:base.en
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import random
import re
import resource
import statistics
import subprocess
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
RATE = 16000
THREADS = 4          # what the listener could spare next to the spotter and the brain

FRED_PHRASES = [
    "fred what time is it", "fred tell me a joke", "fred tell me another joke",
    "fred turn on terminator mode", "fred terminator mode off", "fred can you dance",
    "fred look around", "fred turn your head to the left", "fred what's your name",
    "fred who built you", "fred how many servos do you have", "fred what do you see",
    "fred is anyone there", "fred how far away am i", "fred nod your head",
    "fred shake your head", "fred what is the weather like", "fred stop",
    "fred play your startup sound", "fred louder", "fred what's the date today",
    "fred what can you do", "fred why is the sky blue", "fred what's your favourite colour",
    "fred do you like robots", "fred look at me", "fred what am i wearing", "fred relax",
    "fred hello", "fred goodbye", "fred open your mouth", "fred close your mouth",
    "fred how old are you", "fred can you see me", "fred who's in front of you",
    "fred what's my name", "fred tell me about yourself", "fred how many computers run you",
    "fred is it light outside", "fred what's the biggest number you know",
]

# ---------------------------------------------------------------- text/WER --
_NUM = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
        "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}


def norm(text: str) -> list[str]:
    """Lower-case words, no punctuation, contractions apart, small numbers as digits."""
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    t = re.sub(r"\b(\w+)'s\b", r"\1 's", t)   # both sides do this differently
    words = [_NUM.get(w, w) for w in t.split()]
    return [w for w in words if w]


def edits(ref: list[str], hyp: list[str]) -> int:
    """Levenshtein distance on words."""
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
            prev, d[j] = d[j], cur
    return d[len(hyp)]


# ------------------------------------------------------------------ audio --
def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        rate, n = wf.getframerate(), wf.getnframes()
        pcm = np.frombuffer(wf.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
        if wf.getnchannels() > 1:
            pcm = pcm.reshape(-1, wf.getnchannels()).mean(axis=1)
    if rate != RATE:
        x = np.arange(len(pcm)) / rate
        xi = np.arange(int(len(pcm) * RATE / rate)) / RATE
        pcm = np.interp(xi, x, pcm).astype(np.float32)
    return pcm


def read_flac(path: Path) -> np.ndarray:
    """LibriSpeech is FLAC, 16 kHz mono already; soundfile reads it directly."""
    import soundfile
    pcm, rate = soundfile.read(str(path), dtype="float32")
    if pcm.ndim > 1:
        pcm = pcm.mean(axis=1)
    if rate != RATE:
        x = np.arange(len(pcm)) / rate
        xi = np.arange(int(len(pcm) * RATE / rate)) / RATE
        pcm = np.interp(xi, x, pcm).astype(np.float32)
    return pcm


def with_babble(clean: np.ndarray, others: list[np.ndarray], snr_db: float,
                rng: random.Random) -> np.ndarray:
    """``clean`` under a crowd: four other speakers mixed to the given SNR."""
    n = len(clean)
    babble = np.zeros(n, np.float32)
    for _ in range(4):
        o = rng.choice(others)
        if len(o) < n:
            o = np.tile(o, n // len(o) + 1)
        start = rng.randrange(0, len(o) - n + 1)
        babble += o[start:start + n]
    p_clean = float(np.mean(clean ** 2)) + 1e-9
    p_bab = float(np.mean(babble ** 2)) + 1e-9
    gain = np.sqrt(p_clean / (p_bab * 10 ** (snr_db / 10)))
    return np.clip(clean + babble * gain, -1.0, 1.0).astype(np.float32)


def to_bytes(pcm: np.ndarray) -> bytes:
    return (np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes()


# ---------------------------------------------------------------- engines --
class VoskEngine:
    def __init__(self, model_dir: Path):
        from vosk import KaldiRecognizer, Model, SetLogLevel
        SetLogLevel(-1)
        self._Rec = KaldiRecognizer
        self._model = Model(str(model_dir))

    def transcribe(self, pcm: np.ndarray) -> str:
        import json
        rec = self._Rec(self._model, RATE)
        data = to_bytes(pcm)
        step = RATE * 2 // 8                       # the listener's 125 ms chunks
        for i in range(0, len(data), step):
            rec.AcceptWaveform(data[i:i + step])
        return json.loads(rec.FinalResult()).get("text", "")


class WhisperEngine:
    def __init__(self, size: str):
        from faster_whisper import WhisperModel
        self._model = WhisperModel(size, device="cpu", compute_type="int8",
                                   cpu_threads=THREADS, num_workers=1)

    def transcribe(self, pcm: np.ndarray) -> str:
        segments, _ = self._model.transcribe(
            pcm, language="en", beam_size=1, best_of=1, temperature=0.0,
            condition_on_previous_text=False, vad_filter=False,
            without_timestamps=True)
        return " ".join(s.text.strip() for s in segments)


def make_engine(spec: str):
    kind, _, name = spec.partition(":")
    if kind == "vosk":
        dirs = {"small": "vosk-model-small-en-us-0.15", "lgraph": "vosk-model-en-us-0.22-lgraph"}
        return VoskEngine(MODELS / dirs.get(name, name))
    if kind == "whisper":
        return WhisperEngine(name or "base.en")
    raise SystemExit(f"unknown engine {spec!r}")


# ------------------------------------------------------------------- sets --
def libri_set(root: Path, n: int, rng: random.Random) -> list[tuple[str, np.ndarray]]:
    """n utterances of 2.5-8 s from test-other, with their transcripts."""
    base = root / "LibriSpeech" / "test-other" if (root / "LibriSpeech").exists() else root
    items = []
    for trans in sorted(base.rglob("*.trans.txt")):
        for line in trans.read_text().splitlines():
            uid, _, text = line.partition(" ")
            items.append((trans.parent / f"{uid}.flac", text))
    rng.shuffle(items)
    out = []
    for path, text in items:
        pcm = read_flac(path)
        if 2.5 * RATE <= len(pcm) <= 8 * RATE:
            out.append((text, pcm))
        if len(out) >= n:
            break
    return out


def fred_set(tmp: Path) -> list[tuple[str, np.ndarray]]:
    """The phrases, in every piper voice on this machine."""
    binary = MODELS / "piper" / "bin" / "piper"
    env = {**os.environ, "LD_LIBRARY_PATH": str(binary.parent)}
    out = []
    for voice in sorted((MODELS / "piper" / "voices").glob("*.onnx")):
        for i, text in enumerate(FRED_PHRASES):
            wav = tmp / f"{voice.stem}_{i:02d}.wav"
            if not wav.exists():
                subprocess.run([str(binary), "--model", str(voice), "--output_file", str(wav), "-q"],
                               input=(text + "\n").encode(), env=env, check=True,
                               capture_output=True)
            pcm = read_wav(wav)
            # Trim piper's silence so the clip is the sentence, as the
            # listener's endpointing would hand it over.
            loud = np.nonzero(np.abs(pcm) > 0.01)[0]
            if loud.size:
                pcm = pcm[max(0, loud[0] - RATE // 10): loud[-1] + RATE // 5]
            out.append((text, pcm))
    return out


# ------------------------------------------------------------------- main --
def run(engine, clips: list[tuple[str, np.ndarray]]) -> dict:
    err = words = 0
    secs, audio = [], 0.0
    worst = []
    for ref_text, pcm in clips:
        t0 = time.perf_counter()
        hyp_text = engine.transcribe(pcm)
        dt = time.perf_counter() - t0
        ref, hyp = norm(ref_text), norm(hyp_text)
        e = edits(ref, hyp)
        err += e
        words += len(ref)
        secs.append(dt)
        audio += len(pcm) / RATE
        worst.append((e / max(1, len(ref)), ref_text, hyp_text))
    worst.sort(reverse=True)
    return {"wer": err / max(1, words), "p50": statistics.median(secs), "max": max(secs),
            "rtf": sum(secs) / audio, "worst": worst[:3]}


def main() -> int:
    global THREADS
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--libri", default="", help="directory holding LibriSpeech/test-other")
    ap.add_argument("--n", type=int, default=60, help="LibriSpeech utterances per condition")
    ap.add_argument("--engines", default="vosk:small,vosk:lgraph,whisper:tiny.en,"
                                         "whisper:base.en,whisper:small.en")
    ap.add_argument("--tmp", default=str(Path(os.environ.get("TMPDIR", "/tmp")) / "bench-asr"))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--threads", type=int, default=THREADS,
                    help="CPU threads for whisper (the listener's whisper_threads)")
    args = ap.parse_args()
    THREADS = max(1, args.threads)
    tmp = Path(args.tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    conditions: dict[str, list] = {}
    if args.libri:
        clips = libri_set(Path(args.libri), args.n, rng)
        others = [p for _, p in libri_set(Path(args.libri), 40, random.Random(args.seed + 1))]
        conditions["hall clean"] = clips
        for snr in (10, 5, 0):
            conditions[f"hall babble {snr:>2} dB"] = [
                (t, with_babble(p, others, snr, rng)) for t, p in clips]
        print(f"hall: {len(clips)} utterances of test-other, {sum(len(p) for _, p in clips) / RATE:.0f} s")
    fred = fred_set(tmp)
    conditions["fred clean"] = fred
    if args.libri:
        conditions["fred babble  5 dB"] = [(t, with_babble(p, others, 5, rng)) for t, p in fred]
    print(f"fred: {len(fred)} clips, {len(FRED_PHRASES)} phrases x "
          f"{len(fred) // len(FRED_PHRASES)} voices; threads={THREADS}\n")

    table = []
    for spec in [s.strip() for s in args.engines.split(",") if s.strip()]:
        rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        t0 = time.perf_counter()
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                engine = make_engine(spec)
        except Exception as exc:  # noqa: BLE001
            print(f"== {spec}: could not load ({exc})\n")
            continue
        load = time.perf_counter() - t0
        rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(f"== {spec}  (load {load:.1f} s, +{rss1 - rss0:.0f} MB)")
        row = {"engine": spec, "load": load}
        for cond, clips in conditions.items():
            r = run(engine, clips)
            row[cond] = r
            print(f"   {cond:18} WER {r['wer']:6.1%}   per utt p50 {r['p50']:5.2f} s  "
                  f"max {r['max']:5.2f} s   rtf {r['rtf']:.2f}")
        for cond in ("fred clean", "hall babble  5 dB"):
            if cond in row:
                for frac, ref, hyp in row[cond]["worst"][:2]:
                    print(f"      {cond} worst: {ref!r} -> {hyp!r}")
        print()
        table.append(row)
        del engine

    conds = list(conditions)
    print(f"{'engine':18} " + " ".join(f"{c[:16]:>16}" for c in conds) + f" {'p50 s':>6} {'rtf':>5}")
    for row in table:
        cells = " ".join(f"{row[c]['wer']:15.1%} " for c in conds)
        p50 = statistics.median(row[c]["p50"] for c in conds)
        rtf = statistics.median(row[c]["rtf"] for c in conds)
        print(f"{row['engine']:18} {cells} {p50:6.2f} {rtf:5.2f}")
    print("\nWER: lower is better. p50: seconds a person waits per sentence, on "
          f"{THREADS} threads. The bar is under 1 s and clearly fewer errors than vosk:lgraph.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

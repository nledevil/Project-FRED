"""Always-on wake-word listener for FRED.

Runs ``arecord`` on the USB mic and feeds the raw 16 kHz PCM to a Vosk
recogniser (offline, on-device). When a final transcript contains the wake word
— his name, "Fred", anywhere in the utterance — the rest of that utterance is
treated as a command; if nothing followed it, FRED says "Yes?" and the *next*
utterance is the command. A leading "hey" or "ok" is not required and never was:
_strip_wake scans for the name and keeps whatever comes after it, so "Fred, turn
your head" and "hey Fred, turn your head" are the same sentence to him.

The wake word is how a conversation *starts*, not a toll on every sentence: when
FRED himself ends a turn on a question, ``arm()`` holds the mic open so the
answer to "which one did you mean?" is just the answer. See Assistant.converse.

Design mirrors the other hardware wrappers: ``available()`` is False (and the
thread never starts) when Vosk or the model or the mic are missing, so the app
still runs without voice.

The recogniser is muted while FRED is speaking (``pause``/``resume``) so a reply
cannot answer itself. The microphone itself keeps running throughout — the
PowerConf is full duplex and cancels its own output from its capture; see
``Listener.pause`` for what was measured.
"""
from __future__ import annotations

import collections
import json
import select
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from inmoov import wakestats
from inmoov import uttercap

try:
    from vosk import Model, KaldiRecognizer, SetLogLevel
    SetLogLevel(-1)
    _VOSK_ERR = None
except Exception as exc:  # noqa: BLE001 - no vosk just means "no voice input"
    Model = KaldiRecognizer = None
    _VOSK_ERR = exc

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "vosk-model-small-en-us-0.15"

# Vosk's small model hears the name a few different ways — accept the near ones,
# but keep the set tight (no "red"/"ed") so ordinary speech doesn't false-trigger.
FULL_SCALE = 32767              # int16, so a peak is a fraction of this

# How much level history to keep for the panel's meter. Capture is 4000-byte
# chunks of 16 kHz mono int16, i.e. 125 ms each, so 48 of them is six seconds.
#
# It has to be a history rather than a single number because of the rates
# involved: the panel polls voice status every 1.2 s and would otherwise be
# shown one 125 ms chunk out of every ten, sampling a syllable at random. You
# would talk and watch a meter twitch. Six seconds is enough to say a sentence
# and see the whole of it.
LEVEL_HISTORY = 48

# His name, plus what the recogniser actually produces when someone says it.
# Observed in logs/heard.jsonl rather than guessed — "alfred" is "hey Fred" run
# together into one token, "fraud" is what a general lexicon reaches for when a
# short name has to compete with the whole of English.
#
# The bar for being here is both halves: it has to sound like the name, *and* a
# room must not say it by accident. tools/wake_audit.py measures the second half
# against the model's own lexicon; anything it calls ORDINARY is out however
# well it sounds.
#
# Dropped on that test (2026-08-19):
#   "friend" — 4 derived forms. Here from the beginning, and the TODO had
#     already flagged it: "my friend told me" wakes him. Its cost is worse than
#     the logs suggest, because a false wake is recorded with the word that
#     caused it stripped off — invisible in the very file you would check. Never
#     once observed standing in for his name.
#   "bread" — 4 derived forms, and observed standing in for his name exactly
#     once. A real but rare gain against a word a room says freely; the room wins.
#
# Considered and rejected: "right" tops the audit's evidence table for standing
# where his name should be ("right stop", "what's right stop listening") and is
# also the most ordinary word on it. Being a genuine mishearing is not enough.
# "fread" is absent for a different reason — not in the model's vocabulary, so
# nothing can ever emit it. Checked, not assumed.
WAKE_WORDS = ("fred", "alfred", "frayed", "fraud")
ARM_WINDOW = 6.0     # seconds to wait for the command after a bare "Fred"
# Seconds the mic stays open after FRED has asked a question. Longer than the
# window above because answering a question takes a beat more thought than
# issuing a command after "Yes?" — and because it is measured from the moment he
# stops talking, not from the moment he decided what to say.
FOLLOWUP_WINDOW = 9.0
# Event-mode strictness (V4). In a hall the follow-up window is open on a crowd
# and near-homophones get said; these tighten the wake gate while event mode is
# on and are undone the moment it is switched off — a little responsiveness for
# far fewer answers to nobody, scoped to the event and reversible.
EVENT_NAME_PARTIALS_BONUS = 2       # NAME_MIN_PARTIALS 4 -> 6: more proof before a barge
EVENT_WAKE_MAX_POS = 2             # his name must land in the first two tokens
EVENT_FOLLOWUP_WINDOW = 5.0        # shorter than the 9.0 above
# Interrupting him takes his name. Any-speech-interrupts was tried first and is
# unusable in a real room: with a television on, four lines of dialogue in seven
# seconds each cut him off *and* were taken as commands, so he started an answer,
# lost it, started another, and the panel filled with replies nobody heard. The
# room is full of speech that isn't for him; his name is the one signal that is.
#
# The cost is that a bare "stop" won't do it — "Fred, stop" will. That is the
# same bargain the wake word already makes everywhere else.
BARGE_NEEDS_WAKE_WORD = True
# Barge-in listens with a *restricted* grammar: the only things it can output
# are his name and "[unk]". A short name competing against the full lexicon is
# the whole problem — measured on this model, "Fred, stop talking" came back as
# "fresh start talking" and "hey Fred" as "alfred", so nothing matched and he
# talked straight over the person. Given only the name to find, it finds it.
#
# Needs a model built with a dynamic graph (the "-lgraph" suffix). If the model
# can't take a grammar, _run falls back to watching the ordinary recogniser and
# says so once.
BARGE_GRAMMAR = json.dumps(sorted(WAKE_WORDS) + ["[unk]"])
# Consecutive partial results that must carry his name before it counts.
#
# A grammar recogniser can only ever output the words it was given, so while it
# is deciding, ordinary speech briefly gets mapped onto one of them before the
# decoder settles on "[unk]". Acting on a single partial made him answer a room
# nobody had addressed. Its *finals* are reliable — nine seconds of television
# produced "[unk]" every time — but waiting for a final means waiting for the
# speaker to stop, which is the whole thing barge-in exists to avoid.
#
# Measured over that same television: a stray hypothesis survives at most two
# consecutive chunks, while someone really saying his name holds for sixteen.
# Four is comfortably clear of the noise and still reacts within half a second
# (a chunk is 4000 bytes = 125 ms).
NAME_MIN_PARTIALS = 4
# How long the full recogniser keeps running after it last heard sound. It has to
# outlast a pause mid-sentence, plus the gap between "Fred" and what follows;
# is_armed() covers the follow-up window itself.
HOT_LINGER = 6.0
# Chunk peak (0..32767) above which there is something worth transcribing.
#
# This gates the expensive recogniser on *sound*, not on his name — a deliberate
# retreat from doing it the other way. Measured on this model, the persistence of
# a name in the detector's partials does not separate cleanly from a room: real
# phrases ran 0-3 consecutive chunks while "my friend told me about it" ran 7. A
# detector good enough to decide whether to *interrupt* him is not good enough to
# decide whether he gets to hear you at all, and the failure is silent — he would
# simply not answer.
#
# Sound is a safe gate because missing nothing is the requirement: anything the
# room says still reaches the full recogniser, and _strip_wake still decides
# whether it was for him. The saving is real when he is sitting in a quiet
# workshop, which is most of his life; in a loud hall he pays what he used to.
SPEECH_FLOOR = 250
# Seconds of audio kept so the *command* isn't lost while the full recogniser is
# still cold. The detector fires part-way through his name, so the replay has to
# reach back far enough to include the whole of it — 2 s is comfortably more than
# the 0.5 s the persistence rule costs, plus whatever ran before it.
REPLAY_SECONDS = 2.0
# The most utterance audio kept for the transcriber's second opinion: the
# tail of a long hot spell, at 16 kHz mono 16-bit. Whisper's own cap is the
# same number of seconds (transcriber.MAX_SECONDS).
UTT_MAX_BYTES = 20 * 16000 * 2
# 4000 bytes = 2000 samples at 16 kHz = 125 ms, which is the loop's read size.
CHUNK_SECONDS = 0.125


class Listener:
    """Background wake-word + speech-to-text loop.

    Parameters
    ----------
    on_command : callable(str)   -- called with the recognised command text.
    on_wake : callable()         -- called on a bare "Fred" (say "Yes?").
    device : str                 -- ALSA capture device for arecord.
    channels : int               -- how many channels that device presents. 1 is
                                    an ordinary microphone. A mic array offers
                                    several at once and must be asked for all of
                                    them, because ALSA's plughw reduces a
                                    multi-channel capture to mono by averaging.
    channel : int                -- which of them to transcribe. On the reSpeaker
                                    Flex that is the processed output, not one of
                                    the raw capsules. See _take_channel.
    gain : float                 -- software mic boost applied to the raw PCM
                                    before Vosk sees it. The USB mic's analog
                                    capture is already maxed (+16 dB), so this is
                                    the only remaining sensitivity knob. 1.0 = off;
                                    ~2-3 helps quiet/distant speech. Too high just
                                    amplifies room noise and clips loud words,
                                    which hurts recognition, so keep it modest.

    Call ``pause()``/``resume()`` around playback: this *stops* the arecord
    capture (not just ignores it) so FRED never captures and plays at the same
    time. The USB codec on this rig wedges under simultaneous capture+playback,
    so the two must never overlap.
    """

    def __init__(self, on_command, on_wake=None, on_barge=None,
                 device: str = "plughw:0,0", model_path: str | Path = MODEL_PATH,
                 gain: float = 1.0, barge_in: bool = True,
                 channels: int = 1, channel: int = 0, transcriber=None):
        self._on_command = on_command
        # The optional second opinion on a finished sentence — see
        # inmoov/transcriber.py. ``_utt`` is the audio the full recogniser has
        # been fed for the current utterance, so Whisper hears exactly what
        # Vosk endpointed; one worker so the microphone loop never waits.
        self._transcriber = transcriber
        self._utt = bytearray()
        self._refine_busy = threading.Lock()
        self._refined = 0                      # sentences Whisper answered
        self._refine_fallbacks = 0             # ...and the ones it didn't in time
        self._on_wake = on_wake or (lambda: None)
        # Called the moment somebody starts talking over him, so the reply can be
        # cut short. Separate from on_command because it fires on a *partial* —
        # the point is to stop within a word, not to wait out their sentence.
        self._on_barge = on_barge or (lambda: None)
        # Text-free wake tally: the denominator heard.jsonl can't keep. See wakestats.py.
        self._wakestats = wakestats.stats()
        # Opt-in capture of wake-gated utterances (off by default). See uttercap.py.
        self._cap = uttercap.cap()
        # Whether to listen at all while he speaks. Off = the old behaviour, where
        # audio during a reply is read and dropped.
        self.barge_in = bool(barge_in)
        self.device = device
        # How many channels to ask arecord for, and which one Vosk actually gets.
        # 1/0 is an ordinary mono microphone and costs nothing: the extraction is
        # skipped outright. See _take_channel for why a multi-channel array must
        # not be left to plughw to reduce.
        self.channels = max(1, int(channels))
        self.channel = min(max(0, int(channel)), self.channels - 1)
        self.gain = max(1.0, float(gain))       # never attenuate below the captured level
        self.model_path = Path(model_path)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()      # set = capture suspended (during playback)
        self._proc = None                     # the live arecord subprocess (or None)
        self._lock = threading.Lock()
        self._proc_lock = threading.Lock()    # guards the _proc claim/release swap
        self._model = None                    # loaded lazily on first start()
        # Signal level, so "FRED can't hear me" is answerable without guessing.
        # See _note_level() for why digital silence is worth its own flag.
        self._peak = 0                        # loudest sample in the last chunk
        self._levels = collections.deque(maxlen=LEVEL_HISTORY)   # recent chunk peaks
        # Monotonic deadline: while now < this, an utterance is taken as speech
        # meant for FRED with no wake word in front of it. Set by the bare-wake
        # branch in _run and by arm() from the Assistant, hence an attribute
        # rather than a local — a plain float, written and read as one word.
        self._armed_until = 0.0
        self._warned_grammar = False           # only say "no grammar support" once
        self._name_streak = 0                  # consecutive partials naming him
        # Wake-gate strictness — relaxed by default, tightened by event mode
        # (set_event_strict). See the EVENT_* constants and V4.
        self._name_min_partials = NAME_MIN_PARTIALS
        self._wake_max_pos = None              # None = his name may appear anywhere
        self._followup_window = FOLLOWUP_WINDOW
        # The last couple of seconds of audio, so the full recogniser can be
        # started only once his name has been heard and still be handed the
        # sentence that carried it. See _run: transcribing a room nobody is
        # talking to cost ~88% of a core, against ~9% for the name detector.
        self._replay = collections.deque(maxlen=int(REPLAY_SECONDS / CHUNK_SECONDS))
        self._heard_at = 0.0                  # monotonic, last chunk that wasn't silence
        self._captured_at = 0.0               # monotonic, last chunk of any kind
        # When the current unbroken run of digital silence began. Tracked as its
        # own clock rather than derived from _heard_at, because the case that
        # matters most is a mic that has been muted since before we started and
        # so has *never* set _heard_at at all.
        self._silent_since = 0.0

    def pause(self) -> None:
        """Stop feeding the recogniser while FRED speaks. The mic stays open.

        Still true on the current hardware: audio moved to the reSpeaker Flex
        (XVF3800) and the same conclusion was re-verified for it in commit
        4304641 — its on-device echo canceller removes FRED's own voice from the
        capture (the nine-nonsense-words test, three runs, none returned), so the
        mic can stay open through a reply and barge-in keeps working. The
        measurement below is the original one on the Anker PowerConf; the property
        holds because both devices are sealed speaker+array units, not because of
        the specific board.

        This used to reap arecord and hand the card back, on the grounds that the
        USB codec wedged if capture and playback overlapped even briefly. Measured
        on the Anker PowerConf (2026-08-19) that is not true of this device:

        * Capture and playback are separate USB interfaces with separate
          endpoints (2 OUT / 3 IN, ``/proc/asound/card0/stream0``). Both report
          ``Running`` together indefinitely, a capture taken straight through six
          seconds of continuous playback came back full-length and gap-free, and
          the two streams together use about a fifth of a full-speed bus.
        * The device cancels its own output out of the mic. Playing a phrase of
          nine words no room would produce ("banana helicopter Tuesday...") and
          transcribing the capture — with ``gain`` applied, as below — returned
          not one of them, three times over. Correlation puts the echo near
          -43 dB, roughly 33 dB under the noise floor of an ordinary room; the
          mic actually goes *quieter* while it plays, so it ducks rather than
          merely subtracting.

          It cancels that well because the speaker and the microphones are one
          sealed unit. The A3301 carries a six-mic array around a single driver,
          so the path from driver to capsules is fixed, known to the firmware and
          unchanging, and the array beamforms toward whoever is talking and away
          from its own speaker. (It is Zoom-certified, which is a duplex
          conformance bar, not just a logo.) That is what makes this a property of
          the device rather than of where it happened to be sitting on the day —
          so moving the robot cannot quietly undo it. A separate speaker and a
          single microphone would have neither guarantee, which is very likely
          the arrangement the original "the codec wedges" note was written for.

        So the stream stays up and this only stops the *recogniser* from being
        fed. That matters beyond tidiness: closing meant a device reopen and a
        fresh recogniser on every single reply, and it put arecord's restart in a
        race with aplay for the card — which is the failure ``play_file``'s retry
        and its longer first-clip probe exist to survive.

        Frames are still read while paused (see ``_run``) and simply dropped: stop
        reading and arecord's pipe fills, which really would wedge it.

        Safe to call from the listener thread itself (the usual path: a
        recognised command calls back into Assistant.speak).
        """
        self._paused.set()

    def resume(self) -> None:
        """Start feeding the recogniser again once FRED has stopped talking."""
        self._paused.clear()

    def arm(self, seconds: float | None = None) -> None:
        """Accept the next utterance without a wake word, for ``seconds``
        (default: the current follow-up window, which event mode shortens).

        Call this *after* FRED has finished speaking, not when the reply was
        decided: nothing said while he talks is listened to, so a window opened
        before he starts is mostly spent by the time anyone can answer.

        Cheap to call when already armed — it extends rather than stacks.
        """
        if seconds is None:
            seconds = self._followup_window
        self._armed_until = time.monotonic() + max(0.0, float(seconds))

    def set_event_strict(self, on: bool) -> None:
        """Tighten (or relax) the wake gate for event mode (V4). On: the name
        detector needs more consecutive partials, his name must land in the first
        two tokens, and the follow-up window is shorter — three ways a crowd
        stops him answering nobody, all undone when event mode goes off."""
        if on:
            self._name_min_partials = NAME_MIN_PARTIALS + EVENT_NAME_PARTIALS_BONUS
            self._wake_max_pos = EVENT_WAKE_MAX_POS
            self._followup_window = EVENT_FOLLOWUP_WINDOW
        else:
            self._name_min_partials = NAME_MIN_PARTIALS
            self._wake_max_pos = None
            self._followup_window = FOLLOWUP_WINDOW

    def _strip(self, text: str):
        """Wake-strip honouring event strictness: when ``_wake_max_pos`` is set,
        his name only counts inside the first that-many tokens, so a homophone
        buried mid-sentence in a hall ("...my friend fred said") doesn't wake
        him. Off (None), it is the plain module ``_strip_wake``."""
        if self._wake_max_pos is None:
            return _strip_wake(text)
        toks = text.lower().split()
        for i, tok in enumerate(toks):
            if i >= self._wake_max_pos:
                break
            if tok in WAKE_WORDS:
                return " ".join(toks[i + 1:]).strip()
        return None

    def disarm(self) -> None:
        """Close the window early. The wake word is required again."""
        self._armed_until = 0.0

    def is_armed(self) -> bool:
        return time.monotonic() < self._armed_until

    def available(self) -> bool:
        return (Model is not None and self.model_path.is_dir()
                and Path("/dev/snd").exists())

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _note_level(self, data: bytes) -> None:
        """Record how loud the last chunk was, and when we last heard anything.

        This exists because of a failure that is invisible from every other
        angle: the USB speakerphone has its own mute button, and when it is on
        the host sees nothing wrong. ALSA still reports the capture control at
        100% and unmuted, arecord still runs and still returns data — the data
        is just *exact zeros*. FRED sits there looking like he is listening and
        never hears a word, and nothing in any log says so.

        **A zero run is not proof of a mute, on this hardware.** The PowerConf
        does its own noise suppression and gates a quiet room to exact zeros
        too: measured here, 30 s of unbroken zeros with the mic live and
        unmuted, then a peak of 2 when someone moved. A cheap analogue capture
        would give a noise floor to read; this one does not, so silence is
        reported as the fact it is — no signal for N seconds — and the *reason*
        is left to whoever is standing there. Calling it "muted" would be a
        guess, and a guess in red trains people to ignore the panel.
        """
        try:
            samples = np.frombuffer(data, dtype=np.int16)
            peak = int(np.abs(samples).max()) if samples.size else 0
        except ValueError:                    # odd-length chunk; skip this one
            return
        now = time.monotonic()
        with self._lock:
            self._peak = peak
            self._levels.append(peak)
            self._captured_at = now
            if peak > 0:
                self._heard_at = now
                self._silent_since = 0.0      # the run of silence ends here
            elif not self._silent_since:
                self._silent_since = now      # ...and this is where it began

    def status(self) -> dict:
        """What the microphone is actually doing, for the panels.

        ``silent_for`` is seconds of unbroken digital silence while capturing —
        None when we aren't capturing, so a paused or stopped listener is never
        mistaken for a muted one.

        ``levels`` is the recent run of per-chunk peaks, oldest first, so the
        panel can draw what the microphone has actually been doing instead of
        being handed a threshold and a verdict. That is the whole point: this
        hardware gates a quiet room to exact zeros, so no number here can be
        turned into "muted" honestly — but a person watching a meter while they
        talk can settle it in one second.

        Raw sample values rather than anything pre-scaled: ``full_scale`` says
        what they are out of, and how to draw them is the panel's business.
        Levels keep flowing while paused, so the meter shows the last six
        seconds rather than blanking every time FRED speaks; ``capturing`` is
        there for the panel to dim it.
        """
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            paused = self._paused.is_set()
            peak, captured_at = self._peak, self._captured_at
            silent_since = self._silent_since
            levels = list(self._levels)
        now = time.monotonic()
        capturing = running and not paused
        silent_for = None
        if capturing and captured_at:
            silent_for = round(now - silent_since, 1) if silent_since else 0.0
        tr = self._transcriber
        return {"device": self.device, "running": running, "paused": paused,
                "capturing": capturing, "peak": peak, "silent_for": silent_for,
                "transcriber": {**(tr.status() if tr else {"engine": "vosk"}),
                                "refined": self._refined,
                                "fallbacks": self._refine_fallbacks},
                "levels": levels, "peak_recent": max(levels) if levels else 0,
                "full_scale": FULL_SCALE,
                "capture_age": round(now - captured_at, 1) if captured_at else None,
                # Why a sentence with no wake word in it was answered. Without
                # this the panel's transcript looks like FRED replied to nothing.
                "barge_in": self.barge_in,
                "armed": now < self._armed_until,
                "armed_for": (round(self._armed_until - now, 1)
                              if now < self._armed_until else None)}

    def start(self) -> bool:
        if not self.available():
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="listener", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            t = self._thread
        if t is not None:
            t.join(timeout=3.0)
        with self._lock:
            self._thread = None

    # ---- the loop ---------------------------------------------------------
    def _run(self) -> None:
        if self._model is None:                # ~2s load; do it off the boot path
            self._model = Model(str(self.model_path))
        rec = None                     # the full recogniser, built when he is named
        brec = self._new_barge_rec()
        try:
            dropped = False       # audio was discarded -> restart the recogniser
            barged = False        # already cut this reply short
            name_seen = False     # the detector heard his name in this utterance
            hot_until = 0.0       # monotonic; the full recogniser stays up until then
            while not self._stop.is_set():
                proc = self._proc
                if proc is None:               # first start, or reopen after an EOF
                    proc = subprocess.Popen(
                        ["arecord", "-q", "-D", self.device, "-f", "S16_LE",
                         "-r", "16000", "-c", str(self.channels), "-t", "raw"],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    # Publish only if nobody paused us while arecord was starting;
                    # otherwise this capture would run *during* playback, which is
                    # exactly what pause() exists to prevent.
                    with self._proc_lock:
                        claimed = not (self._paused.is_set() or self._stop.is_set())
                        if claimed:
                            self._proc = proc
                    if not claimed:
                        _reap(proc)
                        continue
                    rec = None                 # cold again after a capture gap
                    brec = self._new_barge_rec()
                    self._replay.clear()
                    with self._lock:
                        # Silence is measured per capture session, and a session
                        # now spans FRED's replies rather than restarting after
                        # each one — so this only fires on a real EOF (arecord
                        # died, or the card went away), where a silence run
                        # measured across the gap would be meaningless.
                        self._silent_since = 0.0
                # Scaled by the channel count so that what survives the
                # extraction below is still 4000 bytes — 125ms, which every
                # timing constant in this file is written in terms of.
                data = _read_chunk(proc.stdout, 4000 * self.channels,
                                   WEDGE_TIMEOUT)
                if data is None:
                    # The pipe is open, arecord is alive, and nothing arrived
                    # for WEDGE_TIMEOUT — on a card that emits 16 kHz PCM
                    # continuously, silence included, that is not quiet, it is
                    # wedged. Without this the blocking read() sat here forever
                    # and FRED was silently deaf until a restart; the recovery
                    # is the same close-and-reopen every other capture death
                    # already takes.
                    print(f"[Listener] capture wedged (no audio for "
                          f"{WEDGE_TIMEOUT:g}s) — reopening")
                    data = b""
                if not data:                   # arecord ended (killed by pause, or died)
                    self._close_proc()
                    continue
                if self.channels > 1:
                    data = _take_channel(data, self.channels, self.channel)
                    if not data:               # a read that ended mid-frame
                        continue
                # Measured *before* gain: this is a question about the hardware,
                # and scaling silence by 8 is still silence.
                self._note_level(data)
                if self.gain > 1.0:
                    data = _amplify(data, self.gain)
                if self._paused.is_set():
                    with self._lock:
                        # Don't let his turn count as the microphone going quiet:
                        # the canceller ducks capture while he speaks, and that
                        # is not the mic failing.
                        self._silent_since = 0.0
                    if not self.barge_in:
                        # Read and drop. Leaving frames in arecord's pipe is what
                        # would actually wedge it; the level meter still sees them,
                        # so the panel keeps moving while he talks.
                        dropped = True
                        continue
                    # Listening *through* his own reply. Safe because the device
                    # cancels its own output out of its capture — see pause() —
                    # so what arrives here is the room, not him.
                    #
                    # Two recognisers, deliberately. brec only knows his name and
                    # decides whether to stop him; rec knows English and produces
                    # the sentence they actually said. Asking one recogniser to do
                    # both is what failed: given the whole lexicon, "Fred, stop
                    # talking" decodes as "fresh start talking" and nothing fires.
                    self._replay.append(data)
                    self._cap.feed(data)   # opt-in; a cheap return when off
                    if not barged and self._detect_name(brec, data):
                        barged = True
                        self._barge()
                    if self._peak >= SPEECH_FLOOR:
                        hot_until = time.monotonic() + HOT_LINGER
                    if time.monotonic() >= hot_until:
                        continue               # nothing being said over him
                    if rec is None:
                        # Transcribe whoever is talking over him even when the
                        # detector didn't fire. Missing the interruption only
                        # costs him a late stop; losing the sentence costs them
                        # the whole request, and _strip_wake still decides
                        # whether it was for him.
                        rec, _ = self._wake_full()
                    self._utt += data
                    if rec.AcceptWaveform(data):
                        text = json.loads(rec.Result()).get("text", "").strip()
                        utt, self._utt = bytes(self._utt), bytearray()
                        if text:
                            if not barged and _wants_him(text):
                                # Nothing caught it earlier — a whole utterance
                                # landing in one chunk. Stop him now.
                                barged = True
                                self._barge()
                            was_barged = barged

                            def over_him(text, was_barged=was_barged):
                                # Not barged: the detector didn't fire, so
                                # route it the ordinary way — his name has to
                                # be in the sentence or it was not for him,
                                # which is what keeps a television talking
                                # through his reply from becoming a command.
                                # Barged: this was aimed at him. If his name
                                # survived into the transcript let the normal
                                # path strip it (and answer a bare "Fred" with
                                # "Yes?"); if it came out as "fresh" or
                                # "alfred", take the sentence whole rather than
                                # demand a name the recogniser just lost.
                                if not was_barged:
                                    self._dispatch(text, time.monotonic())
                                elif _strip_wake(text) is None:
                                    self._dispatch(text, time.monotonic(), armed=True)
                                else:
                                    self._dispatch(text, time.monotonic())
                            self._refine(text, utt, over_him)
                        barged = False
                        name_seen = False
                        self._reset_rec(brec)  # next utterance starts clean
                    continue
                if dropped:
                    # Audio was thrown away, so the recognisers are mid-utterance
                    # on a gap they never saw the far side of. Start clean: the
                    # barge grammar is reset in place, and the full recogniser is
                    # simply discarded — the hot path rebuilds it on the next
                    # loud chunk. (This used to construct a KaldiRecognizer here
                    # and immediately overwrite it with None — a wasted build per
                    # resume, doing nothing.)
                    self._reset_rec(brec)
                    rec = None
                    dropped = False
                barged = False
                # The same two-recogniser split as barge-in, for the same reason:
                # deciding whether he was addressed and transcribing what was said
                # are different jobs, and the first one loses badly when his name
                # has to out-compete the whole lexicon. "hey Fred" comes back from
                # the general model as the single token "alfred", which no amount
                # of splitting on whitespace will match.
                #
                # Latched rather than checked at the end: brec finishes utterances
                # on its own schedule, so the name can be found and gone again
                # before rec has finished the sentence it belongs to.
                self._replay.append(data)
                self._cap.feed(data)       # opt-in; a cheap return when off
                if not name_seen:
                    name_seen = self._detect_name(brec, data)
                # The full recogniser costs about ten times the detector (88% of a
                # core against 9%, measured over the same audio), and in a quiet
                # room every bit of that is spent on silence. It runs while there
                # is sound to transcribe and for a little after, and sleeps
                # otherwise — see SPEECH_FLOOR for why the gate is sound and not
                # his name.
                if self._peak >= SPEECH_FLOOR:
                    hot_until = time.monotonic() + HOT_LINGER
                if not (self.is_armed() or time.monotonic() < hot_until):
                    rec = None                 # let it go; _wake_full rebuilds it
                    self._utt = bytearray()
                    continue
                if rec is None:
                    rec, carried = self._wake_full()
                    if carried:
                        # A whole utterance finished before it was up. Answer it
                        # rather than making them repeat themselves.
                        try:
                            self._dispatch(carried, time.monotonic())
                        except Exception as exc:  # noqa: BLE001
                            print(f"[Listener] handler error: {exc}")
                        name_seen = False
                        self._reset_rec(brec)
                self._utt += data
                if len(self._utt) > UTT_MAX_BYTES:
                    del self._utt[:len(self._utt) - UTT_MAX_BYTES]
                if not rec.AcceptWaveform(data):
                    continue
                text = json.loads(rec.Result()).get("text", "").strip()
                utt, self._utt = bytes(self._utt), bytearray()
                heard_name, name_seen = name_seen, False
                self._reset_rec(brec)          # next utterance starts clean
                if not text:
                    continue

                def to_him(text, heard_name=heard_name):
                    # armed only when the detector heard his name and the
                    # sentence itself doesn't carry it — i.e. the name was
                    # mangled on its way through the general model. When it
                    # did survive, the normal path strips it and still answers
                    # a bare name with "Yes?".
                    self._dispatch(text, time.monotonic(),
                                   armed=heard_name and _strip_wake(text) is None)
                self._refine(text, utt, to_him)
        except Exception as exc:  # noqa: BLE001 - log a crash instead of dying silently
            print(f"[Listener] loop error: {exc}")
        finally:
            self._close_proc()

    def _refine(self, text: str, utt: bytes, then) -> None:
        """Route a finished sentence — on Whisper's words if it can answer now.

        ``text`` is Vosk's final, ``utt`` the audio it came from, ``then`` the
        routing to run on whichever words win. Without a transcriber, or with
        one not yet loaded, the routing runs here and now on Vosk's words. With
        one, it runs on a worker thread after Whisper has heard the audio — and
        if that worker is still busy with the previous sentence, this one goes
        out on Vosk's words at once: queueing would answer the wrong question
        late. Whisper returning nothing also means Vosk's words. A handler
        crash never stops listening, whichever thread it is on.
        """
        tr = self._transcriber
        if tr is None or not tr.ready() or not utt:
            self._safe(then, text)
            return
        if not self._refine_busy.acquire(blocking=False):
            self._refine_fallbacks += 1
            self._safe(then, text)
            return

        def work():
            try:
                try:
                    better = tr.transcribe(utt)
                except Exception as exc:  # noqa: BLE001 - a broken model is Vosk's words
                    print(f"[Listener] transcriber failed: {exc}")
                    better = ""
                if better and _runaway(better, text):
                    # Whisper's one bad habit: under heavy babble it writes a
                    # paragraph — "I have 10 frames left, so far I have 10
                    # frames left, so far..." — where Vosk heard a few words.
                    # A second opinion three times the length of the first is
                    # not a better hearing of the same sentence.
                    print(f"[Listener] whisper runaway ({len(better.split())} words), "
                          f"keeping vosk: {text!r}")
                    better = ""
                if better:
                    self._refined += 1
                    if better != text:
                        print(f"[Listener] whisper: {better!r} (vosk: {text!r})")
                else:
                    self._refine_fallbacks += 1
                self._safe(then, better or text)
            finally:
                self._refine_busy.release()
        threading.Thread(target=work, name="refine", daemon=True).start()

    @staticmethod
    def _safe(then, text: str) -> None:
        try:
            then(text)
        except Exception as exc:  # noqa: BLE001 - a handler crash must not stop listening
            print(f"[Listener] handler error: {exc}")

    def _reset_rec(self, rec) -> None:
        """Clear the name detector between utterances, tolerating one that can't.

        Also drops the partial streak — it belongs to the utterance just ended.

        Guarded because this is the microphone loop: an AttributeError here
        propagates out of _run and FRED goes deaf for the rest of the session,
        which is a wildly disproportionate outcome for a detector that is only
        an optimisation over rebuilding the object.
        """
        self._name_streak = 0
        if rec is None:
            return
        try:
            rec.Reset()
        except Exception as exc:  # noqa: BLE001  (see the docstring)
            print(f"[Listener] could not reset the name detector: {exc}")

    def _new_barge_rec(self):
        """A recogniser that can only hear his name, for deciding to interrupt.

        Returns None when the model has no dynamic graph, in which case _run
        watches the ordinary recogniser instead — worse, but not broken.
        """
        if self._model is None:
            return None
        try:
            return KaldiRecognizer(self._model, 16000, BARGE_GRAMMAR)
        except Exception as exc:  # noqa: BLE001
            if not self._warned_grammar:
                self._warned_grammar = True
                print(f"[Listener] no grammar support in this model ({exc}); "
                      "barge-in will be less reliable at hearing his name")
            return None

    def _detect_name(self, brec, data) -> bool:
        """Feed the name detector one chunk. True when it has really heard him.

        A *final* carrying the name is taken at once; a *partial* has to persist
        for NAME_MIN_PARTIALS chunks, for the reason recorded there.
        """
        if brec is None:
            return False
        if brec.AcceptWaveform(data):
            self._name_streak = 0
            return _wants_him(json.loads(brec.Result()).get("text", ""))
        if _wants_him(json.loads(brec.PartialResult()).get("partial", "")):
            self._name_streak += 1
        else:
            self._name_streak = 0
        return self._name_streak >= self._name_min_partials

    def _wake_full(self):
        """Start the full recogniser and hand it the audio it wasn't running for.

        The name detector fires part-way through his name, so without the replay
        the first recogniser would ever hear of the sentence is whatever came
        *after* "Fred" — losing the name itself, and with it _strip_wake's
        ability to tell a command from a bare prompt.

        Returns ``(recogniser, carried)``. A whole utterance can finish inside the
        buffer — someone who says just "Fred" and stops is the ordinary case, and
        the detector fires part-way through the word, so the final can land in the
        replay rather than the live stream. ``carried`` is that sentence, when it
        named him; anything completing in there that did *not* name him is the
        room, and is dropped.
        """
        rec = KaldiRecognizer(self._model, 16000)
        carried = ""
        self._utt = bytearray()
        for chunk in list(self._replay):
            self._utt += chunk
            if rec.AcceptWaveform(chunk):
                text = json.loads(rec.Result()).get("text", "").strip()
                if text and _wants_him(text):
                    carried = text
        self._replay.clear()
        return rec, carried

    def _barge(self) -> None:
        """Tell whoever is speaking to stop. Never lets a handler kill the loop."""
        try:
            self._on_barge()
        except Exception as exc:  # noqa: BLE001
            print(f"[Listener] barge handler error: {exc}")

    def _dispatch(self, text: str, now: float, armed: bool = False) -> None:
        """Route one final transcript: a command, a wake prompt, or nothing.

        Split out of _run so the barge-in path can use it too. ``armed=True``
        skips the wake word outright — someone talking over him has already made
        it clear who they are talking to.
        """
        if armed or now < self._armed_until:
            # Speech inside an open window: the command after a bare "Fred", the
            # answer to a question he just asked, or an interruption. Consumed
            # *before* the handler runs, so a reply ending on another question
            # can re-arm.
            self._armed_until = 0.0
            self._cap.commit()                 # speech inside an open window is for him
            self._on_command(text)
            return
        # A fresh transcript being weighed against his name: this is the "how
        # often was the gate asked" that heard.jsonl can't record. Continuations
        # above returned already, so every count here is a real wake decision.
        self._wakestats.considered()
        cmd = self._strip(text)   # honours event-mode strictness (V4)
        if cmd is None:
            return                             # not addressed to him -> ignore
        self._wakestats.passed()
        self._cap.commit()                     # it passed the gate: keep the audio (if enabled)
        if cmd:
            self._on_command(cmd)
        else:                                  # bare "Fred" -> prompt & arm
            self._armed_until = now + ARM_WINDOW
            self._on_wake()

    def _close_proc(self) -> None:
        """Terminate the arecord subprocess and release the capture device.

        Callable from any thread: stop() ends the thread that owns it, and the
        loop calls it on EOF. pause() no longer does — the stream stays up while
        FRED speaks, see pause(). The claim/release of ``_proc`` is atomic so a
        concurrent close + loop-EOF can't double-reap. We reap the
        child (so ALSA frees the device) *and* close the pipe (so we don't leak an
        fd per pause) — the loop's read() tolerates the pipe vanishing underneath
        it, which is what makes closing from another thread safe.
        """
        with self._proc_lock:
            p, self._proc = self._proc, None
        _reap(p)


def _runaway(better: str, vosk: str) -> bool:
    """Is Whisper's answer implausibly longer than what Vosk heard?

    Three times Vosk's word count and at least twelve words: a child's
    sentence Vosk mangled to three words can honestly be nine, not thirty.
    """
    n, m = len(better.split()), len(vosk.split())
    return n >= 12 and n > 3 * max(1, m)


def _reap(p) -> None:
    """Terminate an arecord process, wait for ALSA to free the device, and close
    its pipe. Tolerates a process that's already dead."""
    if p is None:
        return
    try:
        p.terminate()
        p.wait(timeout=1)
    except Exception:          # noqa: BLE001
        try:
            p.kill()
            p.wait(timeout=1)
        except Exception:      # noqa: BLE001
            pass
    try:
        p.stdout.close()
    except Exception:          # noqa: BLE001
        pass


# Seconds of a live arecord delivering nothing before the capture is declared
# wedged. This card streams continuously — a quiet room is still 16 kHz of
# near-zero samples — so genuine no-data means the device or the pipe has
# stopped, not that nobody is talking. Comfortably above the 125 ms chunk
# cadence and any startup lag.
WEDGE_TIMEOUT = 3.0


def _read_chunk(pipe, nbytes: int, timeout: float):
    """One capture read that cannot hang the loop.

    Returns the data, b"" on EOF or a pipe closed under us (pause() does
    that), or None when the pipe stayed open but delivered nothing for
    ``timeout`` — the wedged-but-alive case a bare read() would sit in
    forever. select() rather than a reader thread: one extra syscall per
    chunk, no new concurrency.
    """
    try:
        ready, _, _ = select.select([pipe], [], [], timeout)
    except (ValueError, OSError):          # closed while we were waiting
        return b""
    if not ready:
        return None
    try:
        return pipe.read(nbytes)
    except (ValueError, OSError):          # closed between select and read
        return b""


def _take_channel(data: bytes, channels: int, channel: int) -> bytes:
    """Pull one channel out of interleaved S16_LE PCM.

    For a mic array that presents several channels at once. The reSpeaker Flex
    (XVF3800) offers six at 16 kHz. What they *are* depends on the device's
    ``AEC_ASROUTONOFF``: at 1, which is how it ships, every channel is a
    beamformer output rather than a microphone — so all six are already
    echo-cancelled, and none of them is a raw capsule. (Set it to 0 and you get
    the AEC residuals instead, one per microphone; that is what any
    do-it-yourself beamforming would need, and it is not what is on here.)
    Channel 0 carries the loud AGC'd output and is the one to transcribe —
    measured on this
    device, capturing ``-c 1`` through ALSA's ``plughw`` does not pick a channel,
    it *averages all six*, which mixes the beamformed, echo-cancelled,
    noise-suppressed output back together with the raw microphones at a sixth of
    its level. That is worse than the plain mic it replaced: it re-adds the room
    the array just removed, and quietly, because nothing errors.

    Frame-aligned defensively. A short read at EOF can end mid-frame, and
    reshaping that raises rather than returning the audio that did arrive.
    """
    frame = channels * 2                       # int16 per channel
    usable = len(data) - (len(data) % frame)
    if usable <= 0:
        return b""
    block = np.frombuffer(data, dtype=np.int16, count=usable // 2)
    return block.reshape(-1, channels)[:, channel].tobytes()


def _amplify(data: bytes, gain: float) -> bytes:
    """Scale S16_LE mono PCM by ``gain``, clipping hard at the int16 rails so an
    over-driven sample wraps to a click rather than to the opposite polarity."""
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) * gain
    np.clip(samples, -32768, 32767, out=samples)
    return samples.astype(np.int16).tobytes()


def _wants_him(text: str) -> bool:
    """Is this speech addressed to FRED — i.e. does his name appear in it?

    The test for interrupting him. See BARGE_NEEDS_WAKE_WORD for why a room's
    ordinary conversation must not qualify.
    """
    if not BARGE_NEEDS_WAKE_WORD:
        return bool(text.strip())
    return _strip_wake(text) is not None


def _strip_wake(text: str):
    """If ``text`` contains a wake word, return everything after it (may be '');
    return None if no wake word is present."""
    tokens = text.lower().split()
    for i, tok in enumerate(tokens):
        if tok in WAKE_WORDS:
            return " ".join(tokens[i + 1:]).strip()
    return None

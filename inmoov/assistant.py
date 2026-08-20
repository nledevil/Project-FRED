"""FRED's voice assistant — wires the wake-word listener, the hybrid brain, and
lip-synced speech into one object the web app owns.

Flow: Listener hears "Fred ..." -> Brain turns it into an action + reply ->
Assistant speaks the reply while animating the jaw in time with the audio.

Speech is *pipelined*, because a talking head is judged on how fast it answers.
The brain hands over each sentence the moment it's written, one sentence renders
while the previous one is still playing, and the jaw is scheduled against
``Sound.audio_epoch()`` — the moment audio actually becomes audible — rather than
against "now", which would run the mouth ahead of the voice by the whole lead-in.

Everything degrades gracefully: no mic/model -> not listening; no API key ->
local commands only; no speaker -> silent. ``status()`` reports what's live.
"""
from __future__ import annotations

import queue
import re
import threading
import time
import types
import wave

import numpy as np

from pathlib import Path

from . import heardlog
from .brain import LOOK_MIN_SECS, Brain
from .listener import ARM_WINDOW, Listener

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"


class Assistant:
    def __init__(self, controller, led, tracker, sound, *, api_key: str | None = None,
                 device: str = "plughw:0,0", log=None, mic_gain: float = 1.0,
                 model: str | None = None, sensors=None, brain_cfg: dict | None = None,
                 asr_model: str | None = None, barge_in: bool = True,
                 stop_when_alone: bool = True):
        # sensors is the SensorHub, or None on a build with no sensor node — the
        # read_sensors action degrades to saying so rather than failing.
        self._ctx = types.SimpleNamespace(controller=controller, led=led,
                                          tracker=tracker, sound=sound,
                                          sensors=sensors)
        self._sound = sound
        self._controller = controller
        self._log = log                           # ConversationLog (optional)
        bc = brain_cfg or {}
        self.brain = Brain(self._ctx, api_key=api_key, model=model,
                           backend=str(bc.get("backend", "auto")),
                           local_model=bc.get("local_model") or None,
                           local_host=bc.get("local_host") or None,
                           vision=bool(bc.get("vision", True)),
                           look_min_secs=float(bc.get("vision_min_seconds",
                                                      LOOK_MIN_SECS)),
                           web_search=bool(bc.get("web_search", True)),
                           web_location=bc.get("web_search_location") or None,
                           face_recall=bool(bc.get("face_recall", True)),
                           face_hold_camera=bool(bc.get("face_hold_camera", False)))
        listener_kw = {}
        if asr_model:
            # A name under models/, not a path: the setting is edited by a human
            # in a JSON file and should not be a chance to point the recogniser
            # anywhere on disk.
            listener_kw["model_path"] = MODELS_DIR / str(asr_model)
        self.listener = Listener(on_command=self._on_command, on_wake=self._on_wake,
                                 on_barge=self.interrupt,
                                 barge_in=bool(barge_in),
                                 device=device, gain=mic_gain, **listener_kw)
        self._speaking = False
        # True from "FRED heard you" until the first audio of his reply — the
        # Claude round-trip made visible. The chest display shows it as a state.
        self._thinking = False
        self._speak_lock = threading.Lock()
        # Barge-in: set to cut the current reply short because someone started
        # talking over him. Speech checks it between clips and inside the wait
        # for one, so he stops on the sentence he is on rather than finishing
        # the whole answer to a room that has moved on.
        self._interrupt = threading.Event()
        # Cut a reply short when the visitor walks off. See on_sensor_event.
        self.stop_when_alone = bool(stop_when_alone)
        self._speaking_since = 0.0
        # The thread running the current turn. Turns run off the listener thread
        # so the microphone keeps being read while he answers — without that,
        # nothing can hear the person interrupting.
        self._turn: threading.Thread | None = None
        self._turn_lock = threading.Lock()
        self._last_heard = ""
        self._last_reply = ""
        self._last_source = ""
        # The envelope currently driving the jaw, published for the web face so
        # the on-screen mouth is the same motion, not a lookalike. Bumped once
        # per clip; the browser refetches when the sequence number changes.
        self._mouth: dict | None = None
        self._mouth_seq = 0

    @property
    def ctx(self):
        """The namespace actions run against.

        Exposed so the web app can attach hardware that is discovered after the
        assistant is built — the cart is configured from settings the app owns,
        and Brain already holds this same object, so late-attaching to it is
        what makes the new capability visible to both Claude and the matcher.
        """
        return self._ctx

    # ---- capability / status ---------------------------------------------
    def available(self) -> bool:
        return self.listener.available()

    def is_speaking(self) -> bool:
        return self._speaking

    def is_thinking(self) -> bool:
        """Heard you, hasn't answered yet. Cheap enough to poll."""
        return self._thinking

    def mouth_seq(self) -> int:
        """Which clip's envelope is current. Cheap enough for the head poll."""
        return self._mouth_seq

    def mouth(self) -> dict | None:
        """The envelope driving the jaw right now, for the on-screen face.

        ``starts_in`` is seconds from *now* until frame 0 is audible (negative if
        the clip is already playing), so the browser can line its animation up with
        the same audio the servo is following without knowing our monotonic clock.
        """
        m = self._mouth
        if not m:
            return None
        return {"seq": m["seq"], "frame_dt": m["frame_dt"],
                "starts_in": m["epoch"] - time.monotonic(),
                "levels": m["levels"]}

    def _publish_mouth(self, levels, frame_dt: float, epoch: float | None) -> None:
        if not levels or epoch is None:
            return
        self._mouth = {"seq": self._mouth_seq + 1, "frame_dt": frame_dt,
                       "epoch": epoch,
                       "levels": [round(float(v), 3) for v in levels]}
        self._mouth_seq += 1        # publish last: the seq is the browser's trigger

    def status(self) -> dict:
        return {
            "available": self.available(),          # mic + Vosk model present
            "listening": self.listener.is_running(),
            "speaking": self._speaking,
            "thinking": self._thinking,
            "ai_available": self.brain.ai_available(),
            "can_speak": self._sound.can_speak(),
            "last_heard": self._last_heard,
            "last_reply": self._last_reply,
            "last_source": self._last_source,
            "mic": self.listener.status(),     # is the microphone actually hearing?
        }

    # ---- lifecycle --------------------------------------------------------
    def start(self, greet: bool = True) -> bool:
        ok = self.listener.start()
        if ok and greet:
            msg = "Hi, I'm Fred. I'm listening."
            if self._log:
                self._log.fred(msg, source="local")
            threading.Thread(target=self.speak, args=(msg,), daemon=True).start()
        return ok

    def stop(self) -> None:
        self.listener.stop()

    # ---- conversation -----------------------------------------------------
    def converse(self, text: str, source: str = "text") -> dict:
        """Run text through the brain, speak the reply, and record it. ``source``
        is 'voice' or 'text'. Used by both the wake-word path and the web box.

        The speaker runs alongside the brain: Claude streams a sentence, the
        speaker renders and plays it, and by the time FRED reaches the full stop
        the next sentence is usually already synthesised. Blocks until he has
        finished speaking, as the old sequential version did.
        """
        text = (text or "").strip()
        self._last_heard = text
        if not text:                                # nothing to say: don't cycle the mic
            return {"reply": "", "source": "none", "actions": []}
        # A new turn is never born interrupted. Cleared here rather than in
        # _on_command because the panel's text box calls converse() directly —
        # leaving it set there meant one barge-in silenced every typed reply
        # afterwards, with the transcript still showing what he "said".
        # Safe against the previous turn: _on_command has already interrupted
        # and joined it before starting this one.
        self._interrupt.clear()
        if self._log:
            self._log.user(text, source=source)

        # Thinking starts the moment we have something to answer, and is cleared
        # by the speaker thread the instant real audio starts (or here, if the
        # brain fails and no audio ever comes).
        self._thinking = True

        sentences: queue.Queue = queue.Queue()      # unbounded: never stall the brain
        speaker = threading.Thread(target=self.speak_stream, args=(_drain(sentences),),
                                   name="speaker", daemon=True)
        speaker.start()
        try:
            # respond() emits every sentence it will return, so speaking is
            # entirely the speaker thread's job — don't also speak result["reply"].
            result = self.brain.respond(text, on_sentence=sentences.put)
        finally:
            sentences.put(None)                     # end of stream, even on error
            speaker.join()
            self._thinking = False                  # covers the never-spoke path

        self._last_reply = result.get("reply", "")
        self._last_source = result.get("source", "")
        # A question is an invitation to answer, so hold the mic open rather than
        # making the person say his name again to finish the exchange they are
        # already in. Here, not before speaking: capture is off for the whole of
        # playback, so a window opened earlier would have burned down while he
        # was still talking.
        #
        # Only when FRED actually asked something, and only when the turn came in
        # by voice. Arming after every reply would leave the mic live on a room
        # full of people all evening, which is what the wake word is for; arming
        # after somebody typed in the panel would open it on a room that was
        # never talking to him in the first place.
        if source == "voice" and _ends_on_question(self._last_reply):
            self.listener.arm()
            if self._log:
                self._log.event("Asked a question — listening for the answer…")
        # The tuning set a fair produces: what was heard and where it went.
        # After the reply, so a crash while speaking still logged the hearing.
        try:
            heardlog.log().append(
                heard=text, source=source,
                route="matched" if result.get("matched") else result.get("source", ""),
                action=result.get("matched", ""),
                reply=self._last_reply,
                event=bool(getattr(getattr(self.ctx, "event", None), "enabled", False)))
        except Exception as exc:                     # noqa: BLE001
            print(f"[Assistant] heard log failed: {exc}")
        if self._log and self._last_reply:
            self._log.fred(self._last_reply, source=self._last_source,
                           actions=result.get("actions"))
        return result

    # Someone has to have walked well out of both cones for this: the firmware
    # only calls it a departure past 145 cm, held for three cycles, so a visitor
    # standing still while he answers does not trigger it.
    NEAR_CM = 130.0
    # He does not stop for a departure in the first moment of a reply. A person
    # stepping sideways as he starts is common; abandoning the first sentence
    # every time reads as a fault rather than as attentiveness.
    DEPART_GRACE = 2.5

    def on_sensor_event(self, node: str, event: dict) -> None:
        """Stop talking when the person he was talking to leaves.

        He will otherwise finish a thirty-second answer to an empty spot while
        the queue behind waits, which at an event is the whole cost.

        Deliberately narrow. There are two distance sensors, so one cone losing
        somebody who merely moved into the other would cut him off mid-sentence
        — the check below is "nobody is near *any* of them", not "this one
        stopped seeing them".
        """
        kind = str((event or {}).get("event"))
        if kind == "approach":
            # Somebody has walked up. Earlier than the wake word and earlier than
            # the greeting, which is the only way a burst of agreeing frames is
            # ready by the time they actually say something.
            self.brain.attend_faces()
            return
        if not self.stop_when_alone or not self._speaking:
            return
        if kind != "depart":
            return
        if time.monotonic() - self._speaking_since < self.DEPART_GRACE:
            return
        if self._somebody_near():
            return
        if self._log:
            self._log.event("Stopped — they walked away.")
        self.interrupt()

    def _somebody_near(self) -> bool:
        """Is any distance sensor still reading somebody in front of him?

        Unknown counts as "yes": a sensor that has gone quiet is not evidence
        that the room is empty, and the safe failure here is to keep talking.
        """
        hub = getattr(self._ctx, "sensors", None)
        if hub is None:
            return True
        try:
            nodes = (hub.state() or {}).get("nodes") or {}
        except Exception:      # noqa: BLE001 - never let a sensor read stop speech
            return True
        for node in nodes.values():
            for reading in (node.get("readings") or {}).values():
                if reading.get("type") != "distance":
                    continue
                cm = reading.get("cm")
                if cm is not None and float(cm) <= self.NEAR_CM:
                    return True
        return False

    def interrupt(self) -> None:
        """Stop the reply in progress. Someone is talking over him.

        Cuts the audio immediately and unwinds ``speak_stream``; the brain may
        still be writing sentences, but nothing further is spoken and the queue
        is drained by the caller. Harmless when he isn't talking.
        """
        if self._speaking and self._log:
            # Worth a line in the transcript: "he stopped mid-sentence" is
            # otherwise indistinguishable from a crash, and if this starts
            # appearing when nobody is talking it is the thing to look at.
            self._log.event("Interrupted — someone spoke over him")
        self._interrupt.set()
        self._sound.stop()

    def _on_command(self, text: str) -> None:
        """A recognised utterance. Runs the turn *off* the listener thread.

        This used to call converse() inline, which meant the thread that reads
        the microphone sat in speaker.join() for the whole reply — nothing was
        read while he talked, so nobody could interrupt him however loudly they
        spoke. The turn gets its own thread and the listener goes straight back
        to reading.

        A new utterance while a turn is still running *is* the interruption: the
        old turn is cut short and briefly waited for, so two replies can never
        be spoken over each other.
        """
        with self._turn_lock:
            running = self._turn
        if running is not None and running.is_alive():
            self.interrupt()
            # Short join: the point is to serialise turns, not to stall the mic.
            # interrupt() has already killed the audio, so this returns quickly;
            # the timeout is a backstop for a brain call that is mid-request.
            running.join(timeout=2.0)
        turn = threading.Thread(target=self._run_turn, args=(text,),
                                name="turn", daemon=True)
        with self._turn_lock:
            self._turn = turn
        turn.start()

    def _run_turn(self, text: str) -> None:
        try:
            self.converse(text, source="voice")
        except Exception as exc:  # noqa: BLE001 - a turn must never kill the thread
            print(f"[Assistant] turn failed: {exc}")

    def _on_wake(self) -> None:
        if self._log:
            self._log.event("Heard his name — listening…")
        # Start watching for a face now rather than when the question arrives.
        # Recognising somebody takes several frames that agree, and respond()
        # does not have that long — without this a returning visitor is noticed
        # on their *second* question, which is worse than not at all.
        self.brain.attend_faces()
        self.speak("Yes?")
        # Restart the window now that the mic is live again. _run armed it before
        # this call, so without this the second or so spent saying "Yes?" comes
        # out of the time the person has to speak.
        self.listener.arm(ARM_WINDOW)

    # ---- lip-synced speech ------------------------------------------------
    def speak(self, text: str) -> bool:
        """Speak ``text`` as a single utterance, jaw in time with the audio."""
        text = (text or "").strip()
        if not text:
            return False
        return self.speak_stream([text])

    def speak_stream(self, sentences) -> bool:
        """Speak an iterable of sentences back-to-back, lip-syncing each.

        ``sentences`` may be a lazy iterator that blocks — that's the point: it's
        fed by Claude as the reply streams in. A helper thread renders one
        sentence ahead of playback, so the gap between sentences is just aplay's
        restart, not a whole synthesis.

        Serialised so two replies never overlap; sets ``_speaking`` (once audio is
        genuinely coming out) so the listener and the web face know FRED is
        talking. Returns True if anything was spoken.
        """
        if not self._sound.can_speak():
            return False
        with self._speak_lock:
            # Stop listening — but the mic stream stays up through playback now:
            # the PowerConf runs both directions at once and cancels its own
            # voice out of its capture, so there is nothing to hand over. See
            # Listener.pause for the measurements.
            self.listener.pause()
            # Remember where the mouth is, so we RESTORE it after speaking rather
            # than force-closing — e.g. "open your mouth" opens the jaw first, then
            # says "Ahhh", and the mouth stays open afterwards.
            jaw = self._controller.servos.get("jaw")
            hold_angle = self._controller.get_angle("jaw")
            if hold_angle is None and jaw:
                hold_angle = jaw["rest_angle"]

            abort = threading.Event()
            rendered: queue.Queue = queue.Queue(maxsize=2)   # render-ahead depth
            renderer = threading.Thread(target=self._render_ahead,
                                        args=(sentences, rendered, abort),
                                        name="tts-render", daemon=True)
            renderer.start()
            spoke = False
            try:
                first = True
                while True:
                    if self._interrupt.is_set():
                        break                      # barged in: stop mid-reply
                    path = rendered.get()
                    if path is None:               # renderer finished
                        break
                    if self._interrupt.is_set():
                        break
                    # Only the first clip gets the lead-in padding; re-padding each
                    # sentence would insert a silent gap mid-reply.
                    if self._play_chunk(path, pad=first):
                        spoke, first = True, False
            except Exception as exc:  # noqa: BLE001 - speech must NEVER kill the caller (e.g. the listener thread)
                print(f"[Assistant] speak failed: {exc}")
            finally:
                abort.set()                        # unblock the renderer if it's mid-put
                _drain_queue(rendered)
                renderer.join(timeout=5.0)
                if jaw and hold_angle is not None:
                    self._controller.set_angle("jaw", hold_angle)   # restore prior mouth position
                self._speaking = False
                self.listener.resume()             # listening again
            return spoke

    def _render_ahead(self, sentences, out: queue.Queue, abort: threading.Event) -> None:
        """Synthesise each sentence and hand the WAV to the player, one ahead."""
        try:
            for sentence in sentences:
                if abort.is_set():
                    break
                sentence = (sentence or "").strip()
                if not sentence:
                    continue
                path = self._sound.render_tts(sentence, speed=150)
                if path is None:                   # no TTS backend, or it failed
                    continue
                _put(out, path, abort)
        except Exception as exc:  # noqa: BLE001
            print(f"[Assistant] render failed: {exc}")
        finally:
            _put(out, None, abort)                 # sentinel: end of stream

    def _play_chunk(self, path: str, pad: bool) -> bool:
        """Play one rendered sentence and drive the jaw through it. True if it played."""
        levels, frame_dt = self._envelope(path)
        if not self._sound.play_file(path, wait=False, pad=pad):     # async aplay
            return False
        # Only flap the jaw if audio is really playing — never mime silently.
        # Flip _speaking HERE, once sound is actually coming out: the web face
        # animation polls is_speaking(), so raising it any earlier (e.g. during
        # the TTS render) makes the on-screen mouth move before the jaw does.
        if self._sound.is_playing():
            epoch = self._sound.audio_epoch()
            # Publish before raising _speaking, so the poll that first sees
            # "speaking" already carries the new envelope's sequence number.
            self._publish_mouth(levels, frame_dt, epoch)
            if not self._speaking:
                self._speaking_since = time.monotonic()
            self._speaking = True
            self._thinking = False      # sound is out: he's answering, not pondering
            self._animate_jaw(levels, frame_dt, epoch)
            while self._sound.is_playing():                          # let audio finish
                if self._interrupt.is_set():
                    self._sound.stop()             # cut this clip off mid-word
                    break
                time.sleep(0.02)
        return True

    def _envelope(self, path: str, frame_ms: float = 40.0):
        """Return (levels 0..1 per frame, seconds-per-frame) from a WAV's RMS."""
        try:
            w = wave.open(path)
            rate = w.getframerate()
            data = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
            w.close()
        except Exception:      # noqa: BLE001
            return [], frame_ms / 1000.0
        if data.size == 0:
            return [], frame_ms / 1000.0
        n = max(1, int(rate * frame_ms / 1000.0))
        frames = [data[i:i + n] for i in range(0, data.size, n)]
        rms = np.array([float(np.sqrt((f ** 2).mean())) for f in frames])
        peak = rms.max()
        levels = (rms / peak) if peak > 1e-6 else rms
        return levels.tolist(), n / rate

    def _animate_jaw(self, levels, frame_dt: float, epoch: float | None) -> None:
        """Step the jaw through the envelope, timed to when the audio is *audible*.

        ``epoch`` is ``Sound.audio_epoch()`` — the monotonic instant the clip's
        first real sample reaches the speaker, which is later than now by the
        lead-in silence plus the device's own start-up latency. Starting the
        envelope at ``time.monotonic()`` instead (as this used to) marched the jaw
        through the whole utterance a lead-in ahead of the voice.

        Returns early when the reply is interrupted. This loop is the jaw's only
        driver and it blocks for the length of the clip, so without that check a
        barge-in leaves him mouthing the rest of a sentence nobody can hear.
        """
        s = self._controller.servos.get("jaw")
        if not s or not levels:
            return
        closed, opened = s["rest_angle"], s["max_angle"]
        span = opened - closed
        start = epoch if epoch is not None else time.monotonic()
        for i, lvl in enumerate(levels):
            if self._interrupt.is_set():
                # Barged in: the audio was killed the instant it was requested,
                # but this loop owns the next second or two of jaw movement and
                # would mime its way to the end of the clip in silence. Stop
                # here; speak_stream's finally puts the mouth back.
                return
            dt = (start + i * frame_dt) - time.monotonic()
            if dt > 0:
                time.sleep(dt)
            elif dt < -frame_dt:
                continue          # running late (a stalled servo write?) — skip to catch up
            # a small floor so the mouth still flutters on quiet syllables
            open_amt = min(1.0, 0.15 + 0.85 * float(lvl))
            self._controller.set_angle("jaw", closed + span * open_amt)


# Sentence-final punctuation that closes a question. The closing bracket/quote
# class is there because FRED's replies come out of a sentence splitter that
# keeps them ("...which one did you mean?").
_QUESTION_END = re.compile(r"\?[\"\'’”)\]]*\s*$")


def _ends_on_question(reply: str) -> bool:
    """Did FRED finish his turn by asking something?

    The *last* sentence is what decides it. A reply that asks something in
    passing and then answers it ("Is it raining? Yes, and it will be all day.")
    is not waiting on anybody, and holding the mic open after one would leave it
    live on a room that has no reason to reply.
    """
    return bool(_QUESTION_END.search((reply or "").strip()))


def _drain(q: queue.Queue):
    """Yield items from ``q`` until the None sentinel. Turns the brain's push of
    sentences into the pull-style iterable speak_stream() consumes."""
    while True:
        item = q.get()
        if item is None:
            return
        yield item


def _put(q: queue.Queue, item, abort: threading.Event) -> None:
    """Block until ``item`` fits in ``q``, giving up if ``abort`` is set — so the
    render-ahead thread can't wedge on a full queue nobody is draining."""
    while not abort.is_set():
        try:
            q.put(item, timeout=0.1)
            return
        except queue.Full:
            continue


def _drain_queue(q: queue.Queue) -> None:
    """Discard anything buffered, freeing a producer blocked on put()."""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return

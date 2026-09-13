# InMoov TODO

## Testing: narrative scripts, no pytest (recorded 2026-09-09)

Settled, so it stops being re-litigated. The tests are standalone scripts in
`tools/` and `deploy/display/tools/`, each a real failure made executable, each
exiting non-zero on failure, each running without hardware. `tools/test_all.sh`
runs all of them from one command (substring filter, red on any failure) — the
discovery+runner that was the only thing the style actually lacked. pytest would
add a dependency and fixture indirection for nothing this project is missing;
the real coverage gaps are subsystems, not framework. New tests join by being a
`test_*.py` in either directory that exits non-zero when it fails — nothing to
register. Run it before every commit; `tools/preflight.py` is the separate
show-morning check against the live robot.

## What changed 2026-08-19 — conversation, and the microphone underneath it

A session about turn-taking: what it takes for someone to talk to him the way
they would talk to a person. Two older claims in this file turned out to be
false and are corrected where they sit (the half-duplex card, and `"friend"` in
the wake words).

**Built, and verified on the hardware unless noted:**

- **Relax actually relaxes.** It cut the servo pulses and the face tracker put
  them straight back within 1/fps, so the button looked like it did nothing.
  Read off the PCA9685 to confirm the fix: all sixteen channels `FULL_OFF`.
  Chest panel gained the matching button. *(Aside worth keeping: ~0.1 A at 6.3 V
  with everything relaxed is the servos' own idle draw, not a failure to relax.
  Nothing in software reaches zero — that needs a switch on the rail.)*
- **He can look things up.** Anthropic's server-side web search, on the Claude
  path only — the local model never gets a tool it cannot execute. Needed
  `pause_turn` handling, and telling him where he is, because `user_location`
  only tilts result ranking and the model cannot read it.
- **The wake word is his name, and always was.** No code change; every string
  around it said "Hey FRED", which is where the habit came from.
- **He stopped dropping syllables.** A reply is one `aplay` per sentence and
  every one reopens the device; `lead_in` was applied only to the first clip.
  `gap_lead_in` pads the rest. Judged by ear as fixed.
- **The mic stays open while he speaks** — see the corrected STOP item below for
  the measurements. Removes a device reopen and a fresh recogniser per reply.
- **Answering him needs no wake word.** When his reply ends on a question the
  window opens *after* he stops talking, for voice turns only.
- **Talking over him stops him**, if you use his name. Any-speech-interrupts was
  tried first and is unusable: with a television on, four lines of dialogue in
  seven seconds each cut him off *and* became commands.
- **He stops when you walk away** — see the Then-the-rest list; narrow on
  purpose, because two distance cones make the naive version cut him off when
  somebody merely steps sideways.
- **Idle listening got cheaper.** The full recogniser now runs only when there
  is sound to transcribe: 50% of a core with a television on, against ~90%
  before, and far less in a quiet room. Gated on *sound* and not on his name —
  the name detector's persistence does not separate a real "Fred" (0-3
  consecutive chunks) from "my friend told me about it" (7), and on the wake
  path that failure is silent.
- **`tools/wake_audit.py`** replaces hand-mining `heard.jsonl` to tune the wake
  words. Compares sounds rather than spellings; its strongest signal is
  positional. Read the caution attached to the wake-word item below before
  trusting any file-derived number.

**Not done, and now cheaper than it was:** the STOP button (two endpoints, still
no UI), volume, the strictness knob for event mode, push-to-talk.

## Where things stand after 2026-08-12/16

A long session that touched the brain, the GPU, the chest touchscreen, the cart
and the network. Everything below is either unfinished, worth doing, or worth
knowing before someone changes it.

Verified means "watched it work on the hardware", not "should work".

**This section was audited against the code on 2026-08-16** — every claim in it
re-checked rather than assumed. Three had gone stale and are corrected where
they sit, each marked *Corrected 2026-08-16*: event mode exists (this section
said it did not), `ui.Pager` never did, and the terminator clips are present but
out of Claude's reach. Further down, STEM priority 1 turned out to be built and
priority 2 mostly so; both are rewritten to say what is actually left. Two more
things were found broken *by* the audit and fixed the same day — the cleared
note immediately following.

**Cleared 2026-08-16: two things that were quietly wrong.**

- **`logs/` was never actually ignored.** `.gitignore` had `logs/` with a
  trailing `# comment` on the same line, and git has no trailing comments — the
  whole line became the pattern, matching nothing. So `logs/heard.jsonl`, which
  holds what visitors actually said to him, was one `git add -A` from being
  published. The comment now sits on its own line.
- **`tools/test_auth.py` was red.** Nine endpoints from the 2026-08-16 work —
  the four `uplink` routes, three `phrases`, and both `heard` — had never been
  classified, which is precisely the failure that test exists to force. The
  good news is that `app.py` had them right all along: everything that acts is
  `@protected`, and only the two read-only GETs are open. The test now records
  those decisions, with the reasoning, and passes.

**Also cleared 2026-08-16: the touchscreen's PIN only ever asked once.** Unlock
the chest menu, leave it, tap the cog again — and it opened straight up. It
stayed that way until the process restarted, which on a machine running
`inmoov-display` indefinitely means until the next reboot, so the first operator
of the day silently unlocked the robot for everyone who walked up after.

Worth understanding rather than just fixing, because nobody wrote a bug: the
gate re-locked *by itself* when the settings menu was a separate process, since
closing it destroyed the pad. "Keep one panel process instead of respawning per
animation" (2026-08-15) was a rendering change with no visible relationship to
security, and it quietly turned "locked per open" into "locked once per boot".
`PinPad.lock()` now exists and `closeMenu()` calls it — and deliberately keeps
the wrong-guess back-off, or closing the menu would be the way to buy five more
free tries. Leaving also hides the power overlay, so an armed shutdown cannot
come back on top of the keypad. Proved on the hardware by injecting real touch
events and running the sequence against both the old code (walks in) and the new
(asks again); `tools/test_menu_logic.py` covers all four behaviours.

**Still open on it:** the menu does not re-lock or close on its own if it is left
open and abandoned. Deciding that is a judgement call about an operator reading a
page versus a robot left unattended, so it is left for Ryan rather than guessed.

**Also cleared 2026-08-16: the chest touchscreen's servo sliders.** They drew
correctly and ignored every finger. Two independent faults, both in
`ServosPage.qml`: the `Slider` replaced `background` with a size-less `Item`,
which collapses the control's implicit height to zero — QML does not clip, so
the track and knob still drew at full size while the *touchable* area was a
zero-height line — and the `Repeater`'s model was bound to the per-tick-rebuilt
row list, so the first move destroyed the delegate the finger was on. A control
that draws but cannot be touched is invisible to screenshots; it took injecting
real events at `/dev/input/event4` to see it. Confirmed by Ryan on the panel.

**Cleared on 2026-08-12**, and removed from the list rather than left to rot:
the whole session's work is committed in both repos; the Ollama iGPU
fix survived its first real cold boot (`library=Vulkan`, 163 ms prompt-eval);
the chest cart page has a stop button; and the unverified Xbox profile is gone
from `gamepad.py` along with the controller itself.

**Also cleared:** the cart has been driven from the hand controller (confirmed by
Ryan). And releasing the deadman now **stops the cart and revokes the host's
authority** rather than handing control back — the host regains it by going quiet
for a watchdog period and commanding again. Reasoning is in `cart_driver.py`'s
module docstring; the release path is covered in `tools/test_cart_driver.py`, but
has not yet been watched on the real base with a real thumb on R1.

**Also cleared:** the panel has a 4-digit PIN in front of the settings and
everything that moves him, on the web panel and the chest touchscreen both.
Status, the camera, the sound board, speech and — above all — the cart's STOP
stay open. **A PIN is set on this robot** (2026-08-12); a fresh install has none
until someone sets one on the admin page, and is exactly as open as it was until
they do. What four digits over plain HTTP is actually worth is written down in
`inmoov/auth.py`; the AP password and physical access are still the real
perimeter — and that password is still the published default `inmoov-robot`.

**Also cleared (2026-08-13): FRED can see.** `look_at_what_you_see` is the one
tool whose result is a picture rather than a sentence, so it bypasses
`run_tool`'s string contract — `brain.py`'s tool loop special-cases it and calls
`Brain._look`. Two lenses: the eye camera, which always works, and the wide
PanaCast when the spotter is already running (starting it for one glance costs
about four cores). Frames go out at 1024px, ~1000 tokens, ~$0.0016 a look,
rate-limited to one grab per camera per 12 s.

Three things learned building it, worth not rediscovering:

- **The rate limit re-sends the cached frame rather than referring to it.**
  `_history` keeps only final text, never tool blocks, so by the next turn the
  previous picture is gone from the conversation. Answering a repeat look with
  "you already have one, answer from that" produced a confidently invented
  shirt colour — the exact fabrication the tool exists to remove.
- **The matcher was answering the questions vision is for.** The presence rule
  sent "how many people are in front of you?" to `read_sensors`, which cannot
  count. It now stands aside for count/describe/see phrasings; bare "is anyone
  there?" still takes the offline fast path, because that is the case sensors
  win in the dark.
- **Only Claude gets the image** — `local_brain._to_ollama` flattens tool_result
  content with `str()`, so on the local backend he says he can't see.

Still open on it: **a look does not light the privacy LED** (`Camera.acquire()`
tracks MJPEG viewers only), so frames leave the robot with no outward sign.
Raised and deliberately declined on 2026-08-13 — it matches face tracking, which
has never lit it either. The look is also **not** gated by event mode: only the
12 s rate limit and the `brain.vision` switch stand in front of it.

*Corrected 2026-08-16: this paragraph used to say event mode did not exist. It
does — `inmoov/event.py`, and the two items said to be waiting on it are built.*
**Event mode caps his answers** (`max_words`, default 25 — the brain injects a
"you are at a public event, one short sentence" instruction and `ship()` stops
at the first sentence boundary past the cap), **caps the cart** (`cart_speed`,
default 120, applied as `speed_ceiling` per command), **protects the chest
display** from Claude swapping a voice-state screen for a flourish, raises that
display when switched on, and is recorded against every line in the heard log.
It is `event.enabled` in settings with a toggle on the admin page, and it is
**off on this robot today** — turning it on is part of setting up for an event.

**Also cleared (2026-08-14): he can say how he is doing.** `check_health`
reports uptime, processor temperature, and the drive base's battery and board
heat. The facts block already carried the date, addresses and SoC temperature
into every turn, so the tool covers only what was actually missing — and a
missing battery reading is itself an answer, because it nearly always means the
wheels are switched off rather than anything being broken. Battery is spoken as
a rough level beside the voltage: a 10S pack sags under load, so a percentage
would be false precision. No matcher pattern for it on purpose — "how are you
feeling?" deserves warmth, and a regex hands back the same flat recitation
every time.

**Also cleared (2026-08-14): long lists have somewhere to go.** Paging on the
chest, in the animation grid and the servo list. *(Corrected 2026-08-16: this
said "ui.Pager". There is no such class and no `ui.py` — the numpy menu it
belonged to was retired on 2026-08-16, and what survives is the same rule
written twice, in `page_display.py` and `page_servos.py`. Both behave as
described; if a third list ever needs paging, that is the moment to extract
one.)* The grid used to divide its space by
however many presets there were, shrinking buttons to 33px at twelve; the servo
list drew at a fixed pitch with no bound, so the eighth servo was drawn off the
panel entirely and six wired servos were all that hid it. The pager draws
nothing when there is one page, and clamps itself when a poll shrinks the list
underneath it.

**Also cleared (2026-08-15): expression as an action.** `set_chest_display`
and `play_sound`, both over APIs that already existed. Each enumerates what is
actually available when asked for something that is not, because the animation
list lives on the chest Pi and a copy here would go stale.

Worth knowing for the kid-height item below: **the chest screen is already the
turn-taking signal.** `voice-hud` is what it normally shows and it draws
LISTENING / THINKING / SPEAKING from `voice_state.py` — so that item is not
"build it" but "make it big enough to read across a hall". The renderer that
runs is a shader in the panel app (since 2026-09-10; the native C one it
replaced is gone). Event mode now raises that display and stops Claude
swapping it for a flourish.

Also worth knowing: **there is almost nothing Claude can play.** *(Corrected
2026-08-16: the terminator clips are not missing — eight are in
`sounds/terminator/`, uploaded 2026-08-09.)* The real limit is that
`sound.list()` globs `sounds/*.wav` and does not recurse, so the `play_sound`
tool still offers only the three utility clips (`ok`, `startup`, `test`); the
terminator folder is reachable only through terminator mode's
`play_random()`. Letting the tool see the subfolder is a small change and the
cheapest expressiveness available.

**Also cleared (2026-08-15): the speech model, on cost.** He runs
`vosk-model-en-us-0.22-lgraph` now. `voice.asr_model` picks it by directory
name, so changing it is a settings edit and reverting is the same edit
backwards; `tools/bench_asr.py` measures any pair. On this machine: 0.15x real
time for the small model against 0.80x for this one — 5.4x the CPU, still under
real time, and the factor barely moves with the wide camera stopped, so the
cost is the model's own rather than contention. Above 1.0x he would fall behind
the microphone and stop hearing people, which is worse than mishearing, so that
headroom is the thing to watch.

**But the question the list asked is still open.** The decision was made on cost
plus one accuracy win on clean synthetic speech ("my sure" -> "my shirt"). The
case that actually fails is a child at three feet in a hall of four hundred, and
nothing here can synthesise it. **Cleared on 2026-08-16: the collector exists.**
Every utterance that reaches the brain lands in `logs/heard.jsonl` with the
route it took — matched / claude / local / error — plus whether event mode was
on, reviewed on the admin page's Heard tab (misses-only by default) or pulled
whole as JSONL. What the matcher did not recognise after a fair IS the tuning
set; feed the suspicious ones back through `tools/bench_asr.py`. Deliberately
not logged: anything that did not pass the wake word — the mic hears a whole
room, and bystander conversation is surveillance, not tuning data. The known
cost: a *misheard wake word* never appears, because it was dropped before
anything could log it. And note both models hear "servos" as "servers": that is
vocabulary, not size, and no larger download fixes it.

### Finish first

Nothing. The two items that were here — driving the cart from the hand
controller, and what releasing the deadman should do — are settled; see the note
above.

### Waiting on somebody standing at the robot

Not priorities — just the things no session can close by itself, collected in
one place because they are otherwise scattered through the notes above.

- **Releasing the deadman on the real base**, with a real thumb on R1. The
  behaviour is decided and covered in `tools/test_cart_driver.py`; what is
  missing is watching the wheels actually stop.
- **A phone joining the `fred` access point**, to exercise the guest-to-internet
  rule end to end. *(The other half of this is now done: the AP was confirmed
  coming up by itself across a real cold boot on 2026-08-16, with all three
  "AP guests" NAT rules present. Only the guest side is unwatched.)*
- **Face tracking with a real face in front of it** — see the facial-tracking
  section; nothing has ever been tuned and saved.
- **A child at three feet in a hall of four hundred**, which is the speech case
  the model decision was never able to test. `logs/heard.jsonl` is collecting
  the evidence now; feed the suspicious lines through `tools/bench_asr.py`.
  *(2026-08-19: `tools/wake_audit.py` now does the wake-word half of this
  automatically. And there is most of a core spare at idle that there wasn't
  before, so a larger model is affordable in a way it previously wasn't — which
  is the actual answer to this case.)*

### Built 2026-09-13, untested by a person — the test phase

Four things were built remotely with the boot chime off and nobody at the
robot. Each is unit-tested and deployed; what is missing is a finger and an
eye. Ten minutes, in this order:

- **Tap the chest screen** anywhere but the cog corner while an animation is
  showing. Expect: the next look, and a toast naming it at the bottom for a
  second and a half. Tap again through the ring (reactor, copper, flux, face,
  voice HUD, face-talk). *Turn event mode on from the panel and tap again:*
  expect no change and a toast saying `SAY "FRED" TO TALK TO ME`.
- **Swipe up** on the animation. Expect the FRED card: title, the three-machine
  paragraph, how to talk to him. Tap the backdrop or X to close; leave it and
  it closes itself after 45 s. **DRAW** opens the doodle page — draw with a
  finger, it clears after 20 s idle, DONE returns. (Drop a build photo at
  `/home/dietpi/display/about.png` and it appears on the right of the card.)
  Check the cog corner still opens the PIN pad and nothing else does.
- **Attract mode.** Pick `Attract (cycle looks)` from the head panel's display
  dropdown (or the chest DISPLAY tab). Expect the looks to turn over once a
  minute. Tap during it: the look advances and it *stays* in attract. Walk
  away for five minutes with the stomach sensor plugged in: the screen goes
  black; walk back and it wakes on the PIR, or on a touch. Unplug the sensor
  node: it must never sleep. Pick any plain look to leave the mode.
- **Vision after a head move.** With Claude up: "Fred, look at me, then turn
  your head left and tell me what you see now." Expect two different
  descriptions (before, the second was the first frame re-sent). And with a
  panel open on the camera and tracking off, the NUC's `top` should show the
  brain process a few percent lighter than it did with the relay decoding
  every frame.
- **Still from earlier sessions:** one look at the dance (V2), a by-ear pass
  on the "Hmm..." line and its 1.5 s threshold (V1), the lead-in cut with sound
  on (V3), "Fred, stop" over a long reply and Capture forward for the DOA
  offset (R9).

### Local model: benched 2026-09-13, the 3B stays

`tools/bench_local_models.py` times candidates with FRED's real prompt (3.5k
tokens of system + 22 tool schemas) on the NUC's iGPU, six event questions
each. Settled, so it stops being re-asked:

| model | first word p50 | worst | tok/s | answer p50 | verdict |
|---|---|---|---|---|---|
| qwen2.5:3b (current) | 0.4 s | 1.1 s (tool call) | 23 | 1.2 s | keep |
| qwen2.5:7b | 0.6 s | 2.2 s (tool call) | 11 | 2.9 s | answers take twice as long; the tool call misses the 1.5 s earcon |
| qwen3:4b | 17–23 s | — | 17 | 24 s | thinks silently for twenty seconds; unusable without `think: false` |
| llama3.1:8b | 2.2 s | 5.9 s | 9.5 | 2.3 s | invents tools (`speak`, `read_facts`, `tell_weather`), calls the camera for "why is the sky blue" |

The prefix costs ~0.2–0.4 s a turn on Vulkan whatever its size, so the 28 s
stall the brain's notes describe is a CPU-fallback figure, not a live one.
AirLLM-style layer streaming is the wrong direction entirely: it trades
seconds-per-token for fitting models this machine does not need to stream
(30 GB RAM, the 3B uses 2 GB). The quality ceiling at events is still the
speech recognition, not the model. Both larger models are still pulled
(`ollama rm qwen2.5:7b llama3.1:8b` frees 9.6 GB if wanted).

## Where to go next (proposed 2026-08-12)

Ideas, not commitments — nothing here has been agreed. Ordered by what would
change the most for the least work. The STEM list further down still stands;
this is what is *not* already on it.

### Driving him when you are not next to him

1. **One-tap operator actions on the phone layout.** The panel itself became a
   phone layout on 2026-08-16 — a bottom tab bar, the transcript filling the
   Chat tab, the estop one tap away on Drive — so the "page shaped like a
   phone" half of this is done and lives at the same URL. What remains is the
   actions the transcript tab should carry. Checked one by one on 2026-08-16:
   - **Stop speaking — missing, and it is the one that matters.** `/api/sound/stop`
     exists with no caller anywhere in the UI. See the STOP item below.
   - **Mute the mic — effectively there already.** The 🎤 Listen button on the
     chat tab stops the wake-word listener and frees the mic. It is a listener
     toggle rather than a labelled mute, which may be enough.
   - **Reset the conversation — missing, and now load-bearing.** He grew a
     memory (STEM priority 1); 🗑 clears only the transcript *display*, so the
     next visitor inherits the last one's context. `Brain.clear_history()` is
     right there with no endpoint in front of it.
   - **Volume — missing entirely.** There is no volume control on this robot at
     all; see the STOP + volume item below.

2. **Cleared 2026-08-16: the deck of one-tap things to say.** On the web
   panel's chat: a Quick say strip above the input — tabs (Crowd, Stalling,
   About me, Manners to start), one tap speaks through `/api/say`, ✎ Edit adds
   and removes lines, and the deck lives in `config/phrases.json` on the brain
   so every browser sees the same one. `?view=talk&deck=1` is the bookmark for
   the event phone. Deck lines land in the transcript tagged `deck`, so the
   operator sees them fire. Web only for now — the touchscreen was judged the
   wrong surface while an operator is holding a phone.

### Other

Nothing — the failed-understanding log was the last item here; see the cleared
note above.

## STEM event readiness (planned 2026-07-08)

FRED will be shown at STEM events with students walking up and asking questions.
Items below came from a full app review that day; priority order set by Ryan.
Baseline context: speech is pipelined (brain streams sentences → warm piper
daemon → lip-synced jaw + on-screen face from the same envelope), temp sensor is
wired into the brain, tool failures no longer kill a turn.

### ~~Priority 1 — conversation memory (follow-ups)~~ DONE (verified 2026-08-16)
Built, and the method the item named no longer exists: it is `Brain._ask_llm`,
which sends `self._history + [the new turn]`. Six exchanges
(`HISTORY_MAX_EXCHANGES`), dropped after three idle minutes
(`HISTORY_IDLE_SECS`) so the next visitor starts fresh, and dropped on a spoken
"new conversation" (`_NEW_CONV`). `_remember()` stores only the final user and
assistant text, never the tool_use/tool_result blocks, exactly as this item
asked. So "who was Einstein?" → "when was he born?" works today.

**But nothing on the panel can clear it.** `Brain.clear_history()` has exactly
one caller — the spoken reset. The chat tab's 🗑 empties the *display* ring
(`/api/log/clear`) and leaves the brain's memory untouched, so an operator who
wipes the transcript between visitors has not actually given the next one a
fresh start. That is the "reset the conversation" button in the operator-actions
item above, and conversation memory is what makes it matter.

### Priority 2 — auto-greeting when someone walks up (toggleable) — MOSTLY BUILT
`inmoov/greeter.py` exists and does most of this: canned `GREETINGS`, a cooldown
so a crowd is not re-greeted, never speaks over himself (checks speaking and
thinking), speaks off-thread, and is a live
settings toggle on the admin page. The setting is **`greet.enabled` /
`greet.cooldown`**, not the `voice.auto_greet` guessed here, and it is **off on
this robot today**. Its state is in `/api/state` under `greet`.

What is left of the original item:
- **The trigger is the stomach sensor's `approach` event, not a face.** The face
  tracker's `event_cb` goes to the log and nowhere else. Whether that matters is
  a bench question — the sensor may well be the better signal for "someone is
  standing in front of me" — but it is not what this item asked for, and it means
  he greets a passing chair as readily as a child.
- **No idle gate.** There is a 90 s cooldown but nothing checking "idle > 2 min",
  so he can greet someone he was mid-conversation with a minute ago.
- Greeting lines are not pre-rendered; the 64-entry TTS cache makes repeats
  cheap, but the *first* one of the day still waits for piper.

### ~~Priority 3 — thinking earcon~~ BUILT 2026-09-10 (review item V1)
At 2–4 s to first word, kids in a loud hall assume he didn't hear and repeat
themselves. Built as designed, with one change of hook point: the brain still
imports no sound module. `Brain.respond` takes an `on_thinking` callback, fired
once when the matcher misses and a model gets the turn (never for a matched
command, the conversation reset, or the no-brain apology). The assistant hangs
a clock off it, and the *speaker thread* — already blocked waiting for the
first sentence — says a cached "Hmm..." (0.44 s from piper, pre-rendered at
boot by `warm_earcon`) if that wait outlasts `voice.earcon_after` (default
1.5 s; 0 turns it off). The threshold is the measured line between backends: a
plain Claude turn is audible at ~1.1 s and gets no earcon, the local model, a
tool call or a look at the camera run over it and do. Once per turn, never
after a barge-in, and he stays in THINKING (not SPEAKING) on the HUD through
it. The answer that follows is padded as a continuation clip, since the earcon
already opened the device.

The second half of the same finding is also in: the first Claude failure of an
outage now says "My internet brain is out of reach, so bear with me." before
the local answer, once — a cloud success clears `_cloud_failed_at`, so the
retry that fails after the 60 s sulk is the same outage and stays quiet, and
the next outage after a success is announced again. The line rides the reply
for the transcript but is kept out of conversation memory.

`tools/test_thinking_earcon.py` and the V1 checks in
`tools/test_brain_fallback.py` pin all of the above. Still by-ear: how "Hmm..."
sounds in the room with the array's lead-in, and whether 1.5 s is the right
number against real Claude latency on venue WiFi — `earcon_after` is in
settings for exactly that. The LED/eye pulse bonus was not done.

### Priority 4 — "show yourself off" demo routine
One voice command + web button that runs a scripted showcase: eyes sweep, head
turns, terminator blip, while he narrates his own anatomy ("four servos, a
Raspberry Pi brain, I hear with an offline speech model, think with Claude,
speak with a neural voice"). Teachers will ask for this constantly.
- Script = list of (speak, move) steps; reuse `speak_stream` + servo calls.
- Narration lines are fixed → TTS cache makes the whole routine start instantly.
- Local command ("introduce yourself", "show off") + Claude tool + panel button.
- Confirmed entirely absent on 2026-08-16 — no matcher pattern, no tool in the
  14-entry `CLAUDE_TOOLS`, no button. (`demo.py` at the repo root is an old
  servo script, unrelated, and easy to mistake for a start on this.)

### Priority 5 — offline fun-fact fallback
If venue WiFi dies, every open question becomes "my AI brain isn't connected."
- Local bank of canned STEM/robot facts; "tell me a (fun|robot) fact" served
  offline via the local matcher (`commands.py:_PATTERNS`).
- When Claude is unreachable, fall back to something friendlier that points at
  what still works: "My internet brain is down, but ask me for a robot fact!"
- Confirmed open on 2026-08-16. There is no fact bank, and the two dead ends are
  still the flat ones — "Sorry, I can't answer that — my A.I. brain isn't
  connected yet." and "I'm having trouble reaching my brain right now." Worth
  knowing that the `auto` backend already falls back to Ollama before either of
  those is reached, so this is the *last* resort, not the first.

### Then — the rest (rough order)
- **Event persona / audience awareness — half built.** The toggle this item
  asked for exists (`event.enabled`, see the correction at the top) and it
  already changes the prompt, but only to say "you are at a public event, answer
  in one short sentence". Nothing tells him he is talking to *students*: no
  age-appropriate register, no deflecting mischief with humour. That is a
  paragraph in the event-mode prompt, not new machinery.
- ~~**Panel auth**~~ — done 2026-08-12: a 4-digit PIN gates the settings and
  everything that moves him; `/api/say` is deliberately still open, and the
  cart's STOP always is. See the note at the top of this file.
- **Big STOP + volume control:** *Corrected 2026-08-19 — half of this item was
  built on a false premise, and the other half is still true.*
  - **The half-duplex claim was wrong.** This said voice-"stop" mid-speech
    "remains impossible (half-duplex card — the mic is off while he talks)".
    The Anker PowerConf A3301 is full duplex: capture and playback are separate
    USB interfaces (`/proc/asound/card0/stream0`, endpoints 2 OUT / 3 IN), both
    report `Running` together indefinitely, and a capture taken straight through
    six seconds of continuous playback came back full-length and gap-free. It
    also cancels its own output — a nine-word phrase no room would produce,
    played and transcribed with gain applied, returned not one of those words
    across three trials; correlation puts the echo near -43 dB. The mic stays
    open through his replies now, and saying his name over him stops him.
  - **Two stop endpoints now exist, and there is still no button.** Alongside
    `/api/sound/stop` there is `POST /api/voice {"interrupt": true}`, which also
    unwinds the reply rather than only killing the current clip. Both still have
    **zero callers in the UI**. Unchanged as the cheapest item here.
  - **Volume is still missing entirely** (card pinned at 100% via amixer) — add
    an amixer slider in admin + "quieter/louder" local commands. `voice.gain` is
    *microphone* gain, not output; it is not this.
- **Visitor kiosk view:** read-only fullscreen `/kiosk` route — big face, live
  captions of heard/said (noisy rooms + accessibility), "Say 'Fred'…"
  prompt. Face SVG, envelope animation, transcript polling all exist already.
  Confirmed 2026-08-16: no such route, and the word "kiosk" appears nowhere in
  the repo but this line. The pieces really are all there to assemble.
- **Persist the transcript:** `convlog.py` is a 300-entry in-memory ring —
  everything asked evaporates on restart. Append to a dated `.jsonl`; post-event
  review of real student questions + a closing stat ("answered 214 questions").
  Still true on 2026-08-16. Do not mistake `logs/heard.jsonl` for this: that is
  the ASR-miss log, one row per *utterance* with the route it took, and it is
  now the thing whose directory the `.gitignore` fix above actually covers —
  which is worth thinking about before writing full transcripts beside it.
- **Wake-word noise robustness:** *Partly cleared 2026-08-19.* `"friend"` is
  gone, and so is `"bread"`, which had been added the same day chasing a
  mishearing and was the same mistake. The bar is now both halves — it has to
  sound like his name *and* a room must not say it by accident — and the second
  half is measured by `tools/wake_audit.py` against the model's own 368k
  lexicon rather than judged. friend and bread have four derived forms each;
  fred, alfred, frayed and fraud have one or none.
  Still open, and worth knowing:
  - **Event mode still carries no strictness knob.** Now that a grammar-restricted
    detector exists (`BARGE_GRAMMAR`, `NAME_MIN_PARTIALS`), the obvious form is
    to raise the persistence threshold and require the name at the *start* of an
    utterance when `event.enabled`.
  - **Push-to-talk still does not exist** — the chest VOICE page is a latching
    on/off, not a hold.
  - **A caution for whoever tunes this next.** `logs/heard.jsonl` cannot give
    you a false-wake rate: a correctly-ignored sentence is never logged, so
    there is no denominator, and a false wake is logged with the word that
    caused it already stripped off by `_strip_wake`. The file is therefore
    systematically biased toward *keeping* ordinary words — which is exactly the
    error that put "bread" in the list. `wake_audit.py` says so on every run.
- **Thermal:** `get_throttled` already shows `0x80000` (soft temp limit hit on a
  desk). Buy a fan/heatsink before enclosing in the head shell. Software side:
  have FRED say "I'm running a bit hot" when `throttled_now` flips. Still open —
  `/api/health` decodes the throttle bits but only the browser reads them.
  `check_health` says "running hot" above 80 °C, which is close but backwards:
  it answers when *asked*, and this item is about him volunteering it.
- **Rate limiting:** no throttle on `/api/command`; combined with no-auth, one
  bored kid with a phone = denial-of-Fred. Cheap token bucket.

## MyRobotLab — RETIRED 2026-09-09

MRL was installed, ran headless as a service, and could drive the PCA9685 and
consume the camera stream — all of it worked, none of it was being used, and
the hardware-handoff machinery it required put a suspend/resume path through
every shared device plus a 15-endpoint guard through the web app. Removed
whole: the myrobotlab.service unit, the /api/handoff endpoint, the handoff
section of the admin panel, and suspend/resume on Sound, Camera and both servo
controllers. The MJPEG camera streamer stays — the brain consumes it — and the
SERVO_LOCKED eye-cable protection stays, because it guards a cable, not MRL.
If MRL ever comes back, the git history up to this date has the working setup,
including the WiringPi fix and the I2C coexistence notes.

## Facial tracking (Pi Camera 3 → eye/neck servos)

**BUILT 2026-07-04 — needs bench tuning with a real face.** The detector, control
loop, API, and live UI are all in place (see "Implemented" below). What remains
is sitting in front of the camera and dialling in the gains/inverts.

*Still true on 2026-08-16, and now provable: `track` in `config/settings.json` is
`{}`. `POST /api/track` writes the whole tuning set there the first time anything
is changed, so an empty dict means no value has ever been dialled in and saved —
this robot is running FaceTracker's built-in defaults. Nobody has sat in front of
it yet.*

### Implemented (2026-07-04)
- `inmoov/face_tracker.py`: `FaceTracker` — background thread pulls a lores
  grayscale frame from the shared `Camera`, Haar-detects the largest face, and a
  P-controller nudges `eye_x`/`eye_y` toward it (feedback: camera rides the head,
  so error shrinks as it turns). Neck follows when an eye saturates near a limit,
  then the eye eases back toward rest. Deadzone + interruptible pacing (12 fps).
- `inmoov/camera.py`: added a **lores YUV420 stream** + `acquire()`/`release()`
  (hold the sensor open for a non-streaming consumer) + `capture_gray()` (Y-plane
  = grayscale). The status LED still tracks MJPEG viewers only.
- `web/app.py`: `GET /api/track` (status) + `POST /api/track {on, <tuning>}` —
  start/stop and **live-tune** gain_x/gain_y/invert_x/invert_y/invert_neck/
  deadzone/neck_gain/sat_margin/eye_recenter/fps without a restart. Also in
  `/api/state` under `track`.
- `web/templates/index.html`: the "◎ Track face" toggle is now wired to the real
  endpoint; while armed it polls `/api/track` and snaps the reticle onto the
  detected face (position + size), labelled "◉ Locked · N fps" / "◎ Searching…".

### Bench tuning checklist (do with a face in view)
1. Start tracking; confirm the reticle locks onto your face (proves detection +
   the capture path — verified structurally, but not yet against a real face).
2. If an axis moves **away** from you, flip that axis's invert via
   `POST /api/track {"invert_x": true}` (or invert_y / invert_neck). Live, no restart.
3. Tune `gain_x`/`gain_y` for snappy-but-stable (start 6/5); raise `deadzone` if it
   jitters at centre; tune `neck_gain`/`sat_margin` for how eagerly the neck helps.
4. **Note:** direction signs are relative to the *displayed* image — toggling the
   camera 180° flip inverts them, so tune with the flip in its normal state.

### Original investigation notes (2026-07-03)

### What's already in place
- **Camera:** Sony `imx708` (Pi Camera Module 3, autofocus) enumerates via
  `rpicam-hello --list-cameras` and `picamera2` works in `venv/`. Modes up to
  4608x2592; 1536x864@30fps is plenty. `inmoov/camera.py` already owns the sensor
  (lazy start/stop, MJPEG broker, focus control) — a tracker can pull frames from
  a second **lores** stream without disturbing the MJPEG viewers.
- **Actuators:** `eye_x`, `eye_y` (fine, fast) and `neck` (coarse) are already in
  `config/servos.json` and driven by `inmoov.servo_controller.ServoController`.
- **Compute:** Pi 4B, 4 cores / 1.8 GB. A Haar/DNN detector on a small frame
  (~320x240) runs comfortably at ~10–15 fps — enough for smooth tracking.

### Environment — PREPPED (done 2026-07-03)
- Installed `python3-opencv` + `opencv-data` via apt; visible in `venv/` through
  system-site-packages. Verified: `venv/bin/python -c "import cv2"` → **cv2 4.10.0**
  (numpy 2.2.4), cascade loads, `detectMultiScale` runs clean.
- **Haar cascade path (Debian):** `/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml`.
  Note Debian's `python3-opencv` does **not** ship the `cv2.data` helper (that's a
  pip-wheel-ism) — load the cascade by that absolute path, not `cv2.data.haarcascades`.
- MediaPipe Face Detection remains an option (heavier; arm64/py3.13 wheels dicier).
  Start with OpenCV Haar; upgrade to the OpenCV DNN face detector if robustness needs it.

### UI — mockup in place (done 2026-07-03)
- Added a **"◎ Track face"** toggle to the camera panel header in
  `web/templates/index.html` (enabled only when a camera is detected). Arming it
  highlights the button, auto-starts the feed, and overlays a labelled
  **"◉ Tracking · mock"** reticle on the video. **Deliberately UI-only** — not wired
  to any detector or the servos (`toggleTrack()` toasts "UI mockup only, not wired
  up yet"; tracking clears when the camera stops). This is the front-end stub the
  real loop below will hook into.

### Suggested implementation
1. ~~Install OpenCV~~ **done** (apt `python3-opencv` + `opencv-data`; see above).
2. New `inmoov/face_tracker.py`: background thread that
   - requests a lores YUV/RGB stream from the shared `Camera` (add a
     `capture_array`-style hook so it doesn't fight the MJPEG encoder),
   - runs the detector each frame, picks the largest/most-central face,
   - computes the face-center offset from frame center (normalised -1..1),
   - **P-controller** (with deadzone + smoothing/EMA) nudges `eye_x`/`eye_y`;
     when the eyes saturate near their soft limits, let `neck` follow to
     re-center, then relax the eyes toward center. Clamp via existing safe limits.
3. Web panel: the **Track face** toggle already exists as a UI mockup — wire it to
   a real `POST /api/track {on: bool}` in `web/app.py` that starts/stops the thread,
   and drop the "mock" label / `toggleTrack()` placeholder toast once it's live.
4. Tune loop rate, gain, and deadzone on the bench; hold last position when no
   face is seen for N frames rather than snapping to center.

### Notes / risks
- Do all of this **after** the hardware reboot + PCA9685 wiring (see README) so
  the servos actually move; until then it runs in mock mode (prints intended moves).
- Keep the tracker's frame rate modest to leave headroom for the MJPEG stream.
- Manual focus is the current default (continuous AF hunts on this rig); a fixed
  focus at typical face distance (~0.5 m) is fine for detection.

# InMoov TODO

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
turn-taking signal.** `voice-hud-c` is what it normally shows and it draws
LISTENING / THINKING / SPEAKING from `voice_state.py` — so that item is not
"build it" but "make it big enough to read across a hall", and the renderer
that actually runs is the C one. Event mode now raises that display and stops
Claude swapping it for a flourish.

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
thinking), respects the hardware handoff, speaks off-thread, and is a live
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

### Priority 3 — thinking earcon
At 2–4 s to first word, kids in a loud hall assume he didn't hear and repeat
themselves. The moment a Claude-path question is accepted (local matcher missed,
before the API call), play a cached "Hmm…" / soft robot chirp — cache hit is
~0 s. Bonus: pulse the eyes/LED while thinking. Hook point: `Brain.respond`
just before `_ask_llm` (renamed since this was written), or in
`Assistant.converse` keyed off the source. Still entirely absent as of
2026-08-16 — `brain.py` imports no sound module at all. Note the *visual* half
already exists: `Assistant._thinking` drives the chest HUD's THINKING state, so
this is the audible half of a signal the robot is already giving.

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
- **Big STOP + volume control:** `/api/sound/stop` exists and — confirmed
  2026-08-16 — has **zero callers in the UI**. Not "isn't prominent": there is
  no button anywhere on the panel that stops him talking, on any tab, so the
  only way to shut him up mid-sentence is curl. That is the cheapest item on
  this list. Voice-"stop" mid-speech remains impossible (half-duplex card — the
  mic is off while he talks). No volume control exists anywhere (card pinned at
  100% via amixer) — add an amixer slider in admin + "quieter/louder" local
  commands. Note `voice.gain` is *microphone* gain, not output; it is not this.
- **Visitor kiosk view:** read-only fullscreen `/kiosk` route — big face, live
  captions of heard/said (noisy rooms + accessibility), "Say 'Hey Fred'…"
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
- **Wake-word noise robustness:** `WAKE_WORDS` still includes `"friend"`
  (now `listener.py:50` — the old cite drifted) — crowd chatter saying
  "my friend…" triggers him. Matching is single-token, so nothing requires the
  "hey" at all, and event mode carries no strictness knob to add one. Push-to-talk
  does not exist either: the chest VOICE page is a latching on/off, not a hold.
- **Thermal:** `get_throttled` already shows `0x80000` (soft temp limit hit on a
  desk). Buy a fan/heatsink before enclosing in the head shell. Software side:
  have FRED say "I'm running a bit hot" when `throttled_now` flips. Still open —
  `/api/health` decodes the throttle bits but only the browser reads them.
  `check_health` says "running hot" above 80 °C, which is close but backwards:
  it answers when *asked*, and this item is about him volunteering it.
- **Rate limiting:** no throttle on `/api/command`; combined with no-auth, one
  bored kid with a phone = denial-of-Fred. Cheap token bucket.

## MyRobotLab install (headless service)

Install [MyRobotLab](https://myrobotlab.org/) and run it headless as a service on
the Pi with its web interface (WebGui) reachable on the LAN, started on boot.

**DONE 2026-07-10** — MRL Nixie 1.1.1611 installed and running headless as a
systemd service (WebGui on 8888, auto-starts on boot), the hardware handoff toggle
is built, **and** MRL can now drive the PCA9685 over the Pi's native I2C (fixed
the Pi4J/WiringPi `UnsatisfiedLinkError` by building + installing WiringPi 3.18 —
Pi4J 1.4 dynamically links a *system* `libwiringPi.so`). Verified end-to-end:
`RasPi` + `Adafruit16CServoDriver` attach on bus 1/0x40 and init the chip. Write-
ups in `SERVICE.md` ("MyRobotLab service" incl. "the WiringPi fix" + "Hardware
handoff toggle"). Remaining MRL work is InMoov-side config (attach Servo services
to channels, save the MRL config), not infra.

**Camera into MRL — DONE 2026-07-10.** MRL's OpenCV can't grab the imx708 directly
(it's libcamera/CSI; MRL's `Webcam`/v4l4j has no arm64 native, and the legacy
`bcm2835-v4l2` stack doesn't support Camera Module 3). Solution: a standalone
libcamera→MJPEG streamer (`deploy/camera_stream.py` + `camera-stream.service`, on
:8081) that MRL's built-in `MJpegFrameGrabber` consumes. Verified: `cv` OpenCV
service captures live frames from the imx708. MRL's OpenCV native itself works on
arm64 once `libunicap2` is installed. (Considered a custom MRL "Libcamera" plugin
— rejected; MRL already ships an MJPEG grabber and libcamera has no Java binding.)
The camera path is independent of the I2C handoff, so MRL can run camera + servos
together with the InMoov app stopped.
- ~~Install MRL (Java runtime + the MRL distribution); confirm it launches on
  this Pi 4B / arm64 and note the pinned version.~~ OpenJDK 21 + MRL 1.1.1611.
- ~~Run **headless** with the **WebGui** service enabled; pick a port and confirm
  it doesn't clash with our Flask panel.~~ WebGui on 8888 (panel is 8080).
- ~~Wrap it in a **systemd unit** so it auto-restarts and **starts on boot**.~~
  `deploy/myrobotlab.service`, enabled. (Reboot-survival not yet re-verified —
  see the note in SERVICE.md.)
- ~~Coexistence: separate service, separate port + a *hardware* handoff.~~ Done.
- ~~**Hardware handoff toggle in our app** — release the I2C bus (PCA9685), the
  audio card, and the camera so MyRobotLab can drive them; re-acquire on toggle
  off.~~ Built: admin-panel toggle → `POST /api/handoff {release}`. Each device
  got `suspend()`/`resume()` (ServoController relaxes + deinits the PCA9685 and
  drops the I2C handle; Sound stops+blocks playback; Camera force-stops the
  sensor and reports unavailable). The coordinator also stops the voice listener
  (frees the mic) and the face tracker (frees the sensor), and hardware-actuating
  endpoints return 409 while released. The choice persists in `settings.json`
  (`hardware.released`) and is applied at boot. Verified live on the real Pi:
  release → all suspended + 409s + camera 503; resume → servos back to rest;
  boot-into-released comes up without grabbing anything.
  - *Possible follow-up:* have the toggle also **start/stop the `myrobotlab`
    systemd service** (via a small sudoers rule), so one switch both frees our
    hardware and boots MRL — today the operator releases here, then starts MRL
    separately. *Still open on 2026-08-16: no sudoers rule for myrobotlab exists
    (`/etc/sudoers.d/` has only the hotspot one and the login shell's), and
    nothing in the app references the service. There is now a worked example to
    copy — `sudoers-fred-hotspot` plus the `fred-ap-config` helper — so this is
    a smaller job than it was when it was written.*

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

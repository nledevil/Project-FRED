# InMoov TODO

## Where things stand after 2026-08-12/13

A long session that touched the brain, the GPU, the chest touchscreen, the cart
and the network. Everything below is either unfinished, worth doing, or worth
knowing before someone changes it.

Verified means "watched it work on the hardware", not "should work".

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

### Finish first

Nothing. The two items that were here — driving the cart from the hand
controller, and what releasing the deadman should do — are settled; see the note
above.

### New features worth considering

1. **A "who am I" page** — the brain now knows it is three computers and who built
   it. The same facts (hostnames, addresses, versions, uptime) on a touchscreen
   page would replace a lot of SSH.

## STEM event readiness (planned 2026-07-08)

FRED will be shown at STEM events with students walking up and asking questions.
Items below came from a full app review that day; priority order set by Ryan.
Baseline context: speech is pipelined (brain streams sentences → warm piper
daemon → lip-synced jaw + on-screen face from the same envelope), temp sensor is
wired into the brain, tool failures no longer kill a turn.

### Priority 1 — conversation memory (follow-ups)
`brain.py:_ask_claude` builds `messages=[{user: text}]` fresh every turn, so
"who was Einstein?" → "when was he born?" fails — FRED doesn't know who "he" is.
- Keep a rolling window of the last ~6 exchanges in `Brain`, replayed into
  `messages` each turn.
- Clear it after a few idle minutes (monotonic timestamp) so the next visitor
  starts fresh; also clear on an explicit "new conversation"/reset.
- Mind the tool loop: history entries should be the *final* user/assistant text
  pairs, not the intermediate tool_use/tool_result blocks (keeps tokens down).

### Priority 2 — auto-greeting when someone walks up (toggleable)
The face tracker already fires `event_cb` on face-detected (`face_tracker.py:245`).
- When idle > ~2 min and a new face appears → speak a greeting ("Hi! I'm Fred —
  ask me anything about robots!").
- Cooldown so he doesn't re-greet the same crowd; don't greet while speaking or
  mid-conversation.
- **Must be a settings toggle** (`voice.auto_greet` or similar) editable live
  from the admin panel — on for events, off at home.
- Greeting lines are fixed strings → pre-render via the TTS cache so they play
  instantly.

### Priority 3 — thinking earcon
At 2–4 s to first word, kids in a loud hall assume he didn't hear and repeat
themselves. The moment a Claude-path question is accepted (local matcher missed,
before the API call), play a cached "Hmm…" / soft robot chirp — cache hit is
~0 s. Bonus: pulse the eyes/LED while thinking. Hook point: `Brain.respond`
just before `_ask_claude`, or in `Assistant.converse` keyed off the source.

### Priority 4 — "show yourself off" demo routine
One voice command + web button that runs a scripted showcase: eyes sweep, head
turns, terminator blip, while he narrates his own anatomy ("four servos, a
Raspberry Pi brain, I hear with an offline speech model, think with Claude,
speak with a neural voice"). Teachers will ask for this constantly.
- Script = list of (speak, move) steps; reuse `speak_stream` + servo calls.
- Narration lines are fixed → TTS cache makes the whole routine start instantly.
- Local command ("introduce yourself", "show off") + Claude tool + panel button.

### Priority 5 — offline fun-fact fallback
If venue WiFi dies, every open question becomes "my AI brain isn't connected."
- Local bank of canned STEM/robot facts; "tell me a (fun|robot) fact" served
  offline via the local matcher (`commands.py:_PATTERNS`).
- When Claude is unreachable, fall back to something friendlier that points at
  what still works: "My internet brain is down, but ask me for a robot fact!"

### Then — the rest (rough order)
- **Event persona / audience awareness:** system prompt knows it's talking to
  students; age-appropriate, deflect mischief with humor. Settings toggle
  ("event mode") so home personality is unchanged.
- ~~**Panel auth**~~ — done 2026-08-12: a 4-digit PIN gates the settings and
  everything that moves him; `/api/say` is deliberately still open, and the
  cart's STOP always is. See the note at the top of this file.
- **Big STOP + volume control:** `/api/sound/stop` exists but isn't prominent;
  voice-"stop" mid-speech is impossible (half-duplex card — mic is off while he
  talks). No volume control exists anywhere (card pinned at 100% via amixer) —
  add an amixer slider in admin + "quieter/louder" local commands.
- **Visitor kiosk view:** read-only fullscreen `/kiosk` route — big face, live
  captions of heard/said (noisy rooms + accessibility), "Say 'Hey Fred'…"
  prompt. Face SVG, envelope animation, transcript polling all exist already.
- **Persist the transcript:** `convlog.py` is a 300-entry in-memory ring —
  everything asked evaporates on restart. Append to a dated `.jsonl`; post-event
  review of real student questions + a closing stat ("answered 214 questions").
- **Wake-word noise robustness:** `WAKE_WORDS` includes `"friend"`
  (`listener.py:37`) — crowd chatter saying "my friend…" triggers him. Event
  setting requiring the two-word "hey fred" form; push-to-talk button as backup.
- **Thermal:** `get_throttled` already shows `0x80000` (soft temp limit hit on a
  desk). Buy a fan/heatsink before enclosing in the head shell. Software side:
  have FRED say "I'm running a bit hot" when `throttled_now` flips.
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
    separately.

## Facial tracking (Pi Camera 3 → eye/neck servos)

**BUILT 2026-07-04 — needs bench tuning with a real face.** The detector, control
loop, API, and live UI are all in place (see "Implemented" below). What remains
is sitting in front of the camera and dialling in the gains/inverts.

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

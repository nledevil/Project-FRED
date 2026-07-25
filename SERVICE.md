# InMoov systemd service

The web control panel (`web/app.py`, Flask on port **8080**) runs as a systemd
service so it starts automatically at boot and can be managed like any other
service.

- **Unit file:** `/etc/systemd/system/inmoov.service`
- **Runs as:** user `dietpi`, working dir `/home/dietpi/inmoov`
- **Command:** `venv/bin/python web/app.py`
- **Auto-start at boot:** enabled (`WantedBy=multi-user.target`)
- **Auto-restart:** on failure, after 5s
- **URL:** http://<pi-ip>:8080  (IP drifts via DHCP — was 192.168.68.74)

## Managing it

```bash
sudo systemctl start inmoov       # start
sudo systemctl stop inmoov        # stop
sudo systemctl restart inmoov     # restart (do this after code/config changes)
sudo systemctl status inmoov      # is it running?
sudo systemctl enable inmoov      # start at boot (already enabled)
sudo systemctl disable inmoov     # don't start at boot
```

## Logs

`PYTHONUNBUFFERED=1` is set in the unit, so Flask output (including the
`mode: LIVE / MOCK` line) streams to the journal:

```bash
sudo journalctl -u inmoov -f            # follow live
sudo journalctl -u inmoov --since "10 min ago"
```

## Making changes

The service runs the code straight from this working tree, so after editing
`web/app.py`, `config/servos.json`, or anything else the app imports:

```bash
sudo systemctl restart inmoov
```

There is no separate build step. If you edit the unit file itself, run
`sudo systemctl daemon-reload` before restarting.

## Notes / gotchas

- The service holds port 8080. To run the app manually for debugging, stop the
  service first (`sudo systemctl stop inmoov`) or you'll get an address-in-use
  error.
- The unit file lives outside this git repo (in `/etc/systemd/system/`), so it
  is not version-controlled here. A copy of its contents is kept in this repo at
  `deploy/inmoov.service` for reference/reinstall.
- Reinstall from the repo copy:
  ```bash
  sudo cp deploy/inmoov.service /etc/systemd/system/inmoov.service
  sudo systemctl daemon-reload && sudo systemctl restart inmoov
  ```

# MyRobotLab service (headless, WebGui)

[MyRobotLab](https://myrobotlab.org/) (MRL) runs alongside our own stack as a
second systemd service — the InMoov community's Java robotics framework, headless
(no Swing GUI), with its **WebGui** browser interface on port **8888**. Installed
2026-07-10.

- **Version:** Nixie **1.1.1611** (develop branch build). Java: **OpenJDK 21**
  headless (`openjdk-21-jdk-headless`; MRL needs "Java 11 or newer").
- **Install dir:** `/home/dietpi/mrl/myrobotlab-1.1.1611`, reached via the stable
  symlink `/home/dietpi/mrl/current` (so an upgrade is just a re-point — no unit
  edit). ~2.7 GB in `~/mrl` + ~1.7 GB of resolved deps cached in `~/.ivy2`.
- **Unit file:** `/etc/systemd/system/myrobotlab.service` (repo copy at
  `deploy/myrobotlab.service`). Runs `current/myrobotlab.sh` as `dietpi`,
  `WorkingDirectory=current` (the script sets a *relative* `java.library.path`,
  so cwd must be the install root). Auto-start at boot: **enabled**. Restart on
  failure after 10 s.
- **URL:** http://<pi-ip>:8888  — served by the WebGui service.
- **Footprint:** ~231 MB RSS at idle; JVM heap left at the default ergonomic cap
  (~25% of RAM ≈ 460 MB). Leaves ~900 MB free with the InMoov app also running.

```bash
sudo systemctl {start,stop,restart,status} myrobotlab
sudo journalctl -u myrobotlab -f
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8888/   # 200 when WebGui is up
```

## Notes / gotchas

- **First run is slow (~20 min, one-time).** With no `libraries/repo.json`,
  `myrobotlab.sh` runs `Runtime --install`, resolving+downloading the full
  dependency tree (~1.9 GB) from Maven Central. Once `repo.json` exists,
  subsequent starts skip it and WebGui comes up in ~25 s.
- **Do NOT set `_JAVA_OPTIONS`/`JAVA_TOOL_OPTIONS`** in the unit to force `-Xmx`.
  The JVM prints a `Picked up ...` banner that MRL's `java -version | head -1`
  version check parses, so it mis-reads the version and refuses to start
  ("incompatible version of java"). To cap the heap, append `-Xmx` to
  `JAVA_OPTIONS` *inside* `myrobotlab.sh` instead. (Cost us one failed boot.)
- **Ports:** MRL 8888, InMoov panel 8080, ttyd terminal 7681 — no clash.
- **Hardware coexistence — NOT yet solved.** MRL and our app must not both drive
  the PCA9685/I2C servos, the USB audio card, or the Pi camera at once. Today
  nothing enforces that; the planned **hardware handoff toggle** in the InMoov
  app (see `TODO.md`) is what will release those to MRL on demand. Until then,
  don't arm both stacks against the hardware simultaneously.
- **Reboot check:** the service is `enabled`, so it starts at boot via
  `multi-user.target`. A reboot to confirm boot-start wasn't performed during
  install (it would drop the remote session); do `sudo reboot` when convenient
  and verify `systemctl is-active myrobotlab` + port 8888.
- Reinstall the unit from the repo copy:
  ```bash
  sudo cp deploy/myrobotlab.service /etc/systemd/system/myrobotlab.service
  sudo systemctl daemon-reload && sudo systemctl restart myrobotlab
  ```

## Hardware handoff toggle (InMoov ⇄ MyRobotLab)

The InMoov app and MRL both want the *same* physical hardware — the PCA9685
servos on I2C, the USB audio card, and the Pi camera — and only one process may
drive each at a time. The **admin panel** ("MyRobotLab handoff" panel) has a live
toggle that releases all three from the InMoov app so MRL can take them, and takes
them back on toggle-off. Built 2026-07-10.

- **API:** `GET /api/handoff` → state; `POST /api/handoff {"release": true|false}`.
  `/api/state` also carries a `handoff` block.
- **What "release" does** (each hardware object grew `suspend()`/`resume()`):
  - **Servos** (`ServoController.suspend`): relaxes every servo (cuts pulses),
    `deinit()`s the PCA9685, and drops the I2C handle. `set_angle`/`relax` become
    no-ops. `resume()` re-opens I2C, re-applies ranges, returns to rest.
  - **Audio** (`Sound.suspend`): stops playback and refuses new playback. The
    mic/`arecord` is held by the voice **Listener**, so the coordinator also calls
    `assistant.stop()` to free it.
  - **Camera** (`Camera.suspend`): force-stops the sensor (even under viewers),
    reports `available() == False` so streams 503. The coordinator also stops the
    **face tracker** so its sensor hold is dropped.
- **While released**, the hardware-actuating endpoints (`/api/move`, `/rest`,
  `/relax`, `/channel`, `/identify`, `/camera`, `/voice`, `/track`, `/say`,
  `/command`, sound playback) return **409** with a clear message; the camera
  stream/snapshot return 503.
- **Persistence:** the choice is saved to `config/settings.json` under
  `hardware.released` and re-applied at boot (the app starts suspended, without a
  servo-rest sweep), so an event set-up survives a reboot.
- **Note:** this only frees *our* side. It does **not** start/stop the `myrobotlab`
  service — do that separately (`sudo systemctl start myrobotlab`). Wiring the two
  together is a noted follow-up in `TODO.md`.
- **I2C access for MRL is confirmed at the OS level:** MRL runs as `dietpi`, which
  is in the `i2c` group, and `/dev/i2c-1` is `crw-rw---- root i2c` — so MRL can
  open the bus. Verified 2026-07-10 (`i2c-tools` installed): with the InMoov app
  released, `i2cdetect -y -r 1` as `dietpi` sees the PCA9685 at **0x40** and
  `i2cget -y 1 0x40 0x00` reads its MODE1 register. (i2cdetect/i2cget live in
  `/usr/sbin` — not on the default user PATH; use the full path or `sudo`.)

### MRL driving the PCA9685 — the WiringPi fix (REQUIRED)

MRL's Pi-native I2C is the **`RasPi`** service (an `I2CController`), and the
`Adafruit16CServoDriver` (the PCA9685) attaches to it. Out of the box on this Pi,
starting `RasPi` failed with:
```
java.lang.UnsatisfiedLinkError: 'int com.pi4j.wiringpi.Gpio.wiringPiSetup()'
```
**Cause:** MRL's `RasPi` uses **Pi4J 1.4**, whose native `libpi4j-aarch64.so`
*dynamically links* `libwiringPi.so` / `libwiringPiDev.so` (Pi4J 1.4 dropped its
bundled WiringPi and expects a **system** WiringPi). None was installed, so
`wiringPiSetup` and the `wiringPiI2C*` symbols were unresolved.

**Fix (done 2026-07-10):** built + installed the maintained WiringPi (which does
support Pi 4 / 64-bit / recent kernels):
```bash
git clone --depth 1 https://github.com/WiringPi/WiringPi.git ~/mrl/WiringPi
cd ~/mrl/WiringPi && sudo ./build && sudo /usr/sbin/ldconfig
gpio -v          # sanity: should report "Pi 4B" and user-level GPIO access
```
This installs **WiringPi 3.18** → `/usr/local/lib/libwiringPi.so.3.18` (+ the
unversioned `/usr/lib/libwiringPi.so` symlink Pi4J links against) and survives a
reboot (standard lib path + ld.so cache). It does **not** affect the InMoov
Python app, which uses `adafruit_servokit`, not WiringPi.

**After installing WiringPi, restart MRL** (a running JVM has already cached the
failed native load): `sudo systemctl restart myrobotlab`. Then `RasPi` starts and
the PCA9685 attaches. Verified end-to-end: `RasPi` inits, `Adafruit16CServoDriver`
attaches on `bus 1 / 0x40`, and MRL wrote the chip (MODE2=0x04, PRESCALE=0x79 =
50 Hz). Minimal MRL (Python) setup:
```python
raspi = runtime.start("raspi", "RasPi")
head  = runtime.start("HeadDriver", "Adafruit16CServoDriver")
head.setDeviceBus("1"); head.setDeviceAddress("0x40")
head.attach("raspi")
# then attach Servo services to channels on HeadDriver
```
NB: MRL services created in a session are **not persisted** unless you save the
MRL config (or a startup script). And MRL driving the PCA9685 requires the InMoov
app to be **released** (handoff on) — only one owner of the I2C bus at a time.

### Camera into MyRobotLab (libcamera → OpenCV via an MJPEG stream)

MRL's OpenCV service **works on this Pi** (bytedeco JavaCV, OpenCV 4.10 arm64 — it
needs `libunicap2` installed). But it can't grab the camera *directly*: MRL's
`Webcam` service (sarxos/v4l4j) has no arm64 native, and the camera is a **Pi
Camera 3 (imx708) — libcamera/CSI**, not a V4L2 webcam. `VideoCapture(0)` fails
with "Could not read frame". (The old myrobotlab.org "pi camera + opencv" guide is
Jessie-era `bcm2835-v4l2` — that legacy stack doesn't support the imx708.)

**Solution: feed OpenCV an MJPEG stream from libcamera.** A standalone streamer
owns *only* the camera and serves MJPEG; MRL's built-in `MJpegFrameGrabber` reads
the URL. (We evaluated writing a custom MRL "Libcamera" FrameGrabber plugin —
possible but not worth it: libcamera has no Java binding so the plugin would shell
out to `rpicam-vid` anyway, adding a new grabber type needs an MRL source patch,
and MRL *already ships* an MJPEG grabber. See the git history of this file.)

- **Streamer:** `deploy/camera_stream.py` (picamera2/libcamera → MJPEG), run by
  **`camera-stream.service`** (repo copy `deploy/camera-stream.service`), serving
  **:8081** — `GET /stream.mjpg`, `/snapshot.jpg`, `/`. Owns only the camera (no
  I2C/audio), so it coexists with MRL driving the servos. Enabled at boot. Tuning
  via env in the unit (`CAM_STREAM_SIZE=640x480`, `CAM_STREAM_FPS=15`,
  `CAM_AF_MODE=0` manual, `CAM_LENS_POSITION=2.0`, `CAM_FLIP=0`).
  ```bash
  sudo systemctl {start,stop,restart,status} camera-stream
  curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8081/snapshot.jpg   # 200
  ```
- **Wire MRL's OpenCV to it** (Python tab, or REST):
  ```python
  cv = runtime.start("cv", "OpenCV")
  cv.setGrabberType("MJpeg")
  cv.setInputSource("imagefile")                              # INPUT_SOURCE_FILE
  cv.setInputFileName("http://localhost:8081/stream.mjpg")
  cv.capture()
  ```
  Verified end-to-end: `MJpegFrameGrabber` connects, `cv.isCapturing()==true`, and
  OpenCV pulls ~18 KB JPEG frames from the imx708. Face detection etc. then run on
  those frames like any OpenCV input.
- **Note:** this camera path is independent of the servo/I2C handoff — the camera
  and the I2C bus are different resources. So MRL can have the camera (via the
  stream) *and* the servos (via `raspi`/`Adafruit16CServoDriver`) at the same time,
  while the full InMoov app stays stopped.

```bash
curl -s localhost:8080/api/handoff                                   # state
curl -sX POST localhost:8080/api/handoff -d '{"release":true}'  -H 'Content-Type: application/json'   # hand off to MRL
curl -sX POST localhost:8080/api/handoff -d '{"release":false}' -H 'Content-Type: application/json'   # take it back
```

# Spoken IP announcement at boot

Because the head is headless (no monitor) and gets its IP over DHCP, a second
service speaks the LAN/wifi IP over the USB speaker at boot so you know where to
remote in — no screen or router-scan needed.

- **Unit file:** `/etc/systemd/system/inmoov-announce-ip.service` (repo copy at
  `deploy/inmoov-announce-ip.service`)
- **Runs:** `venv/bin/python deploy/announce_ip.py` once, as `dietpi`
- **Type:** `oneshot`, `After=network-online.target`
- Plays the `startup` chime, then says *"InMoov is online. My I P address is
  192 dot 168 …"* (twice). `inmoov.service` is ordered `After=` this unit so the
  two don't collide on the single-open USB audio device.

Requirements (both already set up on this rig):
- TTS (offline): **Piper** (neural, natural voice) is preferred, with `espeak-ng`
  as fallback. `sudo apt-get install -y espeak-ng`. Piper is a self-contained
  binary + voice model under `models/piper/` (git-ignored — see the Piper note
  under "Voice assistant" for the re-download steps).
- user `dietpi` in the `audio` group: `sudo usermod -aG audio dietpi` (needs a
  reboot/re-login to take effect)

```bash
sudo systemctl start inmoov-announce-ip     # trigger it now (re-announce)
sudo journalctl -u inmoov-announce-ip -b    # what it announced this boot
```

Tuning lives in `config/settings.json` under `sound` (`device`, `enabled`) —
the announcement and the web app share it.

# Voice assistant — "Hey FRED" (2026-07-04)

FRED can listen for a wake word, run spoken commands, answer questions, and talk
back with his mouth moving in time with the audio.

- **Modules:** `inmoov/listener.py` (Vosk wake-word + speech-to-text, offline),
  `inmoov/brain.py` (hybrid: local commands → Claude fallback),
  `inmoov/commands.py` (the command vocabulary), `inmoov/assistant.py`
  (ties it together + lip-sync). Owned by `web/app.py` as `_assistant`.
- **How it listens:** always-on `arecord` on the USB mic (`plughw:0,0`) → Vosk
  (`models/vosk-model-small-en-us-0.15`, ~40 MB, git-ignored). Say **"Hey FRED,
  <command>"**; a bare "Hey FRED" makes him say "Yes?" and wait ~6 s for the
  command.
- **Brain (hybrid):** built-in commands (open/close mouth, track face on/off,
  terminator mode on/off, look left/right/up/down/center with the eyes, turn
  head left/right/center with the neck ch15, reset, relax) plus system facts
  (what time is it / what's the date / what's your IP) are matched locally —
  instant, offline, free. Anything else goes to **Claude** (`claude-opus-4-8`)
  which can answer *and* trigger the same actions as tools.
- **System facts (`inmoov/sysinfo.py`, 2026-07-05):** FRED answers the current
  time/date and his own IP/hostname locally; the same facts are injected into
  Claude's system prompt each turn (`sysinfo.context_block()`) so *any* phrasing
  the AI handles uses real values instead of guessing. Note: "look" moves the
  eyes; "turn your head"/"face left" rotates the neck (ch15). Left/right sense
  follows the wiring — flip `_turn_head` / `_look` in `commands.py` if mirrored.
- **Lip-sync:** every reply is rendered to a WAV (Piper, else espeak) and the
  jaw servo (ch14) is driven from the audio's loudness envelope while it plays.
- **Terminator clips (2026-07-05):** engaging terminator mode (the red LED)
  plays a random real movie clip instead of a synthesized catchphrase —
  `set_led` (on) calls `sound.play_random("terminator")`, which picks a random
  `.wav` from `sounds/terminator/` (git-ignored — copyrighted audio, supplied
  locally; see that folder's README). If the folder is empty it falls back to a
  spoken `TERMINATOR_PHRASES` one-liner. **Clips are managed from the Admin page**
  ("Terminator clips" panel): drag-and-drop or browse to upload, preview, delete.
  Uploads are converted to 22 kHz mono WAV via `ffmpeg` (`/api/sounds/terminator/*`).
  (An earlier attempt to synthesize an
  Arnold-style voice via DSP/accented models was dropped — accent/inflection
  come from the voice model, not post-processing, and cloning a real person was
  out of scope.)
- **Piper TTS (2026-07-05):** `inmoov/sound.py` auto-detects a self-contained
  Piper binary at `models/piper/bin/piper` plus the first voice at
  `models/piper/voices/*.onnx`. The active voice is chosen by the `PIPER_VOICE`
  constant in `sound.py` (currently `"northern_english_male"` →
  `en_GB-northern_english_male-medium`, 22 kHz mono, ~0.5 real-time on the Pi 4);
  `en_US-bryce-medium`, `en_US-lessac-medium`, and `en_US-ryan-medium` are also
  installed.
- **Voice gotcha (2026-07-05):** the **`ryan` voice garbles longer utterances**
  on this rig — it renders a valid, correct-length, *un-clipped* WAV whose
  phonemes turn to mush past ~a sentence (short replies were fine, which hid it
  for a while). It is NOT a playback fault: espeak, `lessac`, and `bryce` all
  speak the same long sentence cleanly through the identical resample/`aplay`
  path, so the fault is the ryan model's synthesis. Ruled out (in order):
  render length/format, ALSA `plug` resampling (native 44100 via `hw:0,0` still
  garbled), buffer underruns (a pure sine tone was clean — underruns are
  content-independent), and digital/analog clipping (lowering the +13.6 dB PCM
  mixer only made the garble quieter). Fix was simply switching the voice.
  Aside: PCM mixer at 85% is a real **+13.6 dB boost** (`amixer -c 0 sget PCM`);
  if loud chimes/terminator clips ever distort, turn PCM toward ~50%.
  **After changing voices or `sound.py`, restart the service** (`sudo systemctl
  restart inmoov`) — the long-running web app caches the loaded module. It ships its own shared libs, so `sound.py`
  prepends `bin/` to `LD_LIBRARY_PATH`. Falls back to espeak automatically if
  absent. `models/` is git-ignored — to set up fresh: grab
  `piper_linux_aarch64.tar.gz` from github.com/rhasspy/piper/releases and extract
  it to `models/piper/bin/`, then download a voice's `.onnx` + `.onnx.json` from
  huggingface.co/rhasspy/piper-voices into `models/piper/voices/`. `settings()`
  reports the active engine as `tts`. The active voice came from
  `.../resolve/main/en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium.onnx{,.json}`
  in that repo (bryce lives at `.../en/en_US/bryce/medium/...`).

## Enabling it

- **API key (for open questions only):** put your Anthropic key in
  `config/secrets.env` (git-ignored; template already there), then
  `sudo systemctl restart inmoov`. Built-in commands work without a key.
- **Start listening:** the control panel's **🎤 Listen** button, or persist it
  with the *Voice & AI* toggle on `/admin` (starts at boot). Off by default.

```bash
# APIs (also driven from the web UI)
curl -X POST localhost:8080/api/voice   -d '{"on":true}'  -H 'Content-Type: application/json'  # start/stop listening
curl -X POST localhost:8080/api/command -d '{"text":"do terminator mode"}' -H 'Content-Type: application/json'  # type a command
curl -X POST localhost:8080/api/say     -d '{"text":"hello"}' -H 'Content-Type: application/json'  # speak (lip-sync test)
curl localhost:8080/api/voice   # status: listening / speaking / ai_available / last heard+reply
```

## Notes / gotchas

- The mic and speaker on the P10S are **not** acoustically coupled, so the
  listener never hears FRED's own voice (no echo); the listener is also muted
  while he speaks. A real microphone element must be on the P10S mic input —
  confirm with `arecord -D plughw:0,0 -d 3 t.wav` then check the level while
  speaking.
- Wake words accepted: fred / friend / fread / frayed (Vosk mishears the name).
- Requirements installed in the venv: `vosk`, `anthropic` (+ espeak-ng, already
  present). The Vosk model lives in `models/` (git-ignored — re-download from
  alphacephei.com/vosk/models if setting up fresh).

## Conversation log / transcript

The "Talk to FRED" panel is a live, ChatGPT-style transcript (`inmoov/convlog.py`):
everything FRED hears (voice or typed), everything he says, and events he notices
(face detected/lost, tracking on/off) scroll in a chat view. The log lives
server-side, so it survives page reloads and captures voice-only interactions.

```bash
curl "localhost:8080/api/log?since=0"     # all entries (poll with the last id you saw)
curl -X POST localhost:8080/api/log/clear # wipe the transcript
```

Entries are `{id, t, role: user|fred|event, text, source, actions}`. `/api/say`
(the raw TTS test) intentionally does not log.

## Troubleshooting: no audio (mouth moves, aplay "succeeds", but silence)

The P10S is a cheap USB codec that can **wedge its audio endpoint** under load —
symptom: `aplay` exits 0 and the PCM volume is up, but no sound reaches the
speaker (kernel logs `xhci_hcd ... ERROR Transfer event for disabled endpoint`).
It was triggered by simultaneous capture + playback (the wake-word listener
holding the mic open while FRED spoke).

**Fixes in place so it shouldn't recur:**
- The listener now **stops mic capture entirely while FRED speaks** (`listener.pause()`
  / `resume()` around playback in `assistant.speak`) — FRED never captures and
  plays at the same time.
- `ServoController` now serialises all I2C writes with an internal lock (the
  lip-sync jaw animation + face tracker used to collide with request handlers,
  which could raise and kill the listener thread).
- `speak()` is exception-safe and `play_file()` checks aplay's exit code.

**If it still goes silent**, reset the USB device (software unplug/replug):
```bash
sudo ~/inmoov/deploy/reset-audio.sh      # finds the P10S and issues a USB reset
```
If even that doesn't restore sound, physically unplug and replug the P10S — a
full re-enumeration always clears it. Then check volume: `amixer -c 0 sget PCM`
(should be ~85%, `[on]`).

# Browser terminal — work on FRED without SSH (2026-07-05)

A web terminal serves an interactive shell (and `claude`) in the browser, so you
can improve the InMoov app from any device on the LAN instead of SSHing in.

- **Stack:** `ttyd` (static aarch64 binary at `/usr/local/bin/ttyd`) serves a
  browser terminal on **:7681**, attaching a persistent **`tmux`** session
  (`new-session -A -s claude`) rooted at `~/inmoov`. tmux means a closed tab
  doesn't kill your work — reconnect and you're back in the same session.
- **Service:** `inmoov-terminal.service` (reference copy in `deploy/`, but the
  installed unit has the real password in its `-c` flag — kept out of git).
- **Auth:** HTTP basic auth, user `inmoov`. Change the password by editing the
  `-c inmoov:...` value in `/etc/systemd/system/inmoov-terminal.service`, then
  `sudo systemctl daemon-reload && sudo systemctl restart inmoov-terminal`.
- **Open it:** Admin page → Developer → "🖥 Open Claude terminal" (opens
  `http://<pi>:7681`), or browse there directly. Then type `claude`.
- **Security:** `--check-origin` blocks cross-site websocket hijacking. This is a
  full shell — **LAN-only, do NOT port-forward :7681 to the internet** (basic
  auth is plaintext over HTTP; the open web would need HTTPS/a tunnel).

# Chest display Pi — remote animation control (2026-07-16)

A **second Pi** (192.168.68.81) drives the 7" DSI panel in the chest. It used to
run one hard-coded animation — switching meant SSHing in and editing the unit's
`ExecStart` line. Now it runs a supervisor with a small HTTP API, so the head's
admin page picks the look at runtime.

- **Runs on the display Pi, not the head.** Source of truth is
  `deploy/display/` in this repo; deployed to `/home/dietpi/display/`.
- **Stack:** `display_control.py` (stdlib only — there's no Flask on that Pi)
  supervises the animation as a **child process** and serves the control API on
  **:8081**. Switching = kill the child, spawn the next one (~100ms), so no
  systemd restart and no unit editing.
- **Service:** `inmoov-display.service`, `User=root` (needs `/dev/fb0`).
  The animation is **no longer named in the unit** — it's chosen at runtime and
  remembered in `/home/dietpi/display/state.json`, so it survives a reboot.
- **API:** `GET /api/animations` (preset list — this is what fills the admin
  dropdown, so adding an animation there makes it appear in the head's UI with
  no change on this side), `GET /api/state`, `POST /api/animation`
  `{"animation": "reactor-copper"}`.
- **Presets:** `reactor`, `reactor-copper`, `flux`, `face`, `face-talk`, and
  `off` (stops the child and blanks the panel). Variants are flattened into
  presets rather than exposed as separate flag widgets.
- **Use it:** Admin page → Chest display → set IP/port, pick from the dropdown.
  The pick applies immediately (no Save); the address/port/token need Save.
- **Auth:** optional shared secret. Set `Environment=DISPLAY_TOKEN=...` in the
  unit (or a `token.conf` drop-in) and paste the same value into the admin
  page's Token field; clients send it as `X-Display-Token`. Empty = no auth.
- **Crash handling:** the supervisor respawns an animation that dies, but gives
  up after 3 too-fast exits in a row and reports the child's last stderr line to
  the head (shown as "online · failed") instead of hot-looping. Re-picking from
  the dropdown clears the latch.
- **Sensor overlay:** a live readout of the stomach node's distances and motion,
  drawn on top of *whichever* animation is playing. Toggle it from the admin
  page (Chest display → Sensor readout), or `POST /api/metrics {"enabled":true}`.
  The flag lives in `state.json` beside the animation pick, so it survives both
  a preset switch and a reboot.
  - It has to be drawn by the **animation child**, not the daemon: the child
    mmaps `/dev/fb0` and owns it exclusively. So `metrics_hud.py` is split the
    same way as `voice_state.py` — the daemon publishes to `/dev/shm`, and each
    animation calls `hud.draw(frame)` just before its blit. Adding the overlay
    to a new animation is three lines: import, construct, draw.
  - The relay feeds it via an `on_payload` hook, *before* the payload is queued
    for the head — so the panel keeps updating even when the head is unreachable.
- **Deploy an update:** `scp deploy/display/display_control.py
  dietpi@192.168.68.81:/home/dietpi/display/ && ssh dietpi@192.168.68.81
  'sudo systemctl restart inmoov-display'`.
- **Security:** plain HTTP on the LAN. **Don't port-forward :8081.** The head
  treats this Pi as a decoration: a 2s timeout, and an unreachable panel shows
  as offline rather than hanging or failing the admin page.

## Notes / gotchas

- **The head never blocks on it.** `inmoov/display.py` uses a short timeout and
  raises `DisplayError`; `/api/display` reports `online: false` instead of 5xx.
- **`pgrep -f "python3 reactor.py"` matches your own SSH command line** when you
  test from the head — it'll happily kill your remote shell. Match on the child
  PID from `/api/state` instead.
- The child's stderr goes to `child-stderr.log` beside the script, deliberately
  **not** `/tmp`: this runs as root, and a fixed path in a world-writable dir is
  a symlink attack. It's also never a `PIPE` — an undrained pipe fills its ~64KB
  buffer and blocks the animation forever.

# Stomach sensor node + serial relay (2026-07-24)

Two HC-SR04 ultrasonics and an HC-SR501 PIR, read by a Pico that plugs into the
**chest** Pi. Full wiring, flashing and tuning notes live in
`firmware/pico_sensor_node/README.md`; this is the service-shaped summary.

```
  Pico ──USB serial──> chest Pi ──HTTP over Bluetooth PAN──> head Pi
                    (sensor_relay in          (10.0.0.1:8080
                     display_control.py)       /api/sensors/ingest)
```

- **Why not WiFi on the node.** The head is *always* `10.0.0.1` on `pan0`. Both
  Pis are on DHCP and the head becomes `192.168.50.1` in hotspot mode, so any
  WiFi path means chasing an address that moves. The PAN doesn't move, and it
  works at a venue with no network at all.
- **Why not plugged into the head.** The sensors are in the stomach, and the
  head is on pan/tilt servos — a USB run into a rotating head would fatigue.
- **Why it's inside `display_control.py`.** That's already the supervised,
  always-on process on the chest Pi: one thing to install, one thing to restart.
  It shares nothing with the animation, so a missing Pico or an unreachable head
  can't disturb the screen.
- **Health:** `curl -s http://10.0.0.2:8081/api/sensors` — the *relay's* state,
  not the sensor data. This is what tells "the Pico is unplugged" apart from
  "the head is unreachable": `connected`, `last_line_ago`, `posted`/`failed`/
  `dropped`, `last_error`, and the `last_payload` that went out.
- **Config:** `Environment=SENSOR_PORT|SENSOR_URL|SENSOR_TOKEN` in
  `inmoov-display.service`; `--no-sensor-relay` disables it. The defaults (find
  the Pico automatically, POST to `10.0.0.1`) are right for FRED.
- **Port naming:** `SENSOR_PORT=auto` prefers `/dev/serial/by-id/*MicroPython*`
  over a bare `ttyACM`. That name comes from the RP2040's flash ID, so it's
  stable across reboots and unambiguous if another USB-serial device appears.

## Notes / gotchas

- **The head's `sensors.serial_enabled` stays `false`.** `SerialSensorReader`
  opens a *local* device path — it cannot see the chest Pi's tty. That setting
  is only for a node plugged directly into the head.
- **Two threads on purpose.** The reader must never block on HTTP: if the head
  is down, POSTs sit in their timeout, and a reader waiting on that would let
  the kernel's tty buffer fill and lose the stream. The reader parks payloads in
  a bounded queue and a sender drains it, dropping the oldest when the head is
  unreachable — the right loss, since the node re-sends full state every
  heartbeat and recovery only needs the newest.
- **The relay opens the tty read-only**, which is a safety property rather than
  an accident: the MicroPython REPL shares that CDC with the sensor stream, so a
  stray byte written there would drop the node to the REPL and stop the sensors.
  An fd that can't be written to can't do that.
- **Updating firmware needs the relay stopped** (`systemctl stop inmoov-display`)
  — it holds the port. Use `push.py`, which is deployed alongside the app;
  there's no `mpremote` on that Pi and no pip to install one.
- **A node may name its own `transport`.** `/api/sensors/ingest` used to hardcode
  `"wifi"`, which would have mislabelled everything coming through here; the
  relay stamps `"serial-relay"` so the panel shows the real path.

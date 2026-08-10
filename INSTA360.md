# Insta360 X5 — 360° surround vision

An Insta360 X5 mounted on a mast above FRED's head (on the cart) gives him a
full-circle view of his surroundings. Three things consume it:

| Consumer | What it gets |
|---|---|
| **The brain (Claude)** | `look_around` — real dewarped photos of any direction, so "what can you see?" is answered from actual images; `scan_surroundings` — a spoken summary of where people are. |
| **Face tracking** | A person-bearing that replaces the ultrasonic left/right hint whenever surround vision is watching: the head turns *toward a person seen anywhere in 360°*, not just toward "the nearer side". |
| **Cart navigation** | A safety governor: any drive command (panel, host, or AI) is scaled down or stopped when a person is in the direction of travel. |

Everything was built and tested **before the camera arrived** against a mock
scene (`source: "mock"` — a synthetic panorama with a wandering person), so
the software path is already proven. This file is the checklist for the day
the real X5 shows up.

---

## How it connects

**USB webcam mode — no SDK, no app pairing.** The X5 presents as a standard
UVC video device over USB-C when put in webcam mode; Linux sees a
`/dev/video*` node and OpenCV captures from it like any webcam. In 360 mode
the stream is stitched **equirectangular** (the whole sphere in one frame,
yaw across the width, pitch down the height), which is exactly what
`inmoov/camera360.py` expects.

A network stream also works — set `camera360.source` to an `rtsp://…` URL and
it is opened instead of the UVC device (for a future WiFi mount where a USB
run to the mast is annoying).

## Bring-up checklist (with the camera in hand)

1. **Cable + mode.** Plug the X5 into the head Pi over USB-C. On the camera,
   enable **webcam mode** and pick the **360 / equirectangular** output (not
   the single-lens "wide" mode — that's just a normal webcam picture).
2. **Find the device node.**

   ```bash
   v4l2-ctl --list-devices
   v4l2-ctl -d /dev/videoN --list-formats-ext   # note real sizes + fps
   ```

   The Pi camera stack owns the low-numbered nodes, so the X5 usually lands
   high (`/dev/video8` is the shipped default). Update **Admin → 360° camera
   device** (or `camera360.device` in `config/settings.json`) if it differs,
   and set `camera360.width/height/fps` to a mode the camera actually offers
   — the module asks for MJPEG at that geometry and resizes anything else.
3. **Confirm frames.** Panel → **360° Surround → ▶ Panorama**. The badge
   shows the live mode (`UVC` instead of `MOCK`'s mock scene). If it stays on
   the mock, `GET /api/camera360` reports the open error.
4. **Calibrate the mount (`front_yaw`).** The stitched image's centre is
   wherever the camera's front lens points, which won't be cart-forward.
   Stand something recognisable directly ahead of the cart, open
   `/camera360/view?yaw=0`, and adjust **Admin → Forward offset** until that
   object is centred. Every consumer (sectors, bearings, look_around, the
   governor) shares this one number.
5. **Power.** The X5 runs indefinitely on USB power in webcam mode but it is
   a real load — feed the Pi from a supply with headroom, and expect the
   camera to add heat to the mast.
6. **Detection sanity.** Turn on **◎ Watch**, walk around the cart, and watch
   the presence readout: `motion at 90°R`, upgrading to `face … ~1.5m` when
   you face the camera from a couple of metres. Tune `motion_threshold` via
   `POST /api/surround` if a busy venue floor triggers constantly (higher =
   less sensitive); tuning persists.
7. **Face-tracker handoff.** With Watch on and face tracking on, approach
   from the side/behind: the neck should turn toward you *before* the head
   camera can see you (the surround bearing outranks the ultrasonic hint
   automatically whenever surround is running).
8. **Cart governor.** Enable the cart (Admin), command a slow forward move
   from `POST /api/cart`, and step into the path: speed should scale down
   inside `slow_m` (2.5 m) and stop inside `stop_m` (1 m). A person seen only
   as motion (no range) caps speed at half. Then decide whether to flip on
   **AI driving** — off by default; Claude's `stop_cart` works regardless.

## What runs where

```
X5 (USB-C, webcam mode) ──UVC──> camera360.py ──frames──> surround.py ──┬─> face_tracker bearing
                                    │                                   ├─> cart.py governor
                                    │                                   └─> /api/surround (panel radar)
                                    ├─> /camera360/pano, /camera360/view (panel)
                                    └─> look_around tool (Claude sees JPEGs)
```

- `inmoov/camera360.py` — capture, equirect → rectilinear dewarp, MJPEG.
- `inmoov/surround.py` — motion sectors → targeted Haar detection → presence
  tracks, bearings, the spoken summary, and `nav_assess` for the governor.
- `inmoov/cart.py` — serial link to the cart Pico (see the Project-FRED-Cart
  repo), 10 Hz keepalive under the Pico's 2 s failsafe, command TTL, governor.
- `demo360.py` — the no-hardware bench test of that whole chain. Run it after
  any change; run it on the Pi to sanity-check the environment (it needs
  `python3-opencv` + `opencv-data`, both already required by face tracking).

## Design notes / limits

- **Detection is Haar on dewarped tiles, gated by motion.** Full-frame person
  detection at 360° would swamp a Pi 4; instead the cheap motion pass watches
  all sectors every tick and the Haar pass only inspects the busy ones (plus
  one round-robin sweep sector, so a motionless person is still found within
  a couple of seconds). Faces looking away are caught as `motion` presences —
  direction without range.
- **Distance from a face box is rough** (assumed head size through the tile's
  focal length, ±30%). The governor treats it as "close vs far", nothing
  more; the chest ultrasonics remain the authority up close.
- **The Pico still owns real safety.** The governor is a courtesy layer; the
  cart's own failsafes (2 s host silence → stop, PS2 controller override,
  firmware serial-timeout stop) are unchanged underneath it, and every host
  command dies after its TTL unless renewed.
- The camera stack is **independent of the MyRobotLab handoff** — a plain USB
  device the handoff never touches, so surround vision keeps running while
  MRL owns the servos.

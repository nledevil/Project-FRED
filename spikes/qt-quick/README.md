# Qt Quick / QML on the chest panel — a spike

Run 2026-08-15. **Nothing here is deployed.** It is not in any manifest, the
daemon cannot launch it, and the panel still runs the framebuffer renderers.
It exists to answer three questions with measurements instead of opinions, and
to be deletable in one `rm -r` if the answer is no.

The question behind it: the chest panel's UI is ~3,000 lines of hand-placed
numpy — a bitmap font baked to atlases on the NUC, an evdev touch parser, a
press-animation registry, and absolute pixel coordinates. Three days of layout
bugs (a tab row fused to the header, labels crowding their borders in a wider
typeface, a glow bleeding onto its neighbours) came from having no layout system
and no notion that a control can be visually larger than its rectangle. Would a
real toolkit have made those unrepresentable, and what would it cost?

## What was measured

| | result |
|---|---|
| Runs on the existing panel | yes — `linuxfb`, 16bpp `/dev/fb0`, **no boot change needed** |
| UI frame time, software renderer | **6.1 ms** (164 fps) against a 33 ms budget |
| Startup | 84 ms import + **1.18 s to first frame** (today: instant) |
| Offscreen testable | yes — geometry *and* pixels, no panel, no human |
| Reactor as a GPU shader | **16.6 ms/frame at 60 fps, ~23% of a core** |
| Reactor in numpy, same Pi | 25.8 ms/frame, 38.7 fps ceiling, **~100% of a core** |
| Install footprint | 110 packages on a 304-package system |

The GPU reactor is vsync-locked at the panel's 60 Hz, so that is a floor on its
speed, not a ceiling. The numpy figure excludes the RGB565 pack and the mmap
write, so the real gap is wider than it looks.

## What it costs that is not a number

**The layout bug class does disappear.** `elide` and `fontSizeMode` make an
overflowing label unrepresentable; layout spacing makes the tab row fusing with
the header unrepresentable. That is the strongest argument in favour.

**Testability survives, with a tax.** Every assertion the current tests make
works offscreen — seven tabs found, no two overlapping, gaps exactly 8.0px, and
the armed e-stop reading (64,44,8) against idle's (60,16,12) at the pixel level.
But visual state in QML is *asynchronous*: the first attempt compared (60,16,12)
to itself because the frame was grabbed before a 120 ms colour `Behavior` had
advanced. Tests must settle, ~250 ms per assertion. `draw()` was synchronous.

**Byte-exact equivalence does not survive, and this is the finding that matters
most.** `tools/verify_voice_hud.py` holds two renderers identical across 720
frames, and it has caught real drift. A GPU renderer cannot be held that way:
fragments sample pixel centres, numpy samples corners, and on 4px-wide rings
that half-pixel moves up to 31/255 locally. `check_shader.py` shows both
numbers. Invisible to the eye; fatal to the technique.

**Two dependencies the project does not currently have.** The typefaces are
*gone* — Rajdhani, Orbitron and Exo 2 survive only as baked `.npz` atlases, with
no TTFs in the repo or on the NUC, so Qt renders in DejaVu until they are
re-sourced and vendored. And Qt 6 shaders must be baked with `qsb` on a build
host and shipped precompiled — the same shape as the font-atlas pipeline a
toolkit was supposed to delete.

## What KMS actually did

Enabling `vc4-kms-v3d` is what unlocks the GPU, and the panel now boots with it.
Two things worth knowing, both learned the hard way:

- `lcd_rotate=2` is legacy-firmware only, and a `dtoverlay=` line in
  `cmdline.txt` does nothing. Without `dtoverlay=vc4-kms-dsi-7inch` in
  `config.txt` there is no DSI connector at all — and with no connected output
  there is no `/dev/fb0` either, so the first reboot came up with a dark panel
  and every renderer failing to open a device that did not exist.
- The fbdev emulation here is **16bpp**, not the 32bpp XRGB8888 that was
  predicted. `fb.py` and `voice_hud.c` learned both depths anyway; that work is
  committed and tested, and it is why enabling KMS did not break the panel.
- Anything that takes DRM master can hand the fbdev back **blanked**. That is
  what left the screen dark while `voice_hud` was drawing normally. Both
  renderers now unblank on open; see `fb.unblank()`.

## The files

    spike.py          the menu: --shot per theme, --test, --bench, --panel
    main.qml          chrome, a page of live rows, and the cart e-stop
    reactor.frag      the reactor as a fragment shader, from build_geometry()
    reactor_gpu.qml   one ShaderEffect and one animated float
    reactor_gpu.py    --cpu-bench vs the GPU, on the panel
    check_shader.py   proves the port is faithful, and shows what it costs

`spike.py --shot` and `check_shader.py` need no GPU and no panel.
`reactor_gpu.py` takes the screen, so stop the animation first:

    curl -sX POST -H 'Content-Type: application/json' \
         -d '{"animation":"off"}' http://10.0.0.11:8081/api/animation

## Where this leaves it

The decisive test was the e-stop, not the status page, and it passed: it renders
and it can be checked without a human. So a migration is viable. It is not
obviously *worth it*, and the honest summary is that the menu has a strong case
and the animations have a mixed one — a real CPU saving against the loss of the
only exact verification this project has.

Nothing needs deciding now. The daemon already runs one process at a time on
this panel, so a Qt screen can arrive as another animation preset and the two
can coexist indefinitely, one screen at a time, with no migration ever declared.
If that happens, the theme must stay defined once: `theme.py` feeds the QML side
the way it already generates `theme_colors.h` for the C renderer, or this
project acquires its fourth copy of a palette.

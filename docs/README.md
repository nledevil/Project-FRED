# Design assets

## `animatronic_face_rigged.svg`

FRED's face mask, traced and **rigged for animation**: the eyes and jaw are
separate SVG groups rather than one flat drawing, so they can be driven
independently without re-cutting the artwork.

| Group id | What it is | Moves |
|---|---|---|
| `face` | the main mask | no — stationary backdrop |
| `left-eye` / `right-eye` | iris + pupil | yes — translate for look left/right/up/down |
| `jaw` | lower jaw | yes — translate on Y for open/close |

Drive it with `transform-box: fill-box` and a `transform-origin` of `center` for
the eyes, `top center` for the jaw, then translate each group.

`animatronic_face_rig.png` is the companion sheet: it shows the rig with its
layer stack and worked CSS/JS examples. Kept because the group names and the
transform-origin choices are the whole value here — easy to lose, tedious to
re-derive from the SVG alone.

Neither file is wired into anything yet. `deploy/display/face.py` draws its face
straight into the framebuffer in Python and does not read this SVG; these are
here for a browser-side face, which is where a rigged vector asset actually pays
off.

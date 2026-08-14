"""Buttons and panels for the settings menu, drawn straight into a numpy frame.

There is no toolkit on this Pi and this is not the beginning of one — it is the
four shapes the menu actually needs. Anything more belongs in the head's web
panel, which already has a browser to draw it with.

Three things worth knowing before adding a screen:

**The look is a theme, and it can change while the menu is running.** The
palette names below are rebound by ``apply_theme``; pages reach them as
``ui.INK`` rather than importing the name, so a switch re-skins every page on
the next frame. Import the module, not its constants.

**Text is anti-aliased, from a font baked on the NUC.** ``font_atlas`` blends
coverage rather than setting pixels, which is the whole difference between a
label and a debug readout. If the atlas is missing — a half-finished deploy —
everything falls back to the old 5x7 bitmap rather than failing to boot, so a
panel is never bricked by a missing font.

**Drawing is additive, on a dark frame.** Text adds into the frame the way the
animations composite glow, so a filled panel is drawn first and its text added
on top. ``fill`` therefore *sets* rather than adds — it is the one place that
wants a flat background to put text on.

The affordance rule
-------------------
**A face in the accent colour means you can tap it. Nothing else may wear one.**

The accent border is the signal — not the fill, not the rounding. So:

* Tappable → a ``Button``, or ``draw_face`` if it needs its own colours (the
  e-stops). Both animate the press, which is the other half of the promise: a
  control that acknowledges a tap is one you tap once.
* Not tappable → ``readout``. Flatter face, dim edge, no accent, no animation.

This was already broken once before it was written down — the cart's telemetry
read as a fourth mode button — and writing it down is not what prevents the
next one; having a function for each is. Reach for ``readout`` and the rule
holds by construction. Hand-rolling ``fill`` + ``border`` in the accent colour
is how a panel starts lying about what it does.
"""
from __future__ import annotations

import time

import numpy as np

import theme as theme_mod
from font5x7 import CHAR_H, draw_text, text_width as _bitmap_width

try:
    from font_atlas import Font
except Exception:                                    # noqa: BLE001
    Font = None

# --- palette ----------------------------------------------------------------
# Rebound by apply_theme(). Seeded with the default so importing this module is
# enough to draw, which keeps every page importable in isolation for testing.
INK = DIM_INK = OK_INK = WARN_INK = BAD_INK = (255, 255, 255)
BG = PANEL = PANEL_ON = EDGE = STOP_PANEL = STOP_PANEL_ARM = (0, 0, 0)
READOUT = READOUT_EDGE = (0, 0, 0)

THEME: theme_mod.Theme | None = None
_FONTS: dict[int, object] = {}
_PRESS: dict[int, float] = {}          # id(Button) -> monotonic time of its last press
PRESS_SECS = 0.28                      # how long a press animation runs


def apply_theme(name: str) -> str:
    """Switch the look. Returns the name actually applied.

    Fonts are loaded here rather than per-frame, and a theme whose atlases are
    missing still applies: it degrades to the bitmap font with the new colours,
    which is worth more than refusing to change.
    """
    global THEME, _FONTS
    global INK, DIM_INK, OK_INK, WARN_INK, BAD_INK
    global BG, PANEL, PANEL_ON, EDGE, STOP_PANEL, STOP_PANEL_ARM
    global READOUT, READOUT_EDGE

    th = theme_mod.THEMES.get(name) or theme_mod.THEMES[theme_mod.DEFAULT]
    THEME = th
    for key, value in th.palette.items():
        globals()[key] = value

    _FONTS = {}
    if Font is not None:
        for scale, filename in th.fonts.items():
            # Two layouts: fonts/ beside the source in the repo, and flat beside
            # it on the Pi, because the chest manifest flattens everything into
            # one directory. Try both rather than making the deploy special.
            for path in (theme_mod.HERE / "fonts" / filename,
                         theme_mod.HERE / filename):
                try:
                    _FONTS[scale] = Font.load(path)
                    break
                except Exception:                    # noqa: BLE001
                    continue                         # falls back to the bitmap font
    return th.name


def _font(scale: int):
    """The atlas for a scale, or the nearest one baked. None = use font5x7."""
    if not _FONTS:
        return None
    return _FONTS.get(scale) or _FONTS[min(_FONTS, key=lambda s: abs(s - scale))]


# --- shapes -----------------------------------------------------------------
# One signed-distance field per (w, h, radius), cached. It yields fill, outline
# and glow from the same computation, which is what makes rounded, anti-aliased
# buttons affordable at 30fps on this Pi — the maths happens once per button
# size, and every frame after that is an indexed multiply-add.
_SDF: dict[tuple, np.ndarray] = {}


def _sdf(w: int, h: int, r: float) -> np.ndarray:
    key = (w, h, r)
    if key not in _SDF:
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        dx = np.maximum(np.abs(xx - (w - 1) / 2.0) - ((w - 1) / 2.0 - r), 0.0)
        dy = np.maximum(np.abs(yy - (h - 1) / 2.0) - ((h - 1) / 2.0 - r), 0.0)
        _SDF[key] = np.sqrt(dx * dx + dy * dy) - r
    return _SDF[key]


def _cov_fill(w, h, r):
    # The half-pixel is what makes the edge read as smooth rather than stepped.
    return np.clip(0.5 - _sdf(w, h, r), 0.0, 1.0)


def _cov_outline(w, h, r, t=1.6):
    return np.clip(t / 2.0 + 0.5 - np.abs(_sdf(w, h, r)), 0.0, 1.0)


def _cov_glow(w, h, r, reach=14.0):
    g = np.clip(1.0 - np.maximum(_sdf(w, h, r), 0.0) / reach, 0.0, 1.0)
    return g * g                                     # tight core, soft falloff


def _blend(frame, x, y, cov, rgb, alpha=1.0):
    """Composite a coverage mask in `rgb`. The menu frame is float32 (it is a
    glow-composited image, not a canvas), so this stays in float."""
    h, w = cov.shape
    H, W = frame.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    a = (cov[y0 - y:y1 - y, x0 - x:x1 - x] * float(alpha))[:, :, None]
    region = frame[y0:y1, x0:x1]
    frame[y0:y1, x0:x1] = region * (1.0 - a) + np.asarray(rgb, np.float32) * a


def _ease_out(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


# --- primitives used directly by pages --------------------------------------
def fill(frame: np.ndarray, x0: int, y0: int, x1: int, y1: int, rgb) -> None:
    """Set a rectangle to a flat colour (not additive — see the module docstring)."""
    H, W = frame.shape[:2]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    if x1 <= x0 or y1 <= y0:
        return
    frame[y0:y1, x0:x1] = np.asarray(rgb, dtype=np.float32)


def rounded(frame: np.ndarray, x0: int, y0: int, x1: int, y1: int, rgb,
            radius: int | None = None, alpha: float = 1.0) -> None:
    """A filled rectangle with anti-aliased rounded corners."""
    r = THEME.radius if radius is None else radius
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    _blend(frame, x0, y0, _cov_fill(w, h, min(r, w / 2, h / 2)), rgb, alpha)


# --- the page frame ---------------------------------------------------------
# Every page had these written out itself, and they all happened to agree. They
# agreed because each was copied from the last, which is not the same as being
# shared: the next page agrees only if whoever writes it copies correctly.
# Content top is deliberately *not* here — pages legitimately start at different
# heights depending on whether they carry a heading.
MARGIN = 24
X0, X1 = MARGIN, 800 - MARGIN
CONTENT_Y = 104                 # first row below the title and tab bar


def fit(s: str, width_px: int, scale: int = 2) -> str:
    """Trim ``s`` to ``width_px``, at a word boundary where there is one.

    Cutting mid-word produced things like "SERVO SERVER UNREACHAB", which reads
    as a corrupted message rather than a shortened one — on a status panel that
    is a real cost, because the whole point is being believed at a glance.

    Measured incrementally rather than by dividing by the width of an "M": that
    only ever worked because the old bitmap font was monospaced.
    """
    if text_width(s, scale) <= width_px:
        return s
    out = ""
    for word in s.split():
        candidate = f"{out} {word}".strip()
        if text_width(candidate, scale) > width_px:
            break
        out = candidate
    if out:
        return out
    # A single word too long for the column: hard-cut it, there's nothing else.
    for n in range(len(s), 0, -1):
        if text_width(s[:n], scale) <= width_px:
            return s[:n]
    return s[:1]


def empty(frame: np.ndarray, message: str, y: int = CONTENT_Y,
          scale: int = 2) -> None:
    """What a page says when it has nothing to show.

    A page with nothing on it must say why. A blank where a fault should be is
    the one thing this menu must never do — see page_status's docstring, which
    made the same argument first.
    """
    text(frame, message, X0, y, BAD_INK, scale)


def readout(frame: np.ndarray, x0: int, y0: int, x1: int, y1: int,
            weight: int = 1) -> None:
    """A surface that shows something and cannot be tapped.

    The counterpart to Button: same rounding so the screen still looks like one
    family, but a flatter face and a dim edge instead of the accent. See the
    affordance rule in the module docstring — this exists so "not a button" is
    something you reach for rather than something you remember.
    """
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    r = min(THEME.radius if THEME else 0, w / 2, h / 2)
    _blend(frame, x0, y0, _cov_fill(w, h, r), READOUT)
    _blend(frame, x0, y0, _cov_outline(w, h, r, float(weight)), READOUT_EDGE)


def border(frame: np.ndarray, x0: int, y0: int, x1: int, y1: int, rgb,
           weight: int = 1) -> None:
    """A rectangle outline, ``weight`` pixels thick — rounded to match the theme."""
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    r = min(THEME.radius if THEME else 0, w / 2, h / 2)
    _blend(frame, x0, y0, _cov_outline(w, h, r, float(weight)), rgb)


def text(frame: np.ndarray, s: str, x: int, y: int, rgb, scale: int = 2,
         preserve_case: bool = False) -> None:
    f = _font(scale)
    if f is None:
        draw_text(frame, s, x, y, rgb, scale, preserve_case=preserve_case)
        return
    f.draw(frame, s if preserve_case else s.upper(), x, y, rgb,
           tracking=THEME.tracking if THEME else 0.0)


def text_width(s: str, scale: int = 2, preserve_case: bool = False) -> int:
    f = _font(scale)
    if f is None:
        return _bitmap_width(s, scale)
    return f.width(s if preserve_case else s.upper(),
                   tracking=THEME.tracking if THEME else 0.0)


def text_right(frame: np.ndarray, s: str, x_right: int, y: int, rgb,
               scale: int = 2, preserve_case: bool = False) -> None:
    """Right-align ``s`` so its last pixel lands on ``x_right``."""
    text(frame, s, x_right - text_width(s, scale, preserve_case), y, rgb, scale,
         preserve_case=preserve_case)


def text_centred(frame: np.ndarray, s: str, x0: int, x1: int, y: int, rgb,
                 scale: int = 2, preserve_case: bool = False) -> None:
    w = text_width(s, scale, preserve_case)
    text(frame, s, x0 + (x1 - x0 - w) // 2, y, rgb, scale,
         preserve_case=preserve_case)


def line_height(scale: int = 2) -> int:
    f = _font(scale)
    return f.height if f is not None else CHAR_H * scale


class Button:
    """A rectangle that knows whether it was tapped.

    Holds its own geometry so a page can declare its buttons once and then just
    draw them and ask ``hit()`` — the alternative is the same four numbers
    written out twice, once to draw and once to test, which is exactly the sort
    of thing that drifts.

    ``hit()`` also *records* the tap, so the press animation costs pages nothing:
    every page already asks whether it was hit before acting on it.
    """

    def __init__(self, x0: int, y0: int, x1: int, y1: int, label: str = "",
                 scale: int = 3, preserve_case: bool = False):
        self.rect = (x0, y0, x1, y1)
        self.label = label
        self.scale = scale
        self.preserve_case = preserve_case

    def hit(self, x: int, y: int) -> bool:
        x0, y0, x1, y1 = self.rect
        if x0 <= x < x1 and y0 <= y < y1:
            _PRESS[id(self)] = time.monotonic()
            return True
        return False

    def phase(self) -> float:
        """0 at the moment of the press, 1 once the animation has finished.

        Public because the two e-stops draw their own face — they have to not
        look like the other four buttons — but should still acknowledge a tap.
        They keep a Button for hit-testing, so the timing is already here.
        """
        t = _PRESS.get(id(self))
        if t is None:
            return 1.0
        return min(1.0, max(0.0, (time.monotonic() - t) / PRESS_SECS))

    def draw(self, frame: np.ndarray, on: bool = False, ink=None,
             label: str | None = None) -> None:
        x0, y0, x1, y1 = self.rect
        draw_face(frame, x0, y0, x1, y1,
                  PANEL_ON if on else PANEL, EDGE, self.phase())
        s = self.label if label is None else label
        if s:
            text_centred(frame, s, x0, x1,
                         y0 + ((y1 - y0) - line_height(self.scale)) // 2,
                         ink or INK, self.scale,
                         preserve_case=self.preserve_case)


def draw_face(frame: np.ndarray, x0: int, y0: int, x1: int, y1: int,
              face, edge, phase: float = 1.0, weight: float = 1.6) -> None:
    """Draw a button-shaped panel in the theme's style, animating a press.

    Split out of Button.draw so a control that needs its own colours — the
    e-stops, which must not look like the other four buttons — still gets the
    theme's shape and the same acknowledgement of a tap. Pass the Button's
    ``phase()``; 1.0 means "not pressed".
    """
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    lit = _ease_out(1.0 - phase) if phase < 1.0 else 0.0  # 1 just after a tap, decaying
    style = THEME.style if THEME else "soft"
    r = min(THEME.radius if THEME else 0, w / 2, h / 2)

    if style == "hud":
        _blend(frame, x0 - 12, y0 - 12, _cov_glow(w + 24, h + 24, r + 12),
               edge, 0.10 + 0.35 * lit)
        _blend(frame, x0, y0, _cov_fill(w, h, r), face, 0.95)
        _blend(frame, x0, y0, _cov_outline(w, h, r, 1.6), edge, 0.55 + 0.45 * lit)
    elif style == "neon":
        _blend(frame, x0, y0, _cov_fill(w, h, r), face, 0.9)
        if lit:
            # The fill sweeps in from the left, then drains away again.
            cov = _cov_fill(w, h, r).copy()
            cov[:, int(w * min(1.0, (1.0 - lit) * 2 + 0.15)):] = 0.0
            _blend(frame, x0, y0, cov, edge, 0.30 * lit)
        _blend(frame, x0, y0, _cov_outline(w, h, r, 2.0), edge, 0.95)
    else:                                            # "soft"
        # Press insets the card and dims it, then it springs back out —
        # cheaper than scaling a rendered card, and the same to the eye.
        inset = int(round(3 * lit))
        x0d, y0d = x0 + inset, y0 + inset
        wd, hd = max(1, w - inset * 2), max(1, h - inset * 2)
        ramp = np.linspace(0.0, 1.0, hd, dtype=np.float32)[:, None, None]
        top = np.asarray(face, np.float32) * (1.14 - 0.14 * lit)
        bot = np.asarray(face, np.float32) * (0.80 - 0.10 * lit)
        card = np.clip(top * (1.0 - ramp) + bot * ramp, 0, 255)
        cov = _cov_fill(wd, hd, min(r, wd / 2, hd / 2))[:, :, None]
        xa, ya = max(0, x0d), max(0, y0d)
        xb, yb = min(frame.shape[1], x0d + wd), min(frame.shape[0], y0d + hd)
        if xb > xa and yb > ya:
            c = cov[ya - y0d:yb - y0d, xa - x0d:xb - x0d]
            frame[ya:yb, xa:xb] = frame[ya:yb, xa:xb] * (1 - c) + card[
                ya - y0d:yb - y0d] * c



apply_theme(theme_mod.load_name())

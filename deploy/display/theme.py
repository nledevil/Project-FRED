"""The chest panel's three looks, and the one place their differences live.

A theme is a palette, a typeface with a type scale, a shape style, and one
accent colour the animations are built from. Pure data on purpose: the Qt
panel reads it at startup (and re-execs to change), the shaders get it through
the Ramp below, and the C voice HUD gets it through the header that
tools/gen_theme_colors.py generates — so every renderer answers to this file
and none of them can drift on their own.

The type scale (sizes) is indexed 1/2/3/4/8 for history: those were the bitmap
font's integer scales, and every page still thinks in them. The pixel values
were chosen to match the cap heights of the 5x7 grid they replaced, and scale
8 is the e-stops, sized to be hit without being looked at.
"""
from __future__ import annotations

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"     # shared with display_control.py's animation pick
DEFAULT = "soft"


class Theme:
    def __init__(self, name, label, blurb, style, sizes, ttf, palette, radius,
                 tracking, accent):
        self.name, self.label, self.blurb = name, label, blurb
        self.style = style           # "soft" | "hud" | "neon"
        self.sizes = sizes           # {scale: pixel size} — the type scale
        self.ttf = ttf               # the face, in fonts/ttf/
        self.palette = palette
        self.radius = radius
        self.tracking = tracking     # extra px between glyphs; caps need it, lowercase doesn't
        # The one colour the animations are built from. They are glows rather
        # than panels, so they want a ramp — rim, body, core — not the flat
        # PANEL/EDGE/INK set the pages use. See Ramp below.
        self.accent = accent

    def ramp(self):
        return Ramp(self.accent, self.palette)


class Ramp:
    """A dark-to-white ramp through a theme's accent, sampled by level.

    Every animation on this panel is the same idea: something dim at the rim,
    the accent through the body, and white where it is hottest. Writing that as
    one ramp means a theme is one colour to choose rather than nine to keep in
    agreement, and it is why the reactor still looks like an arc reactor when
    the accent changes — the *structure* is in the levels, which do not move.

        0.0  the rim, and the vignette behind everything
        0.5  the accent itself
        1.0  white hot

    Integer tuples out, no numpy: this module is imported by the C renderer's
    generator as well, and stays pure data.
    """

    def __init__(self, accent, palette=None):
        self.accent = tuple(accent)
        # Not black: a rim that goes to pure black loses the hue exactly where
        # the eye reads the shape against the background.
        self.deep = tuple(int(round(c * 0.18 + 6)) for c in accent)
        # The three meanings that must never be re-tinted. Listening, thinking
        # and a failed sensor have to read the same on every theme — the accent
        # is decoration, but green/amber/red are the message.
        palette = palette or {}
        self.ok = tuple(palette.get("OK_INK", (90, 255, 150)))
        self.warn = tuple(palette.get("WARN_INK", (255, 180, 60)))
        self.bad = tuple(palette.get("BAD_INK", (230, 120, 90)))

    def at(self, level):
        level = max(0.0, min(1.0, float(level)))
        if level <= 0.5:
            t = level * 2.0
            lo, hi = self.deep, self.accent
        else:
            t = (level - 0.5) * 2.0
            lo, hi = self.accent, (255, 255, 255)
        return tuple(int(round(a + (b - a) * t)) for a, b in zip(lo, hi))


# The stop control sits outside every palette on purpose: everything else on the
# screen is one family, so a red face is not "another button" at a glance.
_STOP = {"STOP_PANEL": (60, 16, 12), "STOP_PANEL_ARM": (64, 44, 8)}

THEMES = {
    "soft": Theme(
        "soft", "Soft", "Rounded cards, gentle gradient",
        style="soft",
        sizes={1: 15, 2: 20, 3: 30, 4: 40, 8: 58},
        ttf="Rajdhani-Medium.ttf",
        radius=14, tracking=0.0,
        # A cooler, softer blue than the HUD's cyan — the same family as
        # this theme's panels, so the reactor belongs to the screen it is on.
        accent=(130, 175, 240),
        palette={
            "INK": (238, 240, 246), "DIM_INK": (150, 154, 168),
            "OK_INK": (120, 230, 160), "WARN_INK": (250, 200, 90),
            "BAD_INK": (240, 130, 110),
            "READOUT": (36, 38, 45), "READOUT_EDGE": (48, 50, 58),
            "BG": (24, 25, 30), "PANEL": (52, 56, 66), "PANEL_ON": (78, 96, 130),
            "EDGE": (74, 78, 92), **_STOP},
    ),
    "hud": Theme(
        "hud", "HUD", "Glowing hairlines, letterspaced caps",
        style="hud",
        sizes={1: 13, 2: 18, 3: 26, 4: 34, 8: 50},
        ttf="Orbitron[wght].ttf",
        radius=10, tracking=2.0,
        # The cyan the animations were written in. This theme is the one
        # they already matched, so it is the one that does not move.
        accent=(120, 210, 255),
        palette={
            "INK": (150, 215, 245), "DIM_INK": (85, 150, 185),
            "OK_INK": (90, 255, 170), "WARN_INK": (255, 190, 70),
            "BAD_INK": (240, 120, 95),
            "READOUT": (12, 20, 27), "READOUT_EDGE": (26, 44, 56),
            "BG": (8, 12, 18), "PANEL": (16, 30, 40), "PANEL_ON": (26, 62, 82),
            "EDGE": (80, 190, 235), **_STOP},
    ),
    "neon": Theme(
        "neon", "Neon", "Outlines on black, highest contrast",
        style="neon",
        sizes={1: 14, 2: 19, 3: 28, 4: 38, 8: 52},
        ttf="Exo2[wght].ttf",
        radius=4, tracking=1.0,
        # The green everything else in this theme is outlined in.
        accent=(0, 235, 170),
        palette={
            "INK": (215, 255, 240), "DIM_INK": (0, 180, 132),
            "OK_INK": (0, 240, 170), "WARN_INK": (255, 200, 80),
            "BAD_INK": (255, 110, 110),
            "READOUT": (7, 10, 9), "READOUT_EDGE": (18, 34, 28),
            "BG": (4, 4, 6), "PANEL": (8, 14, 12), "PANEL_ON": (0, 70, 52),
            "EDGE": (0, 235, 170), **_STOP},
    ),
}

ORDER = ["soft", "hud", "neon"]


# The seven colours the voice HUD draws with, as levels on the theme's ramp.
# Here rather than in either renderer because there are two of them — a Python
# one and a compiled one — and tools/gen_theme_colors.py turns these into the C
# header so the numbers cannot drift apart. See tools/verify_voice_hud.py.
HUD_LEVELS = {
    "base":  0.45,      # idle and speaking: the HUD's own colour
    "white": 0.82,      # highlights struck off it
    "title": 0.33,      # metrics labels
    "value": 0.50,      # metrics readings
}


def hud_colours(name: str | None = None) -> dict:
    """The voice HUD's seven RGB triples for one theme."""
    r = ramp(name)
    out = {k: r.at(v) for k, v in HUD_LEVELS.items()}
    # Not on the ramp, and deliberately: listening is green, thinking is amber
    # and a fault is red on every theme. Those three are the message, not the
    # decoration.
    out.update(green=r.ok, amber=r.warn, alert=r.bad)
    return out


def palette(name: str | None = None) -> dict:
    """The active theme's palette, for code that states facts in colour.

    The page classes' rows()/view() methods label their answers with inks —
    OK green, BAD red — and used to read them from menu_ui's rebindable
    globals. The numpy renderer retired; the Qt panel re-execs on a theme
    change, so a read at import time is always current.
    """
    return THEMES.get(name or load_name(), THEMES[DEFAULT]).palette


def ramp(name: str | None = None) -> "Ramp":
    """The active theme's animation ramp.

    Read once at startup by each animation rather than polled: the daemon stops
    the animation to hand the panel to the settings menu and respawns it when
    the menu closes, so the process that draws the reactor is always younger
    than the last theme change. One read, no per-frame stat().
    """
    return THEMES.get(name or load_name(), THEMES[DEFAULT]).ramp()


def load_name() -> str:
    """The remembered theme, or the default. Never raises: a panel that cannot
    read its own preferences should still draw something."""
    try:
        name = json.loads(STATE_PATH.read_text()).get("theme")
    except Exception:                                    # noqa: BLE001
        return DEFAULT
    return name if name in THEMES else DEFAULT


def save_name(name: str) -> None:
    """Remember the pick beside the animation choice — one file for everything
    this panel remembers. Read-modify-write so the two settings don't clobber
    each other."""
    if name not in THEMES:
        return
    try:
        data = json.loads(STATE_PATH.read_text())
        if not isinstance(data, dict):
            data = {}
    except Exception:                                    # noqa: BLE001
        data = {}
    data["theme"] = name
    try:
        STATE_PATH.write_text(json.dumps(data, indent=2) + "\n")
    except Exception:                                    # noqa: BLE001
        pass                                             # read-only fs: keep running

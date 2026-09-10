#!/usr/bin/env python3
"""The chest panel, as one long-running Qt application.

Before this, every animation was its own process: the daemon killed one and
spawned the next on every change, which under Qt means paying ~1.2s of start-up
to swap a shader. This starts once and switches scenes on a property, so a
change is a frame.

It is also the menu: the cog switches to the menu scene in place, with the
animation still running underneath. The numpy menu this replaced had to take
the whole framebuffer to draw, so opening settings meant killing the animation
and closing meant a daemon round-trip to restart it; that machinery retired on
2026-08-16 and the page classes it left behind now serve their view() data to
the QML here.

What to show is read from state.json, the same file the daemon already writes
when the panel's animation is changed through its API. Polling a file the daemon
already maintains beats inventing a control channel, and it is how voice state
and metrics already reach the animations.

    python3 panel.py                 # what state.json says, and follow it
    python3 panel.py --anim flux     # ignore state.json, for testing
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import numpy as np                                      # noqa: E402

import cog_hud                                          # noqa: E402
import metrics_hud                                      # noqa: E402
import theme                                            # noqa: E402
import touch                                            # noqa: E402
import voice_hud                                        # noqa: E402
from voice_state import VoiceFeed                       # noqa: E402
# The menu's facts come from the poller the numpy menu already uses. Imported
# rather than copied; it moves into its own module when that menu is retired at
# the end of the port, which is the moment it stops having two callers.
from net_snapshot import Net, NUC                        # noqa: E402
from page_status import StatusPage                       # noqa: E402
from page_info import InfoPage                           # noqa: E402
from page_voice import VoicePage                         # noqa: E402
from page_display import DisplayPage                     # noqa: E402
from page_servos import ServosPage                       # noqa: E402
from page_cart import CartPage                           # noqa: E402
from page_wireless import WirelessPage                   # noqa: E402
from power_menu import PowerMenu                         # noqa: E402
import pin_gate                                          # noqa: E402

# The tabs, left to right. This is the one copy: it moved here from
# settings_menu.py when the numpy menu retired.
MENU_PAGES = (StatusPage, VoicePage, ServosPage, CartPage,
              DisplayPage, WirelessPage, InfoPage)


def _pages():
    return MENU_PAGES

from PySide6.QtCore import (QEvent, QObject, QTimer, QUrl, Signal, Slot,  # noqa: E501
                            Property, Qt)   # noqa: E402
from PySide6.QtGui import (QColor, QFontDatabase, QGuiApplication, QImage,  # noqa: E402
                           QVector4D)
from PySide6.QtQuick import QQuickImageProvider, QQuickView               # noqa: E402

W, H = 800, 480

# Seconds the menu may sit with nobody touching the screen before it closes
# itself. Exists because it once sat open for twenty-two hours: a stray tap
# opened it, nothing ever closed it, and the visitor-facing animation was a
# PIN keypad for a day. Long enough to walk around the robot and think;
# short enough that a stray tap costs minutes, not the show.
MENU_IDLE_S = 180.0

# Two layouts, as everywhere else here: subdirectories beside the source in the
# repo, and everything flat on the chest Pi, whose manifest flattens the tree.
def _find(*names):
    for name in names:
        for d in (os.path.join(_HERE, os.path.dirname(name)), _HERE):
            p = os.path.join(d, os.path.basename(name))
            if os.path.exists(p):
                return p
    return ""


# Which shader each animation preset draws with. Presets the panel has that are
# not shaders — the voice HUD, "off" — are not this app's job and the daemon
# still runs them itself.
SHADERS = {"reactor": "reactor", "reactor-copper": "reactor",
           "flux": "flux", "face": "face", "face-talk": "face",
           "voice-hud": "voice_hud"}


def qsb_for(name: str) -> str:
    """Compile <name>.frag to .qsb if missing or stale, and return the path."""
    src = _find(f"shaders/{name}.frag")
    if not src:
        raise SystemExit(f"no shader for '{name}'")
    out = src + ".qsb"
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
        return out
    for qsb in ("/usr/lib/qt6/bin/qsb", "qsb"):
        try:
            subprocess.run([qsb, "--qt6", "-o", out, src], check=True,
                           capture_output=True)
            print(f"baked {os.path.basename(out)}", flush=True)
            return out
        except (OSError, subprocess.CalledProcessError) as exc:
            last = exc
    raise SystemExit(f"could not bake {src}: {last}")


def load_fonts() -> dict:
    """Register the panel's typefaces with Qt and return {theme: family}.

    These are the faces the baked atlases were made from — see fonts/README.md,
    where the weights are recorded and how they were confirmed. Without them Qt
    falls back to DejaVu and the panel stops looking like itself.
    """
    families = {}
    for name, th in theme.THEMES.items():
        filename = th.ttf
        path = _find(f"fonts/ttf/{filename}")
        if not path:
            print(f"[fonts] missing {filename}; Qt will substitute", flush=True)
            continue
        ident = QFontDatabase.addApplicationFont(path)
        got = QFontDatabase.applicationFontFamilies(ident)
        if got:
            families[name] = got[0]
    return families


def overlay_image(cog, hud, frame_buf):
    """The cog and sensor panel, as RGBA to composite over a shader.

    Still drawn by cog_hud and metrics_hud: they are text and a bitmap icon,
    never the expensive part, and drawing them any other way would mean a second
    implementation of the panel's look.
    """
    frame_buf[:] = 0.0
    hud.draw(frame_buf)
    cog.draw(frame_buf)
    rgb = np.clip(frame_buf, 0, 255).astype(np.uint8)
    return np.dstack([rgb, rgb.max(axis=2)]).copy()


class Overlay(QQuickImageProvider):
    def __init__(self):
        super().__init__(QQuickImageProvider.Image)
        self._cog = cog_hud.CogHud()
        self._hud = metrics_hud.MetricsHud()
        self._buf = np.zeros((H, W, 3), np.float32)
        self._img = None
        self._stamp = -1.0
        self.refresh()

    def stale(self) -> bool:
        """Has anything the overlay draws actually changed?"""
        try:
            stamp = metrics_hud.METRICS_PATH.stat().st_mtime
        except OSError:
            stamp = 0.0
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        return True

    def refresh(self):
        rgba = overlay_image(self._cog, self._hud, self._buf)
        self._rgba = rgba                          # keep the buffer alive for Qt
        self._img = QImage(rgba.data, W, H, 4 * W, QImage.Format_RGBA8888)

    def requestImage(self, _id, _size, _requested):
        return self._img


class EnvelopeLayer(QQuickImageProvider):
    """The clip's envelope as a one-row texture — see voice_hud.encode_levels."""

    def __init__(self):
        super().__init__(QQuickImageProvider.Image)
        self.set([0.0])

    def set(self, levels) -> None:
        rgba = voice_hud.encode_levels(levels)
        self._rgba = rgba                          # keep the buffer alive for Qt
        n = rgba.shape[1]
        self._img = QImage(rgba.data, n, 1, 4 * n, QImage.Format_RGBA8888)

    def requestImage(self, _id, _size, _requested):
        return self._img


class WordLayer(QQuickImageProvider):
    """The state word and its dot, drawn by voice_hud.Hud.word_layer.

    Four words, drawn once each and kept: the URL names the state, so Qt asks
    again only when the state changes, and the font work happens four times
    in the life of the process rather than thirty times a second.
    """

    def __init__(self, hud: "voice_hud.Hud"):
        super().__init__(QQuickImageProvider.Image)
        self._hud = hud
        self._cache: dict = {}

    def requestImage(self, ident, _size, _requested):
        state = (ident or "idle").split("/")[0].lower()
        if state not in self._cache:
            layer = self._hud.word_layer(state)
            u8 = (layer * 255.0 + 0.5).astype(np.uint8)
            rgba = np.dstack([u8, u8, u8, np.full_like(u8, 255)]).copy()
            self._cache[state] = (rgba, QImage(rgba.data, rgba.shape[1], rgba.shape[0],
                                               4 * rgba.shape[1], QImage.Format_RGBA8888))
        return self._cache[state][1]


class Panel(QObject):
    """What the scene needs from the rest of FRED, and which scene to show."""

    # Two clocks, on purpose. ``changed`` is the animation's: the shader's
    # uniforms (glow, gaze, blink, voice level) move every frame, so it fires
    # at 30 Hz — and only while the animation is what's on screen.
    # ``viewsChanged`` is the menu's, and fires when a page's facts actually
    # differ from the last time they were built. They used to be one signal at
    # 30 Hz, which made every binding on the visible page re-read its map
    # through PySide and re-lay itself out thirty times a second to arrive at
    # the same picture: measured on the chest Pi, 53% of a core idling on the
    # PIN pad, 84% on the STATUS tab. The page builders themselves cost
    # 0.26 ms a tick for all seven — under 1% — so the waste was never the
    # Python; it was telling QML something had changed when nothing had.
    changed = Signal()
    viewsChanged = Signal()
    sceneChanged = Signal()
    themeChanged = Signal()
    # The voice HUD's two textures change URL on this: a new clip, or a new
    # state word. Rare, and its own signal so the two Image sources are not
    # re-evaluated thirty times a second for the same string.
    voiceChanged = Signal()

    def __init__(self, forced: str | None, scene: str = "anim"):
        super().__init__()
        self._forced = forced
        self._scene = scene                # "anim" | "menu"
        # Last touch anywhere on the screen, fed by eventFilter below — the
        # menu's idle clock. Qt-level rather than QML-level on purpose: every
        # control's press passes through the application before QML routes it,
        # so no page has to remember to report activity (the class of bug where
        # a control draws but does not respond is bad enough the other way
        # round without inventing its twin).
        self._last_touch = time.monotonic()
        self._opened_as_menu = False       # --menu: the daemon put us here
        self._no_gate = False              # --no-gate: stay open, for grabs
        self._page = 0
        self._snap = {}
        # The numpy page, used for its logic and not its drawing: it is what
        # decides whether the head is up, and that answer must not exist twice.
        self._status = StatusPage()
        self._info = InfoPage()
        self._voice_page = VoicePage()
        self._display_page = DisplayPage()
        self._servos_page = ServosPage()
        self._cart_page = CartPage()
        self._wifi_page = WirelessPage()
        self._power = PowerMenu()
        # Asked once, here, rather than on every frame — and so a slow answer
        # costs the menu's opening beat instead of its frame rate.
        self._gate = pin_gate.PinPad(pin_gate.material(NUC))
        if not self._gate.unlocked:
            print("panel: locked - PIN required", flush=True)
        # What the visible scene can see, keyed by page — see _build_views.
        # Compared whole against the next build; a difference is the only
        # thing that emits viewsChanged.
        self._views: dict = {}
        self._uplink_page = 0
        self._net = Net()
        self._net.start()
        self._anim = forced or "reactor"
        self._shader = ""
        self._copper = 0.0
        self._talk = 0.0
        self._state_mtime = -1.0
        self._feed = VoiceFeed()
        # The voice HUD's geometry and its two texture layers. The picture is
        # defined by voice_hud.py; the shader takes what it cannot draw for
        # itself from here. See _voice_inputs.
        self._hud = voice_hud.Hud(W, H)
        self.envelope = EnvelopeLayer()
        self.word = WordLayer(self._hud)
        self._voice_word = "idle"
        self._env_levels = None
        self._env_gen = 0
        self._env_len = 1.0
        self._head = -1.0
        self._have_clip = 0.0
        # --at: a frozen clock, for grabs the verifier can reproduce. Both the
        # animation's t and the playhead's now stand still at this value.
        self._frozen = -1.0
        self._start = time.monotonic()
        self._level = 0.0
        self._voice = 0.0
        self._glow = 1.0
        self._gaze = [0.0, 0.0]
        self._target = [0.0, 0.0]
        self._next_gaze = 0.0
        self._next_blink = 1.5
        self._openness = 1.0
        self.apply(self._anim)

    # ---- which animation ------------------------------------------------
    def apply(self, preset: str) -> None:
        if preset not in SHADERS:
            return
        self._anim = preset
        self._copper = 1.0 if preset == "reactor-copper" else 0.0
        self._talk = 1.0 if preset == "face-talk" else 0.0
        self._shader = QUrl.fromLocalFile(qsb_for(SHADERS[preset])).toString()
        self.sceneChanged.emit()

    def follow_state(self) -> None:
        """Pick up an animation change written by the daemon.

        mtime rather than a re-read every tick: the file changes when somebody
        taps a preset, which is a human-scale event.
        """
        if self._forced:
            return
        try:
            stamp = theme.STATE_PATH.stat().st_mtime
        except OSError:
            return
        if stamp == self._state_mtime:
            return
        self._state_mtime = stamp
        try:
            want = json.loads(theme.STATE_PATH.read_text()).get("animation")
        except Exception:                              # noqa: BLE001
            return
        if want != self._anim and want in SHADERS:
            print(f"panel: -> {want}", flush=True)
            self.apply(want)

    # ---- per-tick state -------------------------------------------------
    def eventFilter(self, obj, ev):  # noqa: N802 - Qt's name
        if ev.type() in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                         QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove):
            self._last_touch = time.monotonic()
        return False                       # observe, never consume

    def freeze(self, at: float) -> None:
        """Stop the clocks at ``at`` seconds — see --at."""
        self._frozen = float(at)

    def set_feed(self, feed: VoiceFeed) -> None:
        """Read the voice state from somewhere else — see --voice-json."""
        self._feed = feed

    def tick(self) -> None:
        now = self._frozen if self._frozen >= 0 else time.monotonic()
        t = self._frozen if self._frozen >= 0 else now - self._start
        # The menu times itself out. --no-gate is exempt: that flag exists so
        # the page-grab harnesses can sit on a tab as long as a render takes.
        if (self._scene == "menu" and not self._no_gate
                and time.monotonic() - self._last_touch > MENU_IDLE_S):
            self.closeMenu()
        self._refresh_views()
        doc = self._feed.poll()
        name = self._feed.state()
        self._voice = {"idle": 0.0, "listening": 1.0,
                       "thinking": 2.0, "speaking": 3.0}.get(name, 0.0)
        self._level = float(self._feed.level(now) or 0.0)
        self._voice_inputs(doc, now, name)
        rate = {"idle": 1.6, "listening": 2.6,
                "thinking": 7.0, "speaking": 1.6}.get(name, 1.6)
        self._glow = (0.7 + 0.5 * self._level if name == "speaking"
                      else 0.6 + 0.4 * (0.5 + 0.5 * math.sin(t * rate)))
        if t >= self._next_gaze:
            self._target = [math.sin(t * 0.7) * 0.6, math.sin(t * 0.9 + 1.0) * 0.4]
            self._next_gaze = t + 1.2
        for i in (0, 1):
            self._gaze[i] += (self._target[i] - self._gaze[i]) * 0.15
        self._openness = 1.0
        if t >= self._next_blink:
            bt = t - self._next_blink
            if bt < 0.18:
                self._openness = abs(math.cos(bt / 0.18 * math.pi))
            else:
                self._next_blink = t + 3.5 + (math.sin(t) + 1.0)
        # The uniforms only matter while the animation is what's on screen.
        # In the menu the ShaderEffect is hidden, and telling its bindings to
        # re-read seven floats thirty times a second would be the small
        # cousin of the waste the two-signal split above removed.
        if self._scene != "menu":
            self.changed.emit()

    # ---- the voice HUD's inputs ----------------------------------------------
    def _voice_inputs(self, doc: dict, now: float, state: str) -> None:
        """What voice_hud.frag needs per frame, from the feed's document.

        The playhead and the have-a-clip flag follow voice_hud.Hud exactly, so
        the shader and the reference agree about *when* a clip is on screen,
        not just what it looks like. The envelope texture is re-encoded only
        when the document's list is a new object — the feed re-parses only on
        a new file, so that is once per utterance.
        """
        hud = self._hud
        levels = doc.get("levels")
        showing = hud.clip_showing(doc, now)
        self._have_clip = 1.0 if showing else 0.0
        self._head = hud.clip_fraction(doc, now) * hud.cols if showing else -1.0
        if showing and levels is not self._env_levels:
            self._env_levels = levels
            self.envelope.set(levels)
            self._env_len = float(len(levels))
            self._env_gen += 1
            self.voiceChanged.emit()
        if state != self._voice_word:
            self._voice_word = state
            self.voiceChanged.emit()

    # ---- the menu's facts -------------------------------------------------
    def _refresh_views(self) -> None:
        """Rebuild what the visible scene shows, and say so only if it moved.

        One snapshot per tick, off the poller's own thread. The network is
        never on the drawing path — that rule predates this app and is why a
        brain that has gone away makes the page say so rather than freezing
        the panel for the length of a TCP timeout.

        Called every tick, and from the scene and page setters, because the
        Loader builds a page the moment the setter emits and the page's
        bindings must find its facts already there — not 33 ms later.
        """
        self._snap = self._net.snapshot()
        views = self._build_views()
        if views != self._views:
            self._views = views
            self.viewsChanged.emit()

    def _build_views(self) -> dict:
        """The view-models for what is on screen right now, and nothing else.

        Only the visible tab is built: the Loader in MenuScene.qml instantiates
        one page at a time, so a hidden tab's facts have no reader. The two
        overlays — the PIN pad and the power menu — sit above every tab and are
        always built while the menu is up; behind a locked gate they are all
        there is. Everything here is plain data (str, int, bool, lists and
        dicts of the same), so the whole-dict compare in _refresh_views is both
        cheap and exact.
        """
        if self._scene != "menu":
            return {}
        snap = self._snap
        out = {"gate": self._gate.view(), "power": self._power.view(),
               "brain": bool(snap.get("whoami"))}
        if not self._gate.unlocked:
            return out
        page = self._page
        if page == 0:
            out["status"] = [{"name": n, "where": w, "state": st,
                              "ink": "#%02x%02x%02x" % tuple(int(c) for c in ink),
                              "detail": " ".join(d for d in det if d)}
                             for n, w, st, ink, det in self._status.rows(snap)]
            # Whole seconds, because that is what the page prints: the raw
            # age moves every tick and would make this the one field that
            # never compared equal.
            age = snap.get("age")
            out["age"] = -1 if age is None else int(round(age))
        elif page == 1:
            out["voice"] = self._voice_page.view(snap)
        elif page == 2:
            out["servos"] = self._servos_page.view(snap)
        elif page == 3:
            out["cart"] = self._cart_page.view(snap)
        elif page == 4:
            out["display"] = self._display_page.view(snap)
        elif page == 5:
            out["wifi"] = self._wifi_page.view(snap)
            out["uplink"] = self._uplink()
        else:
            iv = self._info.view(snap)
            out["info"] = [{"label": lab, "value": val,
                            "ink": "#%02x%02x%02x" % tuple(int(c) for c in ink)}
                           for lab, val, ink in iv["rows"]]
            out["infoPaging"] = {"page": iv["page"], "pages": iv["pages"]}
        return out

    # ---- the menu -------------------------------------------------------
    @Property(str, notify=sceneChanged)
    def scene(self):
        return self._scene

    @scene.setter
    def scene(self, value):
        if value != self._scene:
            self._scene = value
            self._refresh_views()          # before the Loader builds the scene
            self.sceneChanged.emit()

    @Property(int, notify=sceneChanged)
    def page(self):
        return self._page

    @page.setter
    def page(self, value):
        if value != self._page:
            self._page = int(value)
            self._refresh_views()          # before the Loader builds the page
            self.sceneChanged.emit()

    # Every map and list below notifies on viewsChanged, never on changed —
    # test_panel_views.py checks that through the meta-object, because putting
    # one of these back on the 30 Hz signal would quietly restore the half-core
    # the split removed. A tab that is not showing reads as empty here; nothing
    # is bound to it while it is hidden, and the page setter rebuilds before
    # the Loader can look.
    @Property(int, notify=viewsChanged)
    def snapAge(self):
        """Seconds since the poller last heard from the brain; -1 for never."""
        return self._views.get("age", -1)

    @Property("QVariantList", notify=viewsChanged)
    def statusRows(self):
        return self._views.get("status", [])

    @Property("QVariantList", notify=viewsChanged)
    def infoRows(self):
        return self._views.get("info", [])

    @Property("QVariantMap", notify=viewsChanged)
    def infoPaging(self):
        return self._views.get("infoPaging", {"page": 0, "pages": 1})

    @Property("QVariantMap", notify=viewsChanged)
    def voiceView(self):
        return self._views.get("voice", {})

    @Property("QVariantMap", notify=viewsChanged)
    def displayView(self):
        return self._views.get("display", {})

    @Property("QVariantMap", notify=viewsChanged)
    def servosView(self):
        return self._views.get("servos", {})

    @Property("QVariantMap", notify=viewsChanged)
    def cartView(self):
        return self._views.get("cart", {})

    @Property("QVariantMap", notify=viewsChanged)
    def wifiView(self):
        return self._views.get("wifi", {})

    # The other radio. Assembled here rather than in a page class because it
    # is the brain's state plus a scan the panel asked for, and neither belongs
    # to the numpy page that draws the access point.
    PER_PAGE = 5

    def _uplink(self) -> dict:
        up = self._snap.get("uplink") or {}
        scan = self._net.scan_state()
        nets = scan["networks"]
        saved = set(up.get("saved") or [])
        rows = [{"ssid": n["ssid"], "signal": int(n.get("signal") or 0),
                 "secure": bool(n.get("secure")), "saved": n["ssid"] in saved,
                 "current": n["ssid"] == up.get("ssid")}
                for n in nets]
        pages = max(1, -(-len(rows) // self.PER_PAGE))
        page = max(0, min(self._uplink_page, pages - 1))
        start = page * self.PER_PAGE
        return {
            "available": bool(up.get("available")),
            "ssid": up.get("ssid") or "",
            "signal": up.get("signal"),
            "address": up.get("address") or "",
            "saved": up.get("saved") or [],
            "error": up.get("error") or "",
            "busy": scan["busy"],
            "scanned": scan["at"] > 0,
            "rows": rows[start:start + self.PER_PAGE],
            "page": page, "pages": pages,
        }

    @Property("QVariantMap", notify=viewsChanged)
    def uplinkView(self):
        return self._views.get("uplink", {})

    @Property("QVariantMap", notify=viewsChanged)
    def gateView(self):
        return self._views.get("gate", {})

    @Property("QVariantMap", notify=viewsChanged)
    def powerView(self):
        return self._views.get("power", {})

    @Property(bool, notify=viewsChanged)
    def brainReachable(self):
        return bool(self._views.get("brain", False))

    # ---- what a tap does. The page classes still decide; QML only reports
    # that a control was pressed, so a button means the same thing in both
    # renderers for as long as both exist.
    @Slot()
    def toggleVoice(self):
        self._voice_page.toggle(self._net)

    @Slot(str)
    def pickAnimation(self, anim):
        self._display_page.pick(anim, self._net)

    @Slot(str)
    def pickTheme(self, name):
        theme.save_name(name)
        print(f"panel: theme -> {name}, restarting to wear it", flush=True)
        # --reopen-menu, never --menu. Both land on the menu scene, but --menu
        # also means "the daemon killed a renderer to put me here", and closing
        # then asks it to restore instead of changing scene. The daemon rightly
        # refuses — its preset is an ordinary look, not the hidden settings one —
        # so the close did nothing while the gate re-locked underneath it, and
        # the only way out of the menu was the keypad, forever.
        os.execv(sys.executable,
                 [sys.executable, os.path.join(_HERE, "panel.py"),
                  "--reopen-menu", "--page", "4"])

    def _info_page_turn(self, delta):
        self._info.turn_page(delta, len(self._info.rows(self._snap)))

    @Slot(str, float, bool)
    def moveServo(self, name, angle, final):
        self._servos_page.set_angle(name, angle, self._net, final)

    @Slot(bool)
    def setVoiceAtBoot(self, on):
        self._voice_page.toggle_at_boot(self._net, bool(on))

    @Slot(float)
    def setVolume(self, percent):
        self._voice_page.set_volume(self._net, float(percent))

    @Slot()
    def restServos(self):
        self._net.post_rest()

    @Slot()
    def relaxServos(self):
        self._net.post_relax()

    @Slot(int)
    def turnServoPage(self, delta):
        self._servos_page.turn_page(delta)

    @Slot(str)
    def pickCartMode(self, mode):
        self._cart_page.pick_mode(mode, self._net)

    @Slot()
    def cartStop(self):
        self._cart_page.stop_tap(self._net)

    @Property("QVariantList", constant=True)
    def cogHotspot(self):
        """Where the cog's touch target is, from cog_hud — never a second copy.
        The hit area is grown up and left of the drawn icon, because a fingertip
        is wider than a 48px picture."""
        return list(cog_hud.hotspot(W, H))

    @Slot()
    def openMenu(self):
        self._last_touch = time.monotonic()   # the tap that opened it counts
        self.scene = "menu"

    def unlock_for_testing(self) -> None:
        """Open the gate without a PIN, and keep it open across a close.

        Only reachable from --no-gate, which exists so the page harnesses can
        grab every tab without a PIN in the way.
        """
        self._no_gate = True
        self._gate.unlocked = True

    @Slot()
    def closeMenu(self):
        """Leaving the menu is a scene change — unless the daemon opened it.

        When the panel is already running, closing is free: the animation
        underneath never stopped. But the cog also works from the native voice
        HUD, where the daemon kills that renderer and starts this app with
        --menu; closing must then hand the screen back the same way the numpy
        menu did, by asking the daemon to restore whatever was showing.
        """
        if self._opened_as_menu:
            self._net.post_restore()
        # Leave the menu either way. When the daemon put us here it is about to
        # kill this process, so the scene change is harmless; when a restore is
        # *refused* — the daemon's preset is a look, so there is nothing hidden
        # to leave — this is the only thing that gets the operator out. Without
        # it the close was a no-op that still re-locked the gate below, which
        # trapped the panel on the keypad after every theme change.
        self.scene = "anim"
        # Leaving re-locks, and leaves nothing armed behind it. Both matter
        # because this is one long-lived process now: without the first, one
        # unlock outlives the operator who typed it; without the second, a power
        # menu left open would come back on top of the keypad, which is the one
        # overlay that must never be reachable without the PIN.
        if not self._no_gate:
            self._power.hide()
            self._gate.lock()

    @Slot(str)
    def pinKey(self, label):
        self._gate.key(label)

    @Slot()
    def pinStop(self):
        self._gate.stop(self._net)

    @Slot()
    def showPower(self):
        self._power.show()

    @Slot(str)
    def powerTap(self, key):
        self._power.tap(key, self._net)

    @Slot()
    def scanUplink(self):
        self._net.scan_uplink()

    @Slot(str, str)
    def joinUplink(self, ssid, password):
        self._net.post_uplink_join(ssid, password)

    @Slot(str)
    def forgetUplink(self, ssid):
        self._net.post_uplink_forget(ssid)

    @Slot(int)
    def turnUplinkPage(self, delta):
        self._uplink_page = max(0, self._uplink_page + delta)

    @Slot()
    def toggleHotspot(self):
        self._wifi_page.toggle(self._net)

    @Slot(str, result="QVariantMap")
    def hotspotEditor(self, field):
        return self._wifi_page.editor(field, self._snap)

    @Slot(str, str)
    def commitHotspot(self, field, text):
        self._wifi_page.commit(field, text, self._net)

    @Slot(int)
    def turnInfoPage(self, delta):
        self._info_page_turn(delta)

    @Slot(int)
    def turnPage(self, delta):
        from page_display import net_animations
        self._display_page.turn_page(delta, len(net_animations(self._snap)))

    @Property(str, notify=sceneChanged)
    def shader(self):
        return self._shader

    @Property(float, notify=sceneChanged)
    def copper(self):
        return self._copper

    @Property(float, notify=sceneChanged)
    def talk(self):
        return self._talk

    @Property(float, notify=changed)
    def level(self):
        return self._level

    @Property(float, notify=changed)
    def voiceState(self):
        return self._voice

    @Property(float, notify=changed)
    def glow(self):
        return self._glow

    @Property(float, notify=changed)
    def gazeX(self):
        return self._gaze[0]

    @Property(float, notify=changed)
    def gazeY(self):
        return self._gaze[1]

    @Property(float, notify=changed)
    def openness(self):
        return self._openness

    # ---- the voice HUD shader's own inputs ----
    @Property(float, notify=changed)
    def head(self):
        return self._head

    @Property(float, notify=changed)
    def haveClip(self):
        return self._have_clip

    @Property(float, notify=changed)
    def envLen(self):
        return self._env_len

    @Property(int, notify=voiceChanged)
    def envGen(self):
        return self._env_gen

    @Property(str, notify=voiceChanged)
    def voiceWord(self):
        return self._voice_word

    @Property(QVector4D, constant=True)
    def win(self):
        h = self._hud
        return QVector4D(h.wx0, h.wy0, h.wx1, h.wy1)

    @Property(QVector4D, constant=True)
    def meter(self):
        h = self._hud
        return QVector4D(h.mx0, h.my0, h.mw, h.mh)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--anim", default=None,
                    help="show this and ignore state.json (for testing)")
    ap.add_argument("--seconds", type=float, default=0.0)
    # A GPU animation scans out through DRM, so /dev/fb0 no longer shows what is
    # on the panel. This is how a screenshot is taken now — and how a ported
    # page gets compared against the numpy one it replaces.
    ap.add_argument("--grab", metavar="PNG", default="")
    ap.add_argument("--grab-after", type=float, default=1.2,
                    help="seconds to let the scene settle before grabbing")
    # The three the shader verifier needs, the same as gpu_anim.py takes: a
    # frozen clock, a theme that is not the one in state.json, and no cog or
    # sensor panel over the picture. --voice-json is the fourth: the voice
    # HUD is a function of the feed's document, so the verifier hands it one.
    ap.add_argument("--at", type=float, default=-1.0,
                    help="freeze the animation clock here, for a reproducible grab")
    ap.add_argument("--theme", default=None, help="wear this theme instead of state.json's")
    ap.add_argument("--no-overlay", action="store_true",
                    help="leave the cog and sensor panel off, for comparing the picture")
    ap.add_argument("--voice-json", default="",
                    help="read the voice state from this file instead of the live one")
    ap.add_argument("--page", type=int, default=0,
                    help="open the menu on this tab (0-6), for grabbing one page")
    ap.add_argument("--no-gate", action="store_true",
                    help="skip the PIN, for grabbing pages. Weakens nothing that "
                         "matters: the brain gates its own writes by PIN and "
                         "trusts the robot LAN either way, and anyone who can "
                         "pass this flag already has a shell on the Pi.")
    ap.add_argument("--wifi-half", type=int, default=0,
                    help="which half of the WIFI tab to open on, for grabbing")
    ap.add_argument("--power", action="store_true",
                    help="open the power overlay, for grabbing it")
    ap.add_argument("--menu", action="store_true",
                    help="open on the menu scene (the port is not wired to the cog yet)")
    ap.add_argument("--reopen-menu", action="store_true",
                    help="open on the menu scene after restarting ourselves, as "
                         "a theme change does. Deliberately not --menu: that one "
                         "also means the daemon killed a renderer to put us here "
                         "and owes us a restore on the way out, which is only "
                         "true when the daemon spawned the settings preset.")
    args = ap.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "eglfs")
    os.environ.setdefault("QT_QPA_EGLFS_KMS_ATOMIC", "1")
    os.environ.setdefault("QT_QPA_EGLFS_HIDECURSOR", "1")
    # Qt reads the touchscreen itself, so it does not inherit touch.py's
    # software rotation — and this panel's picture is turned 180 by the kernel
    # while the digitiser is not. Without this every tap lands diagonally
    # opposite the finger: the cog does nothing because the tap arrived in the
    # top-left corner. Same device and same angle that touch.py works out, from
    # the same /proc/cmdline, so the two cannot disagree.
    rot = touch.display_rotation()
    if rot:
        dev = touch.find_device()
        # Both halves are needed. Qt 6 prefers libinput, which has no rotation
        # of its own and quietly ignored QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS —
        # taps kept arriving at the opposite corner with nothing in the log to
        # say why. So eglfs's own input is switched off and the evdev touch
        # plugin is loaded by hand, which does take a rotation.
        os.environ.setdefault("QT_QPA_EGLFS_DISABLE_INPUT", "1")
        os.environ.setdefault("QT_QPA_GENERIC_PLUGINS",
                              f"evdevtouch:{dev}:rotate={rot}")
        os.environ.setdefault("QT_QPA_EVDEV_TOUCHSCREEN_PARAMETERS",
                              f"{dev}:rotate={rot}")
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")

    app = QGuiApplication(sys.argv[:1])
    families = load_fonts()
    name = args.theme or theme.load_name()
    ramp = theme.ramp(name)

    overlay = Overlay()
    panel = Panel(args.anim, "menu" if (args.menu or args.reopen_menu) else "anim")
    app.installEventFilter(panel)          # feeds the menu's idle clock
    panel._opened_as_menu = bool(args.menu)     # --reopen-menu must not set this
    panel.page = args.page
    if args.no_gate:
        panel.unlock_for_testing()
    if args.power:
        panel.showPower()
    if args.voice_json:
        from pathlib import Path
        panel.set_feed(VoiceFeed(Path(args.voice_json)))
    if args.at >= 0:
        panel.freeze(args.at)

    view = QQuickView()
    view.engine().addImageProvider("overlay", overlay)
    view.engine().addImageProvider("env", panel.envelope)
    view.engine().addImageProvider("word", panel.word)
    ctx = view.rootContext()
    ctx.setContextProperty("P", panel)
    ctx.setContextProperty("Deep", QColor(*ramp.deep))
    ctx.setContextProperty("Accent", QColor(*ramp.accent))
    ctx.setContextProperty("OkCol", QColor(*ramp.ok))
    ctx.setContextProperty("WarnCol", QColor(*ramp.warn))
    ctx.setContextProperty("FontFamily", families.get(name, ""))
    ctx.setContextProperty("StartWifiHalf", int(args.wifi_half))
    ctx.setContextProperty("FrozenT", float(args.at) if args.at >= 0 else -1.0)
    ctx.setContextProperty("HideOverlay", bool(args.no_overlay))
    # The whole palette as one map, straight off theme.py. QML gets the same
    # numbers the numpy pages read as ui.INK — theme.py stays the one place a
    # theme is defined, as it already is for the C renderer's generated header.
    th = theme.THEMES[name]

    def hexof(rgb):
        return "#%02x%02x%02x" % tuple(int(v) for v in rgb)

    # The theme's type scale, straight from theme.py. String keys: a
    # QVariantMap with integer keys comes back into QML with nothing at
    # Th.px[2], which shows up as "Unable to assign [undefined]".
    px = {str(scale): int(v) for scale, v in th.sizes.items()}
    # One size for the whole tab row — the largest that fits every label,
    # because tabs at mixed sizes look broken rather than tidy. Measured with
    # QFontMetrics against the face Qt will actually draw, plus the theme's
    # letterspacing, which the old atlas arithmetic also had to carry.
    from PySide6.QtGui import QFont, QFontMetrics
    tab_w = (776 - 24 - 8 * 6) // 7 - 16          # minus the button's padding
    titles = [c.title for c in _pages()]
    tab_px = min(int(v) for v in px.values())
    for cand in sorted((int(v) for v in px.values()), reverse=True):
        if cand > int(px["2"]):
            continue
        f = QFont(families.get(name, ""), -1)
        f.setPixelSize(cand)
        f.setLetterSpacing(QFont.AbsoluteSpacing, th.tracking)
        if all(QFontMetrics(f).horizontalAdvance(t) <= tab_w for t in titles):
            tab_px = cand
            break

    ctx.setContextProperty("Th", {
        "px": px, "tabPx": tab_px,
        "name": name, "style": th.style, "radius": th.radius,
        "tracking": th.tracking, "font": families.get(name, ""),
        **{k.lower().replace("_ink", "Ink").replace("_edge", "Edge")
             .replace("_on", "On").replace("_panel", "Panel").replace("_arm", "Arm"):
           hexof(v) for k, v in th.palette.items()},
    })
    view.setSource(QUrl.fromLocalFile(os.path.join(_HERE, "panel.qml")))
    if view.status() != QQuickView.Ready:
        for e in view.errors():
            print("  QML:", e.toString(), file=sys.stderr)
        return 2
    view.setColor(Qt.black)
    view.showFullScreen()

    ticker = QTimer()
    ticker.timeout.connect(panel.tick)
    ticker.start(33)

    watcher = QTimer()

    def poll():
        panel.follow_state()
        if overlay.stale():
            overlay.refresh()
            root = view.rootObject()
            if root is not None:
                root.setProperty("overlayGeneration",
                                 root.property("overlayGeneration") + 1)

    watcher.timeout.connect(poll)
    watcher.start(250)

    if args.grab:
        def grab():
            img = view.grabWindow()
            if not img.save(args.grab):
                print(f"could not write {args.grab}", file=sys.stderr)
                app.exit(2)
                return
            print(f"wrote {args.grab}", flush=True)
            app.quit()
        QTimer.singleShot(int(args.grab_after * 1000), grab)
    if args.seconds:
        QTimer.singleShot(int(args.seconds * 1000), app.quit)
    rc = app.exec()
    # Drop the scene while what its bindings point at is still alive, or every
    # binding logs "cannot read property of null" on the way out.
    view.setSource(QUrl())
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

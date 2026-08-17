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

from PySide6.QtCore import (QObject, QTimer, QUrl, Signal, Slot,  # noqa: E501
                            Property, Qt)   # noqa: E402
from PySide6.QtGui import QColor, QFontDatabase, QGuiApplication, QImage  # noqa: E402
from PySide6.QtQuick import QQuickImageProvider, QQuickView               # noqa: E402

W, H = 800, 480

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
           "flux": "flux", "face": "face", "face-talk": "face"}


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


class Panel(QObject):
    """What the scene needs from the rest of FRED, and which scene to show."""

    changed = Signal()
    sceneChanged = Signal()
    themeChanged = Signal()

    def __init__(self, forced: str | None, scene: str = "anim"):
        super().__init__()
        self._forced = forced
        self._scene = scene                # "anim" | "menu"
        self._opened_as_menu = False       # --menu: the daemon put us here
        self._no_gate = False              # --no-gate: stay open, for grabs
        self._page = 0
        self._snap = {}
        self._rows = []
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
        self._info_rows = []
        self._info_page = {"page": 0, "pages": 1}
        self._voice_view = {}
        self._display_view = {}
        self._servos_view = {}
        self._cart_view = {}
        self._wifi_view = {}
        self._uplink_view = {}
        self._uplink_page = 0
        self._gate_view = {}
        self._power_view = {}
        self._net = Net()
        self._net.start()
        self._anim = forced or "reactor"
        self._shader = ""
        self._copper = 0.0
        self._talk = 0.0
        self._state_mtime = -1.0
        self._feed = VoiceFeed()
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
    def tick(self) -> None:
        t = time.monotonic() - self._start
        # One snapshot per tick, off the poller's own thread. The network is
        # never on the drawing path — that rule predates this app and is why a
        # brain that has gone away makes the page say so rather than freezing
        # the panel for the length of a TCP timeout.
        self._snap = self._net.snapshot()
        iv = self._info.view(self._snap)
        self._info_rows = [{"label": lab, "value": val,
                            "ink": "#%02x%02x%02x" % tuple(int(c) for c in ink)}
                           for lab, val, ink in iv["rows"]]
        self._info_page = {"page": iv["page"], "pages": iv["pages"]}
        self._voice_view = self._voice_page.view(self._snap)
        self._display_view = self._display_page.view(self._snap)
        self._servos_view = self._servos_page.view(self._snap)
        self._cart_view = self._cart_page.view(self._snap)
        self._wifi_view = self._wifi_page.view(self._snap)
        self._uplink_view = self._uplink()
        self._gate_view = self._gate.view()
        self._power_view = self._power.view()
        self._rows = [{"name": n, "where": w, "state": st,
                       "ink": "#%02x%02x%02x" % tuple(int(c) for c in ink),
                       "detail": " ".join(d for d in det if d)}
                      for n, w, st, ink, det in self._status.rows(self._snap)]
        self._feed.poll()
        name = self._feed.state()
        self._voice = {"idle": 0.0, "listening": 1.0,
                       "thinking": 2.0, "speaking": 3.0}.get(name, 0.0)
        self._level = float(self._feed.level() or 0.0)
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
        self.changed.emit()

    # ---- the menu -------------------------------------------------------
    @Property(str, notify=sceneChanged)
    def scene(self):
        return self._scene

    @scene.setter
    def scene(self, value):
        if value != self._scene:
            self._scene = value
            self.sceneChanged.emit()

    @Property(int, notify=sceneChanged)
    def page(self):
        return self._page

    @page.setter
    def page(self, value):
        if value != self._page:
            self._page = int(value)
            self.sceneChanged.emit()

    @Property("QVariantMap", notify=changed)
    def snap(self):
        return self._snap

    @Property("QVariantList", notify=changed)
    def statusRows(self):
        return self._rows

    @Property("QVariantList", notify=changed)
    def infoRows(self):
        return self._info_rows

    @Property("QVariantMap", notify=changed)
    def infoPaging(self):
        return self._info_page

    @Property("QVariantMap", notify=changed)
    def voiceView(self):
        return self._voice_view

    @Property("QVariantMap", notify=changed)
    def displayView(self):
        return self._display_view

    @Property("QVariantMap", notify=changed)
    def servosView(self):
        return self._servos_view

    @Property("QVariantMap", notify=changed)
    def cartView(self):
        return self._cart_view

    @Property("QVariantMap", notify=changed)
    def wifiView(self):
        return self._wifi_view

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

    @Property("QVariantMap", notify=changed)
    def uplinkView(self):
        return self._uplink_view

    @Property("QVariantMap", notify=changed)
    def gateView(self):
        return self._gate_view

    @Property("QVariantMap", notify=changed)
    def powerView(self):
        return self._power_view

    @Property(bool, notify=changed)
    def brainReachable(self):
        return bool(self._snap.get("whoami"))

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
        os.execv(sys.executable,
                 [sys.executable, os.path.join(_HERE, "panel.py"),
                  "--menu", "--page", "4"])

    def _info_page_turn(self, delta):
        self._info.turn_page(delta, len(self._info.rows(self._snap)))

    @Slot(str, float, bool)
    def moveServo(self, name, angle, final):
        self._servos_page.set_angle(name, angle, self._net, final)

    @Slot()
    def restServos(self):
        self._net.post_rest()

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
        else:
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
    name = theme.load_name()
    ramp = theme.ramp(name)

    overlay = Overlay()
    panel = Panel(args.anim, "menu" if args.menu else "anim")
    panel._opened_as_menu = bool(args.menu)
    panel.page = args.page
    if args.no_gate:
        panel.unlock_for_testing()
    if args.power:
        panel.showPower()

    view = QQuickView()
    view.engine().addImageProvider("overlay", overlay)
    ctx = view.rootContext()
    ctx.setContextProperty("P", panel)
    ctx.setContextProperty("Deep", QColor(*ramp.deep))
    ctx.setContextProperty("Accent", QColor(*ramp.accent))
    ctx.setContextProperty("OkCol", QColor(*ramp.ok))
    ctx.setContextProperty("WarnCol", QColor(*ramp.warn))
    ctx.setContextProperty("FontFamily", families.get(name, ""))
    ctx.setContextProperty("StartWifiHalf", int(args.wifi_half))
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

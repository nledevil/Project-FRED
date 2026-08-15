"""Shut the robot down from the screen on its chest, for packing up.

At the end of an event three machines have to be switched off and the only one
you are standing in front of is this one. Doing it by SSH means a laptop; doing
it by holding power buttons means finding them inside a robot.

**The order is not a preference, it is a constraint, and it is the whole reason
this is a menu rather than three buttons on a page.**

* The **head** goes first. The NUC bridges both wired NICs — it *is* the switch
  for the robot LAN — so the moment it is off, nothing on this Pi can reach the
  head to ask it to stop. Powering the brain first strands the head running in a
  crate.
* The **NUC** goes second.
* This **chest Pi** goes last, because it is drawing the button. Switch it off
  first and you have thrown away the screen you needed for the other two.

Every action takes two taps. The first arms it and says so; the second does it.
That is the e-stop's pattern, for the same reason — the cost of an accidental
tap here is a robot that goes dark in front of a queue of children.
"""
from __future__ import annotations

import subprocess
import threading
import time

import menu_ui as ui

# The overlay covers the page but not the title bar, so the way out stays where
# it has been all along.
X0, X1 = 60, 740
Y0, Y1 = 96, 452

# Laid out so nothing overlaps anything: the first version had CANCEL sitting on
# top of the CHEST PI row, and since CANCEL is hit-tested first, tapping the
# chest simply shut the menu. Every band below is checked against its neighbours
# in tools/test_power_menu.py rather than by eye.
ALL_BTN = (X0 + 24, Y0 + 64, X1 - 24, Y0 + 128)     # 160 .. 224
ROW_Y = Y0 + 140                                    # first row top: 236
ROW_H = 52                                          # pitch; button is ROW_H - 6
CANCEL = (X0 + 24, Y1 - 54, X1 - 24, Y1 - 10)       # 398 .. 442

ARM_SECONDS = 6.0          # how long a first tap stays armed before it forgets


def poweroff_this_pi() -> None:
    """Switch this machine off. Separated out so it can be replaced in a test.

    Without a seam here the only way to exercise the ordering was to actually
    power the chest Pi off, which is exactly what happened the first time this
    was tested — the logic is pure and deserves to be checkable without costing
    someone a walk to the robot.
    """
    subprocess.run(["sudo", "-n", "systemctl", "poweroff"], check=False)

# Ordered. Iterated in this sequence for "all", and drawn in it so the screen
# says what will happen in the order it will happen.
MACHINES = (
    ("head", "HEAD PI", "servos and camera"),
    ("nuc", "BRAIN (NUC)", "speech, vision, the panel"),
    ("chest", "CHEST PI", "this screen - goes last"),
)


class PowerMenu:
    def __init__(self, log=print, poweroff=poweroff_this_pi, settle=4.0):
        self.open = False
        self._log = log
        # Injectable: `poweroff` is the local, irreversible one, and `settle` is
        # how long each machine is given to go down before the next request
        # takes away its network. A test replaces both.
        self._poweroff = poweroff
        self._settle = float(settle)
        self._armed: str | None = None      # "all" or a machine key
        self._armed_at = 0.0
        self._doing: str | None = None
        self._failed: list[str] = []        # machines that would not take it
        self._all = ui.Button(*ALL_BTN, "SHUT DOWN ALL", scale=3)
        self._rows = [
            (key, ui.Button(X0 + 24, ROW_Y + i * ROW_H,
                            X1 - 24, ROW_Y + i * ROW_H + ROW_H - 6, label, scale=2))
            for i, (key, label, _) in enumerate(MACHINES)]
        self._cancel = ui.Button(*CANCEL, "CANCEL", scale=2)

    # ---- state -----------------------------------------------------------
    def show(self) -> None:
        self.open, self._armed, self._doing = True, None, None

    def hide(self) -> None:
        self.open, self._armed, self._doing = False, None, None

    def _is_armed(self, key: str) -> bool:
        return (self._armed == key
                and time.monotonic() - self._armed_at < ARM_SECONDS)

    def _arm(self, key: str) -> None:
        self._armed, self._armed_at = key, time.monotonic()

    # ---- doing it --------------------------------------------------------
    def _fire(self, key: str, net) -> None:
        """Run the shutdown off the touch thread so the screen keeps drawing."""
        self._doing = key
        self._armed = None
        self._failed = []
        targets = [k for k, _, _ in MACHINES] if key == "all" else [key]
        self._log(f"power off: {' -> '.join(targets)}")
        threading.Thread(target=self._run, args=(targets, net),
                         name="poweroff", daemon=True).start()

    def _run(self, targets: list[str], net) -> None:
        for key in targets:
            if key == "chest" and self._failed:
                break                       # see below: keep the screen alive
            try:
                if key == "chest":
                    # Local, and last. Nothing after this line matters: the
                    # screen this is drawn on is about to go.
                    self._poweroff()
                elif not net.post_poweroff(key):
                    # Reported, not swallowed. A head that quietly refuses is a
                    # head left running in a crate, and the screen is about to
                    # be the last thing anyone looks at.
                    self._failed.append(key)
                    self._log(f"power off {key}: refused or unreachable")
            except Exception as exc:            # noqa: BLE001 - keep going
                # A machine that is already off, or unreachable, must not stop
                # the ones after it — that is exactly the case where the rest
                # still need switching off.
                self._failed.append(key)
                self._log(f"power off {key}: {exc}")
            # Let each one actually go down before taking away its network.
            if key != targets[-1]:
                time.sleep(self._settle)
        if self._failed:
            # Do not switch off the screen that is the only way to say something
            # went wrong. Whoever is packing up needs to see this.
            self._doing = None

    # ---- input -----------------------------------------------------------
    def on_touch(self, kind: str, x: int, y: int, net) -> bool:
        """True if the overlay swallowed the touch — it always does while open."""
        if not self.open:
            return False
        if kind != "down":
            return True                       # swallow the up/move as well
        if self._doing:
            return True                       # nothing to press while it happens
        if self._cancel.hit(x, y):
            self.hide()
            return True
        if self._all.hit(x, y):
            self._fire("all", net) if self._is_armed("all") else self._arm("all")
            return True
        for key, button in self._rows:
            if button.hit(x, y):
                self._fire(key, net) if self._is_armed(key) else self._arm(key)
                return True
        return True

    # ---- drawing ---------------------------------------------------------
    def draw(self, frame, snap: dict) -> None:
        if not self.open:
            return
        # Dim the page underneath rather than replacing it: this is a thing in
        # front of the menu, not another screen, and it should be obvious that
        # cancelling puts you back where you were.
        frame *= 0.25
        ui.readout(frame, X0, Y0, X1, Y1, weight=2)
        ui.text(frame, "POWER OFF", X0 + 24, Y0 + 18, ui.INK, 3)

        if self._doing:
            what = ("ALL THREE" if self._doing == "all"
                    else dict((k, l) for k, l, _ in MACHINES)[self._doing])
            ui.text(frame, f"SHUTTING DOWN {what}", X0 + 24, Y0 + 92, ui.WARN_INK, 3)
            ui.text(frame, "HEAD FIRST, THEN THE BRAIN, THIS SCREEN LAST",
                    X0 + 24, Y0 + 140, ui.DIM_INK, 2)
            ui.text(frame, "WAIT FOR EVERY LIGHT TO GO OUT BEFORE UNPLUGGING",
                    X0 + 24, Y0 + 172, ui.DIM_INK, 2)
            return

        armed_all = self._is_armed("all")
        self._all.draw(frame, on=armed_all,
                       ink=ui.WARN_INK if armed_all else ui.INK,
                       label="TAP AGAIN TO CONFIRM" if armed_all else None)
        # Beside the title, not under it: the title is scale 3 and a line at
        # Y0+40 ran straight through its descenders.
        if self._failed:
            names = ", ".join(dict((k, l) for k, l, _ in MACHINES)[k]
                              for k in self._failed)
            ui.text_right(frame, f"{names} DID NOT ANSWER - SCREEN LEFT ON",
                          X1 - 24, Y0 + 26, ui.BAD_INK, 1)
        else:
            ui.text_right(frame, "HEAD, THEN BRAIN, THEN THIS SCREEN",
                          X1 - 24, Y0 + 26, ui.DIM_INK, 1)

        for (key, label, note), (_, button) in zip(MACHINES, self._rows):
            armed = self._is_armed(key)
            ink = ui.WARN_INK if armed else ui.INK
            if armed:
                button.draw(frame, on=True, ink=ink, label="TAP AGAIN TO CONFIRM")
                continue
            # Drawn by hand rather than through the button's centred label: the
            # label and its note share the row, and a centred label has no idea
            # the note is there. In the widest typeface "BRAIN (NUC)" ran
            # straight through "SPEECH, VISION, THE PANEL".
            bx0, by0, bx1, _ = button.rect
            button.draw(frame, on=False, ink=ink, label="")
            ly = by0 + (ROW_H - 6 - ui.line_height(2)) // 2
            ui.text(frame, label, bx0 + 20, ly, ink, 2)
            note_w = ui.text_width(note.upper(), 1)
            used = bx0 + 20 + ui.text_width(label, 2) + 24
            if used + note_w <= bx1 - 20:
                ui.text_right(frame, note.upper(), bx1 - 20,
                              by0 + (ROW_H - 6 - ui.line_height(1)) // 2,
                              ui.DIM_INK, 1)

        self._cancel.draw(frame, ink=ui.DIM_INK)

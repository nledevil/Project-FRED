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


# The overlay covers the page but not the title bar, so the way out stays where
# it has been all along.

# Laid out so nothing overlaps anything: the first version had CANCEL sitting on
# top of the CHEST PI row, and since CANCEL is hit-tested first, tapping the
# chest simply shut the menu. Every band below is checked against its neighbours
# in tools/test_power_menu.py rather than by eye.

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

    def view(self) -> dict:
        """The overlay as data: what is armed, what is happening, what failed.

        Split out of draw() so the Qt panel keeps the arm-then-confirm and the
        order — head, then the brain, then this screen last, because the chest
        taking itself down first would leave nothing able to ask the others.
        """
        return {
            "open": self.open,
            "doing": self._doing or "",
            "failed": list(self._failed),
            "allArmed": self._is_armed("all"),
            "machines": [{"key": k, "label": label, "why": why,
                          "armed": self._is_armed(k)}
                         for k, label, why in MACHINES],
        }

    def tap(self, key: str, net) -> None:
        """One tap on a control: arms it, or fires it if it was already armed."""
        if self._doing:
            return                            # nothing to press while it happens
        if key == "cancel":
            self.hide()
            return
        self._fire(key, net) if self._is_armed(key) else self._arm(key)

    # ---- drawing ---------------------------------------------------------

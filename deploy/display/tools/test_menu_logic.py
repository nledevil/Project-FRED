#!/usr/bin/env python3
"""The menu's judgements, tested without a renderer.

The numpy menu retired on 2026-08-16 and took its pixel tests with it. What
those tests were really protecting was never the pixels — it was the calls the
pages make: whether the head is up, which cart mode hands a moving machine to a
hand controller, that the power menu takes the chest down last. Those all live
in view()/rows() and the tap handlers now, and this drives them directly.

The Qt layer above is layout, checked by looking at panel.py --grab output;
what the panel *claims about the robot* is checked here.

    python3 tools/test_menu_logic.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [_HERE, os.path.dirname(_HERE)]      # repo layout and flattened Pi layout

import pin_gate                                      # noqa: E402
import power_menu as pm                              # noqa: E402
from page_cart import CartPage                       # noqa: E402
from page_display import DisplayPage                 # noqa: E402
from page_info import InfoPage                       # noqa: E402
from page_servos import ServosPage                   # noqa: E402
from page_status import StatusPage                   # noqa: E402
from page_voice import VoicePage                     # noqa: E402
from page_wireless import WirelessPage               # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:                                    # noqa: PLR0915
    print("status: what the panel claims about the machines")
    rows = StatusPage().rows({})
    check("five rows with nothing known", len(rows) == 5)
    check("an empty snapshot reads as NO LINK, not as fine",
          rows[0][2] == "NO LINK", str(rows[0][:3]))

    print("info: paging, and the CPU-fallback warning")
    page = InfoPage()
    page.rows = lambda snap: [("L%d" % i, "V%d" % i, (1, 2, 3)) for i in range(20)]
    v = page.view({})
    check("thirteen lines a page", len(v["rows"]) == 13 and v["pages"] == 2)
    page.turn_page(5, 20)
    check("...and the last page clamps", page.view({})["page"] == 1)
    real = InfoPage().rows({"whoami": {"inference": {"library": "cpu"}}})
    check("a CPU fallback is called out as a fault",
          any("CPU" in r[1] for r in real), str(real[0]))

    print("voice: when pressing the button would lie")
    check("no brain reads NO LINK", VoicePage().view({})["label"] == "NO LINK")
    check("voice unavailable is not pressable",
          not VoicePage().view({"nuc": {"voice": {"available": False}}})["live"])
    on = VoicePage().view({"nuc": {"voice": {"available": True, "listening": True}}})
    check("listening reads ON and is pressable", on["label"] == "ON" and on["live"])

    print("display: the grid, the pager, and off-is-not-healthy")
    d = DisplayPage()
    anims = [{"id": f"a{i}", "label": f"A{i}"} for i in range(19)]
    snap = {"chest": {"animations": anims,
                      "display": {"animation": "off", "running": True}}}
    v = d.view(snap)
    check("eight a page, three pages", len(v["animations"]) == 8 and v["pages"] == 3)
    d.turn_page(9, 19)
    check("the pager clamps at the end", d.view(snap)["page"] == 2)
    snap2 = {"chest": {"animations": [{"id": "off", "label": "Off"}],
                       "display": {"animation": "off", "running": True}}}
    lit = DisplayPage().view(snap2)["animations"][0]
    check("a lit 'off' is dim, not healthy-green", lit["ink"] == "dim", str(lit))

    print("servos: order, paging, the finger, the flush")
    p = ServosPage()
    servos = {f"servo_{i}": {"current": 90, "min_angle": 0, "max_angle": 180,
                             "rest_angle": 90} for i in range(14)}
    net_snap = {"nuc": {"servos": servos}}
    v = p.view(net_snap)
    check("six a page, three pages for FRED's fourteen",
          len(v["rows"]) == 6 and v["pages"] == 3)
    sent = []

    class FakeNet:
        def snapshot(self):
            return net_snap

        def post_move(self, name, angle):
            sent.append((name, angle))

    p.set_angle("servo_0", 45.0, FakeNet(), final=True)
    check("a released drag always sends", sent == [("servo_0", 45)], str(sent))
    check("...and the view shows the finger, not the stale robot",
          p.view(net_snap)["rows"][0]["angle"] == 45.0)
    blocked = ServosPage().view({"nuc": {"servos": servos,
                                         "handoff": {"released": True}}})
    check("a handoff blocks the sliders and says why",
          blocked["blocked"] == "HANDED OFF TO MYROBOTLAB")

    print("cart: the stop's arm-then-confirm")
    c = CartPage()
    latched = {"chest": {"cart": {"connected": True, "estop": True,
                                  "controller_mode": "off"}}}
    fired = []

    class CartNet:
        def post_cart_stop(self):
            fired.append("stop")

        def post_cart_clear_estop(self):
            fired.append("clear")

    v = c.view(latched)
    check("latched reads CLEAR and says nothing will move",
          v["stopLabel"] == "CLEAR" and "LATCHED" in v["why"])
    c.stop_tap(CartNet())
    check("one tap arms and clears nothing",
          c.view(latched)["armed"] and fired == [], str(fired))
    c.stop_tap(CartNet())
    check("the second tap clears", fired == ["clear"], str(fired))
    c2 = CartPage()
    c2.view({"chest": {"cart": {"connected": True, "estop": False,
                                "controller_mode": "off"}}})
    c2.stop_tap(CartNet())
    check("unlatched, one tap stops immediately — no confirm on STOP",
          fired[-1] == "stop", str(fired))
    modes = CartPage().view({"chest": {"cart": {"connected": True, "estop": False,
                                                "controller_mode": "takeover"}}})
    take = next(m for m in modes["modes"] if m["mode"] == "takeover")
    check("handing the cart to a controller is coloured as a warning",
          take["on"] and take["ink"] == "warn", str(take))

    print("wireless: the password rules")
    w = WirelessPage()
    check("the password editor is never seeded",
          w.editor("psk", {"hotspot": {"ssid": "fred"}})["value"] == "")
    posts = []

    class WifiNet:
        def post_hotspot_config(self, ssid, psk):
            posts.append((ssid, psk))

    w.commit("ssid", "NewName", WifiNet())
    check("a name alone does not save", posts == [], w._note)
    w.commit("psk", "longenough", WifiNet())
    check("name plus password saves", posts == [("NewName", "longenough")])
    check("...and the password is not kept for the session", w._pending_psk == "")

    print("the PIN gate: the two ends agree")
    # The brain makes the material; the chest checks against it. If these ever
    # drift apart, the PIN opens one surface and not the other.
    sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "..", ".."))
    try:
        from inmoov import auth
    except ImportError:
        # The chest Pi does not carry the inmoov package; the two-ends check
        # runs on the NUC, where both halves live.
        auth = None
        print("  (inmoov.auth not importable here; brain-side check skipped)")
    material2 = None
    if auth is not None:
        material2 = auth.make_material("4271")
        check("the chest accepts a PIN the brain hashed",
              pin_gate.check("4271", material2))
        check("...and rejects a wrong one", not pin_gate.check("4270", material2))
        check("the brain accepts what the chest would",
              auth.check_pin({"auth": {"pin": material2}}, "4271"))
    check("a short PIN is refused",
          not pin_gate.check("427", material2 or {"salt": "a", "digest": "b"}))
    check("empty material means nothing verifies", not pin_gate.check("4271", {}))
    check("junk material does not raise",
          not pin_gate.check("4271", {"salt": "zz", "hash": "nope"}))

    print("the PIN gate: the cache")
    import pathlib as _pl
    original = pin_gate.PIN_PATH
    pin_gate.PIN_PATH = _pl.Path(_HERE) / "pin-test.json"
    try:
        pin_gate.PIN_PATH.unlink(missing_ok=True)
        check("no cache means no PIN", not pin_gate.is_set(pin_gate.load()))
        if material2 is not None:
            check("saving works", pin_gate.save(material2))
            check("...and round-trips", pin_gate.load() == material2)
            check("the cached copy still verifies",
                  pin_gate.check("4271", pin_gate.load()))
        check("an empty push clears it",
              pin_gate.save({}) and not pin_gate.is_set(pin_gate.load()))
    finally:
        pin_gate.PIN_PATH.unlink(missing_ok=True)
        pin_gate.PIN_PATH = original

    print("the PIN gate: the pad")
    material = {"salt": "ab", "digest": "cd", "rounds": 1}
    g = pin_gate.PinPad(material, log=lambda *a: None)
    for k in ("1", "2", "3"):
        g.key(k)
    check("three taps show three dots", g.view()["filled"] == 3)
    g.key("DEL")
    check("DEL takes one back", g.view()["filled"] == 2)
    g.key("CLEAR")
    check("CLEAR takes them all", g.view()["filled"] == 0)
    # FREE_TRIES is 5: the sixth wrong guess starts the back-off.
    for _ in range(6):
        for k in ("1", "2", "3", "4"):
            g.key(k)
    check("the sixth wrong guess locks the pad", g.view()["locked"],
          g.view()["message"])
    check("...and the keys go dead while it is", (g.key("1"), g.view()["filled"])[1] == 0)
    stopped = []

    class GateNet:
        def post_cart_stop(self):
            stopped.append(True)

    g.stop(GateNet())
    check("the stop works while locked — no PIN between a person and it",
          stopped == [True])

    # Leaving the menu must ask again. This is not hypothetical: the gate was a
    # separate process until the Qt port, so closing it re-locked by itself, and
    # afterwards one unlock lasted until the next reboot.
    real = {"salt": "aabb", "hash": pin_gate.hashlib.pbkdf2_hmac(
        "sha256", b"4271", bytes.fromhex("aabb"), 1).hex(), "iterations": 1}
    h = pin_gate.PinPad(real, log=lambda *a: None)
    check("a PIN that is set starts locked", not h.view()["unlocked"])
    for k in ("4", "2", "7", "1"):
        h.key(k)
    check("the right PIN opens it", h.view()["unlocked"])
    h.lock()
    check("leaving locks it again", not h.view()["unlocked"])
    check("...and clears the digits behind it", h.view()["filled"] == 0)

    # The one thing lock() must NOT do. Closing the menu five times over would
    # otherwise buy five fresh free tries each time, and tap-close-tap is a lot
    # faster than thinking of a number.
    b = pin_gate.PinPad(real, log=lambda *a: None)
    for _ in range(6):
        for k in ("9", "9", "9", "9"):
            b.key(k)
    check("six wrong guesses lock the pad out", b.view()["locked"])
    b.lock()
    check("leaving does not reset the back-off", b.view()["locked"],
          b.view()["message"])

    # A robot with no PIN set must stay open, or the touchscreen bricks itself.
    n = pin_gate.PinPad({}, log=lambda *a: None)
    n.lock()
    check("no PIN set means leaving still leaves it open", n.view()["unlocked"])

    print("power: the order that cannot be a matter of opinion")

    def drive(fail=(), pick="all", taps=2):
        order = []

        class Net:
            def post_poweroff(self, machine):
                order.append(machine)
                return machine not in fail

        menu = pm.PowerMenu(log=lambda *_: None,
                            poweroff=lambda: order.append("chest"), settle=0.02)
        menu.show()
        for _ in range(taps):
            menu.tap(pick, Net())
        time.sleep(0.4)
        return order, menu

    order, menu = drive(taps=1)
    check("one tap arms and does not fire", order == [] and menu.view()["allArmed"])
    order, _ = drive(taps=2)
    check("the second tap fires all three", len(order) == 3, str(order))
    check("the head goes before the brain",
          order.index("head") < order.index("nuc"),
          "the NUC is the switch; after it, the head is unreachable")
    check("this screen goes last", order[-1] == "chest")
    order, menu = drive(fail=("head",))
    check("a refusal spares the chest so the failure can be read",
          "chest" not in order and menu.view()["failed"] == ["head"], str(order))
    for pick in ("head", "nuc", "chest"):
        order, _ = drive(pick=pick)
        check(f"{pick} alone powers off only {pick}", order == [pick], str(order))
    order, menu = drive(taps=1)
    menu.tap("cancel", None)
    check("cancel closes and fires nothing", not menu.view()["open"] and order == [])

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

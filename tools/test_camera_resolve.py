#!/usr/bin/env python3
"""Finding the wide camera by name, because its number moves.

The PanaCast was greyed out in the panel with one line of explanation at
startup, and the cause was that ``spotter.device`` said 0 while the camera had
come up as /dev/video1. Nothing had been touched: changing the *audio* hardware
renumbered the video nodes. During one debugging session the number moved twice.

So the config takes a card name, and this checks the matching — including the
part that is easy to get wrong, which is that a camera can present several nodes
and only the first streams. The PanaCast offers two; the one the device calls
its own index 0 is the real one, and that is the kernel's answer rather than a
guess about numbering.

Builds a fake sysfs tree, so it runs with no camera attached.

    python3 tools/test_camera_resolve.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import wide_spotter as W                        # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def _hub_port_from(devpath: str):
    """W._hub_port's logic against a literal path, with no sysfs to resolve."""
    from pathlib import Path as P
    import re as _re
    dev = P(devpath)
    for parent in [dev, *dev.parents][:5]:
        name = parent.name
        if not _re.fullmatch(r"\d+-\d+(\.\d+)*", name):
            continue
        if "." in name:
            hub, _, port = name.rpartition(".")
            return hub, port
        bus, _, port = name.partition("-")
        return bus, port
    return None


def fake_sysfs(nodes: dict[str, tuple[str, int]]) -> str:
    """nodes: {"video1": ("Jabra PanaCast", 0), ...} -> a sysfs root."""
    root = tempfile.mkdtemp()
    for node, (name, index) in nodes.items():
        d = Path(root) / node
        d.mkdir()
        (d / "name").write_text(name + "\n")
        (d / "index").write_text(f"{index}\n")
    return root


def main() -> int:
    print("a name finds the camera wherever it landed")
    # The exact pair seen on the robot, at both numberings it took.
    shifted = fake_sysfs({"video1": ("Jabra PanaCast", 0),
                          "video2": ("Jabra PanaCast", 1)})
    normal = fake_sysfs({"video0": ("Jabra PanaCast", 0),
                         "video1": ("Jabra PanaCast", 1)})
    for label, root, want in [("shifted up by one", shifted, 1),
                              ("back at zero", normal, 0)]:
        got = W._resolve_device("PanaCast", root)
        check(f"{label} -> video{want}", got == want, str(got))
    check("matching is case-insensitive and partial",
          W._resolve_device("panacast", shifted) == 1)

    print("the streaming node is chosen, not the lowest number")
    # The trap: the node that streams is not always the lower /dev/videoN. Sort
    # by what the *device* calls index 0.
    inverted = fake_sysfs({"video0": ("Jabra PanaCast", 1),
                           "video1": ("Jabra PanaCast", 0)})
    check("picks the device's own index 0, not video0",
          W._resolve_device("PanaCast", inverted) == 1,
          str(W._resolve_device("PanaCast", inverted)))

    print("other cameras are not mistaken for it")
    mixed = fake_sysfs({"video0": ("Integrated Webcam", 0),
                        "video2": ("Jabra PanaCast", 0),
                        "video3": ("Jabra PanaCast", 1)})
    check("skips the laptop webcam", W._resolve_device("PanaCast", mixed) == 2,
          str(W._resolve_device("PanaCast", mixed)))
    check("a name that matches nothing is None, not 0",
          W._resolve_device("Logitech", mixed) is None)
    check("no cameras at all is None", W._resolve_device("PanaCast", fake_sysfs({})) is None)
    check("an empty name is None rather than everything",
          W._resolve_device("", mixed) is None)

    print("an explicit number still means that number")
    # Nobody's existing config should change meaning under them.
    check("int 0 stays 0", W._resolve_device(0, mixed) == 0)
    check("int 2 stays 2", W._resolve_device(2, mixed) == 2)
    check("the string '1' is a number, not a name",
          W._resolve_device("1", mixed) == 1)

    print("the USB port is derived from sysfs, not parsed out of a tool")
    # uhubctl names a port as (hub location, port). The kernel already encodes
    # that in the device path — "4-1.4" is port 4 of hub 4-1 by construction —
    # so this reads it rather than scraping uhubctl's output.
    for path, want in [("/sys/devices/pci0000:00/usb4/4-1/4-1.4/4-1.4:1.0", ("4-1", "4")),
                       ("/sys/devices/pci0000:00/usb4/4-1/4-1.2/4-1.2:1.0", ("4-1", "2")),
                       ("/sys/devices/pci0000:00/usb2/2-3/2-3.1.4/2-3.1.4:1.0", ("2-3.1", "4")),
                       ("/sys/devices/pci0000:00/usb4/4-2/4-2:1.0", ("4", "2"))]:
        got = _hub_port_from(path)
        check(f"{path.rsplit('/', 2)[1]:10} -> hub {want[0]} port {want[1]}",
              got == want, str(got))

    print("the node list is readable when it fails")
    listing = W._video_nodes(mixed)
    check("names every node it can see", len(listing) == 3, str(listing))
    check("...with the name attached", any("Jabra PanaCast" in x for x in listing),
          str(listing))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): " + "; ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

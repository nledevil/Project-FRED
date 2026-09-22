#!/usr/bin/env python3
"""FRED paints (2026-09-21): the promises the picture path rests on.

"Draw me a dragon" goes: Claude calls make_picture -> commands._make_picture
-> images.ImageMaker on a thread -> a PNG under logs/pictures -> re-encoded
for the chest -> display_control's /api/picture -> panel.py shows it. The
painter itself is stubbed here; what is pinned is everything around it:

**The reply is never held by the brush.** A painter that finishes inside
wait_s gets "here it is"; one that does not gets "on the way", and the "it's
ready" announcement is registered only then — so a fast one is never
announced twice, and a slow one is announced exactly once.

**No painter is a sentence, not a crash.** Off, uninstalled, no key, busy:
each is a spoken reason, and a failure mid-paint lands in status().

**Pictures are kept and pruned**, latest.png always the newest.

**The chest re-encode fits 800x480** and is a JPEG a Pi decodes in a blink.

**The daemon's endpoint says no to junk**: not base64, not an image, too
big — and yes to a real JPEG, with a counter the panel can follow.

    ./venv/bin/python tools/test_pictures.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy" / "display"))

from inmoov import commands, images                              # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def png_bytes(w: int = 64, h: int = 64) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 200, 90)).save(buf, "PNG")
    return buf.getvalue()


class FakePainter(images.ImageMaker):
    """An ImageMaker whose brush is a sleep and a green square."""
    delay = 0.0
    fail = False

    def backend(self) -> str:
        return "fake"

    def _paint(self, prompt, backend):
        time.sleep(self.delay)
        if self.fail:
            raise images.ImageError("brush broke")
        return png_bytes()


class FakeDisplay:
    shown: list = []

    def configured(self):
        return True

    def show_picture(self, jpeg, caption="", hold_s=180):
        FakeDisplay.shown.append((jpeg[:3], caption, hold_s))
        return {"ok": True}


def main() -> int:
    said: list = []
    with tempfile.TemporaryDirectory() as td:
        print("the reply is never held by the brush")
        maker = FakePainter({"wait_s": 1.0, "hold_s": 90}, announce=said.append,
                            pictures_dir=Path(td))
        ctx = types.SimpleNamespace(images=maker, display=FakeDisplay())
        FakePainter.delay = 0.05
        out = commands.execute_action(ctx, "make_picture", prompt="  a   dragon ")
        check("a fast painter: 'here it is', with the prompt tidied",
              out.startswith("Here it is") and "a dragon" in out, out)
        maker.wait(2)
        time.sleep(0.05)
        check("...and the picture reached the chest as a JPEG with its caption and hold",
              FakeDisplay.shown and FakeDisplay.shown[-1] == (b"\xff\xd8\xff", "a dragon", 90),
              str(FakeDisplay.shown[-1:]))
        check("...and nothing was announced", said == [], str(said))

        FakePainter.delay = 1.6
        out = commands.execute_action(ctx, "make_picture", prompt="a slow sunset")
        check("a slow painter: 'on the way'", out.startswith("I'm painting it now"), out)
        check("busy while it paints", maker.busy())
        out2 = commands.execute_action(ctx, "make_picture", prompt="another")
        check("asking again meanwhile is a sentence, not a second job",
              out2.startswith("I can't paint that right now") and "still painting" in out2, out2)
        maker.wait(3)
        time.sleep(0.1)
        check("...announced exactly once when it lands",
              said == ["Your picture is ready. It's on my chest."], str(said))
        check("...and it reached the chest", FakeDisplay.shown[-1][1] == "a slow sunset")

        said.clear()
        FakePainter.delay = 1.4
        FakePainter.fail = True
        out = commands.execute_action(ctx, "make_picture", prompt="a doomed one")
        maker.wait(3)
        time.sleep(0.1)
        check("a slow failure is said out loud, once",
              said == ["Sorry, that picture didn't come out."], str(said))
        check("...and recorded", (maker.status()["last"] or {}).get("error") == "brush broke")
        FakePainter.delay = 0.0
        out = commands.execute_action(ctx, "make_picture", prompt="a fast doomed one")
        check("a fast failure is the reply itself",
              out.startswith("I tried to paint that, but it didn't work"), out)
        FakePainter.fail = False

        print("the on_finish hook is race-free")
        hits = []
        maker.on_finish(hits.append)
        check("with nothing in progress the hook runs at once, told how the last job went",
              hits == [False], str(hits))          # the last one above failed
        FakePainter.delay = 0.3
        maker.request("x")
        maker.on_finish(hits.append)
        check("...and while busy it waits", hits == [False])
        maker.wait(2)
        time.sleep(0.05)
        check("...then runs when the job ends, with its own result", hits == [False, True], str(hits))

        print("no painter is a sentence, not a crash")
        real = images.ImageMaker({"backend": "off"}, pictures_dir=Path(td))
        out = commands.execute_action(types.SimpleNamespace(images=real, display=FakeDisplay()),
                                      "make_picture", prompt="anything")
        check("off: he says so", "switched off" in out, out)
        real = images.ImageMaker({"backend": "local", "sd_bin": "/nonexistent/sd",
                                  "sd_model": "/nonexistent/m"}, pictures_dir=Path(td))
        check("local wanted but not installed: not available, and says why",
              not real.available() and "isn't installed" in real.why_not(), real.why_not())
        real = images.ImageMaker({"backend": "openai", "openai_api_key": ""},
                                 pictures_dir=Path(td))
        os.environ.pop("OPENAI_API_KEY", None)
        check("openai wanted with no key: says so", "key" in real.why_not(), real.why_not())
        real = images.ImageMaker({"backend": "auto", "sd_bin": "/nonexistent/sd",
                                  "openai_api_key": "sk-test"}, pictures_dir=Path(td))
        check("auto falls through to openai when a key is set", real.backend() == "openai")
        out = commands.execute_action(types.SimpleNamespace(images=None), "make_picture",
                                      prompt="anything")
        check("no ImageMaker at all: a sentence", out.startswith("I can't make pictures"), out)
        out = commands.execute_action(ctx, "make_picture", prompt="   ")
        check("an empty prompt asks for one", out == "Tell me what to paint.", out)
        out = commands.execute_action(types.SimpleNamespace(images=maker, display=None),
                                      "make_picture", prompt="x")
        check("no chest screen: says so", "chest screen" in out, out)

        print("pictures are kept and pruned")
        keep = images.KEEP
        images.KEEP = 3
        try:
            pruner = FakePainter({}, pictures_dir=Path(td) / "p")
            for i in range(5):
                pruner.generate(f"p{i}")
                time.sleep(1.05)                 # the filename is a second stamp
            pngs = sorted(p.name for p in (Path(td) / "p").glob("*.png"))
            check("only KEEP stay, plus latest",
                  len(pngs) == 4 and "latest.png" in pngs, str(pngs))
            meta = pruner.latest_meta()
            check("latest.json names the newest", meta.get("prompt") == "p4", str(meta))
            jsons = sorted(p.name for p in (Path(td) / "p").glob("*.json"))
            check("the prompts go with their pictures", len(jsons) == 4, str(jsons))
        finally:
            images.KEEP = keep

        print("the chest re-encode fits 800x480")
        from PIL import Image
        jpeg = images.for_chest(png_bytes(1024, 1024))
        with Image.open(io.BytesIO(jpeg)) as im:
            check("a square lands 480 tall as a JPEG",
                  im.format == "JPEG" and im.size == (480, 480), str(im.size))
        jpeg = images.for_chest(png_bytes(2000, 500))
        with Image.open(io.BytesIO(jpeg)) as im:
            check("a wide one fits the width", im.size[0] <= 960 and im.size[1] <= 480, str(im.size))

        print("the daemon's endpoint says no to junk")
        import display_control as dc
        sent = []

        class H:
            def _send(self, code, body):
                sent.append((code, body))
        keep_dir, keep_file = dc.PICTURE_DIR, dc.PICTURE_FILE
        keep_state = dc.STATE_PATH
        dc.PICTURE_DIR = Path(td) / "pictures"
        dc.PICTURE_FILE = dc.PICTURE_DIR / "latest.jpg"
        dc.STATE_PATH = Path(td) / "state.json"
        try:
            dc.Handler._picture(H(), {"jpeg_b64": "not base64!!"})
            check("not base64: 400", sent[-1][0] == 400, str(sent[-1]))
            dc.Handler._picture(H(), {"jpeg_b64": base64.b64encode(b"hello").decode()})
            check("not an image: 400", sent[-1][0] == 400, str(sent[-1]))
            big = base64.b64encode(b"\xff\xd8\xff" + b"\0" * dc.PICTURE_MAX_BYTES).decode()
            dc.Handler._picture(H(), {"jpeg_b64": big})
            check("too big: 400", sent[-1][0] == 400, str(sent[-1])[:80])
            dc.Handler._picture(H(), {"jpeg_b64": base64.b64encode(jpeg).decode(),
                                      "caption": "a dragon", "hold_s": "77"})
            code, body = sent[-1]
            entry = body.get("picture") or {}
            check("a real JPEG: 200, kept, and announced through state.json",
                  code == 200 and dc.PICTURE_FILE.is_file()
                  and dc.read_state().get("picture", {}).get("caption") == "a dragon"
                  and entry.get("hold_s") == 77, str(body)[:120])
            n1 = entry.get("n")
            dc.Handler._picture(H(), {"jpeg_b64": base64.b64encode(jpeg).decode()})
            check("the counter moves on each picture", sent[-1][1]["picture"]["n"] != n1)
            dc.Handler._picture(H(), {"clear": True})
            check("clear takes it out of state.json", dc.read_state().get("picture") is None)
        finally:
            dc.PICTURE_DIR, dc.PICTURE_FILE, dc.STATE_PATH = keep_dir, keep_file, keep_state

        print("the tool is offered to Claude and dispatched")
        tool = next((t for t in commands.CLAUDE_TOOLS if t["name"] == "make_picture"), None)
        check("make_picture is in CLAUDE_TOOLS with a required prompt",
              tool is not None and tool["input_schema"]["required"] == ["prompt"])
        FakePainter.delay = 0.0
        out = commands.run_tool(ctx, "make_picture", {"prompt": "a cat"})
        check("run_tool reaches the painter", out.startswith("Here it is"), out)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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

**The audience is children** (2026-09-24, inmoov/picture_guard.py): words
he does not paint are refused before the brush, with the kind named for the
brain to relay; a picture that came out undressed (the detector is stubbed
here) is not shown, not the latest, kept under refused/ for the admin, and
said as a no rather than a fault; "off" runs neither. And the local
painter's argv follows the weights: a split set (FLUX) or a single file.

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
        # sd_diffusion_model "" both times: the default split set may well be on
        # the disk this runs on, and sd_bin falls back to the CPU build when it
        # exists — "not installed" has to be said, not assumed.
        real = images.ImageMaker({"backend": "local", "sd_bin": "/nonexistent/sd",
                                  "sd_model": "/nonexistent/m", "sd_diffusion_model": ""},
                                 pictures_dir=Path(td))
        check("local wanted but not installed: not available, and says why",
              not real.available() and "isn't installed" in real.why_not(), real.why_not())
        real = images.ImageMaker({"backend": "openai", "openai_api_key": ""},
                                 pictures_dir=Path(td))
        os.environ.pop("OPENAI_API_KEY", None)
        check("openai wanted with no key: says so", "key" in real.why_not(), real.why_not())
        real = images.ImageMaker({"backend": "auto", "sd_bin": "/nonexistent/sd",
                                  "sd_model": "/nonexistent/m", "sd_diffusion_model": "",
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

        print("the audience is children: the words")
        from inmoov import picture_guard as pg
        for text, want in [
                ("a romance novel hunk, shirtless muscular man", "nudity or revealing clothing"),
                ("a bare-chested pirate", "nudity or revealing clothing"),
                ("a Romantic Novel cover", "sexual content"),
                ("a bloody battle", "blood or gore"),
                ("a creepy clown", "horror or frightening pictures"),
                ("a soldier with a rifle", "weapons"),
                ("a man drinking beer", "drugs, smoking or alcohol"),
                ("a swastika flag", "hate symbols"),
                ("a killer whale jumping", ""), ("a shooting star over a lake", ""),
                ("a knight with a sword fighting a dragon", ""), ("a fruit cocktail", ""),
                ("a haunted house at halloween", ""), ("a Tasmanian devil", ""),
                ("a hunk of cheese", ""), ("kids at the beach in swimsuits", ""),
                ("a cheerful clown", ""), ("a butterfly on a flower", ""), ("", "")]:
            got = pg.check_prompt(text)
            check(f"{text!r} -> {want or 'fine'}", got == want, got)

        print("the audience is children: the picture")

        class FakeDetector:
            hits: list = []

            def detect(self, image):
                return list(FakeDetector.hits)
        keep_det, keep_err = pg._detector, pg._detector_error
        pg._detector, pg._detector_error = FakeDetector(), ""
        try:
            check("a clean picture passes", pg.check_picture(b"png") == ("", []))
            FakeDetector.hits = [{"class": "FACE_MALE", "score": 0.9},
                                 {"class": "ARMPITS_EXPOSED", "score": 0.9},
                                 {"class": "BELLY_COVERED", "score": 0.9}]
            check("faces, armpits and covered anything are not a reason",
                  pg.check_picture(b"png") == ("", []))
            FakeDetector.hits = [{"class": "MALE_BREAST_EXPOSED", "score": 0.3}]
            check("a bare chest is, even at low confidence",
                  pg.check_picture(b"png") == ("undressed people", ["MALE_BREAST_EXPOSED:0.30"]))
            FakeDetector.hits = [{"class": "BELLY_EXPOSED", "score": 0.3}]
            check("...a faint belly is not", pg.check_picture(b"png") == ("", []))

            FakeDetector.hits = []
            said.clear()
            FakeDisplay.shown.clear()
            guarded = FakePainter({"wait_s": 1.0}, announce=said.append,
                                  pictures_dir=Path(td) / "g")
            gctx = types.SimpleNamespace(images=guarded, display=FakeDisplay())
            out = commands.execute_action(gctx, "make_picture", prompt="a shirtless pirate")
            check("words he does not paint: refused before the brush, with the kind",
                  out.startswith("Not painted: that would show nudity or revealing clothing")
                  and "offer a different picture" in out and not guarded.busy(), out)
            check("...and nothing was painted", not (Path(td) / "g").exists())
            st = guarded.status()
            check("status says the guard is on and the picture check has its detector",
                  st["guard"] == "family" and st["picture_check"] is True, str(st))

            FakeDetector.hits = [{"class": "FEMALE_BREAST_EXPOSED", "score": 0.6}]
            out = commands.execute_action(gctx, "make_picture", prompt="a handsome lifeguard")
            guarded.wait(2)
            time.sleep(0.05)
            check("a picture that came out undressed: painted, not shown, and the reply says so",
                  out.startswith("I painted it, but it came out showing undressed people")
                  and FakeDisplay.shown == [], out)
            check("...it is not the latest", guarded.latest_path() is None)
            refused = sorted((Path(td) / "g" / "refused").glob("*.json"))
            meta = json.loads(refused[-1].read_text()) if refused else {}
            check("...but kept under refused/ with what was seen, for the admin",
                  len(refused) == 1 and meta.get("refused") == "undressed people"
                  and meta.get("seen") == ["FEMALE_BREAST_EXPOSED:0.60"], str(meta))
            check("...and recorded in status", guarded.status()["last"].get("refused") == "undressed people")

            FakePainter.delay = 1.4
            out = commands.execute_action(gctx, "make_picture", prompt="a handsome lifeguard")
            guarded.wait(3)
            time.sleep(0.1)
            check("a slow one refused on sight is said out loud, as a no, not a fault",
                  out.startswith("I'm painting it now")
                  and said == ["Sorry, that picture came out as something I don't show "
                               "here. Ask me for a different one."], str(said))
            FakePainter.delay = 0.0
            try:
                guarded.generate("a bloody battle")
                got = "no exception"
            except images.PictureRefused as exc:
                got = str(exc)
            check("the synchronous path refuses the same way", got == "blood or gore", got)

            FakeDetector.hits = []
            off = FakePainter({"guard": "off", "wait_s": 1.0}, pictures_dir=Path(td) / "off")
            out = commands.execute_action(types.SimpleNamespace(images=off, display=FakeDisplay()),
                                          "make_picture", prompt="a shirtless pirate")
            check("guard off: neither check runs", out.startswith("Here it is"), out)
            check("...and status says so", off.status()["guard"] == "off"
                  and off.status()["picture_check"] is False)
        finally:
            pg._detector, pg._detector_error = keep_det, keep_err
            FakeDisplay.shown.clear()

        print("the local painter's command follows the weights")
        split = images.ImageMaker({"sd_bin": "/x/sd", "sd_diffusion_model": "/m/flux.gguf",
                                   "sd_vae": "/m/ae", "sd_clip_l": "/m/clip",
                                   "sd_t5xxl": "/m/t5", "sd_model": "/m/ignored"},
                                  pictures_dir=Path(td))
        cmd = split._local_command("a dragon", Path("/o.png"))
        check("a split set: --diffusion-model with its encoders and VAE, euler, 4 steps",
              "--diffusion-model" in cmd and "--model" not in cmd
              and cmd[cmd.index("--vae") + 1] == "/m/ae" and "--t5xxl" in cmd and "--clip_l" in cmd
              and cmd[cmd.index("--sampling-method") + 1] == "euler"
              and cmd[cmd.index("--steps") + 1] == "4" and "--taesd" not in cmd, " ".join(cmd))
        check("...and it names the model", split.model_name() == "flux")
        single = images.ImageMaker({"sd_bin": "/x/sd", "sd_diffusion_model": "",
                                    "sd_model": "/m/turbo.gguf", "steps": 2},
                                   pictures_dir=Path(td))
        cmd = single._local_command("a dragon", Path("/o.png"))
        check("a single file: --model, euler_a, its own steps",
              cmd[cmd.index("--model") + 1] == "/m/turbo.gguf" and "--diffusion-model" not in cmd
              and cmd[cmd.index("--sampling-method") + 1] == "euler_a"
              and cmd[cmd.index("--steps") + 1] == "2", " ".join(cmd))
        check("the prompt goes through untouched", cmd[cmd.index("--prompt") + 1] == "a dragon")

        class Capture(images.ImageMaker):
            got = ""

            def backend(self):
                return "local"

            def _paint_local(self, prompt):
                Capture.got = prompt
                return png_bytes()
        styled = Capture({"style": "storybook illustration, bright colours", "guard": "off"},
                         pictures_dir=Path(td) / "s")
        styled.generate("a cat")
        check("the style setting is added to what the painter is told",
              Capture.got == "a cat, storybook illustration, bright colours", Capture.got)
        check("...but the caption is the person's words",
              styled.latest_meta().get("prompt") == "a cat")

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

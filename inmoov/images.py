"""FRED paints: a description in, a picture out, and onto the chest screen.

"Draw me a dragon" is the request this answers. The language model has no
brush of its own — Claude describes, it does not paint — so the picture comes
from one of two places, chosen by ``images.backend`` in config/settings.json:

  local   stable-diffusion.cpp on the NUC, on the Arc iGPU through Vulkan
          (or the cores, if the Vulkan build is missing): no internet, no
          key, no cost. tools/install_sdcpp.sh builds it and fetches weights.
          Which weights is the ``sd_*`` settings — see _local_command below
          and the numbers in TODO.md, "FRED paints".
  openai  the Images API (gpt-image-1 by default), which is a much better
          painter and a much slower one, and needs a key and an uplink. Sent
          as plain HTTPS from here so the brain's own SDK is not involved.
  auto    local when its binary and weights are present, else openai when a
          key is set, else nothing — the same shape as the brain's backend
          switch, and for the same reason: he goes to venues without WiFi.

Generation runs on its own thread, one picture at a time. The spoken tool
(commands._make_picture) waits a short while for a fast backend and otherwise
lets him say it is on the way; ``on_done`` pushes the finished picture to the
chest and, if a voice is wired in, announces it. Nothing here blocks the reply.

Every picture is kept under logs/pictures/ with its prompt, the latest also as
latest.png, so the admin page can show what he painted and the next person can
see the last one. logs/ is git-ignored.

The audience is children (inmoov/picture_guard.py). With ``images.guard`` at
its default "family", a prompt that asks for what he does not paint is
refused before the easel (``PictureRefused``, which the tool turns into a kind
no), and a finished picture is looked at before it is kept or shown; one that
came out undressed lands in logs/pictures/refused/ for the admin's eyes and
nowhere else. "off" skips both.
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

try:
    import requests
except ImportError:                                        # pragma: no cover
    requests = None

from . import picture_guard

ROOT = Path(__file__).resolve().parent.parent
PICTURES_DIR = ROOT / "logs" / "pictures"
KEEP = 40                       # pictures kept on disk before the oldest go
KEEP_REFUSED = 10               # ...and refused ones, for the admin to judge the guard by

BACKENDS = ("auto", "local", "openai", "off")

# Where tools/install_sdcpp.sh puts things. Overridable in settings; these are
# the defaults so a fresh install works with no admin visit. The Vulkan build
# paints on the Arc iGPU; the plain build is the fallback when it is absent.
SDCPP = "~/fred/sdcpp"
DEFAULT_SD_BIN = f"{SDCPP}/stable-diffusion.cpp/build-vulkan/bin/sd-cli"
CPU_SD_BIN = f"{SDCPP}/stable-diffusion.cpp/build/bin/sd-cli"
# The weights. A model is either one file (``sd_model``: sd-turbo, sdxl-turbo)
# or a split set (``sd_diffusion_model`` plus its text encoders and VAE: FLUX);
# when both are set the split set wins. Defaults are the FLUX.1-schnell set
# tools/install_sdcpp.sh fetches; the settings comments carry the others.
DEFAULT_SD_MODEL = ""
DEFAULT_SD_DIFFUSION = f"{SDCPP}/models/flux1-schnell-q4_k_s.gguf"
DEFAULT_SD_VAE = f"{SDCPP}/models/ae.safetensors"
DEFAULT_SD_CLIP_L = f"{SDCPP}/models/clip_l.safetensors"
DEFAULT_SD_T5XXL = f"{SDCPP}/models/t5xxl-q8_0.gguf"
# The tiny autoencoder: a fast, slightly soft decode. Worth it on the cores
# (10 s -> 1 s for SD); on the iGPU the real VAE is quick enough, so the
# default is none. Used when set and present; it must match the model family
# (taesd for SD, taesdxl for SDXL, taef1 for FLUX) or the picture is noise.
DEFAULT_SD_TAESD = ""
LOCAL_TIMEOUT_S = 180.0
OPENAI_TIMEOUT_S = 120.0
OPENAI_URL = "https://api.openai.com/v1/images/generations"


class ImageError(Exception):
    """No painter, a painter that failed, or a request that cannot be taken."""


class PictureRefused(ImageError):
    """Not a picture for this audience. ``str()`` is the kind of thing asked for
    ("nudity or revealing clothing"), speakable as "I don't paint ..."."""


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(str(p or "")))


class ImageMaker:
    """One painter, one easel: generates pictures one at a time on a thread."""

    def __init__(self, settings: dict | None = None, log=None, announce=None,
                 pictures_dir: Path = PICTURES_DIR):
        self._cfg: dict = {}
        self._log = log or (lambda m: None)
        # A callable taking one sentence — the assistant's speak — so a slow
        # painter can say "it's ready" when it is. Optional: without it the
        # picture simply appears.
        self._announce = announce
        self._dir = Path(pictures_dir)
        self._lock = threading.Lock()
        self._busy = False
        self._thread: threading.Thread | None = None
        self._last: dict = {}              # prompt, at, seconds, file, error, backend
        self._done = threading.Event()
        self._done.set()
        self._finish_hooks: list = []
        self.configure(settings or {})

    # ---- configuration ----------------------------------------------------
    def configure(self, cfg: dict) -> None:
        """Take (a subset of) the ``images`` settings section, live."""
        cur = dict(self._cfg)
        cur.update({k: v for k, v in (cfg or {}).items() if v is not None})
        self._cfg = cur

    def _get(self, key: str, default):
        return self._cfg.get(key, default)

    def sd_bin(self) -> Path:
        """The painter binary: the configured one, else the CPU build if only
        that exists (a machine where the Vulkan build was never made)."""
        b = _expand(self._get("sd_bin", DEFAULT_SD_BIN))
        if not b.is_file():
            cpu = _expand(CPU_SD_BIN)
            if cpu.is_file():
                return cpu
        return b

    def sd_model(self) -> Path | None:
        """The single-file model, or None when a split set is configured."""
        if self._optional_path("sd_diffusion_model", DEFAULT_SD_DIFFUSION) is not None:
            return None
        return self._optional_path("sd_model", DEFAULT_SD_MODEL)

    def sd_diffusion_model(self) -> Path | None:
        return self._optional_path("sd_diffusion_model", DEFAULT_SD_DIFFUSION)

    def _optional_path(self, key: str, default: str) -> Path | None:
        raw = str(self._get(key, default) or "").strip()
        return _expand(raw) if raw else None

    def sd_taesd(self) -> Path | None:
        p = self._optional_path("sd_taesd", DEFAULT_SD_TAESD)
        return p if p is not None and p.is_file() else None

    def model_name(self) -> str:
        """What the local painter is, for the log and the admin page."""
        m = self.sd_diffusion_model() or self.sd_model()
        return m.stem if m is not None else ""

    def _openai_key(self) -> str:
        return str(self._get("openai_api_key", "") or os.environ.get("OPENAI_API_KEY", ""))

    def local_available(self) -> bool:
        b = self.sd_bin()
        m = self.sd_diffusion_model() or self.sd_model()
        return b.is_file() and os.access(b, os.X_OK) and m is not None and m.is_file()

    # ---- the guard ------------------------------------------------------------
    def guard_on(self) -> bool:
        return str(self._get("guard", "family") or "family").lower() != "off"

    def style(self) -> str:
        """Words added to every prompt sent to the painter, or ""."""
        return " ".join(str(self._get("style", "") or "").split())

    def openai_available(self) -> bool:
        return bool(self._openai_key()) and requests is not None

    def backend(self) -> str:
        """Which painter a request would go to right now, or "" for none."""
        want = str(self._get("backend", "auto") or "auto").lower()
        if want == "off":
            return ""
        if want == "local":
            return "local" if self.local_available() else ""
        if want == "openai":
            return "openai" if self.openai_available() else ""
        if self.local_available():
            return "local"
        if self.openai_available():
            return "openai"
        return ""

    def available(self) -> bool:
        return bool(self.backend())

    def why_not(self) -> str:
        """A spoken reason there is no painter, for the tool's answer."""
        want = str(self._get("backend", "auto") or "auto").lower()
        if want == "off":
            return "picture-making is switched off"
        if want in ("local", "auto") and not self.local_available():
            if want == "local":
                return "my local painter isn't installed"
        if want in ("openai", "auto") and not self.openai_available():
            if want == "openai":
                return "I don't have a key for the picture service"
        return "I don't have a painter set up"

    def hold_s(self) -> int:
        try:
            return max(0, int(self._get("hold_s", 180)))
        except (TypeError, ValueError):
            return 180

    def wait_s(self) -> float:
        try:
            return max(0.0, float(self._get("wait_s", 8.0)))
        except (TypeError, ValueError):
            return 8.0

    # ---- status -------------------------------------------------------------
    def busy(self) -> bool:
        return self._busy

    def status(self) -> dict:
        return {
            "backend": self.backend() or None,
            "wanted": str(self._get("backend", "auto") or "auto"),
            "local_installed": self.local_available(),
            "local_model": self.model_name(),
            "openai_key": bool(self._openai_key()),
            "guard": "family" if self.guard_on() else "off",
            # Whether the picture check has its detector. Asked lazily, so an
            # admin page load is what first loads NudeNet — 0.2 s, once.
            "picture_check": (picture_guard.detector_available()
                              if self.guard_on() else False),
            "picture_check_error": picture_guard.detector_error() if self.guard_on() else "",
            "busy": self._busy,
            "hold_s": self.hold_s(),
            "last": dict(self._last),
        }

    def latest_path(self) -> Path | None:
        p = self._dir / "latest.png"
        return p if p.is_file() else None

    # ---- the request ----------------------------------------------------------
    def request(self, prompt: str, on_done=None) -> None:
        """Start painting ``prompt`` on the worker thread.

        Raises ImageError at once when there is no painter or one is already
        at work — the caller can say so — and PictureRefused when the words
        ask for what he does not paint; never for a failure during painting,
        which lands in status()["last"]["error"] and in the log, nor for a
        picture refused on sight, which lands there too with "refused" set.
        ``on_done(path, prompt)`` runs on the worker after success.
        """
        prompt = " ".join(str(prompt or "").split())
        if not prompt:
            raise ImageError("nothing to paint")
        backend = self.backend()
        if not backend:
            raise ImageError(self.why_not())
        self._refuse_words(prompt)
        with self._lock:
            if self._busy:
                raise ImageError("I'm still painting the last one")
            self._busy = True
            self._done.clear()
            self._last = {"prompt": prompt, "at": time.time(), "backend": backend,
                          "file": None, "seconds": None, "error": None, "refused": None}
        self._thread = threading.Thread(target=self._work, args=(prompt, backend, on_done),
                                        name="image-maker", daemon=True)
        self._thread.start()

    def wait(self, seconds: float) -> bool:
        """True once the current picture is finished (or none is in progress)."""
        return self._done.wait(seconds)

    def on_finish(self, hook) -> None:
        """Run ``hook(ok)`` when the picture in progress ends — at once if it
        already has. Race-free by construction: the busy flag and the hook
        list change under the same lock the worker clears them under."""
        with self._lock:
            if self._busy:
                self._finish_hooks.append(hook)
                return
        hook(not (self._last.get("error")))

    def announce(self, sentence: str) -> None:
        """Say something, if a voice was wired in; otherwise only log it."""
        if self._announce is None:
            self._log(sentence)
            return
        try:
            self._announce(sentence)
        except Exception as exc:                                # noqa: BLE001
            self._log(f"could not announce the picture: {exc}")

    def _work(self, prompt: str, backend: str, on_done) -> None:
        started = time.monotonic()
        try:
            png = self._paint(prompt, backend)
            self._refuse_sight(prompt, png, backend)
            path = self._store(prompt, png, backend)
            took = round(time.monotonic() - started, 1)
            self._last.update(file=str(path), seconds=took)
            self._log(f"painted '{prompt}' in {took}s ({backend})")
            if on_done is not None:
                try:
                    on_done(path, prompt)
                except Exception as exc:                        # noqa: BLE001
                    self._log(f"picture made but not shown: {exc}")
        except PictureRefused as exc:
            self._last.update(error=f"it came out showing {exc}", refused=str(exc),
                              seconds=round(time.monotonic() - started, 1))
        except Exception as exc:                                # noqa: BLE001
            self._last.update(error=str(exc)[:200],
                              seconds=round(time.monotonic() - started, 1))
            self._log(f"painting '{prompt}' failed: {exc}")
        finally:
            with self._lock:
                self._busy = False
                self._done.set()
                hooks, self._finish_hooks = self._finish_hooks, []
            ok = not self._last.get("error")
            for hook in hooks:
                try:
                    hook(ok)
                except Exception as exc:                        # noqa: BLE001
                    self._log(f"picture hook failed: {exc}")

    def generate(self, prompt: str) -> Path:
        """Paint synchronously and return the PNG's path. For tools and tests.
        The guard applies here too: PictureRefused before or after the brush."""
        prompt = " ".join(str(prompt or "").split())
        backend = self.backend()
        if not backend:
            raise ImageError(self.why_not())
        self._refuse_words(prompt)
        png = self._paint(prompt, backend)
        self._refuse_sight(prompt, png, backend)
        return self._store(prompt, png, backend)

    # ---- the guard, applied --------------------------------------------------
    def _refuse_words(self, prompt: str) -> None:
        if not self.guard_on():
            return
        reason = picture_guard.check_prompt(prompt)
        if reason:
            self._log(f"not painting '{prompt}': {reason}")
            raise PictureRefused(reason)

    def _refuse_sight(self, prompt: str, png: bytes, backend: str) -> None:
        """Look at what the painter did; keep a refused one aside, unshown."""
        if not self.guard_on():
            return
        reason, seen = picture_guard.check_picture(png)
        if not reason:
            if seen:                       # the detector complained, not the picture
                self._log(f"picture check skipped: {seen[0]}")
            return
        self._log(f"refusing the picture for '{prompt}': {reason} ({', '.join(seen)})")
        self._store_refused(prompt, png, backend, reason, seen)
        raise PictureRefused(reason)

    # ---- the painters ---------------------------------------------------------
    def _paint(self, prompt: str, backend: str) -> bytes:
        style = self.style()
        if style:
            prompt = f"{prompt}, {style}"
        if backend == "local":
            return self._paint_local(prompt)
        if backend == "openai":
            return self._paint_openai(prompt)
        raise ImageError(f"no such painter: {backend}")

    def _local_command(self, prompt: str, out: Path) -> list[str]:
        """The sd-cli argv for one picture.

        The weights decide the shape: a single-file model goes as --model, a
        split set as --diffusion-model with its VAE and text encoders. The
        distilled models this runs (sd-turbo, sdxl-turbo, FLUX.1-schnell) all
        want few steps and no classifier-free guidance (cfg 1.0), at which the
        negative prompt is ignored — it is passed anyway for a model that
        does use it. Flow models (FLUX) sample with euler, the SD family with
        euler_a; the default follows the shape. Threads only matter to the
        CPU build; they leave a few cores for the voice.
        """
        size = int(self._get("size", 512))
        steps = int(self._get("steps", 4))
        threads = int(self._get("threads", max(2, (os.cpu_count() or 4) - 4)))
        split = self.sd_diffusion_model()
        cmd = [str(self.sd_bin()), "--mode", "img_gen"]
        if split is not None:
            cmd += ["--diffusion-model", str(split)]
            for flag, key, default in (("--vae", "sd_vae", DEFAULT_SD_VAE),
                                       ("--clip_l", "sd_clip_l", DEFAULT_SD_CLIP_L),
                                       ("--t5xxl", "sd_t5xxl", DEFAULT_SD_T5XXL)):
                p = self._optional_path(key, default)
                if p is not None:
                    cmd += [flag, str(p)]
        else:
            cmd += ["--model", str(self.sd_model())]
        sampler = str(self._get("sampler", "") or ("euler" if split is not None else "euler_a"))
        cmd += ["--prompt", prompt,
                "--negative-prompt", str(self._get(
                    "negative", "blurry, low quality, text, watermark, deformed, "
                                "extra limbs, extra fingers, bad anatomy, nsfw")),
                "--width", str(size), "--height", str(size),
                "--steps", str(steps), "--cfg-scale", str(self._get("cfg", 1.0)),
                "--sampling-method", sampler,
                "--seed", "-1", "--threads", str(threads),
                "--output", str(out)]
        taesd = self.sd_taesd()
        if taesd is not None:
            cmd += ["--taesd", str(taesd)]
        return cmd

    def _paint_local(self, prompt: str) -> bytes:
        """stable-diffusion.cpp, txt2img, one process per picture."""
        out_dir = Path(self._get("work_dir", "") or (self._dir / "work"))
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"sd-{int(time.time() * 1000)}.png"
        cmd = self._local_command(prompt, out)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=float(self._get("timeout_s", LOCAL_TIMEOUT_S)))
        except FileNotFoundError:
            raise ImageError("the local painter is not installed") from None
        except subprocess.TimeoutExpired:
            raise ImageError("the local painter took too long") from None
        if proc.returncode != 0 or not out.is_file():
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            raise ImageError("the local painter failed: " + " | ".join(tail)[:300])
        try:
            return out.read_bytes()
        finally:
            try:
                out.unlink()
            except OSError:
                pass

    def _paint_openai(self, prompt: str) -> bytes:
        """The Images API, plain HTTPS. Returns PNG bytes."""
        if requests is None:
            raise ImageError("the requests library is missing")
        key = self._openai_key()
        if not key:
            raise ImageError("no key for the picture service")
        body = {"model": str(self._get("openai_model", "gpt-image-1")),
                "prompt": prompt, "n": 1,
                "size": str(self._get("openai_size", "1024x1024")),
                "quality": str(self._get("openai_quality", "low"))}
        try:
            r = requests.post(OPENAI_URL, json=body,
                              headers={"Authorization": f"Bearer {key}"},
                              timeout=float(self._get("timeout_s", OPENAI_TIMEOUT_S)))
        except requests.RequestException as exc:
            raise ImageError(f"could not reach the picture service: {exc}") from None
        try:
            data = r.json()
        except ValueError:
            raise ImageError(f"the picture service answered oddly (HTTP {r.status_code})") from None
        if r.status_code >= 400:
            msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
            raise ImageError(f"the picture service refused: {msg or r.status_code}")
        try:
            b64 = data["data"][0]["b64_json"]
        except (KeyError, IndexError, TypeError):
            raise ImageError("the picture service sent no picture") from None
        return base64.b64decode(b64)

    # ---- keeping them -----------------------------------------------------------
    def _store(self, prompt: str, png: bytes, backend: str) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self._dir / f"{stamp}.png"
        path.write_bytes(png)
        meta = {"prompt": prompt, "backend": backend, "at": time.time(), "file": path.name}
        path.with_suffix(".json").write_text(json.dumps(meta) + "\n")
        latest = self._dir / "latest.png"
        tmp = latest.with_suffix(".tmp")
        shutil.copyfile(path, tmp)
        os.replace(tmp, latest)
        (self._dir / "latest.json").write_text(json.dumps(meta) + "\n")
        self._prune()
        return path

    def _store_refused(self, prompt: str, png: bytes, backend: str,
                       reason: str, seen: list) -> None:
        """Keep a refused picture under refused/, never as latest, never shown:
        the admin's way to see what the guard is refusing and tune it."""
        d = self._dir / "refused"
        try:
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{time.strftime('%Y%m%d-%H%M%S')}.png"
            path.write_bytes(png)
            path.with_suffix(".json").write_text(json.dumps(
                {"prompt": prompt, "backend": backend, "at": time.time(),
                 "file": path.name, "refused": reason, "seen": seen}) + "\n")
            self._prune(d, KEEP_REFUSED)
        except OSError as exc:
            self._log(f"could not keep the refused picture: {exc}")

    def _prune(self, where: Path | None = None, keep: int | None = None) -> None:
        where = where or self._dir
        keep = KEEP if keep is None else keep
        pngs = sorted(p for p in where.glob("*.png") if p.name != "latest.png")
        for old in pngs[:-keep] if len(pngs) > keep else []:
            for victim in (old, old.with_suffix(".json")):
                try:
                    victim.unlink()
                except OSError:
                    pass

    def latest_meta(self) -> dict:
        try:
            return json.loads((self._dir / "latest.json").read_text())
        except (OSError, ValueError):
            return {}


def for_chest(png: bytes, max_side: int = 480, quality: int = 88) -> bytes:
    """Re-encode a picture for the 800x480 chest panel: JPEG, fitted.

    A 512px PNG is a few hundred kilobytes and the Pi decodes it fine, but the
    picture goes over the wire as base64 in JSON, and a ~60 KB JPEG lands
    before anyone has finished asking whether it worked. Needs Pillow; without
    it the PNG goes as-is.
    """
    try:
        from PIL import Image                                 # noqa: PLC0415
    except ImportError:
        return png
    with Image.open(io.BytesIO(png)) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side * 2, max_side))                # fit 800x480
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()

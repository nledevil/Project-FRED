"""FRED paints: a description in, a picture out, and onto the chest screen.

"Draw me a dragon" is the request this answers. The language model has no
brush of its own — Claude describes, it does not paint — so the picture comes
from one of two places, chosen by ``images.backend`` in config/settings.json:

  local   stable-diffusion.cpp on the NUC's own cores, with sd-turbo: a
          512px picture in a handful of seconds, no internet, no key, no cost.
          tools/install_sdcpp.sh builds the binary and fetches the weights.
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

ROOT = Path(__file__).resolve().parent.parent
PICTURES_DIR = ROOT / "logs" / "pictures"
KEEP = 40                       # pictures kept on disk before the oldest go

BACKENDS = ("auto", "local", "openai", "off")

# Where tools/install_sdcpp.sh puts things. Overridable in settings; these are
# the defaults so a fresh install works with no admin visit.
DEFAULT_SD_BIN = "~/fred/sdcpp/stable-diffusion.cpp/build/bin/sd-cli"
DEFAULT_SD_MODEL = "~/fred/sdcpp/models/sd_turbo-f16-q8_0.gguf"
# The tiny autoencoder: the full VAE decode of a 512px latent is ten seconds
# on these cores, TAESD's is one. Slightly softer picture, and the difference
# between "here it is" and "it's on the way". Used when present.
DEFAULT_SD_TAESD = "~/fred/sdcpp/models/taesd.safetensors"
LOCAL_TIMEOUT_S = 180.0
OPENAI_TIMEOUT_S = 120.0
OPENAI_URL = "https://api.openai.com/v1/images/generations"


class ImageError(Exception):
    """No painter, a painter that failed, or a request that cannot be taken."""


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
        return _expand(self._get("sd_bin", DEFAULT_SD_BIN))

    def sd_model(self) -> Path:
        return _expand(self._get("sd_model", DEFAULT_SD_MODEL))

    def sd_taesd(self) -> Path | None:
        p = _expand(self._get("sd_taesd", DEFAULT_SD_TAESD))
        return p if p.is_file() else None

    def _openai_key(self) -> str:
        return str(self._get("openai_api_key", "") or os.environ.get("OPENAI_API_KEY", ""))

    def local_available(self) -> bool:
        b, m = self.sd_bin(), self.sd_model()
        return b.is_file() and os.access(b, os.X_OK) and m.is_file()

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
            "openai_key": bool(self._openai_key()),
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
        at work — the caller can say so — and never for a failure during
        painting, which lands in status()["last"]["error"] and in the log.
        ``on_done(path, prompt)`` runs on the worker after success.
        """
        prompt = " ".join(str(prompt or "").split())
        if not prompt:
            raise ImageError("nothing to paint")
        backend = self.backend()
        if not backend:
            raise ImageError(self.why_not())
        with self._lock:
            if self._busy:
                raise ImageError("I'm still painting the last one")
            self._busy = True
            self._done.clear()
            self._last = {"prompt": prompt, "at": time.time(), "backend": backend,
                          "file": None, "seconds": None, "error": None}
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
            path = self._store(prompt, png, backend)
            took = round(time.monotonic() - started, 1)
            self._last.update(file=str(path), seconds=took)
            self._log(f"painted '{prompt}' in {took}s ({backend})")
            if on_done is not None:
                try:
                    on_done(path, prompt)
                except Exception as exc:                        # noqa: BLE001
                    self._log(f"picture made but not shown: {exc}")
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
        """Paint synchronously and return the PNG's path. For tools and tests."""
        backend = self.backend()
        if not backend:
            raise ImageError(self.why_not())
        return self._store(prompt, self._paint(prompt, backend), backend)

    # ---- the painters ---------------------------------------------------------
    def _paint(self, prompt: str, backend: str) -> bytes:
        if backend == "local":
            return self._paint_local(prompt)
        if backend == "openai":
            return self._paint_openai(prompt)
        raise ImageError(f"no such painter: {backend}")

    def _paint_local(self, prompt: str) -> bytes:
        """stable-diffusion.cpp, txt2img, one process per picture.

        sd-turbo is a distilled model: 1–4 steps and no classifier-free
        guidance (cfg 1.0). Steps and size are settings because a slower
        machine wants fewer of both. Threads leave a few cores for the voice.
        """
        size = int(self._get("size", 512))
        steps = int(self._get("steps", 2))
        threads = int(self._get("threads", max(2, (os.cpu_count() or 4) - 4)))
        out_dir = Path(self._get("work_dir", "") or (self._dir / "work"))
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"sd-{int(time.time() * 1000)}.png"
        cmd = [str(self.sd_bin()), "--mode", "img_gen",
               "--model", str(self.sd_model()),
               "--prompt", prompt,
               "--negative-prompt", str(self._get("negative", "blurry, low quality, text, watermark")),
               "--width", str(size), "--height", str(size),
               "--steps", str(steps), "--cfg-scale", str(self._get("cfg", 1.0)),
               "--sampling-method", str(self._get("sampler", "euler_a")),
               "--seed", "-1", "--threads", str(threads),
               "--output", str(out)]
        taesd = self.sd_taesd()
        if taesd is not None:
            cmd += ["--taesd", str(taesd)]
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

    def _prune(self) -> None:
        pngs = sorted(p for p in self._dir.glob("*.png") if p.name != "latest.png")
        for old in pngs[:-KEEP] if len(pngs) > KEEP else []:
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

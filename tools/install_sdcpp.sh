#!/usr/bin/env bash
# Give FRED a local painter: stable-diffusion.cpp on the NUC, with weights.
# After this, "Fred, draw me a dragon" needs no internet and no key.
#
#   tools/install_sdcpp.sh                # build + fetch the default weights (FLUX), idempotent
#   tools/install_sdcpp.sh --model sdxl   # ...or sdxl-turbo / sd-turbo instead (or as well)
#   tools/install_sdcpp.sh --try          # ...and paint one picture to time it
#
# Lands where inmoov/settings.py's images.sd_* point by default: ~/fred/sdcpp/.
# Nothing here touches the repo.
#
# Two builds. build/ is CPU-only, and build-vulkan/ paints on the Arc iGPU
# through Mesa's Vulkan driver — the same chip Ollama uses, five to ten times
# the speed of the cores, which is what makes a model good enough for faces
# affordable. The Vulkan build needs libvulkan-dev, glslc and spirv-headers
# (apt; asked for with sudo if missing) and is skipped, with a note, when
# vulkaninfo sees no GPU. The brain uses build-vulkan/ when it exists and
# falls back to build/ (inmoov/images.py, sd_bin).
#
# Three weight sets, all distilled (few steps, no guidance pass):
#   flux  FLUX.1-schnell q4_k_s (6.8 GB) + T5-XXL q8 (5 GB) + CLIP-L + the VAE.
#         The one that draws people with the right number of arms. Default.
#   sdxl  sdxl-turbo fp16 (6.9 GB) + its tiny decoder. Middle ground.
#   sd    sd-turbo q8 (2 GB) + its tiny decoder. Fast, and painted three-armed
#         children with two faces — kept for a slower machine, not for him.
# TODO.md "FRED paints" has the timings and what each looked like.
#
# Also installs NudeNet into the venv: the picture check in
# inmoov/picture_guard.py that keeps an undressed picture off the chest.
set -euo pipefail

BASE="${SDCPP_HOME:-$HOME/fred/sdcpp}"
SRC="$BASE/stable-diffusion.cpp"
MODELS="$BASE/models"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOBS="$(( $(nproc) > 4 ? $(nproc) - 4 : 2 ))"
HF="https://huggingface.co"

WANT="flux"; TRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --model) WANT="$2"; shift ;;
    --try)   TRY=1 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$BASE" "$MODELS"

# cmake is not on this NUC; the venv can carry it, and the venv exists.
if ! command -v cmake >/dev/null 2>&1; then
  echo "== cmake: installing into the venv"
  "$REPO/venv/bin/pip" install -q cmake
fi
export PATH="$REPO/venv/bin:$PATH"

if [ ! -d "$SRC/.git" ]; then
  echo "== cloning stable-diffusion.cpp"
  git clone -q --recursive https://github.com/leejet/stable-diffusion.cpp.git "$SRC"
fi

if [ ! -x "$SRC/build/bin/sd-cli" ]; then
  echo "== building (CPU), -j$JOBS"
  cmake -S "$SRC" -B "$SRC/build" -DCMAKE_BUILD_TYPE=Release -DSD_BUILD_SHARED_LIBS=OFF >/dev/null
  cmake --build "$SRC/build" --config Release -j "$JOBS" | tail -3
fi
echo "binary (cpu): $SRC/build/bin/sd-cli"

# ---- the Vulkan build, when there is a GPU to use --------------------------
if ! command -v vulkaninfo >/dev/null 2>&1 || ! vulkaninfo --summary 2>/dev/null \
     | grep -q "PHYSICAL_DEVICE_TYPE_\(INTEGRATED\|DISCRETE\)_GPU"; then
  echo "== no Vulkan GPU seen (vulkaninfo); skipping the Vulkan build — the cores will paint"
elif [ ! -x "$SRC/build-vulkan/bin/sd-cli" ]; then
  need=""
  for pkg in libvulkan-dev glslc spirv-headers; do
    dpkg -s "$pkg" >/dev/null 2>&1 || need="$need $pkg"
  done
  if [ -n "$need" ]; then
    echo "== apt: installing$need (sudo)"
    sudo apt-get install -y -q $need >/dev/null
  fi
  echo "== building (Vulkan), -j$JOBS"
  cmake -S "$SRC" -B "$SRC/build-vulkan" -DCMAKE_BUILD_TYPE=Release \
        -DSD_BUILD_SHARED_LIBS=OFF -DSD_VULKAN=ON >/dev/null
  cmake --build "$SRC/build-vulkan" --config Release -j "$JOBS" | tail -3
fi
[ -x "$SRC/build-vulkan/bin/sd-cli" ] && echo "binary (vulkan): $SRC/build-vulkan/bin/sd-cli"

# ---- weights ---------------------------------------------------------------
# Every file is checked against its SHA-256 (Hugging Face publishes it as the
# LFS ETag). A weight file that is nearly right paints noise, or nothing, with
# no error to read: on 2026-09-24 two downloads appending to one T5 file left
# it 96 MB too long and only the hash said so. A wrong hash removes the file
# and stops; run again to fetch it afresh.
fetch() {   # url  file  label  sha256
  if [ ! -s "$MODELS/$2" ]; then
    echo "== fetching $3"
    curl -fL --progress-bar -C - -o "$MODELS/$2.part" "$1"
    mv -f "$MODELS/$2.part" "$MODELS/$2"
  fi
  local sum; sum="$(sha256sum "$MODELS/$2" | cut -d' ' -f1)"
  if [ "$sum" != "$4" ]; then
    echo "!! $3: SHA-256 $sum, expected $4 — removing it; run this again" >&2
    rm -f "$MODELS/$2"; exit 1
  fi
  echo "$3: $MODELS/$2 ($(du -h "$MODELS/$2" | cut -f1), sha256 ok)"
}

case "$WANT" in
  flux)
    fetch "$HF/city96/FLUX.1-schnell-gguf/resolve/main/flux1-schnell-Q4_K_S.gguf" \
          flux1-schnell-q4_k_s.gguf "FLUX.1-schnell q4_k_s (~6.8 GB)" \
          4fd16477b3a5296d0cf722c4b92a9fd7f30d09ac7495826e4465d8de9c9fd973
    fetch "$HF/city96/t5-v1_1-xxl-encoder-gguf/resolve/main/t5-v1_1-xxl-encoder-Q8_0.gguf" \
          t5xxl-q8_0.gguf "T5-XXL encoder q8_0 (~5 GB)" \
          9ec60f6028534b7fe5af439fcb535d75a68592a9ca3fcdeb175ef89e3ee99825
    fetch "$HF/comfyanonymous/flux_text_encoders/resolve/main/clip_l.safetensors" \
          clip_l.safetensors "CLIP-L text encoder (~250 MB)" \
          660c6f5b1abae9dc498ac2d21e1347d2abdb0cf6c0c0c8576cd796491d9a6cdd
    # The FLUX VAE. black-forest-labs' own copy sits behind a licence click;
    # this is the same file, published ungated with Lumina 2, which uses it too.
    fetch "$HF/Comfy-Org/Lumina_Image_2.0_Repackaged/resolve/main/split_files/vae/ae.safetensors" \
          ae.safetensors "FLUX VAE (~335 MB)" \
          afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38
    fetch "$HF/madebyollin/taef1/resolve/main/diffusion_pytorch_model.safetensors" \
          taef1.safetensors "tiny FLUX decoder (~10 MB, optional: images.sd_taesd)" \
          47a6c2bff850da04b267cab70fe3553fef57255eb9a8e76852baa0a87850e54d
    ;;
  sdxl)
    fetch "$HF/stabilityai/sdxl-turbo/resolve/main/sd_xl_turbo_1.0_fp16.safetensors" \
          sd_xl_turbo_1.0_fp16.safetensors "sdxl-turbo fp16 (~6.9 GB)" \
          e869ac7d6942cb327d68d5ed83a40447aadf20e0c3358d98b2cc9e270db0da26
    fetch "$HF/madebyollin/taesdxl/resolve/main/diffusion_pytorch_model.safetensors" \
          taesdxl.safetensors "tiny SDXL decoder (~10 MB)" \
          ff4824aca94dd6111e0340fa749347fb74101060d9712cb5ef1ca8f1cf17502f
    ;;
  sd)
    fetch "$HF/Green-Sky/SD-Turbo-GGUF/resolve/main/sd_turbo-f16-q8_0.gguf" \
          sd_turbo-f16-q8_0.gguf "sd-turbo q8_0 (~2 GB)" \
          d50be7655f0a554cf8041c145d88b210bd5f3c545423119dee62ae08cae51580
    fetch "$HF/madebyollin/taesd/resolve/main/diffusion_pytorch_model.safetensors" \
          taesd.safetensors "tiny SD decoder (~10 MB)" \
          db169d69145ec4ff064e49d99c95fa05d3eb04ee453de35824a6d0f325513549
    ;;
  *) echo "unknown --model $WANT (flux, sdxl, sd)" >&2; exit 2 ;;
esac

# ---- the picture check -------------------------------------------------------
# --no-deps on purpose: nudenet depends on opencv-python-headless, and letting
# pip satisfy that installed the non-contrib OpenCV 5 over the venv's contrib
# build — same cv2 directory, and OpenCV 5 keeps HOGDescriptor and
# CascadeClassifier only in contrib, so face_id died at the next import
# (found 2026-09-24, before a restart, by tools/test_prompt_stability.py).
# The contrib build already provides cv2; onnxruntime is the other dependency.
if ! "$REPO/venv/bin/python" -c "import nudenet" 2>/dev/null; then
  echo "== NudeNet: installing into the venv (the guard's picture check)"
  "$REPO/venv/bin/pip" install -q onnxruntime
  "$REPO/venv/bin/pip" install -q --no-deps nudenet
fi
echo "picture check: $("$REPO/venv/bin/python" -c "import nudenet, os; print(os.path.dirname(nudenet.__file__))")"

if [ "$TRY" = 1 ]; then
  BIN="$SRC/build-vulkan/bin/sd-cli"; [ -x "$BIN" ] || BIN="$SRC/build/bin/sd-cli"
  OUT="$BASE/try-$WANT.png"
  case "$WANT" in
    flux) W=(--diffusion-model "$MODELS/flux1-schnell-q4_k_s.gguf" --vae "$MODELS/ae.safetensors"
             --clip_l "$MODELS/clip_l.safetensors" --t5xxl "$MODELS/t5xxl-q8_0.gguf"
             --steps 4 --sampling-method euler) ;;
    sdxl) W=(--model "$MODELS/sd_xl_turbo_1.0_fp16.safetensors" --taesd "$MODELS/taesdxl.safetensors"
             --steps 2 --sampling-method euler_a) ;;
    sd)   W=(--model "$MODELS/sd_turbo-f16-q8_0.gguf" --taesd "$MODELS/taesd.safetensors"
             --steps 2 --sampling-method euler_a) ;;
  esac
  echo "== painting a test picture with $BIN"
  SECONDS=0
  "$BIN" --mode img_gen "${W[@]}" \
    --prompt "three children flying a kite in a park, storybook illustration" \
    --negative-prompt "blurry, low quality, text, watermark" \
    --width 512 --height 512 --cfg-scale 1.0 \
    --seed -1 --threads "$JOBS" --output "$OUT" 2>&1 | grep -E "completed|error|Vulkan devices" | tail -3
  echo "wrote $OUT in ${SECONDS}s"
fi

echo "done. The brain finds these by default (images.sd_* in inmoov/settings.py) —"
echo "check config/settings.json's images block points at the set you fetched, then"
echo "restart fred-panel or Save the admin page, and 'Fred, draw me a dragon' works."

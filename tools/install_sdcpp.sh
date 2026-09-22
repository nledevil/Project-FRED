#!/usr/bin/env bash
# Give FRED a local painter: stable-diffusion.cpp on the NUC's cores, with
# sd-turbo. After this, "Fred, draw me a dragon" needs no internet and no key.
#
#   tools/install_sdcpp.sh              # build + fetch weights (~2 GB), idempotent
#   tools/install_sdcpp.sh --try        # ...and paint one picture to time it
#
# Lands where inmoov/settings.py's images.sd_bin / images.sd_model point by
# default: ~/fred/sdcpp/. Nothing here touches the repo. CPU-only on purpose —
# the Intel Arc iGPU works for Ollama through its bundled Vulkan, but the
# system Mesa only offers llvmpipe to everything else, and a build against
# that is slower than the cores. Revisit if the Vulkan SDK ever lands here.
#
# Why sd-turbo: it is a distilled SD 2.1 that paints a 512px picture in 1-4
# steps with no guidance pass, which on sixteen cores is seconds rather than
# the minute SD 1.5 takes at twenty steps. The q8_0 GGUF is 2 GB against 5 GB
# for the safetensors, and loads straight into sd.cpp without conversion.
set -euo pipefail

BASE="${SDCPP_HOME:-$HOME/fred/sdcpp}"
SRC="$BASE/stable-diffusion.cpp"
MODELS="$BASE/models"
MODEL_URL="https://huggingface.co/Green-Sky/SD-Turbo-GGUF/resolve/main/sd_turbo-f16-q8_0.gguf"
MODEL="$MODELS/sd_turbo-f16-q8_0.gguf"
# The tiny autoencoder (TAESD): decoding the finished latent through the
# full VAE is ten seconds on these cores, through this one second. Measured
# 2026-09-21: 16.5 s a picture without it, 6.7 s with.
TAESD_URL="https://huggingface.co/madebyollin/taesd/resolve/main/diffusion_pytorch_model.safetensors"
TAESD="$MODELS/taesd.safetensors"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOBS="$(( $(nproc) > 4 ? $(nproc) - 4 : 2 ))"

mkdir -p "$BASE" "$MODELS"

# cmake is not on this NUC; the venv can carry it, and the venv exists.
if ! command -v cmake >/dev/null 2>&1; then
  echo "== cmake: installing into the venv"
  "$REPO/venv/bin/pip" install -q cmake
  export PATH="$REPO/venv/bin:$PATH"
fi

if [ ! -d "$SRC/.git" ]; then
  echo "== cloning stable-diffusion.cpp"
  git clone -q --recursive https://github.com/leejet/stable-diffusion.cpp.git "$SRC"
fi

if [ ! -x "$SRC/build/bin/sd-cli" ]; then
  echo "== building (CPU), -j$JOBS"
  cmake -S "$SRC" -B "$SRC/build" -DCMAKE_BUILD_TYPE=Release -DSD_BUILD_SHARED_LIBS=OFF >/dev/null
  cmake --build "$SRC/build" --config Release -j "$JOBS" | tail -3
fi
echo "binary: $SRC/build/bin/sd-cli"

if [ ! -s "$MODEL" ]; then
  echo "== fetching sd-turbo q8_0 (~2 GB)"
  curl -fL --progress-bar -o "$MODEL.part" "$MODEL_URL"
  mv "$MODEL.part" "$MODEL"
fi
echo "weights: $MODEL ($(du -h "$MODEL" | cut -f1))"

if [ ! -s "$TAESD" ]; then
  echo "== fetching the tiny decoder (~10 MB)"
  curl -fL --progress-bar -o "$TAESD.part" "$TAESD_URL"
  mv "$TAESD.part" "$TAESD"
fi
echo "decoder: $TAESD"

if [ "${1:-}" = "--try" ]; then
  OUT="$BASE/try.png"
  echo "== painting a test picture"
  SECONDS=0
  "$SRC/build/bin/sd-cli" --mode img_gen --model "$MODEL" --taesd "$TAESD" \
    --prompt "a friendly robot painting a picture, watercolour" \
    --negative-prompt "blurry, low quality, text, watermark" \
    --width 512 --height 512 --steps 2 --cfg-scale 1.0 --sampling-method euler_a \
    --seed -1 --threads "$JOBS" --output "$OUT" 2>&1 | grep -E "txt2img|completed|generated|error" | tail -3
  echo "wrote $OUT in ${SECONDS}s"
fi

echo "done. The brain finds these by default (images.sd_bin / images.sd_model);"
echo "restart fred-panel or Save the admin page and 'Fred, draw me a dragon' works."

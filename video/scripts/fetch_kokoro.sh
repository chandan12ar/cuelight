#!/usr/bin/env bash
# Get the Kokoro-82M voice (Apache-2.0, offline, CPU) when Hugging Face / GitHub releases are blocked.
# The model weights and voice packs are republished on npm, which sandboxes usually allow.
#
#   scripts/fetch_kokoro.sh [DEST]          # DEST defaults to ~/.cache/kokoro
#   eval "$(scripts/fetch_kokoro.sh DEST | tail -2)"   # exports KOKORO_MODEL / KOKORO_VOICES
#
# Needs: node/npm, python3 with `pip install kokoro-onnx numpy`.
set -euo pipefail
DEST="${1:-$HOME/.cache/kokoro}"
mkdir -p "$DEST" && cd "$DEST"

if [ ! -s model_q8.onnx ]; then
  npm pack kokoro-q8-shards >/dev/null            # 6 parts of the int8 ONNX model (~92 MB joined)
  mkdir -p _shards && tar xzf kokoro-q8-shards-*.tgz -C _shards
  cat _shards/package/kokoro-q8.part{0,1,2,3,4,5}.bin > model_q8.onnx
fi
if [ ! -d voices ]; then
  npm pack kokoro-js >/dev/null                   # ships voices/<name>.bin (float32, 510x256 style vectors)
  mkdir -p _kjs && tar xzf kokoro-js-*.tgz -C _kjs
  cp -r _kjs/package/voices voices
fi
echo "model: $(ls -l model_q8.onnx | awk '{print $5}') bytes, voices: $(ls voices/*.bin | wc -l)"
echo "export KOKORO_MODEL=$DEST/model_q8.onnx"
echo "export KOKORO_VOICES=$DEST/voices"

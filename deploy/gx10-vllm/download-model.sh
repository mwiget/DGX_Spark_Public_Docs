#!/usr/bin/env bash
# Fetch the NVFP4 checkpoint (~46 GB) onto the Spark. Resumable — re-run if it
# drops. Requires an HF token with access to the gated repo (see deploy/README.md).
set -euo pipefail

REPO="${REPO:-saricles/Qwen3-Coder-Next-NVFP4-GB10}"
MODEL_DIR="${MODEL_DIR:-$HOME/models}"
HF="${HF:-$HOME/hf-venv/bin/hf}"

mkdir -p "$MODEL_DIR"
exec "$HF" download "$REPO" \
  --local-dir "$MODEL_DIR/$(basename "$REPO")" \
  --max-workers 4

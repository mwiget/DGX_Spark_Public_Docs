#!/usr/bin/env bash
# Fetch the NVFP4 checkpoint (~46 GB) onto the Spark. Requires an HF token with
# access to the gated repo (see deploy/README.md).
#
# Transfer notes, measured on this host:
#   * xet path (default):        ~10 MB/s, but throws intermittently, e.g.
#       "CAS Client Error: Format error: I/O error: error decoding response body"
#   * HF_HUB_DISABLE_XET=1:      ~150 KB/s — 60x slower, not worth it
#
# So: keep xet, and retry around its crashes. Every attempt resumes from the
# .incomplete files, so a crash costs nothing but a restart.
set -uo pipefail

REPO="${REPO:-saricles/Qwen3-Coder-Next-NVFP4-GB10}"
MODEL_DIR="${MODEL_DIR:-$HOME/models}"
HF="${HF:-$HOME/hf-venv/bin/hf}"
MAX_TRIES="${MAX_TRIES:-40}"
export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-0}"

mkdir -p "$MODEL_DIR"
dest="$MODEL_DIR/$(basename "$REPO")"

for try in $(seq 1 "$MAX_TRIES"); do
  echo "=== $(date -Is) attempt $try/$MAX_TRIES ==="
  if "$HF" download "$REPO" --local-dir "$dest" --max-workers 4; then
    echo "=== $(date -Is) download complete ==="
    exit 0
  fi
  echo "=== $(date -Is) attempt $try failed, resuming in 10s ==="
  sleep 10
done

echo "giving up after $MAX_TRIES attempts" >&2
exit 1

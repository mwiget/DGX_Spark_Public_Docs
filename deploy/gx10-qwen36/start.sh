#!/usr/bin/env bash
# start.sh — serve nvidia/Qwen3.6-35B-A3B-NVFP4 on gx10 with vLLM 0.30, :8896.
#
# NVIDIA's DGX Spark command from the model card, with the KV pool pinned
# (KV_CACHE_GIB, default 16 = ~1.6M tokens fp8: all 4 slots at the full 262k;
# only 10 of 40 layers keep a KV cache) instead of a utilization share.
# Measured 2026-10-06: weights ~20 GiB, plus ~10 GiB during warm-up
# (compile, CUDA graphs, MTP drafter) before the KV pool is allocated.
# Runs alone: Kolibri-1 (kolibri.service) is disabled. To run both, Qwen needs
# --language-model-only, 4096 batched tokens and a ~5 GiB pool, and Kolibri's
# pool must drop to 14 GiB.
# Never downloads: the checkpoint must be in ~/models/hf (hf download, anonymous).
set -euo pipefail

NAME=vllm-qwen36
PORT="${PORT:-8896}"
IMAGE="${IMAGE:-vllm/vllm-openai:v0.30.0}"
MODEL=nvidia/Qwen3.6-35B-A3B-NVFP4
HF_HOME="${HF_HOME:-$HOME/models/hf}"
KV_CACHE_GIB="${KV_CACHE_GIB:-16}"
# vLLM refuses to start unless GMU x MemTotal is free; with an explicit KV size
# GMU only feeds that check, so keep it just above what this server needs.
GMU="${GMU:-0.45}"
SPEC_TOKENS="${SPEC_TOKENS:-3}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-1200}"

SNAP=$(ls -d "$HF_HOME"/hub/models--nvidia--Qwen3.6-35B-A3B-NVFP4/snapshots/*/ 2>/dev/null | head -1)
[[ -n "$SNAP" ]] || { echo "checkpoint not in $HF_HOME" >&2; exit 1; }
REV=$(basename "$SNAP")

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
    --gpus all --network host --ipc host \
    --ulimit memlock=-1 --ulimit stack=67108864 \
    --memory 40g --memory-swap 40g \
    --log-opt max-size=50m --log-opt max-file=3 \
    -e HF_HOME=/root/.cache/huggingface -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
    -e FLASHINFER_DISABLE_VERSION_CHECK=1 -e CUTE_DSL_ARCH=sm_121a \
    -v "$HF_HOME:/root/.cache/huggingface" \
    -v "$HOME/.cache/vllm:/root/.cache/vllm" \
    -v "$HOME/.cache/flashinfer:/root/.cache/flashinfer" \
    "$IMAGE" "$MODEL" --revision "$REV" \
    --served-model-name qwen3.6-35b \
    --host 0.0.0.0 --port "$PORT" \
    --trust-remote-code \
    --kv-cache-dtype fp8 \
    --attention-backend flashinfer \
    --moe-backend marlin \
    --gpu-memory-utilization "$GMU" \
    --kv-cache-memory-bytes $((KV_CACHE_GIB * 1073741824)) \
    --max-model-len 262144 \
    --max-num-seqs 4 \
    --max-num-batched-tokens 8192 \
    --enable-chunked-prefill \
    --enable-prefix-caching \
    --enable-prompt-tokens-details \
    --speculative-config "{\"method\":\"mtp\",\"num_speculative_tokens\":$SPEC_TOKENS,\"moe_backend\":\"triton\"}" \
    --reasoning-parser qwen3 \
    --tool-call-parser qwen3_xml \
    --enable-auto-tool-choice >/dev/null

echo "launched $NAME on :$PORT (KV ${KV_CACHE_GIB} GiB, GMU $GMU), waiting for /health"
t0=$(date +%s)
until curl -sf -o /dev/null "http://127.0.0.1:$PORT/health"; do
    docker ps -q -f "name=^${NAME}$" | grep -q . || { docker logs --tail 40 "$NAME" >&2; echo "container exited" >&2; exit 1; }
    (( $(date +%s) - t0 > WAIT_TIMEOUT )) && { echo "not healthy after ${WAIT_TIMEOUT}s" >&2; exit 1; }
    sleep 5
done
echo "ready after $(( $(date +%s) - t0 ))s"

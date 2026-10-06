# Qwen3.6-35B-A3B on gx10 (DGX Spark, GB10)

The default model on gx10 since 2026-10-06, replacing
[Kolibri-1](../gx10-kolibri/README.md) (installed, service disabled).

| | |
|---|---|
| Model | Qwen3.6-35B-A3B: 35B MoE, ~3B active (256 experts, 8 routed), 40 layers, Apache-2.0 |
| Checkpoint | [`nvidia/Qwen3.6-35B-A3B-NVFP4`](https://huggingface.co/nvidia/Qwen3.6-35B-A3B-NVFP4), 23.5 GB, with an MTP head |
| Serving | stock `vllm/vllm-openai:v0.30.0`, no plugin |
| Endpoint | `http://gx10:8896/v1` (OpenAI + Anthropic `/v1/messages`), model `qwen3.6-35b` |
| Unit | `qwen36.service`, enabled at boot, `Conflicts=kolibri.service` |
| Context | **262,144** per request (native) |
| Dashboard | <http://lake1:3001/d/gx10-vllm> |

## Why this model

From a web survey of what runs well on one GB10 (October 2026). The most useful
source was [DG1001/local-agentic-coding-128gb](https://github.com/DG1001/local-agentic-coding-128gb),
which ran agentic coding tasks (86 hidden tests, opencode / Oh My Pi / Claude Code)
on an ASUS GX10 — the same box as gx10:

| Model | Weights | Hidden tests | Wall clock |
|---|---|---|---|
| **Qwen3.6-35B-A3B NVFP4** | 23 GB | 86/86 | **21:01** (fastest full score) |
| DeepSeek-V4-Flash | 88 GB | 86/86 | 25:49 — needs ~113 GiB and a non-vLLM server |
| Laguna-S-2.1 | 93 GB | 86/86 | 30:56 |
| Qwen3.8-Flash-Next Q3_K_XL | 84 GB | 86/86 twice | 33:33 |
| Kolibri-1 FP8 | 79 GB | 85/86, 84/86 | ~33 min |

The author's own warning applies: the same model moved up to 38 points between
runs, more than the spread between models. Qwen3.6-35B-A3B also scored 64 and 67
on earlier runs. Read the table as "these all solve this class of task"; the
reason to pick Qwen3.6 here is that it does so 2.5x faster than the rest, in a
quarter of the memory.

Of the NVFP4 builds, NVIDIA's (modelopt, static) measured 12-17 % faster than
Unsloth's on a Spark at comparable quality
([classmethod](https://dev.classmethod.jp/en/articles/dgx-spark-qwen3-6-35b-a3b-nvfp4-new-champion/)).

Engines considered: vLLM (chosen: same stack, metrics and clients as before),
[Atlas](https://github.com/Avarok-Cybersecurity/atlas) (fastest published numbers,
but young, tool-call JSON problems reported, 65k context in reports, no vLLM
metrics), llama.cpp (native NVFP4 now, ~65 tok/s decode on GB10) and SGLang
(no GB10 numbers for this model).

## Context and memory

Only 10 of the 40 layers are full attention (2 KV heads x 256); the other 30
are linear attention with a fixed-size state. KV is ~10 KB per token in FP8, so
a full 262k request needs ~2.6 GiB. 262,144 is the model's limit, not a memory
one: stopping Kolibri freed memory but did not raise it. Qwen documents YaRN
extension towards ~1M; it is static in vLLM (applies to every request) and was
not enabled.

| | GiB |
|---|---:|
| Weights (measured) | 21.97 |
| Extra during warm-up (compile, CUDA graphs, MTP drafter) | ~10 |
| KV pool (`KV_CACHE_GIB=16`) = 1,350,611 tokens, 5.15x 262k | 16 |
| Host `MemAvailable` after start | ~68 |

### Next to Kolibri: possible, not kept

Running both was tried first. With Kolibri at its 24 GiB KV pool, the first Qwen
start drove `MemAvailable` from 38 to 6 GiB before Qwen had allocated any KV,
and a guard script killed it (Kolibri's own watchdog stops Kolibri below 4 GiB).
It fits with `--language-model-only`, `--max-num-batched-tokens 4096`, a 5 GiB
Qwen pool and Kolibri's pool cut to 14 GiB (1.38M tokens, still one full 1M
request), but having both side by side was not worth that.

## Install

```bash
docker run --rm -e HF_HOME=/hf -v ~/models/hf:/hf --entrypoint hf \
    vllm/vllm-openai:v0.30.0 download nvidia/Qwen3.6-35B-A3B-NVFP4
mkdir -p ~/git/qwen36-spark && cp start.sh ~/git/qwen36-spark/
sudo cp qwen36.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now qwen36
```

The download is anonymous on purpose — see the stale-token note in
[gx10-kolibri](../gx10-kolibri/README.md#2-recipe-and-checkpoint).

`start.sh` is NVIDIA's DGX Spark command from the model card with two changes:
the KV pool is pinned with `--kv-cache-memory-bytes` instead of
`--gpu-memory-utilization 0.4` (vLLM then uses the utilization value only for its
free-memory check at startup), and `--load-format fastsafetensors` is dropped.
Everything else is as NVIDIA recommends: FP8 KV, flashinfer attention, Marlin
MoE, MTP with 3 speculative tokens, `qwen3` reasoning and `qwen3_xml` tool
parsers, 4 sequences, 8192 batched tokens. Start to `/health`: ~4.5 min.

Switching back to Kolibri:

```bash
sudo systemctl stop qwen36 && sudo systemctl start kolibri
```

## Clients

airouter's `gx10` backend now points at `:8896` (model `qwen3.6-35b`), so the
`coder` alias lands on Qwen; `qwen` is a new alias, and `kolibri` has its own
backend. pi and opencode default to `homelab/qwen` (262k context);
`claude-local --model qwen` sets the 262k window.

Qwen's chat template takes `enable_thinking` (via `chat_template_kwargs`), not
`reasoning_effort`, so pi's `/thinking` level does not reach it; the model
decides how much to think.

## Measured (2026-10-06)

| | |
|---|---|
| Decode, single stream, code task | **131 tok/s** thinking off, **111 tok/s** thinking on (end-to-end) |
| MTP acceptance | 71.7 % (2,301 of 3,210 draft tokens) |
| Tool call (OpenAI) | correct, 63 tokens of which 34 reasoning |
| pi through airouter | works |

Kolibri-1 on the same box: ~50 tok/s single stream.

## Dashboard notes

The vLLM row of the gx10 dashboard now describes Qwen and has an MTP panel. The
per-stream DECODE TOK/S stat is generated tokens over decode time: with
speculative decoding vLLM records one inter-token interval per decode **step**,
which emits ~3 tokens here (1,070 intervals for 3,370 tokens), so the earlier
`1 / mean inter-token latency` read ~3x low.

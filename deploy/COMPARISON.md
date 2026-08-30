# vLLM on gx10 vs the llama.cpp stack in `../claude-local`

Both stacks run Claude Code against a self-hosted model. This compares them on
measurements, not estimates. Numbers for lake1 and for gx10-under-llama.cpp are
from `../claude-local/CLAUDE.md`; the gx10-under-vLLM column was measured here.

| | lake1 · llama.cpp<br>Qwen3.8-27B **Q8** + MTP | gx10 · llama.cpp<br>Qwen3.8-27B Q8 | **gx10 · vLLM<br>Qwen3-Coder-Next NVFP4** |
|---|---|---|---|
| Hardware | RTX PRO 5000, 48 GB, sm_120 | GB10, 128 GB, sm_121 | GB10, 128 GB, sm_121 |
| Mem bandwidth | ~1344 GB/s | ~273 GB/s | ~273 GB/s |
| Active params | 27.3B (dense) | 27.3B (dense) | **~3B** (10 of 512 experts) |
| Prefill, cold | 1372 tok/s @100k | 659 tok/s | **2916 tok/s** @64k |
| Prefill, warm | **0.31 s** | — | 0.80 s |
| Decode, 1 stream | **76.2 tok/s** | 16.6 tok/s | 64.5 tok/s |
| Decode, 8 concurrent | — (2 slots) | — | **288 tok/s aggregate** |
| Context | 2 × 131k slots | — | 262k, up to 64 seqs |
| Spec decoding | MTP, +115% | — | unusable (see below) |

## The finding: on GB10, sparsity beats size — and beats the bandwidth deficit

`../claude-local/CLAUDE.md` concludes "lake1 wins decisively — 2.9x prefill, 4.6x
decode" and that gx10's 128 GB "only pays off for models that don't fit in 48 GB,
and it doesn't." That holds for the model it was measured on. It does not
generalise to the Spark.

GB10's problem is bandwidth (273 vs 1344 GB/s), and batch-1 decode is bandwidth
bound — but it is bound by **active** parameters, not total ones. Three data
points on the same box line up on that:

| Model on gx10 | Active params | Decode |
|---|---:|---:|
| Qwen3.8-27B (dense) | 27.3B | 16.6 tok/s |
| Qwen3.8-Flash-Next | 6B | 27.1 tok/s |
| Qwen3-Coder-Next | ~3B | **64.5 tok/s** |

Roughly inverse in active parameters. The right move on a Spark is a *sparser*
model, not a bigger one — and Coder-Next's 42.7 GiB also leaves the unified pool
swap-free, which is where Flash-Next's 93.7 GiB lost its prefill (176 tok/s while
pinning 15 GiB of swap).

The result is that gx10 **beats** lake1 on prefill (2.1x) and on concurrent
throughput, and lands within 15% on single-stream decode.

## What this comparison is not

- **Different models.** Qwen3-Coder-Next (79.7B MoE, coding-specialised) is not
  Qwen3.8-27B (27.3B dense, general, SWE-bench Pro 61.7, has a vision encoder).
- **Different precision.** NVFP4 (4-bit) vs Q8_0 (8-bit). The lake1 stack is
  running a materially higher-fidelity quant.
- **Different prefill depth.** 64k here vs 100k there.

So this is a **throughput** comparison, not a quality one. If output quality at
Q8 matters more than speed, lake1 remains the better box.

## Carried over from `../claude-local` — these all still applied

- **`CLAUDE_CODE_ATTRIBUTION_HEADER=0`** is just as mandatory here. Measured on
  this server: a 64k-token turn is 21.9 s cold vs **0.80 s** warm.
- **`pkill -f <pattern>` kills your own shell** when the pattern appears in the
  invoking command line. Hit it twice (exit 144) writing this. Use a bracket
  guard — `pkill -f "claude-spark-shi[m]"` — and never put the kill and the
  restart in the same command.
- **GPU memory is invisible in RSS on GB10.** Unified memory; use
  `nvidia-smi --query-compute-apps=...`. It also means the host's ~20 GiB counts
  against vLLM's budget — see the 0.80 note in README.md.

## What differed

- **Chat template.** The Jinja bug that broke both GGUF templates
  (`System message must be at the beginning`) does **not** occur here — the
  NVFP4 checkpoint's `chat_template.jinja` has no `raise_exception`.
- **But Claude Code still fails without a shim**, one layer up: it sends a
  `role="system"` message *inside* the messages array, and vLLM's `/v1/messages`
  validator rejects it (`Input should be 'user' or 'assistant'`). No template can
  fix a request-validation error. `claude-spark-shim.py` hoists those messages
  into the top-level `system` field. Note Claude Code posts to
  `/v1/messages?beta=true` — match on the path component, not the raw string.
- **fp8 KV cache.** `../claude-local` warns "f16, NOT fp8 — silent corruption on
  sm_120 + DeltaNet". Qwen3-Coder-Next *is* DeltaNet and we do run fp8 KV on
  sm_121. A needle-in-haystack probe passed 3/3 at 10/50/90% depth — but only at
  ~19k tokens, and vLLM's fp8 path is not llama.cpp's. Not disproven at the ~107k
  depths real turns reach. If output degrades on long sessions, drop
  `--kv-cache-dtype fp8` first.
- **Speculative decoding is unavailable on Coder-Next**, where lake1 gets +115%
  from MTP. The checkpoint ships no MTP weights, and ngram crashes the engine
  under concurrency (`gdn_attn.py` assert) — see below.

## Speculative decoding on GB10 — measured, three ways

All on gx10, identical harness, `max_num_seqs=64`.

| | Coder-Next<br>no spec | Coder-Next<br>+ ngram | Qwen3.8-27B<br>+ MTP |
|---|---:|---:|---:|
| Active params | ~3B | ~3B | 27.3B |
| Edit prompt (1 stream) | 61.2 tok/s | **126.4** | 20.7 |
| From-scratch (1 stream) | 64.5 tok/s | 61.1 | 21.4 |
| Concurrency 4 | 156.1 agg | **engine died** | 72.1 agg (4/4) |
| Concurrency 8 | **288.2 agg** | not reached | 135.1 agg (8/8) |
| Draft acceptance | — | 37.2% | **69.4%** |
| Acceptance by position | — | 52/36/31/29% | **84/68/56%** |

### MTP does work under concurrency; ngram does not

The `gdn_attn.py` assertion that killed ngram —
`assert not (num_decodes > 0 and num_spec_decodes > 0)` — fires on *mixed*
batches. ngram drafts **opportunistically** (only when an n-gram matches), so a
batch contains both drafting and non-drafting sequences: the forbidden mix. MTP
drafts **unconditionally**, so no mix arises. Qwen3.8-27B is also a hybrid
attention model (48 linear + 16 full), and it ran 8/8 concurrent cleanly.

So hybrid attention does not rule out speculative decoding — *conditional*
drafting does.

### MTP's acceptance is far better, and it still loses

69.4% overall vs ngram's 37.2%, and much flatter by position (84/68/56 vs
52/36/31/29) — MTP predicts any token, ngram only repeated spans. It is the
better speculative method by a wide margin.

It still loses on absolute throughput, because it is attached to a model with
**9x the active parameters**. Decode on GB10 is bandwidth-bound in active params,
and a ~2x speculative win cannot close a 9x gap:

    Coder-Next  ~3B active, no spec   ->  64.5 tok/s
    Qwen3.8-27B 27.3B active, +MTP    ->  21.4 tok/s

This is the same scaling law as the table above, and it holds *through* a
working speculative decoder. On a bandwidth-starved box, picking a sparser model
beats adding speculation to a denser one.

### Getting an MTP-capable 27B to load at all

Three NVFP4 repos of the same model failed in `avarok:v23`, each differently.
Check all three before spending 20 GB of download:

| Repo | Failure |
|---|---|
| `unsloth/Qwen3.8-27B-NVFP4` | compressed-tensors `actorder=static` with `strategy=tensor_group` |
| `RadixArk/Qwen3.8-27B-NVFP4` | ModelOpt `quant_algo=MIXED_PRECISION`, not in vLLM's supported list |
| `sakamakismile/Qwen3.8-27B-MTP-NVFP4` | loads — **but only with `VLLM_NVFP4_GEMM_BACKEND=cutlass`** |

The last is a kernel constraint, not config: with `marlin` (which the Coder-Next
model card specifies) it dies on `size_n = 96 is not divisible by tile_n_size = 64`.
Backend options are cutlass, flashinfer-cutlass, flashinfer-trtllm,
flashinfer-cudnn, fbgemm, marlin, emulation. Metadata alone cannot predict this
one — only the first two are checkable ahead of time.

## Verdict

Keep **Qwen3-Coder-Next NVFP4 with no speculative decoding** on gx10. It is the
fastest option measured here at every concurrency level, it is the
coding-specialised model, and it is stable. The llama.cpp stack on lake1 remains
ahead on single-stream decode (76.2 tok/s) and on quant fidelity (Q8 vs NVFP4).

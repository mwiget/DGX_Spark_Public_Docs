# Kolibri-1 on gx10 (DGX Spark, GB10)

[Aleph Alpha Kolibri-1](https://huggingface.co/Aleph-Alpha/Kolibri-1) served with
vLLM on the DGX Spark, with the full **1,048,576-token** context, for agentic
coding through pi, opencode and Claude Code. Replaced Qwen3-Coder-Next
(section 3 of [../README.md](../README.md)) on 2026-10-06.

> **Not the default any more (same day).** gx10 now serves Qwen3.6-35B-A3B —
> [gx10-qwen36](../gx10-qwen36/README.md). Everything below is still installed;
> `kolibri.service` is disabled. Switch back with
> `sudo systemctl stop qwen36 && sudo systemctl start kolibri`; the units conflict,
> so only one runs. In the meantime DG1001's GX10 benchmark scored Kolibri-1 FP8
> 85/86 and 84/86 with 0 malformed tool calls in 257 — close to the leaders.

| | |
|---|---|
| Model | Kolibri-1: 78.1B MoE, 3.46B active (384 experts, 6 + 1 shared), 50 layers, Apache-2.0 |
| Checkpoint | [`iSkye/Kolibri-1-NVFP4-Experts`](https://huggingface.co/iSkye/Kolibri-1-NVFP4-Experts) |
| Serving | stock `vllm/vllm-openai:v0.30.0` + Aleph Alpha's `aleph-alpha-inference` plugin |
| Recipe | [15ky3/Kolibri-1-DGX-Spark](https://github.com/15ky3/Kolibri-1-DGX-Spark) @ `4f76c24`, in `~/git/Kolibri-1-DGX-Spark` |
| Endpoint | `http://gx10:8895/v1` (OpenAI + Anthropic `/v1/messages`), model `kolibri-1` |
| Unit | `kolibri.service` (disabled since 2026-10-06) |
| Dashboard | <http://lake1:3001/d/gx10-vllm> |

## Quantisation

The checkpoint is mixed precision, not "4-bit everything":

| Part | Format |
|---|---|
| Routed experts (70 of 73.4 GiB) | **NVFP4 weight-only** (W4A16): FP4 E2M1, group 16, FP8 E4M3 group scales, served by vLLM's Marlin kernel |
| Attention, shared expert, dense layers | Aleph Alpha's original FP8, 128x128 block scales |
| Embeddings, LM head, norms, router | BF16 |
| KV cache | FP8 (what Aleph Alpha evaluated with) |

42.8 GiB of weights instead of 73.6 GiB for the original FP8. The recipe author
measured perplexity +1.2 % (English) and +3.6 % (German) against FP8, and
+8 to +48 % decode speed. `QUANT=fp8` in `.env` serves the original instead.

## Why 1M context fits

Only 10 of the 50 layers attend over the whole context (4 KV heads x 128); the
other 40 use a 513-token sliding window. The full-attention layers carry no
positional encoding, which is why Aleph Alpha can extend 262k to 1M without
position scaling. KV per token is about 20 KB in BF16 and half that in FP8, so
a full 1M-token request needs only ~12.6 GiB of KV.

Aleph Alpha validated quality up to 1M but recommend <= 262k for complex tasks.
In practice agents send far less; the dashboard's PROMPT LENGTH panel shows it.

## Install

### 1. Free disk and memory

46 GB checkpoint + 22 GB image. On gx10 that meant pruning orphaned Docker
volumes and build cache (~195 GB) and moving the Qwen3.8-Flash-Next and
Qwen3-Coder-Next weights to `tnas:/zfs/archive/models` (`/mnt/tnas`).

The old Qwen servers have to be off — they hold the unified memory:

```bash
sudo systemctl disable --now vllm-coder-next
# and stop any hand-started llama-server
```

### 2. Recipe and checkpoint

```bash
cd ~/git && git clone https://github.com/15ky3/Kolibri-1-DGX-Spark
cd Kolibri-1-DGX-Spark
cp /path/to/this/dir/kolibri.env .env
docker run --rm -e HF_HOME=/hf -v ~/models/hf:/hf --entrypoint hf \
    vllm/vllm-openai:v0.30.0 download iSkye/Kolibri-1-NVFP4-Experts
./start.sh --no-launch      # prints the budget and the vllm command, runs nothing
```

The recipe's own `./download.sh` does the same, but it passes the stored
Hugging Face token. On gx10 that token was a stale OAuth token, and the Hub
answers a bad token with **"Model ... not found"** even for public repos. The
checkpoint is public, so the anonymous download above sidesteps it.

The Aleph Alpha plugin is bind-mounted into the stock image rather than
pip-installed: its wheel pins `vllm<0.30`, but every symbol it imports exists in
0.30.0.

### 3. systemd

```bash
sudo cp kolibri.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now kolibri
```

`Type=oneshot` + `RemainAfterExit`: `start.sh` launches the container detached
and returns once `/health` answers (~80-140 s with `LOAD_STRATEGY=eager`).
The recipe's memory watchdog (`scripts/memwatch.sh`) stays in the unit's cgroup.
`Restart=on-failure` retries every 2 min, for when the pre-flight memory check
fails because the k3s stacks are still starting at boot.

From then on use `sudo systemctl restart kolibri`, not `./start.sh` by hand.
If the watchdog emergency-stops the container, the unit still reads
`active (exited)` — check `/health`, not `systemctl status`.

## Local deviations from the recipe (`kolibri.env`)

| Setting | Recipe default | Here | Why |
|---|---|---|---|
| `MAX_MODEL_LEN` | 262144 | **1048576** | the point of the exercise; adds the `max_position_embeddings` override |
| `KV_CACHE_GIB` | 48 (nvfp4) | **24** | 48 needs ~108 GiB free; k3s/bnkscope stacks on gx10 need ~20 GiB. 24 GiB = 2.36M tokens = 2.25 concurrent 1M requests |
| `MAX_NUM_SEQS` | 8 | **4** | personal use; fewer slots, more KV per slot |
| `LOAD_STRATEGY` | lazy (~500 s) | **eager** (~80 s) | same weights, faster start |
| reasoning default | template: `high` | **`medium`** | `--default-chat-template-kwargs`; at high the model can spend the whole budget thinking |

Memory budget: 43 weights + 24 KV + 7 overhead = **74 GiB** (GMU 0.608). With
the k3s stacks running, gx10 keeps ~37 GiB `MemAvailable`.

## Clients

Clients reach gx10 through **airouter** on each notebook (`127.0.0.1:4000`),
which fails over between backends. Its `gx10` backend points at
`http://100.67.215.111:8895`, model `kolibri-1`, with a `kolibri` alias, so the
existing `coder` alias now lands on Kolibri as well.

| Client | Config | Notes |
|---|---|---|
| pi | `~/.pi/agent/models.json`, provider `homelab`, model `kolibri`, `contextWindow` 1048576 | `thinkingLevelMap: {off: "none", minimal: "low"}` — vLLM honours a top-level `reasoning_effort` |
| opencode | `opencode.json`, provider `homelab`, model `kolibri`, `limit.context` 1048576 | |
| Claude Code | `claude-local --model kolibri` | Anthropic `/v1/messages` passes through; context window set to 1M |

Reasoning effort per request: `"reasoning_effort": "none" | "low" | "medium" | "high"`
(or `chat_template_kwargs`). Recommended sampling: temperature 1.0, top_p 0.97,
top_k 128.

## Measured (2026-10-06)

| | |
|---|---|
| Startup (eager, warm page cache) | 80-136 s |
| Decode, single stream | ~50 tok/s |
| KV pool | 2,364,060 tokens |
| OpenAI tool call / Anthropic `tool_use` | both correct, ~1 s |
| pi, trivial bug fix + verify | **9 s** |
| Claude Code, same kind of task | **2 min 48 s** — cold prefill of its large system prompt plus reasoning; expect better once the prefix cache is warm |

For reference, the recipe author measured on FP8: ~5.3k tok/s prefill at 21k
context, ~580 tok/s averaged over a 978k fill, needle-in-a-haystack pass from
5k to 978k.

## Monitoring

Prometheus on lake1 scrapes `gx10:8895` as job `vllm` (label `host=gx10`).
The dashboard is generated:

```bash
cd deploy/lake1-observability
./build-lake1-dashboard.py --host gx10 --engine vllm \
    > grafana/provisioning/dashboards/json/gx10_vllm.json
```

The upstream DGX Spark dashboard (`dgx-spark-vllm-v1`) reads the same job and
works too.

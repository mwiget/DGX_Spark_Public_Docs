# Deployment: lake1 (observability) + gx10 (DGX Spark)

Concrete deployment of the upstream dashboard across two machines:

| Host | Role | What runs there |
|---|---|---|
| **lake1** (`192.168.68.113`) | observability | Prometheus `:9494`, Grafana `:3001` |
| **gx10** (`100.67.215.111`, Tailscale) | DGX Spark GB10 | vLLM `:8006`, node_exporter `:9100` |

lake1 reaches gx10 over Tailscale. Prometheus scrapes both exporters on gx10;
nothing needs to be installed on lake1 beyond Docker.

```
gx10 (GB10)                          lake1
┌──────────────────────┐             ┌─────────────────────────┐
│ vLLM        :8006 ───┼──scrape────▶│ Prometheus       :9494  │
│ node_exporter :9100 ─┼──scrape────▶│          │              │
└──────────────────────┘             │          ▼              │
                                     │ Grafana          :3001  │
                                     └─────────────────────────┘
```

## Why a separate stack from bnkscope

lake1 already runs `bnkscope-grafana` (11.4.0) and `bnkscope-prometheus` on
`:3000` / `:9491`. The upstream dashboard is **Grafana schema v2**, which needs
Grafana **13.x** plus the `dashboardNewLayouts` and `kubernetesDashboards`
feature toggles — 11.4.0 cannot render it. This stack is therefore independent
and uses `:3001` / `:9494` (3000, 9090 and 9491 are all taken on lake1).

## The datasource UID trick

The upstream dashboard hardcodes the Prometheus datasource UID `dfr1d9ottv8xsc`
in ~72 places, and the upstream README tells you to search-and-replace it.

We do the opposite: `grafana/provisioning/datasources/prometheus.yaml` pins our
datasource to that exact UID. The dashboard file then stays **byte-identical to
upstream**, so `git merge upstream/main` never conflicts on it.

## 1. Observability stack on lake1

```bash
cd deploy/lake1-observability
cp .env.example .env      # set a real GF_ADMIN_PASSWORD before exposing :3001
docker compose up -d
```

Then import the dashboard:

```bash
./import-dashboard.sh     # idempotent; re-run after pulling upstream changes
```

The dashboard file is a bare v2 *spec* with no `apiVersion`/`kind` wrapper, so
the legacy `/api/dashboards/db` endpoint rejects it. The script wraps it in a
`Dashboard` object and POSTs to the k8s-style resource API
(`/apis/dashboard.grafana.app/v2beta1/...`), falling back to PUT on 409.

Dashboard: <http://lake1:3001/d/dgx-spark-vllm-v1>

## 2. node_exporter on gx10

```bash
ssh gx10 mkdir -p '~/dgx-spark-obs/node-exporter'
scp deploy/gx10-node-exporter/docker-compose.yml gx10:~/dgx-spark-obs/node-exporter/
ssh gx10 'cd ~/dgx-spark-obs/node-exporter && docker compose up -d'
```

Verify from lake1: `curl -s http://100.67.215.111:9100/metrics | head`

## Verifying

```bash
# Both targets, with health and last error
curl -s http://localhost:9494/api/v1/targets \
  | python3 -c 'import sys,json;[print(t["labels"]["job"],t["health"],t.get("lastError","")) for t in json.load(sys.stdin)["data"]["activeTargets"]]'

# Live host data through the Grafana datasource
curl -s -u admin:admin -G \
  http://localhost:3001/api/datasources/proxy/uid/dfr1d9ottv8xsc/api/v1/query \
  --data-urlencode 'query=100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100)'
```

The `vllm` target reads **down** until vLLM is running on gx10:8006. That is
expected, and the dashboard's vLLM panels show `0` rather than "No data"
because every vLLM expression ends in `or vector(0)`.

## Notes on gx10

- Ubuntu 24.04.4, aarch64, GB10, driver 580.173.02, 121 GB unified memory
- Docker 29.2.1 + nvidia-container-toolkit 1.20.0; `docker run --gpus all` works
  (CDI spec at `/var/run/cdi/nvidia.yaml`)
- ollama also runs on `127.0.0.1:11434` and holds GPU memory when a model is
  loaded — account for it when setting vLLM's `--gpu-memory-utilization`

## 3. vLLM on gx10

Model: [`saricles/Qwen3-Coder-Next-NVFP4-GB10`](https://huggingface.co/saricles/Qwen3-Coder-Next-NVFP4-GB10)
— 79.7B MoE coding model (512 experts, 10 active), NVFP4-quantised to 45.9 GB,
~62 tok/s decode, 262k context.

The repo is **gated**: accept the terms once on huggingface.co, then put a token
on gx10:

```bash
ssh gx10 '~/hf-venv/bin/hf auth login'
```

Then fetch the weights and start the service:

```bash
ssh gx10 '~/dgx-spark-obs/vllm/download-model.sh'          # ~46 GB, resumable
ssh gx10 'sudo cp ~/dgx-spark-obs/vllm/vllm-coder-next.service /etc/systemd/system/ \
          && sudo systemctl daemon-reload \
          && sudo systemctl enable --now vllm-coder-next'
```

### Deviations from the model card

| | Model card | Here | Why |
|---|---|---|---|
| Port | 8000 | **8006** | matches the upstream README's scrape target |
| `GPU_MEMORY_UTIL` | 0.90 | **0.80** | see below — 0.90 does not fit on this host |
| healthcheck | port 8888 | port 8000 | the image's baked-in check probes a port we never serve on |
| model name | env var | `--served-model-name` | the image **ignores** `SERVED_MODEL_NAME` |

Without the `--served-model-name qwen3-coder-next` flag, `/v1/models` reports the
id as `/models/Qwen3-Coder-Next-NVFP4-GB10` and every client has to send that
full path. Setting the `SERVED_MODEL_NAME` env var does nothing.

**0.90 does not work here, even with ollama stopped.** GB10 memory is unified, so
the host's own footprint (k3s, FRR, the other containers — roughly 20 GiB) comes
out of the same 121.63 GiB pool. Only ~101 GiB is free at startup, so vLLM aborts:

```
ValueError: Free memory on device cuda:0 (101.28/121.63 GiB) on startup is less
than desired GPU memory utilization (0.9, 109.46 GiB).
```

The ceiling is ~0.83. We use **0.80** for headroom, which still leaves ~54 GiB
of KV cache after the 42.7 GiB of weights. Raising it means freeing host memory
first, not just stopping ollama.

ollama was disabled to free unified memory for vLLM:

```bash
sudo systemctl disable --now ollama    # re-enable if you want it back
```

### Speculative-decoding panels will read zero

Qwen3-Coder-Next is a DeltaNet+attention hybrid with no MTP draft model, so
`vllm:spec_decode_*` is never emitted. Those panels show `0` rather than
"No data" because of the `or vector(0)` fallbacks — this is not a broken
dashboard. To exercise them, run Qwen3.8-27B-NVFP4 with `--speculative-config`.

## Local customisation

`import-dashboard.sh` patches local values into the dashboard at import time
instead of editing the file, keeping `Grafana_Dashboards/` merge-clean:

```bash
SPARK_POWER_W=240 ELECTRICITY_RATE_KWH=0.27 ./import-dashboard.sh
```

Those are the current defaults (upstream ships 240 W at $0.151/kWh). The script
fails loudly if the upstream energy expression changes shape, rather than
silently importing an unpatched dashboard.

## Measured on this deployment

| | |
|---|---|
| Model load time | ~330 s from a cold container |
| First request after load | ~34 s (CUDA graph capture; not representative) |
| Warm decode | **61 tok/s** (model card claims 62) |
| Context | 262,144 tokens |
| GPU memory util | 0.80 — see the note above on why not 0.90 |

Expect a ~34 s first request after every restart. Warm requests settle at 61 tok/s.

The speculative-decoding panels read `0` permanently with this model — see the
note above. Everything else populates under load.

## lake1 dashboard — RTX PRO 5000 + llama.cpp

A second dashboard covers lake1 itself, alongside the DGX Spark one.

**<http://lake1:3001/d/lake1-llamacpp>**

```bash
docker compose -f deploy/lake1-exporters/docker-compose.yml up -d
python3 deploy/lake1-observability/build-lake1-dashboard.py \
  > Grafana_Dashboards/lake1/lake1_llamacpp.json
# import via /api/dashboards/db (schema v1; Grafana 13 migrates it)
```

Two exporters on lake1:

| Exporter | Port | Notes |
|---|---|---|
| `prom/node-exporter` | 9100 | host CPU/mem/disk/net |
| `utkuozdemir/nvidia_gpu_exporter` | 9835 | wraps `nvidia-smi`; works on RTX/workstation cards, unlike DCGM which targets datacenter GPUs |

The dashboard is generated by `build-lake1-dashboard.py` rather than hand-written.
It emits **schema v1** on purpose — Grafana 13 migrates v1 to v2 losslessly for
these panel types, and v1 is far easier to generate correctly than hand-rolled
v2 `elements`/`layout`. (The upstream DGX Spark dashboard is native v2 and is
imported differently — see `import-dashboard.sh`.)

### An NFS hang can take out all host metrics

Seen on gx10: `tnas:/zfs/archive` became unreachable, the filesystem collector
blocked, and at a 5s scrape interval requests piled up until node_exporter hit
its 40-request ceiling. Every scrape then returned:

```
Limit of concurrent requests reached (40), try again later.
```

That kills *all* host metrics, not just filesystem ones, and it does not recover
on its own — the stuck requests are never released. Both exporters now set
`--collector.filesystem.fs-types-exclude=...|nfs|nfs4|cifs|smb3|fuse\..*` and the
node/GPU jobs scrape at 15s with a 10s timeout instead of the global 5s.

### llama.cpp metrics

llama.cpp has a built-in Prometheus endpoint — no exporter needed. It required
two changes in `../claude-local/serve-lake1.sh` plus a restart:

```diff
- --alias qwen3.8-27b-q8 --host 127.0.0.1 --port 8090 \
+ --alias qwen3.8-27b-q8 --host 0.0.0.0 --port 8090 --metrics \
```

`--metrics` because the endpoint otherwise returns `501 ... Start it with
--metrics`, and the bind because a bridged Prometheus container cannot reach the
host loopback even via `host-gateway`. **This exposes 8090 on the LAN/tailnet** —
the same posture as the gx10 llama-server and gx10 vLLM, but it is a change;
firewall the port if that is not wanted.

**llama.cpp exposes no `kv_cache_*` metrics** (vLLM does). The equivalent signal
is `prompt_tokens_cached_total` — prefix-cache reuse — and it is the one that
matters, since ../claude-local measures 0.31s warm vs 73s for a 100k cold
prefill. `prompt_tokens_total` *excludes* cached tokens, so hit rate is
`cached / (cached + total)`.

It does expose **MTP speculative-decoding counters**, which the vLLM dashboard
can be compared against directly. Measured on lake1 immediately after enabling:

| | |
|---|---:|
| MTP acceptance | **93.9%** |
| Draft tokens per step | 1.84 (capped at 2 by `--spec-draft-n-max 2`) |
| Decode, counter-derived | 74.8 tok/s |

For reference, on gx10: vLLM MTP reached 69.4% acceptance and vLLM ngram 37.2%.
Different models and implementations, so this is not a like-for-like ranking.

**Use the counters, not the `*_tokens_seconds` gauges.** Those gauges report only
during active generation and read 0 whenever the server is idle, so a dashboard
built on them looks broken between requests. The panels derive throughput as
`rate(tokens_predicted_total) / rate(tokens_predicted_seconds_total)`, which is
correct over any window.

## Running Claude Code against it

```bash
ln -s "$PWD/deploy/claude-spark" ~/bin/claude-spark   # matches ../claude-local
claude-spark                    # in any repo, like `claude`
claude-spark -p "..."           # headless
BACKEND=lake1 claude-spark      # the llama-server stack in ../claude-local
```

`claude-spark` health-checks the server, starts `claude-spark-shim.py` on
127.0.0.1:8016 if it is not already up, and execs `claude`. See COMPARISON.md
for why the shim is needed — Claude Code puts a `role="system"` message inside
the messages array, which vLLM's `/v1/messages` rejects.

It is normally invoked through a `~/bin` symlink, so the script resolves its own
path with `readlink -f`; without that it would look for the shim next to the
link rather than next to itself.

## Disk

The NVFP4 checkpoints are large. Currently on gx10:

| Path | Size | |
|---|---:|---|
| `~/models/Qwen3-Coder-Next-NVFP4-GB10` | 43 GB | in use |
| `~/models/qwen3.8-cc.jinja` | 12 KB | patched Claude Code template, from ../claude-local |

229 GB free. The two Qwen3.8-27B NVFP4 checkpoints were deleted after
benchmarking, along with the gx10 copy of `mtp-Qwen3.8-27B-Q4_0.gguf` — 42 GB
reclaimed. The lake1 copy of that GGUF is untouched and still backs
`serve-lake1.sh` in ../claude-local. To redo the 27B comparison, re-download
`sakamakismile/Qwen3.8-27B-MTP-NVFP4` — the only one of three that loads, and
only with `VLLM_NVFP4_GEMM_BACKEND=cutlass`. See COMPARISON.md.

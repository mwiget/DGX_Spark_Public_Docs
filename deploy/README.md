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
| `GPU_MEMORY_UTIL` | 0.90 | **0.90** | ollama is stopped + disabled, so the memory is free |

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

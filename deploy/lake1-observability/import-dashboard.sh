#!/usr/bin/env bash
# Import the upstream schema-v2 dashboard into Grafana 13, applying local overrides.
#
# The dashboard file is a bare v2 *spec* — it has no apiVersion/kind wrapper, so
# the legacy /api/dashboards/db endpoint rejects it. It has to go through the
# k8s-style resource API, wrapped in a Dashboard object. Re-running this updates
# the existing dashboard in place.
#
# Local values (electricity rate, Spark power draw) are patched in HERE rather
# than edited into the dashboard file, so Grafana_Dashboards/ stays byte-identical
# to upstream and `git merge upstream/main` never conflicts on it.
set -euo pipefail

GRAFANA_URL="${GRAFANA_URL:-http://localhost:3001}"
GRAFANA_AUTH="${GRAFANA_AUTH:-admin:admin}"
DASH_NAME="${DASH_NAME:-dgx-spark-vllm-v1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DASH_FILE="${DASH_FILE:-$REPO_ROOT/Grafana_Dashboards/vllm_25.1/dgx_spark_vllm_grafana_v1.yaml}"

# Local overrides — upstream ships 240 W at $0.151/kWh.
export SPARK_POWER_W="${SPARK_POWER_W:-240}"
export ELECTRICITY_RATE_KWH="${ELECTRICITY_RATE_KWH:-0.27}"

payload=$(mktemp); trap 'rm -f "$payload" "$payload.up"' EXIT

python3 - "$DASH_FILE" "$DASH_NAME" > "$payload" <<'PY'
import json, os, re, sys

spec = json.load(open(sys.argv[1]))
power_w = float(os.environ["SPARK_POWER_W"])
rate    = float(os.environ["ELECTRICITY_RATE_KWH"])

# Local energy-cost panel. Upstream: vector((($__range_s) / 3600) * 0.240 * 0.151)
ENERGY = re.compile(r"vector\(\(\(\$__range_s\) / 3600\) \* [0-9.]+ \* [0-9.]+\)")
new_expr = f"vector((($__range_s) / 3600) * {power_w / 1000:g} * {rate:g})"

patched = 0
for el in spec["elements"].values():
    s = el["spec"]
    for q in s["data"]["spec"]["queries"]:
        qspec = q.get("spec", {}).get("query", {}).get("spec", {})
        if ENERGY.fullmatch(qspec.get("expr", "").strip()):
            qspec["expr"] = new_expr
            s["description"] = ("Estimated local electricity cost for selected "
                                f"range: {power_w:g}W at ${rate:g}/kWh.")
            patched += 1

if patched != 1:
    sys.exit(f"expected 1 energy-cost panel, patched {patched} — "
             "upstream expression may have changed")

print(f"energy cost -> {power_w:g}W @ ${rate:g}/kWh", file=sys.stderr)

# Local GPU row. Upstream's only "GPU" number is vLLM's KV-cache usage, which stays
# empty when gx10 runs llama.cpp instead of vLLM. These read nvidia_gpu_exporter
# (container gx10-gpu-exporter, Prometheus job gx10-gpu) and work for any engine.
# The row goes in above the host-telemetry row (y=13); everything below moves down.
import copy

GPU_Y, GPU_H = 13, 6
G = 'nvidia_smi_'
SEL = '{host="gx10"}'

def gauge(eid, title, expr, unit, steps, desc, maxv=None):
    el = copy.deepcopy(spec["elements"]["panel-25"])   # upstream MEMORY UTILIZATION gauge
    s = el["spec"]
    q = s["data"]["spec"]["queries"][0]["spec"]["query"]["spec"]
    q["expr"], q["legendFormat"] = expr, title.lower()
    s["title"], s["description"], s["id"] = title, desc, eid
    d = s["vizConfig"]["spec"]["fieldConfig"]["defaults"]
    d["unit"] = unit
    d["thresholds"]["steps"] = [{"color": c, "value": v} for v, c in steps]
    d.pop("max", None)
    if maxv is not None:
        d["max"] = maxv
    return el

def timeseries(eid, title, series, desc):
    el = copy.deepcopy(spec["elements"]["panel-44"])   # upstream OUTPUT TOKENS / SEC OVER TIME
    s = el["spec"]
    tmpl = s["data"]["spec"]["queries"][0]
    s["data"]["spec"]["queries"] = []
    overrides = []
    for i, (expr, legend, unit, color, right) in enumerate(series):
        q = copy.deepcopy(tmpl)
        q["spec"]["refId"] = "ABCD"[i]
        q["spec"]["query"]["spec"]["expr"] = expr
        q["spec"]["query"]["spec"]["legendFormat"] = legend
        s["data"]["spec"]["queries"].append(q)
        props = [{"id": "color", "value": {"fixedColor": color, "mode": "fixed"}},
                 {"id": "unit", "value": unit}]
        if right:
            props.append({"id": "custom.axisPlacement", "value": "right"})
        overrides.append({"matcher": {"id": "byName", "options": legend}, "properties": props})
    s["vizConfig"]["spec"]["fieldConfig"]["overrides"] = overrides
    s["title"], s["description"], s["id"] = title, desc, eid
    return el

GREEN, YELLOW, RED, BLUE = "#76B900", "#E5C100", "#FF4D4D", "#3D9DF3"
new = {
    "panel-201": (0, 4, gauge(201, "GPU UTILIZATION", f"100 * avg({G}utilization_gpu_ratio{SEL})", "percent",
                              [(0, GREEN)], "GB10 busy time from nvidia-smi (any engine: vLLM, llama.cpp).",
                              maxv=100)),
    "panel-202": (4, 4, gauge(202, "GPU POWER", f"avg({G}power_draw_watts{SEL})", "watt",
                              [(0, GREEN)], "GB10 GPU power draw from nvidia-smi. nvidia-smi reports no "
                              "power limit for the GB10, so the gauge has no fixed maximum.")),
    "panel-203": (8, 4, gauge(203, "GPU TEMPERATURE", f"avg({G}temperature_gpu{SEL})", "celsius",
                              [(0, GREEN), (80, YELLOW), (88, RED)],
                              "GB10 temperature. nvidia_smi_temperature_gpu_tlimit is the headroom to the "
                              "slowdown limit; 84 C read 6 C below it under a sustained benchmark "
                              "(2026-10-02).", maxv=100)),
    "panel-204": (12, 4, gauge(204, "GPU SM CLOCK", f"avg({G}clocks_current_sm_clock_hz{SEL})", "hertz",
                               [(0, GREEN)], "Current SM clock; a drop under load with a high temperature "
                               "means thermal throttling.", maxv=3.003e9)),
    "panel-205": (16, 8, timeseries(205, "GPU UTILIZATION / POWER OVER TIME", [
        (f"100 * avg({G}utilization_gpu_ratio{SEL})", "gpu %", "percent", GREEN, False),
        (f"avg({G}power_draw_watts{SEL})", "power (W)", "watt", BLUE, True),
        (f"avg({G}temperature_gpu{SEL})", "temp (C)", "celsius", YELLOW, True),
    ], "GB10 utilization (left axis), power and temperature (right axis) from nvidia-smi.")),
}
for eid in new:
    if eid in spec["elements"]:
        sys.exit(f"{eid} already exists upstream - pick other ids for the local GPU row")

items = spec["layout"]["spec"]["items"]
for it in items:
    if it["spec"]["y"] >= GPU_Y:
        it["spec"]["y"] += GPU_H
for eid, (x, w, el) in new.items():
    spec["elements"][eid] = el
    tmpl = copy.deepcopy(items[0])
    tmpl["spec"].update({"x": x, "y": GPU_Y, "width": w, "height": GPU_H})
    tmpl["spec"]["element"]["name"] = eid
    items.append(tmpl)
print(f"GPU row -> {len(new)} panels at y={GPU_Y}", file=sys.stderr)

json.dump({
    "apiVersion": "dashboard.grafana.app/v2beta1",
    "kind": "Dashboard",
    "metadata": {"name": sys.argv[2]},
    "spec": spec,
}, sys.stdout)
PY

api="$GRAFANA_URL/apis/dashboard.grafana.app/v2beta1/namespaces/default/dashboards"

code=$(curl -s -u "$GRAFANA_AUTH" -X POST -H 'Content-Type: application/json' \
         --data-binary @"$payload" "$api" -o /dev/null -w '%{http_code}')

if [[ "$code" == "409" ]]; then
  echo "dashboard exists, updating..."
  rv=$(curl -s -u "$GRAFANA_AUTH" "$api/$DASH_NAME" \
       | python3 -c 'import sys,json; print(json.load(sys.stdin)["metadata"]["resourceVersion"])')
  python3 - "$payload" "$rv" > "$payload.up" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["metadata"]["resourceVersion"] = sys.argv[2]
json.dump(d, sys.stdout)
PY
  mv "$payload.up" "$payload"
  code=$(curl -s -u "$GRAFANA_AUTH" -X PUT -H 'Content-Type: application/json' \
           --data-binary @"$payload" "$api/$DASH_NAME" -o /dev/null -w '%{http_code}')
fi

case "$code" in
  200|201) echo "OK ($code) -> $GRAFANA_URL/d/$DASH_NAME" ;;
  *)       echo "FAILED with HTTP $code" >&2; exit 1 ;;
esac

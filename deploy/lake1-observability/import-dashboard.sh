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

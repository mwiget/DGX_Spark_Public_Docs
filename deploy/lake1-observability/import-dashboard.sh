#!/usr/bin/env bash
# Import the upstream schema-v2 dashboard into Grafana 13.
#
# The dashboard file is a bare v2 *spec* — it has no apiVersion/kind wrapper, so
# the legacy /api/dashboards/db endpoint rejects it. It has to go through the
# k8s-style resource API, wrapped in a Dashboard object. Re-running this updates
# the existing dashboard in place.
set -euo pipefail

GRAFANA_URL="${GRAFANA_URL:-http://localhost:3001}"
GRAFANA_AUTH="${GRAFANA_AUTH:-admin:admin}"
DASH_NAME="${DASH_NAME:-dgx-spark-vllm-v1}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DASH_FILE="${DASH_FILE:-$REPO_ROOT/Grafana_Dashboards/vllm_25.1/dgx_spark_vllm_grafana_v1.yaml}"

payload=$(mktemp); trap 'rm -f "$payload"' EXIT

python3 - "$DASH_FILE" "$DASH_NAME" > "$payload" <<'PY'
import json, sys
spec = json.load(open(sys.argv[1]))
json.dump({
    "apiVersion": "dashboard.grafana.app/v2beta1",
    "kind": "Dashboard",
    "metadata": {"name": sys.argv[2]},
    "spec": spec,
}, sys.stdout)
PY

api="$GRAFANA_URL/apis/dashboard.grafana.app/v2beta1/namespaces/default/dashboards"

# Create, or PUT over an existing one (needs the current resourceVersion).
code=$(curl -s -u "$GRAFANA_AUTH" -X POST -H 'Content-Type: application/json' \
         --data-binary @"$payload" "$api" -o /dev/null -w '%{http_code}')

if [[ "$code" == "409" ]]; then
  echo "dashboard exists, updating..."
  rv=$(curl -s -u "$GRAFANA_AUTH" "$api/$DASH_NAME" \
       | python3 -c 'import sys,json; print(json.load(sys.stdin)["metadata"]["resourceVersion"])')
  python3 - "$payload" "$rv" <<'PY' > "$payload.up"
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

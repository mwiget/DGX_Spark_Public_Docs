# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A documentation/configuration repository for the NVIDIA DGX Spark. There is no application code, no build system, and no test suite. The only artifact is a Grafana dashboard definition plus the README that explains how to deploy it.

```
Grafana_Dashboards/vllm_25.1/dgx_spark_vllm_grafana_v1.yaml   # the entire "codebase"
README.md                                                     # setup, PromQL notes, tuning workflow
```

The observability pipeline the dashboard assumes: vLLM `/metrics` (port 8006) + Node Exporter (port 9100) → Prometheus → Grafana 13.1.0.

## Working with the dashboard file

**The `.yaml` file is actually JSON.** It parses as JSON and must stay valid JSON — Grafana exported it that way. Validate after any edit:

```bash
python3 -c "import json; json.load(open('Grafana_Dashboards/vllm_25.1/dgx_spark_vllm_grafana_v1.yaml'))"
```

**It uses Grafana's schema v2 (`elements` + `layout`), not the legacy `panels[]` array.** This is the single most important structural fact:

- `elements` is a *map* keyed by `panel-<id>` (e.g. `"panel-13"`), each `{kind: "Panel", spec: {...}}`. Panel spec holds `data.spec.queries[]` (each a `PanelQuery` wrapping a `DataQuery` with `spec.expr`), `title`, `description`, and `vizConfig` (`group` is the panel type: `stat`, `timeseries`, `gauge`, `bargauge`).
- `layout` is a separate `GridLayout` with `items[]` of `GridLayoutItem`, each referencing an element by name and carrying `x/y/width/height`. **There is no `gridPos` inside the panel.**

Consequences when editing:

- Adding a panel requires **two** edits — a new `elements["panel-N"]` entry *and* a matching `GridLayoutItem` in `layout.spec.items`. A panel present in only one place silently does not render.
- Moving/resizing a panel is a `layout` edit only; changing a query is an `elements` edit only.
- Prefer editing via a Python script over hand-patching — the file is ~6300 lines and deeply nested.

**Datasource UID `dfr1d9ottv8xsc` is hardcoded ~72 times** as `datasource.name` inside each query. Any global replacement must hit all of them; the README instructs users to do exactly this search-and-replace on import.

## PromQL conventions used throughout

Follow these when adding or fixing panels — they exist to keep the dashboard working across vLLM releases and configurations:

- **Metric-name fallback chains with `or`**, ending in `vector(0)` so panels render "0" instead of "No data" when a feature is off:
  `avg(vllm:kv_cache_usage_perc) or avg(vllm:gpu_cache_usage_perc) or vector(0)`. Same pattern for `prefix_cache_hits` / `prefix_cache_hits_total` and `inter_token_latency_seconds_bucket` / `time_per_output_token_seconds_bucket`.
- **Ratios use `clamp_min(..., 1)`** in the denominator to avoid divide-by-zero spikes.
- **Range totals use `increase(...[$__range])`**; rates use `rate(...[$__rate_interval])`. Percentiles are `histogram_quantile(q, sum by (le) (rate(..._bucket[$__rate_interval])))`, with `by (le, model_name)` for the per-model comparison panels and `by (position)` for speculative-decode acceptance.
- **Cost constants are embedded directly in the expressions**, not variables (`variables` is empty). Cloud tiers are `prompt*5 + generation*25) / 1000000` (and the 3/15 and 1/5 tiers); local energy is `vector((($__range_s) / 3600) * 0.240 * 0.151)` — 240 W at $0.151/kWh. Changing pricing means editing these exprs and the matching panel `description`, plus the README table.

## Visual conventions

NVIDIA-inspired theme: `#76B900` green as primary, dark gray/white for structure, amber and red **reserved for operational warning states only** — do not use them as ordinary series colors. Series colors are set via `overrides` with `byName` matchers. Dashboard defaults to a 15-minute window at 5s auto-refresh.

## Repository conventions

- Dashboard versions are namespaced by vLLM release directory (`vllm_25.1/`); a new vLLM series gets a new directory rather than an in-place rewrite.
- The dashboard's top-level `description` field is maintained as a changelog of what the current revision changed — update it alongside meaningful panel changes, as commit `b212283` did.
- README claims about panels, metrics, and cost assumptions must stay in sync with the dashboard file; the README is the primary user-facing documentation and is deliberately detailed.

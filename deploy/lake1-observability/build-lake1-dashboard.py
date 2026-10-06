#!/usr/bin/env python3
"""Generate the lake1 dashboard (RTX PRO 5000 + llama.cpp + host).

Emits Grafana schema v1, which Grafana 13 migrates to v2 on import. That is
deliberate: v1 is far easier to generate correctly than hand-rolled v2
elements/layout, and the migration is lossless for these panel types.

    ./build-lake1-dashboard.py > ../../Grafana_Dashboards/lake1/lake1_llamacpp.json
    ./build-lake1-dashboard.py --host gx10 > grafana/provisioning/dashboards/json/gx10_llamacpp.json
    ./build-lake1-dashboard.py --host gx10 --engine vllm > grafana/provisioning/dashboards/json/gx10_vllm.json

Every query is filtered by the Prometheus `host` label: since gx10's GPU exporter
joined the same Prometheus (2026-10-02), an unfiltered avg(nvidia_smi_*) mixes the
two GPUs.
"""
import argparse
import json
import re

HOSTS = {
    "lake1": {"gpu": "RTX PRO 5000 Blackwell", "vram": True,
              "llama": "Qwen3.8-27B Q8 + MTP (../claude-local)",
              "title": "lake1 — RTX PRO 5000 + llama.cpp", "uid": "lake1-llamacpp",
              "description": "RTX PRO 5000 Blackwell, llama-server (../claude-local) and host "
                             "telemetry for lake1. Companion to the DGX Spark vLLM dashboard.",
              "tags": ["lake1", "llama.cpp", "nvidia", "rtx-pro-5000", "claude-local"]},
    # GB10: unified memory, so nvidia-smi reports no VRAM, no fan and no power limit.
    "gx10": {"gpu": "GB10", "vram": False,
             "llama": "Qwen3.8-Flash-Next IQ3_S, no MTP (~/llama-flashnext.log)",
             "title": "gx10 — GB10 + llama.cpp", "uid": "gx10-llamacpp",
             "description": "GB10, llama-server and host telemetry for gx10 when it runs "
                            "llama.cpp instead of vLLM. Companion to the DGX Spark vLLM dashboard.",
             "tags": ["gx10", "llama.cpp", "nvidia", "gb10"],
             # --engine vllm: Qwen3.6-35B-A3B via ~/git/qwen36-spark on gx10 (since 2026-10-06;
             # Kolibri-1 before that, same job and port label)
             "vllm": {"model": "Qwen3.6-35B-A3B NVFP4 + MTP, 262k ctx (qwen36.service)",
                      "title": "gx10 — GB10 + vLLM", "uid": "gx10-vllm",
                      "description": "GB10, vLLM (Qwen3.6-35B-A3B) and host telemetry for gx10. "
                                     "Replaces the upstream DGX Spark vLLM dashboard for day-to-day use.",
                      "tags": ["gx10", "vllm", "qwen", "nvidia", "gb10"]}},
}
ap = argparse.ArgumentParser()
ap.add_argument("--host", choices=sorted(HOSTS), default="lake1")
ap.add_argument("--engine", choices=["llamacpp", "vllm"], default="llamacpp")
ARGS = ap.parse_args()
H, ENGINE = ARGS.host, ARGS.engine
HC = HOSTS[H]
if ENGINE == "vllm":
    HC = {**HC, **HC["vllm"]}
METRIC = re.compile(r'\b((?:nvidia_smi_|node_)\w+|llamacpp:\w+|vllm:\w+)(\{[^}]*\})?')


def hostify(expr):
    """Pin every metric to this host; existing host="..." matchers are rewritten."""
    def sub(m):
        name, sel = m.group(1), m.group(2)
        if sel is None:
            return f'{name}{{host="{H}"}}'
        if "host=" in sel:
            return name + re.sub(r'host="[^"]*"', f'host="{H}"', sel)
        return f'{name}{{host="{H}",{sel[1:]}'
    return METRIC.sub(sub, expr)

DS = {"type": "prometheus", "uid": "dfr1d9ottv8xsc"}
GREEN, AMBER, RED, BLUE = "#76B900", "#FF9830", "#F2495C", "#5794F2"

panels, _id, y = [], [0], [0]


def nid():
    _id[0] += 1
    return _id[0]


def targets(exprs):
    return [{"datasource": DS, "expr": hostify(e), "legendFormat": l, "refId": chr(65 + i), "range": True}
            for i, (e, l) in enumerate(exprs)]


def stat(title, expr, unit, w=4, h=4, thresholds=None, desc=""):
    p = {"id": nid(), "type": "stat", "title": title, "description": desc,
         "datasource": DS, "gridPos": {"h": h, "w": w, "x": stat.x, "y": y[0]},
         "targets": targets([(expr, "")]),
         "options": {"colorMode": "value", "graphMode": "area", "justifyMode": "auto",
                     "textMode": "auto", "reduceOptions": {"calcs": ["lastNotNull"]}},
         "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "thresholds"},
                                      "thresholds": {"mode": "absolute", "steps": thresholds or
                                                     [{"color": GREEN, "value": None}]}},
                         "overrides": []}}
    stat.x += w
    panels.append(p)


def ts(title, exprs, unit, w=12, h=8, desc="", stack=False, maxv=None, colors=None):
    fc = {"unit": unit, "color": {"mode": "palette-classic"},
          "custom": {"lineWidth": 2, "fillOpacity": 12, "showPoints": "never",
                     "stacking": {"mode": "normal" if stack else "none"}}}
    if maxv is not None:
        fc["max"] = maxv
        fc["min"] = 0
    ov = []
    if colors:
        for name, col in colors.items():
            ov.append({"matcher": {"id": "byName", "options": name},
                       "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": col}}]})
    panels.append({"id": nid(), "type": "timeseries", "title": title, "description": desc,
                   "datasource": DS, "gridPos": {"h": h, "w": w, "x": ts.x, "y": y[0]},
                   "targets": targets(exprs),
                   "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                               "tooltip": {"mode": "multi", "sort": "desc"}},
                   "fieldConfig": {"defaults": fc, "overrides": ov}})
    ts.x += w
    if ts.x >= 24:
        ts.x = 0


def row(title):
    panels.append({"id": nid(), "type": "row", "title": title, "collapsed": False,
                   "gridPos": {"h": 1, "w": 24, "x": 0, "y": y[0]}, "panels": []})
    y[0] += 1
    stat.x = 0
    ts.x = 0


def q(p, metric):
    """Quantile of a vLLM histogram across all requests in the window."""
    return f"histogram_quantile({p}, sum by (le) (rate({metric}_bucket[$__rate_interval])))"


def vllm_row():
    V = "vllm:"
    row(f"vLLM — {HC['model']}")
    # vLLM records the per-request histograms (request_*) only when a request
    # FINISHES. A long reasoning request runs for minutes, so a stat built on them
    # over $__rate_interval reads ~0 while the GPU is flat out. Live speed comes from
    # inter_token_latency (observed every step) and the token counters instead.
    stat("PREFILL TOK/S (10m)",
         f"sum(rate({V}request_prefill_kv_computed_tokens_sum[10m])) / "
         f"clamp_min(sum(rate({V}request_prefill_time_seconds_sum[10m])), 0.001)", "short", w=3,
         desc="Newly computed (not prefix-cached) prompt tokens per second of prefill time, "
              "over requests that FINISHED in the last 10 min — vLLM records it at completion. "
              "Published for this checkpoint on GB10: ~6.3k tok/s at 8k input.")
    # Not 1 / mean inter-token latency: with speculative decoding vLLM observes one
    # "inter-token" interval per decode STEP, which emits several tokens (count 1070
    # vs 3370 tokens with MTP-3), so that reads ~3x low. Tokens over decode time is
    # right either way, and both counters update live.
    stat("DECODE TOK/S PER STREAM",
         f"sum(rate({V}generation_tokens_total[$__rate_interval])) / "
         f"clamp_min(sum(rate({V}inter_token_latency_seconds_sum[$__rate_interval])), 0.001)",
         "short", w=3,
         desc="Generated tokens per second of decode time, summed over streams: per-stream speed, "
              "live. Qwen3.6-35B-A3B NVFP4 + MTP on GB10: ~110-130 tok/s at 1 stream (Kolibri-1 was ~50).")
    stat("GENERATED TOK/S (ALL)", f"sum(rate({V}generation_tokens_total[$__rate_interval])) or vector(0)",
         "short", w=3,
         desc="Aggregate output tokens per second across all running requests (counter-based, live). "
              "Kolibri-1 reached ~128 tok/s with 4 concurrent reasoning streams.")
    stat("PREFIX CACHE HIT RATE",
         f"100 * ((sum({V}prefix_cache_hits_total) or vector(0)) / "
         f"clamp_min(sum({V}prefix_cache_queries_total) or vector(0), 1))", "percent",
         desc="Since server start. Agent loops resend the whole conversation each turn, "
              "so this should sit high; low means every turn pays a cold prefill.",
         thresholds=[{"color": RED, "value": None}, {"color": AMBER, "value": 40},
                     {"color": GREEN, "value": 70}])
    stat("KV CACHE USAGE", f"100 * (max({V}kv_cache_usage_perc) or vector(0))", "percent",
         desc="Share of the pinned KV pool in use (KV_CACHE_GIB=16, ~1.35M tokens = 5x 262k).",
         thresholds=[{"color": GREEN, "value": None}, {"color": AMBER, "value": 80},
                     {"color": RED, "value": 95}])
    stat("REQUESTS RUNNING", f"sum({V}num_requests_running) or vector(0)", "short", w=3,
         desc="MAX_NUM_SEQS=4.")
    stat("REQUESTS WAITING", f"sum({V}num_requests_waiting) or vector(0)", "short",
         desc="Non-zero means all sequence slots or the KV pool are full.",
         thresholds=[{"color": GREEN, "value": None}, {"color": AMBER, "value": 1}])
    y[0] += 4

    ts("THROUGHPUT (AGGREGATE)",
       [(f"sum(rate({V}generation_tokens_total[$__rate_interval]))", "generated tok/s"),
        (f"sum(rate({V}prompt_tokens_total[$__rate_interval])) - "
         f"(sum(rate({V}prompt_tokens_cached_total[$__rate_interval])) or vector(0))", "prompt computed tok/s"),
        (f"sum(rate({V}prompt_tokens_cached_total[$__rate_interval])) or vector(0)", "prompt cached tok/s")],
       "short", colors={"generated tok/s": GREEN, "prompt computed tok/s": AMBER,
                        "prompt cached tok/s": BLUE},
       desc="Wall-clock token rates across all streams. Cached prompt tokens are "
            "served from the prefix cache without recompute.")
    ts("LATENCY",
       [(q(0.5, f"{V}time_to_first_token_seconds"), "TTFT p50"),
        (q(0.95, f"{V}time_to_first_token_seconds"), "TTFT p95"),
        (q(0.5, f"{V}inter_token_latency_seconds"), "inter-token p50"),
        (q(0.95, f"{V}inter_token_latency_seconds"), "inter-token p95")],
       "s", colors={"TTFT p50": BLUE, "TTFT p95": RED},
       desc="TTFT is dominated by prefill: long cold prompts take minutes at 1M.")
    y[0] += 8

    ts("KV CACHE / PREEMPTIONS",
       [(f"100 * max({V}kv_cache_usage_perc)", "kv usage %"),
        (f"sum(rate({V}num_preemptions_total[$__rate_interval])) * 60 or vector(0)", "preemptions/min")],
       "short", colors={"kv usage %": GREEN, "preemptions/min": RED},
       desc="Preemptions mean the KV pool ran out and a request was evicted and recomputed.")
    ts("REQUESTS",
       [(f"sum({V}num_requests_running) or vector(0)", "running"),
        (f"sum({V}num_requests_waiting) or vector(0)", "waiting"),
        (f"sum(rate({V}request_success_total[$__rate_interval])) * 60 or vector(0)", "finished/min")],
       "short", colors={"running": GREEN, "waiting": RED, "finished/min": BLUE})
    y[0] += 8

    ts("PROMPT LENGTH (CONTEXT PER REQUEST)",
       [(q(0.5, f"{V}request_prompt_tokens"), "p50"),
        (q(0.95, f"{V}request_prompt_tokens"), "p95"),
        (q(1.0, f"{V}request_prompt_tokens"), "max")],
       "short", colors={"max": RED},
       desc="How much context the agents actually send. Histogram buckets, so values "
            "are bucket-bounded; max_model_len is 1,048,576.")
    ts("REQUEST PHASES (p50)",
       [(q(0.5, f"{V}request_queue_time_seconds"), "queue"),
        (q(0.5, f"{V}request_prefill_time_seconds"), "prefill"),
        (q(0.5, f"{V}request_decode_time_seconds"), "decode"),
        (q(0.5, f"{V}e2e_request_latency_seconds"), "end-to-end")],
       "s", colors={"queue": RED, "prefill": AMBER, "decode": GREEN, "end-to-end": BLUE},
       desc="Decode dominates with thinking on: the model reasons before most tool calls.")
    y[0] += 8

    ts("GENERATED TOKENS PER REQUEST",
       [(q(0.5, f"{V}request_generation_tokens"), "p50"),
        (q(0.95, f"{V}request_generation_tokens"), "p95")],
       "short", desc="Includes reasoning tokens.")
    ts("TOOL-CALL PARSER",
       [(f"sum by (outcome) (rate({V}tool_call_parser_invocations_total[$__rate_interval])) * 60",
         "{{outcome}}")],
       "short", desc="Tool-call parser invocations per minute (qwen3_xml parser).")
    y[0] += 8

    ts("MTP SPECULATIVE DECODING",
       [(f"100 * (sum(rate({V}spec_decode_num_accepted_tokens_total[$__rate_interval])) or vector(0)) / "
         f"clamp_min(sum(rate({V}spec_decode_num_draft_tokens_total[$__rate_interval])) or vector(0), 1)",
         "acceptance %"),
        (f"(sum(rate({V}spec_decode_num_accepted_tokens_total[$__rate_interval])) or vector(0)) / "
         f"clamp_min(sum(rate({V}spec_decode_num_drafts_total[$__rate_interval])) or vector(0), 1)",
         "accepted tokens per step")],
       "short", colors={"acceptance %": GREEN, "accepted tokens per step": BLUE},
       desc="Qwen3.6 ships an MTP head; vLLM drafts 3 tokens per step. ~72 % acceptance measured on "
            "code (2026-10-06). Reads 0 for models without speculative decoding (Kolibri-1).")
    y[0] += 8


stat.x = 0
ts.x = 0
G = 'nvidia_smi_'

row(f"GPU — {HC['gpu']} ({H})")
stat("GPU UTILIZATION", f"100 * (avg({G}utilization_gpu_ratio) or vector(0))", "percent",
     thresholds=[{"color": GREEN, "value": None}, {"color": AMBER, "value": 90}])
if HC["vram"]:
    stat("VRAM USED", f"avg({G}memory_used_bytes) or vector(0)", "bytes",
         desc="RTX PRO 5000 has 48 GB. llama-server with 2x131k slots measures ~46.3 GB.")
    stat("VRAM UTILIZATION",
         f"100 * (avg({G}memory_used_bytes) / clamp_min(avg({G}memory_total_bytes), 1) or vector(0))",
         "percent", thresholds=[{"color": GREEN, "value": None},
                                {"color": AMBER, "value": 90}, {"color": RED, "value": 97}])
else:
    stat("UNIFIED MEMORY USED",
         "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes", "bytes",
         desc="GB10 shares one memory pool between CPU and GPU; nvidia-smi reports no VRAM.")
    stat("UNIFIED MEMORY UTILIZATION",
         "100 * (1 - node_memory_MemAvailable_bytes / clamp_min(node_memory_MemTotal_bytes, 1))",
         "percent", thresholds=[{"color": GREEN, "value": None},
                                {"color": AMBER, "value": 90}, {"color": RED, "value": 97}])
stat("GPU TEMPERATURE", f"avg({G}temperature_gpu) or vector(0)", "celsius",
     thresholds=[{"color": GREEN, "value": None}, {"color": AMBER, "value": 80},
                 {"color": RED, "value": 88}])
stat("POWER DRAW", f"avg({G}power_draw_watts) or vector(0)", "watt",
     desc="Compare against nvidia_smi_power_limit_watts.")
stat("SM CLOCK", f"avg({G}clocks_current_sm_clock_hz) or vector(0)", "hertz")
y[0] += 4

ts("GPU / MEMORY-CONTROLLER UTILIZATION",
   [(f"100 * avg({G}utilization_gpu_ratio)", "gpu"),
    (f"100 * avg({G}utilization_memory_ratio)", "memory controller")],
   "percent", maxv=100, colors={"gpu": GREEN, "memory controller": BLUE},
   desc="Memory-controller utilization near 100% with GPU below it means "
        "bandwidth-bound decode — the expected shape for batch-1 LLM inference.")
if HC["vram"]:
    ts("VRAM USED vs TOTAL",
       [(f"avg({G}memory_used_bytes)", "used"), (f"avg({G}memory_total_bytes)", "total")],
       "bytes", colors={"used": GREEN, "total": "#808080"})
else:
    ts("UNIFIED MEMORY USED vs TOTAL",
       [("node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes", "used"),
        ("node_memory_MemTotal_bytes", "total")],
       "bytes", colors={"used": GREEN, "total": "#808080"})
y[0] += 8

ts("POWER DRAW vs LIMIT",
   [(f"avg({G}power_draw_watts)", "draw"), (f"avg({G}power_limit_watts)", "limit")],
   "watt", colors={"draw": GREEN, "limit": RED})
ts("TEMPERATURE / FAN",
   [(f"avg({G}temperature_gpu)", "gpu temp (C)"),
    (f"100 * avg({G}fan_speed_ratio)", "fan (%)")], "short",
   colors={"gpu temp (C)": AMBER, "fan (%)": BLUE})
y[0] += 8

ts("CLOCKS", [(f"avg({G}clocks_current_sm_clock_hz)", "sm"),
              (f"avg({G}clocks_current_memory_clock_hz)", "memory"),
              (f"avg({G}clocks_max_sm_clock_hz)", "sm max")], "hertz")
ts("THROTTLE REASONS",
   [(f"avg({G}clocks_event_reasons_sw_power_cap)", "sw power cap"),
    (f"avg({G}clocks_event_reasons_sw_thermal_slowdown)", "sw thermal"),
    (f"avg({G}clocks_event_reasons_hw_thermal_slowdown)", "hw thermal"),
    (f"avg({G}clocks_event_reasons_hw_power_brake_slowdown)", "hw power brake")],
   "short", maxv=1, colors={"sw power cap": AMBER, "sw thermal": AMBER, "hw thermal": RED, "hw power brake": RED},
   desc="1 = actively throttling. Sustained sw power cap means the card is at its limit.")
y[0] += 8

if ENGINE == "llamacpp":
    L = "llamacpp:"
    row(f"llama.cpp — {HC['llama']}")
    # Derived from counters, NOT the *_tokens_seconds gauges: those report only
    # during active generation and read 0 whenever the server is idle, which makes
    # them useless on a dashboard you look at after the fact.
    stat("PREFILL TOK/S",
         f"sum(rate({L}prompt_tokens_total[$__rate_interval])) / "
         f"clamp_min(sum(rate({L}prompt_seconds_total[$__rate_interval])), 0.001)", "short",
         desc="Prompt tokens per second of prefill time. ~1372 tok/s at 100k cold "
              "in ../claude-local; small prompts read much lower.")
    stat("DECODE TOK/S",
         f"sum(rate({L}tokens_predicted_total[$__rate_interval])) / "
         f"clamp_min(sum(rate({L}tokens_predicted_seconds_total[$__rate_interval])), 0.001)", "short",
         desc="Generated tokens per second of generation time. With MTP: "
              "35.5 -> 76.2 tok/s in ../claude-local.")
    # NB: llama.cpp exposes NO kv_cache_* metrics (unlike vLLM). Prefix-cache reuse
    # is the equivalent signal, and it is the one that matters here: ../claude-local
    # measures 0.31s warm vs 73s for a 100k cold prefill.
    stat("PREFIX CACHE HIT RATE",
         f"100 * ((sum({L}prompt_tokens_cached_total) or vector(0)) / "
         f"clamp_min((sum({L}prompt_tokens_cached_total) or vector(0)) + "
         f"(sum({L}prompt_tokens_total) or vector(0)), 1))", "percent",
         desc="Cached / (cached + processed). prompt_tokens_total EXCLUDES cached "
              "tokens, so the two sum to the total prompt volume.",
         thresholds=[{"color": RED, "value": None}, {"color": AMBER, "value": 40},
                     {"color": GREEN, "value": 70}])
    stat("MTP ACCEPTANCE",
         f"100 * ((sum({L}spec_decode_num_accepted_tokens_total) or vector(0)) / "
         f"clamp_min(sum({L}spec_decode_num_draft_tokens_total) or vector(0), 1))", "percent",
         desc="Draft tokens accepted by the target model. Compare with the DGX Spark "
              "dashboard: vLLM ngram managed 37.2%, vLLM MTP 69.4%.",
         thresholds=[{"color": RED, "value": None}, {"color": AMBER, "value": 40},
                     {"color": GREEN, "value": 60}])
    stat("REQUESTS PROCESSING", f"sum({L}requests_processing) or vector(0)", "short")
    stat("REQUESTS DEFERRED", f"sum({L}requests_deferred) or vector(0)", "short",
         desc="Non-zero means both slots are busy and requests are queueing.",
         thresholds=[{"color": GREEN, "value": None}, {"color": AMBER, "value": 1}])
    y[0] += 4

    ts("THROUGHPUT",
       [(f"sum(rate({L}tokens_predicted_total[$__rate_interval])) / "
         f"clamp_min(sum(rate({L}tokens_predicted_seconds_total[$__rate_interval])), 0.001)",
         "decode tok/s"),
        (f"sum(rate({L}prompt_tokens_total[$__rate_interval])) / "
         f"clamp_min(sum(rate({L}prompt_seconds_total[$__rate_interval])), 0.001)",
         "prefill tok/s")], "short",
       colors={"prefill tok/s": BLUE, "decode tok/s": GREEN},
       desc="Counter-derived. The llamacpp:*_tokens_seconds gauges report only "
            "during active generation and sit at 0 when idle.")
    ts("PREFIX CACHE: REUSED vs PROCESSED",
       [(f"sum(rate({L}prompt_tokens_cached_total[$__rate_interval])) or vector(0)", "reused/s"),
        (f"sum(rate({L}prompt_tokens_total[$__rate_interval])) or vector(0)", "processed/s")],
       "short", colors={"reused/s": GREEN, "processed/s": AMBER},
       desc="Reused high and processed near zero is a warm cache — the 0.31s turn.")
    y[0] += 8

    ts("MTP SPECULATIVE DECODING",
       [(f"100 * ((sum(rate({L}spec_decode_num_accepted_tokens_total[$__rate_interval])) or vector(0)) / "
         f"clamp_min(sum(rate({L}spec_decode_num_draft_tokens_total[$__rate_interval])) or vector(0), 1))",
         "acceptance %"),
        (f"(sum(rate({L}spec_decode_num_draft_tokens_total[$__rate_interval])) or vector(0)) / "
         f"clamp_min(sum(rate({L}spec_decode_num_drafts_total[$__rate_interval])) or vector(0), 1)",
         "draft tokens per step")],
       "short", colors={"acceptance %": GREEN, "draft tokens per step": BLUE},
       desc="serve-lake1.sh uses --spec-draft-n-max 2, so draft tokens per step "
            "tops out at 2. Acceptance is the lever: it took decode 35.5 -> 76.2 tok/s.")
    ts("SLOTS / QUEUE",
       [(f"sum({L}requests_processing) or vector(0)", "processing"),
        (f"sum({L}requests_deferred) or vector(0)", "deferred"),
        (f"avg({L}n_busy_slots_per_decode) or vector(0)", "busy slots per decode")],
       "short", colors={"processing": GREEN, "deferred": RED},
       desc="Two slots of 131k. deferred > 0 means both are busy.")
    y[0] += 8

else:
    vllm_row()

row(f"Host — {H}")
ts("CPU UTILIZATION",
   [('100 - (avg(rate(node_cpu_seconds_total{mode="idle",host="lake1"}[$__rate_interval])) * 100)',
     "cpu busy %")], "percent", maxv=100, colors={"cpu busy %": GREEN})
ts("MEMORY UTILIZATION",
   [('100 * (1 - node_memory_MemAvailable_bytes{host="lake1"} '
     '/ clamp_min(node_memory_MemTotal_bytes{host="lake1"}, 1))', "memory used %")],
   "percent", maxv=100, colors={"memory used %": GREEN})
y[0] += 8

ts("NETWORK RX / TX",
   [('sum(rate(node_network_receive_bytes_total{host="lake1",device!~"lo|veth.*|docker.*|br-.*"}[$__rate_interval]))', "rx"),
    ('sum(rate(node_network_transmit_bytes_total{host="lake1",device!~"lo|veth.*|docker.*|br-.*"}[$__rate_interval]))', "tx")],
   "Bps")
ts("DISK IOPS",
   [('sum(rate(node_disk_reads_completed_total{host="lake1"}[$__rate_interval]))', "reads/s"),
    ('sum(rate(node_disk_writes_completed_total{host="lake1"}[$__rate_interval]))', "writes/s")],
   "iops")
y[0] += 8

print(json.dumps({
    "title": HC["title"],
    "uid": HC["uid"],
    "description": HC["description"],
    "tags": HC["tags"],
    "timezone": "browser", "editable": True, "schemaVersion": 39,
    "refresh": "10s", "time": {"from": "now-15m", "to": "now"},
    "panels": panels, "templating": {"list": []}, "annotations": {"list": []},
}, indent=2))

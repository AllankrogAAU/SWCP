#!/usr/bin/env bash
# ==============================================================================
# show-pipeline-timings.sh
# Queries Loki and Tempo for pipeline event timings, queue delays, and deep trace waterfalls.
# Linux/Kubernetes replacement for scripts/Show-PipelineTimings.ps1
# ==============================================================================
set -euo pipefail

SUBMISSION_ID=""
TAIL=100

while [[ $# -gt 0 ]]; do
  case "$1" in
    --submission-id|-s)
      SUBMISSION_ID="$2"
      shift 2
      ;;
    --tail|-t)
      TAIL="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [SUBMISSION_ID] [--tail <N>]"
      echo ""
      echo "Options:"
      echo "  SUBMISSION_ID          Optional UUID of the submission to inspect."
      echo "                         If omitted, the most recent completed submission is used."
      echo "  --tail, -t <N>         Number of recent Loki log records to inspect (default: 100)."
      exit 0
      ;;
    *)
      if [ -z "$SUBMISSION_ID" ]; then
        SUBMISSION_ID="$1"
        shift
      else
        echo "Unknown argument: $1" >&2
        exit 1
      fi
      ;;
  esac
done

# Discover ClusterIPs for Loki and Tempo in the monitoring namespace
LOKI_IP="$(kubectl get svc loki -n monitoring -o jsonpath='{.spec.clusterIP}' 2>/dev/null || echo '127.0.0.1')"
TEMPO_IP="$(kubectl get svc tempo -n monitoring -o jsonpath='{.spec.clusterIP}' 2>/dev/null || echo '127.0.0.1')"
TAILSCALE_IP="$(tailscale ip -4 2>/dev/null || echo '127.0.0.1')"

python3 - <<EOF
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime

loki_ip = "${LOKI_IP}"
tempo_ip = "${TEMPO_IP}"
tailscale_ip = "${TAILSCALE_IP}"
submission_id = "${SUBMISSION_ID}".strip()
tail_count = int("${TAIL}")

def fetch_json(url):
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None

# 1. If submission_id not specified, find the latest completed submission
if not submission_id:
    loki_search_url = (
        f"http://{loki_ip}:3100/loki/api/v1/query_range?"
        + urllib.parse.urlencode({
            "query": '{namespace="swcp"} |= "pipeline.completed"',
            "limit": str(tail_count),
            "direction": "BACKWARD"
        })
    )
    res = fetch_json(loki_search_url)
    completed_entries = []
    if res and res.get("data", {}).get("result"):
        for stream in res["data"]["result"]:
            for entry in stream.get("values", []):
                ts = int(entry[0])
                line = entry[1]
                idx = line.find("{")
                if idx != -1:
                    try:
                        data = json.loads(line[idx:])
                        state = data.get("State") or data
                        sub = state.get("submission_id")
                        if sub:
                            completed_entries.append((ts, sub))
                    except Exception:
                        pass
        if completed_entries:
            completed_entries.sort(key=lambda x: x[0], reverse=True)
            submission_id = completed_entries[0][1]

if not submission_id:
    print("No recent submission events found in Loki. Run a submission first!")
    sys.exit(0)

# 2. Query Loki for all logs related to this submission
query = f'{{namespace="swcp"}} |= "{submission_id}"'
loki_url = (
    f"http://{loki_ip}:3100/loki/api/v1/query_range?"
    + urllib.parse.urlencode({"query": query, "limit": "200"})
)
loki_data = fetch_json(loki_url)

events = []
trace_id = None

if loki_data and loki_data.get("data", {}).get("result"):
    for stream in loki_data["data"]["result"]:
        for entry in stream.get("values", []):
            line = entry[1]
            idx = line.find("{")
            if idx != -1:
                try:
                    data = json.loads(line[idx:])
                    state = data.get("State") or data
                    if "event_name" in state and str(state.get("event_name", "")).startswith("pipeline."):
                        events.append(state)
                        if not trace_id and state.get("trace_id"):
                            trace_id = state.get("trace_id")
                except Exception:
                    pass

print("=" * 80)
print(f" SUBMISSION TIMINGS & WATERFALL: {submission_id}")
if trace_id:
    print(f" Trace ID:     {trace_id}")
    print(f" Grafana URL:  http://{tailscale_ip}:3000/d/swcp-pipeline-timings/swcp-pipeline-stress-test-and-timings")
print("=" * 80)

# 3. Print high-level stage timing table from Loki logs
print("\n[Pipeline Stages (from Loki Logs)]")
header = f"{'STAGE':<26} {'DURATION':<16} {'QUEUE WAIT':<14} {'STATUS':<14} {'OUTCOME'}"
print(header)
print("-" * len(header))

found_stages = set()
for e in events:
    evt = e.get("event_name", "")
    stage = e.get("stage") or "pipeline"
    key = (evt, stage)
    if key in found_stages:
        continue
    found_stages.add(key)

    dur = f"{e.get('duration_ms')} ms" if e.get("duration_ms") is not None else "-"
    wait = f"{e.get('queue_wait_ms')} ms" if e.get("queue_wait_ms") is not None else "-"
    status = e.get("status") or "-"
    outcome = e.get("outcome") or ""

    label = stage
    if evt == "pipeline.stage.started" and e.get("queue_wait_ms") is not None:
        label = f"{stage} (queue wait)"
    elif evt == "pipeline.completed":
        label = "TOTAL PIPELINE"

    print(f"{label:<26} {dur:<16} {wait:<14} {status:<14} {outcome[:35]}")

# 4. If trace_id is known, query Tempo for child spans
if trace_id:
    tempo_url = f"http://{tempo_ip}:3200/api/traces/{trace_id}"
    tempo_data = fetch_json(tempo_url)

    if tempo_data and tempo_data.get("batches"):
        spans = []
        for batch in tempo_data.get("batches", []):
            service = "unknown"
            for attr in batch.get("resource", {}).get("attributes", []):
                if attr.get("key") == "service.name":
                    service = attr.get("value", {}).get("stringValue", "unknown")
            for scope in batch.get("scopeSpans", []):
                for s in scope.get("spans", []):
                    start_ns = int(s.get("startTimeUnixNano", 0))
                    end_ns = int(s.get("endTimeUnixNano", 0))
                    dur_ms = round((end_ns - start_ns) / 1_000_000, 2)
                    attrs = {}
                    for a in s.get("attributes", []):
                        val_dict = a.get("value", {})
                        val = next(iter(val_dict.values()), None)
                        attrs[a.get("key")] = val

                    spans.append({
                        "service": service,
                        "name": s.get("name"),
                        "duration_ms": dur_ms,
                        "start_ns": start_ns,
                        "attrs": attrs
                    })

        spans.sort(key=lambda x: x["start_ns"])
        min_start = spans[0]["start_ns"] if spans else 0

        print("\n[Deep Waterfall & Sub-Span Breakdown (from Tempo)]")
        trace_hdr = f"{'OFFSET':<10} {'SERVICE':<18} {'SPAN NAME':<26} {'DURATION':<12} {'DETAILS'}"
        print(trace_hdr)
        print("-" * len(trace_hdr))

        for s in spans:
            offset_ms = round((s["start_ns"] - min_start) / 1_000_000, 1)
            offset_str = f"+{offset_ms:.1f}ms"
            dur_str = f"{s['duration_ms']:.1f} ms"
            details = []
            if "compile.duration_ms" in s["attrs"]:
                details.append(f"gcc compile: {s['attrs']['compile.duration_ms']}ms")
            if "run.duration_ms" in s["attrs"]:
                details.append(f"binary run: {s['attrs']['run.duration_ms']}ms (exit: {s['attrs'].get('run.exit_code')})")
            if "http.response.status_code" in s["attrs"]:
                details.append(f"http: {s['attrs']['http.response.status_code']}")
            if "outcome" in s["attrs"]:
                details.append(f"outcome: {str(s['attrs']['outcome'])[:30]}")

            detail_str = ", ".join(details)
            print(f"{offset_str:<10} {s['service']:<18} {s['name']:<26} {dur_str:<12} {detail_str}")

print("\n" + "=" * 80 + "\n")
EOF

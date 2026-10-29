#!/usr/bin/env bash
# ==============================================================================
# sweep-vllm.sh
# Automates vLLM Hardware Parameter Sweep across customizable
# ranges or lists of VLLM_GPU_MEMORY_UTILIZATION and VLLM_MAX_NUM_SEQS.
#
# Examples:
#   # 1. Print table & generate single chart from ALREADY completed sweep runs:
#   ./scripts/sweep-vllm.sh --report-only
#
#   # 2. Sweep ranges (start:end:step) for both variables and apply the winner:
#   ./scripts/sweep-vllm.sh --mem-range 0.80:0.90:0.05 --seqs-range 4:12:4 --apply-winner
#
#   # 3. Sweep explicit comma-separated lists:
#   ./scripts/sweep-vllm.sh --mem-values 0.80,0.88,0.92 --seqs-values 4,8,16
#
#   # 4. Sweep specific (mem:seqs) pairs only instead of full grid:
#   ./scripts/sweep-vllm.sh --pairs 0.80:4,0.88:8,0.90:12,0.92:16 --apply-winner
# ==============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Find workable kubeconfig
if [ -z "${KUBECONFIG:-}" ]; then
  if [ -f "$HOME/.kube/config" ]; then
    export KUBECONFIG="$HOME/.kube/config"
  elif [ -f "/etc/rancher/k3s/k3s.yaml" ]; then
    export KUBECONFIG="/etc/rancher/k3s/k3s.yaml"
  fi
fi

# Defaults
MEM_VALUES="0.88"
SEQS_VALUES="4,8,12,16"
MEM_RANGE=""
SEQS_RANGE=""
EXPLICIT_PAIRS=""
TARGET_URL="http://localhost"
RATE="16"
DURATION="120"
MIN_COMPLETIONS="16"
USERS="100"
HARDWARE_PREFIX="rtx5070ti"
DRAIN_TIMEOUT="90"
APPLY_WINNER=0
REPORT_ONLY=0
SWEEP_ID=""

usage() {
  cat <<EOF
Usage: ./scripts/sweep-vllm.sh [OPTIONS]

Variable Range / Value Options:
  --mem-range START:END:STEP     Range for VLLM_GPU_MEMORY_UTILIZATION (e.g. 0.80:0.92:0.04)
  --mem-values v1,v2,...         Comma-separated memory utilization values (default: 0.88)
  --seqs-range START:END:STEP    Range for VLLM_MAX_NUM_SEQS (e.g. 4:16:4)
  --seqs-values s1,s2,...        Comma-separated max_num_seqs values (default: 4,8,12,16)
  --pairs m1:s1,m2:s2,...        Test specific (mem:seqs) pairs instead of full Cartesian grid

Load & Execution Options:
  --rate RPM                     Submission arrival rate per minute per test (default: 16)
  --duration SECONDS             Duration in seconds per configuration (default: 120)
  --min-completions COUNT        Minimum completed submissions per configuration (default: 16)
  --drain-timeout SECONDS        Max seconds to wait for queues/DB to drain between runs (default: 90)
  --users COUNT                  Pre-seeded virtual user pool size (default: 100)
  --hardware TAG                 Hardware prefix tag (default: rtx5070ti)
  --target-url URL               Target ingress URL (default: http://localhost)
  --sweep-id ID                  Custom sweep ID folder name
  --apply-winner                 Automatically apply the winning config to k8s when finished
  --report-only                  Do not run new tests; print table & plot from existing SQLite data
  -h, --help                     Show this help message
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --mem-range)       MEM_RANGE="$2"; shift 2 ;;
    --mem-values)      MEM_VALUES="$2"; shift 2 ;;
    --seqs-range)      SEQS_RANGE="$2"; shift 2 ;;
    --seqs-values)     SEQS_VALUES="$2"; shift 2 ;;
    --pairs)           EXPLICIT_PAIRS="$2"; shift 2 ;;
    --rate)            RATE="$2"; shift 2 ;;
    --duration)        DURATION="$2"; shift 2 ;;
    --min-completions) MIN_COMPLETIONS="$2"; shift 2 ;;
    --drain-timeout)   DRAIN_TIMEOUT="$2"; shift 2 ;;
    --users)           USERS="$2"; shift 2 ;;
    --hardware)        HARDWARE_PREFIX="$2"; shift 2 ;;
    --target-url)      TARGET_URL="$2"; shift 2 ;;
    --sweep-id)        SWEEP_ID="$2"; shift 2 ;;
    --apply-winner)    APPLY_WINNER=1; shift 1 ;;
    --report-only)     REPORT_ONLY=1; shift 1 ;;
    -h|--help)         usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

mkdir -p "$ROOT/benchmarks/results"

echo "=== Building SWCP Benchmark Container (swcp-benchmarks:latest) ==="
docker build -t swcp-benchmarks:latest -f benchmarks/Dockerfile benchmarks/

DOCKER_TTY=""
if [ -t 0 ]; then
  DOCKER_TTY="-it"
fi

run_in_container() {
  docker run --rm $DOCKER_TTY \
    --network host \
    -v "$ROOT/benchmarks/results:/app/results" \
    swcp-benchmarks:latest "$@"
}

drain_pipeline() {
  local timeout="${1:-$DRAIN_TIMEOUT}"
  echo ">>> Checking pipeline drain status (active DB submissions and NATS queue depth)..."
  local start_time
  start_time=$(date +%s)
  while true; do
    local nats_pending=0
    local db_pending=0

    # 1. Check NATS pending & unacked messages across all consumers
    local nats_stats
    nats_stats=$(kubectl exec -n swcp nats-0 -- wget -qO- 'http://localhost:8222/jsz?consumers=1' 2>/dev/null || echo "{}")
    if command -v jq >/dev/null 2>&1; then
      nats_pending=$(echo "$nats_stats" | jq '[.. | .num_ack_pending?, .num_pending? // empty] | add // 0' 2>/dev/null || echo 0)
    fi

    # 2. Check Postgres submissions that are still in flight
    local db_res
    db_res=$(kubectl exec -n swcp postgres-0 -- psql -U swcp -d swcp -t -c "SELECT count(*) FROM \"Submissions\" WHERE \"Status\" NOT IN ('COMPLETED', 'FAILED');" 2>/dev/null || echo 0)
    db_pending=$(echo "$db_res" | tr -d '[:space:]')
    db_pending="${db_pending:-0}"

    if [ "$nats_pending" -eq 0 ] && [ "$db_pending" -eq 0 ]; then
      echo "✓ Pipeline is completely idle (NATS pending: 0, DB in-flight: 0)."
      break
    fi

    local now
    now=$(date +%s)
    local elapsed=$(( now - start_time ))
    if [ "$elapsed" -ge "$timeout" ]; then
      echo "Warning: Pipeline drain timed out after ${timeout}s (NATS pending: $nats_pending, DB in-flight: $db_pending). Proceeding..." >&2
      break
    fi

    echo "  [Drain] NATS in-flight/pending: $nats_pending | DB in-flight: $db_pending | Elapsed: ${elapsed}s / ${timeout}s"
    sleep 2
  done
}

if [ "$REPORT_ONLY" -eq 1 ]; then
  echo "=== Generating vLLM Sweep Table & Consolidated Plot from SQLite ==="
  if [ -n "$SWEEP_ID" ]; then
    run_in_container sweep-report --sweep-id "$SWEEP_ID"
  else
    run_in_container sweep-report
  fi
  exit 0
fi

# Build list of (MEM:SEQS) pairs to test
declare -a TEST_PAIRS=()

if [ -n "$EXPLICIT_PAIRS" ]; then
  IFS=',' read -ra PAIR_ARR <<< "$EXPLICIT_PAIRS"
  for p in "${PAIR_ARR[@]}"; do
    [ -n "$p" ] && TEST_PAIRS+=("$p")
  done
else
  # Expand MEM list
  declare -a MEM_LIST=()
  if [ -n "$MEM_RANGE" ]; then
    IFS=':' read -r M_START M_END M_STEP <<< "$MEM_RANGE"
    while IFS= read -r val; do
      [ -n "$val" ] && MEM_LIST+=("$val")
    done < <(LC_ALL=C seq "$M_START" "$M_STEP" "$M_END" | awk '{printf "%.2f\n", $1}')
  else
    IFS=',' read -ra MEM_LIST <<< "$MEM_VALUES"
  fi

  # Expand SEQS list
  declare -a SEQS_LIST=()
  if [ -n "$SEQS_RANGE" ]; then
    IFS=':' read -r S_START S_END S_STEP <<< "$SEQS_RANGE"
    while IFS= read -r val; do
      [ -n "$val" ] && SEQS_LIST+=("$val")
    done < <(LC_ALL=C seq "$S_START" "$S_STEP" "$S_END" | awk '{printf "%d\n", $1}')
  else
    IFS=',' read -ra SEQS_LIST <<< "$SEQS_VALUES"
  fi

  for m in "${MEM_LIST[@]}"; do
    for s in "${SEQS_LIST[@]}"; do
      TEST_PAIRS+=("${m}:${s}")
    done
  done
fi

if [ "${#TEST_PAIRS[@]}" -eq 0 ]; then
  echo "Error: No (MEM:SEQS) combinations generated." >&2
  exit 1
fi

if [ -z "$SWEEP_ID" ]; then
  SWEEP_ID="obj3_vllm_sweep_$(date -u +%Y%m%d_%H%M%S)"
fi

echo ""
echo "=================================================================="
echo " Starting vLLM Parameter Sweep ($SWEEP_ID)"
echo " Total configurations to test: ${#TEST_PAIRS[@]} -> ${TEST_PAIRS[*]}"
echo " Target rate: ${RATE}/min | Duration: ${DURATION}s | Min completions: ${MIN_COMPLETIONS}"
echo "=================================================================="

# Ensure pipeline is clean and virtual users are seeded once before starting loop
drain_pipeline
run_in_container seed --users "$USERS" --target-url "$TARGET_URL"

IDX=0
TOTAL="${#TEST_PAIRS[@]}"
for cfg in "${TEST_PAIRS[@]}"; do
  IDX=$((IDX + 1))
  MEM="${cfg%:*}"
  SEQS="${cfg#*:}"

  echo ""
  echo "------------------------------------------------------------------"
  echo " [$IDX/$TOTAL] Testing VLLM_GPU_MEMORY_UTILIZATION=$MEM, VLLM_MAX_NUM_SEQS=$SEQS"
  echo "------------------------------------------------------------------"

  # 1. Patch vllm-config and llm-worker-config
  kubectl patch configmap vllm-config -n swcp --type=merge \
    -p "{\"data\":{\"VLLM_GPU_MEMORY_UTILIZATION\":\"$MEM\",\"VLLM_MAX_NUM_SEQS\":\"$SEQS\"}}"
  kubectl patch configmap llm-worker-config -n swcp --type=merge \
    -p "{\"data\":{\"LocalLlm__MockEnabled\":\"false\",\"LocalLlm__MaxConcurrent\":\"$SEQS\",\"LocalLlm__MaxAckPending\":\"$SEQS\"}}"

  # 2. Restart vllm & llm-worker and wait until ready
  kubectl rollout restart deployment/vllm deployment/llm-worker -n swcp
  kubectl rollout status deployment/llm-worker -n swcp --timeout=120s
  if ! kubectl rollout status deployment/vllm -n swcp --timeout=300s; then
    echo "Warning: vLLM failed to start within 300s for MEM=$MEM, SEQS=$SEQS (likely OOM). Skipping..." >&2
    continue
  fi

  # Warmup: Execute 3 requests to eliminate cold-start CUDA graph and JIT compilation latency
  echo ">>> Warming up vLLM (3 requests) to eliminate CUDA graph/JIT compilation cold-start latency..."
  run_in_container warmup --count 3 --target-url "$TARGET_URL" || true
  drain_pipeline

  # 3. Record this point into SQLite (--skip-plots avoids creating per-point folders/images)
  run_in_container run \
    --suite obj3_vllm_sweep \
    --sweep-id "$SWEEP_ID" \
    --skip-plots \
    --vllm-gpu-mem "$MEM" \
    --vllm-max-seqs "$SEQS" \
    --hardware-tag "${HARDWARE_PREFIX}_mem${MEM}_seqs${SEQS}" \
    --arrival-mode stepped-poisson \
    --rates "$RATE" \
    --step-duration-s "$DURATION" \
    --min-completions "$MIN_COMPLETIONS" \
    --users "$USERS" \
    --target-url "$TARGET_URL" || {
      echo "Warning: Benchmark step failed for MEM=$MEM, SEQS=$SEQS; continuing sweep..." >&2
    }

  # 4. Drain any in-flight work before restarting deployments for the next step
  drain_pipeline
done

echo ""
echo "=================================================================="
echo " Generating Consolidated Sweep Report & Plot ($SWEEP_ID)"
echo "=================================================================="
run_in_container sweep-report --sweep-id "$SWEEP_ID"

WINNER_FILE="$ROOT/benchmarks/results/$SWEEP_ID/winner.env"
if [ "$APPLY_WINNER" -eq 1 ] && [ -f "$WINNER_FILE" ]; then
  # shellcheck disable=SC1090
  source "$WINNER_FILE"
  echo ""
  echo "=== Applying Winning Configuration (MEM=$BEST_MEM, SEQS=$BEST_SEQS) to Cluster ==="
  kubectl patch configmap vllm-config -n swcp --type=merge \
    -p "{\"data\":{\"VLLM_GPU_MEMORY_UTILIZATION\":\"$BEST_MEM\",\"VLLM_MAX_NUM_SEQS\":\"$BEST_SEQS\"}}"
  kubectl patch configmap llm-worker-config -n swcp --type=merge \
    -p "{\"data\":{\"LocalLlm__MockEnabled\":\"false\",\"LocalLlm__MaxConcurrent\":\"$BEST_SEQS\",\"LocalLlm__MaxAckPending\":\"$BEST_SEQS\"}}"
  kubectl rollout restart deployment/vllm deployment/llm-worker -n swcp
  kubectl rollout status deployment/llm-worker -n swcp --timeout=120s
  kubectl rollout status deployment/vllm -n swcp --timeout=300s
  echo "✓ Cluster is now running optimal settings: VLLM_GPU_MEMORY_UTILIZATION=$BEST_MEM, VLLM_MAX_NUM_SEQS=$BEST_SEQS"

  if [ -f "$ROOT/.env" ]; then
    sed -i "s/^VLLM_GPU_MEMORY_UTILIZATION=.*/VLLM_GPU_MEMORY_UTILIZATION=$BEST_MEM/" "$ROOT/.env"
    sed -i "s/^VLLM_MAX_NUM_SEQS=.*/VLLM_MAX_NUM_SEQS=$BEST_SEQS/" "$ROOT/.env"
    echo "✓ Updated .env: VLLM_GPU_MEMORY_UTILIZATION=$BEST_MEM, VLLM_MAX_NUM_SEQS=$BEST_SEQS"
  fi
fi


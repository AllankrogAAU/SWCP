#!/usr/bin/env bash
# ==============================================================================
# sweep-vllm.sh
# Automates single-node vLLM Hardware Parameter Sweep across customizable
# ranges or lists of VLLM_GPU_MEMORY_UTILIZATION and VLLM_MAX_NUM_SEQS, then
# optionally replicates the winning config across both cluster GPUs.
# Supports local control-plane or off-cluster execution via --ssh-host.
# ==============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck source=scripts/lib-benchmark-cluster.sh
source "$ROOT/scripts/lib-benchmark-cluster.sh"

_toml_val() {
  local section="$1" key="$2" fallback="$3"
  local toml_file="$ROOT/benchmarks/suites.toml"
  if [ -f "$toml_file" ] && command -v python3 >/dev/null 2>&1; then
    local val
    val=$(python3 -c '
import sys
try:
    import tomllib
except ImportError:
    sys.exit(0)
with open(sys.argv[1], "rb") as f:
    d = tomllib.load(f)
s = d
for part in sys.argv[2].split("."):
    s = s.get(part, {}) if isinstance(s, dict) else {}
v = s.get(sys.argv[3]) if isinstance(s, dict) else None
if v is not None:
    print(v)
' "$toml_file" "$section" "$key" 2>/dev/null || true)
    if [ -n "$val" ]; then
      echo "$val"
      return 0
    fi
  fi
  echo "$fallback"
}

# Defaults (sourced from benchmarks/suites.toml when present)
MEM_VALUES=$(_toml_val "suites.vllm_sweep" "mem_values" "0.88")
SEQS_VALUES=$(_toml_val "suites.vllm_sweep" "seqs_values" "4,8,12,16")
MEM_RANGE=""
SEQS_RANGE=""
EXPLICIT_PAIRS=""
SWCP_CONTROL_SSH="${SWCP_CONTROL_SSH:-}"
TARGET_URL="http://localhost"
TARGET_URL_SET=0
RATE="16"
RATE_SET=0
CONCURRENCY=$(_toml_val "suites.vllm_sweep" "concurrency" "16")
DURATION=$(_toml_val "suites.vllm_sweep" "step_duration_s" "120")
MIN_COMPLETIONS=$(_toml_val "suites.vllm_sweep" "min_completions" "16")
USERS=$(_toml_val "common" "users" "200")
HARDWARE_PREFIX="rtx5070ti"
DRAIN_TIMEOUT="90"
APPLY_WINNER=0
REPORT_ONLY=0
SWEEP_ID=""
OVERFLOW_SERVICE_URL="http://local-llm-overflow.swcp.svc.cluster.local:8080"

usage() {
  cat <<EOF
Usage: ./scripts/sweep-vllm.sh [OPTIONS]

Variable Range / Value Options:
  --mem-range START:END:STEP     Range for VLLM_GPU_MEMORY_UTILIZATION (e.g. 0.80:0.92:0.04)
  --mem-values v1,v2,...         Comma-separated memory utilization values (default: $MEM_VALUES)
  --seqs-range START:END:STEP    Range for VLLM_MAX_NUM_SEQS (e.g. 4:16:4)
  --seqs-values s1,s2,...        Comma-separated max_num_seqs values (default: $SEQS_VALUES)
  --pairs m1:s1,m2:s2,...        Test specific (mem:seqs) pairs instead of full Cartesian grid

Load & Execution Options:
  --rate RPM                     Use Poisson arrival rate/min instead of default closed-loop concurrency
  --concurrency COUNT            Closed-loop concurrency level (default from suites.toml: ${CONCURRENCY:-16})
  --duration SECONDS             Duration in seconds per configuration (default: $DURATION)
  --min-completions COUNT        Minimum completed submissions per configuration (default: $MIN_COMPLETIONS)
  --drain-timeout SECONDS        Max seconds to wait for queues/DB to drain between runs (default: 90)
  --users COUNT                  Pre-seeded virtual user pool size (default: $USERS)
  --hardware TAG                 Hardware prefix tag (default: rtx5070ti)
  --target-url URL               Target ingress URL (default: http://localhost, or http://<ssh-host>)
  --ssh-host USER@HOST           Run kubectl & monitoring tunnels over SSH (off-cluster runner mode)
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
    --rate)            RATE="$2"; RATE_SET=1; CONCURRENCY=""; shift 2 ;;
    --concurrency)     CONCURRENCY="$2"; RATE_SET=0; shift 2 ;;
    --duration)        DURATION="$2"; shift 2 ;;
    --min-completions) MIN_COMPLETIONS="$2"; shift 2 ;;
    --drain-timeout)   DRAIN_TIMEOUT="$2"; shift 2 ;;
    --users)           USERS="$2"; shift 2 ;;
    --hardware)        HARDWARE_PREFIX="$2"; shift 2 ;;
    --target-url)      TARGET_URL="$2"; TARGET_URL_SET=1; shift 2 ;;
    --target-url=*)    TARGET_URL="${1#*=}"; TARGET_URL_SET=1; shift 1 ;;
    --ssh-host)        SWCP_CONTROL_SSH="$2"; shift 2 ;;
    --ssh-host=*)      SWCP_CONTROL_SSH="${1#*=}"; shift 1 ;;
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

export SWCP_CONTROL_SSH

if [ -n "$SWCP_CONTROL_SSH" ] && [ "$TARGET_URL_SET" -eq 0 ]; then
  TARGET_URL="http://${SWCP_CONTROL_SSH#*@}"
fi

mkdir -p "$ROOT/benchmarks/results"

NEED_RESTORE_CONFIG=0
WINNER_APPLIED=0

restore_two_node_config() {
  if [ "$NEED_RESTORE_CONFIG" -eq 0 ] || [ "$WINNER_APPLIED" -eq 1 ]; then
    return 0
  fi
  echo ""
  echo "=== Restoring 2-Node Configuration (MEM=$ORIG_MEM, SEQS=$ORIG_SEQS, OverflowThreshold=$ORIG_THRESHOLD, MaxAckPending=$ORIG_MAX_ACK) ==="
  run_kubectl patch configmap vllm-config -n swcp --type=merge \
    -p "{\"data\":{\"VLLM_GPU_MEMORY_UTILIZATION\":\"$ORIG_MEM\",\"VLLM_MAX_NUM_SEQS\":\"$ORIG_SEQS\"}}" || true
  run_kubectl patch configmap llm-worker-config -n swcp --type=merge \
    -p "{\"data\":{\"LocalLlm__MockEnabled\":\"false\",\"LocalLlm__OverflowBaseUrl\":\"$OVERFLOW_SERVICE_URL\",\"LocalLlm__PrimaryMaxConcurrent\":\"$ORIG_THRESHOLD\",\"LocalLlm__MaxConcurrent\":\"$ORIG_SEQS\",\"LocalLlm__MaxAckPending\":\"$ORIG_MAX_ACK\"}}" || true
  if [ -n "${ORIG_SBX_READY_TIMEOUT:-}" ]; then
    run_kubectl patch configmap c-sandbox-config -n swcp --type=merge \
      -p "{\"data\":{\"SANDBOX_READY_TIMEOUT_S\":\"$ORIG_SBX_READY_TIMEOUT\"}}" || true
  else
    run_kubectl patch configmap c-sandbox-config -n swcp --type=json \
      -p '[{"op":"remove","path":"/data/SANDBOX_READY_TIMEOUT_S"}]' >/dev/null 2>&1 || true
  fi
  run_kubectl rollout restart deployment/vllm deployment/vllm-overflow deployment/llm-worker deployment/c-sandbox -n swcp || true
  run_kubectl rollout status deployment/llm-worker -n swcp --timeout=120s || true
  run_kubectl rollout status deployment/c-sandbox -n swcp --timeout=120s || true
  run_kubectl rollout status deployment/vllm -n swcp --timeout=300s || true
  run_kubectl rollout status deployment/vllm-overflow -n swcp --timeout=300s || true
}

on_exit() {
  restore_two_node_config
  cleanup_monitoring_tunnel
}
trap on_exit EXIT INT TERM

init_local_kubeconfig
setup_monitoring_endpoints
measure_inter_node_rtt

echo "=== Building SWCP Benchmark Container (swcp-benchmarks:latest) ==="
docker build -t swcp-benchmarks:latest -f benchmarks/Dockerfile benchmarks/

DOCKER_TTY=""
if [ -t 0 ]; then
  DOCKER_TTY="-it"
fi

run_in_container() {
  docker run --rm $DOCKER_TTY \
    --network host \
    ${PROMETHEUS_URL:+-e PROMETHEUS_URL="$PROMETHEUS_URL"} \
    ${LOKI_URL:+-e LOKI_URL="$LOKI_URL"} \
    ${INTER_NODE_RTT_MS:+-e INTER_NODE_RTT_MS="$INTER_NODE_RTT_MS"} \
    -v "$ROOT/benchmarks/results:/app/results" \
    swcp-benchmarks:latest "$@"
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
  declare -a MEM_LIST=()
  if [ -n "$MEM_RANGE" ]; then
    IFS=':' read -r M_START M_END M_STEP <<< "$MEM_RANGE"
    while IFS= read -r val; do
      [ -n "$val" ] && MEM_LIST+=("$val")
    done < <(LC_ALL=C seq "$M_START" "$M_STEP" "$M_END" | awk '{printf "%.2f\n", $1}')
  else
    IFS=',' read -ra MEM_LIST <<< "$MEM_VALUES"
  fi

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
echo " Starting Single-Node vLLM Parameter Sweep ($SWEEP_ID)"
echo " Target: $TARGET_URL${SWCP_CONTROL_SSH:+ | Cluster SSH: $SWCP_CONTROL_SSH}"
echo " Total configurations to test: ${#TEST_PAIRS[@]} -> ${TEST_PAIRS[*]}"
if [ -n "$CONCURRENCY" ]; then
  echo " Closed-loop concurrency: ${CONCURRENCY} | Duration: ${DURATION}s (overflow isolated/disabled)"
else
  echo " Target rate: ${RATE}/min | Duration: ${DURATION}s | Min completions: ${MIN_COMPLETIONS} (overflow isolated/disabled)"
fi
echo "=================================================================="

ORIG_MEM=$(read_env_var "VLLM_GPU_MEMORY_UTILIZATION" "0.80")
ORIG_SEQS=$(read_env_var "VLLM_MAX_NUM_SEQS" "4")
ORIG_THRESHOLD=$(read_env_var "VLLM_OVERFLOW_THRESHOLD" "$ORIG_SEQS")
ORIG_MAX_ACK=$(read_env_var "LLM_LOCAL_MAX_ACK_PENDING" "$((ORIG_SEQS * 2))")
ORIG_SBX_READY_TIMEOUT=$(run_kubectl get configmap c-sandbox-config -n swcp -o jsonpath='{.data.SANDBOX_READY_TIMEOUT_S}' 2>/dev/null || true)
NEED_RESTORE_CONFIG=1

# Raise sandbox pod startup timeout during high-concurrency sweep so burst cold-starts
# under gVisor complete cleanly instead of triggering 30s SandboxStartError retry storms.
run_kubectl patch configmap c-sandbox-config -n swcp --type=merge \
  -p '{"data":{"SANDBOX_READY_TIMEOUT_S":"60"}}'
run_kubectl rollout restart deployment/c-sandbox -n swcp
run_kubectl rollout status deployment/c-sandbox -n swcp --timeout=120s

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
  echo " [$IDX/$TOTAL] Testing Single-Node VLLM_GPU_MEMORY_UTILIZATION=$MEM, VLLM_MAX_NUM_SEQS=$SEQS"
  echo "------------------------------------------------------------------"

  # 1. Patch vllm-config and isolate llm-worker to primary vLLM only (no overflow to worker-node)
  run_kubectl patch configmap vllm-config -n swcp --type=merge \
    -p "{\"data\":{\"VLLM_GPU_MEMORY_UTILIZATION\":\"$MEM\",\"VLLM_MAX_NUM_SEQS\":\"$SEQS\"}}"
  run_kubectl patch configmap llm-worker-config -n swcp --type=merge \
    -p "{\"data\":{\"LocalLlm__MockEnabled\":\"false\",\"LocalLlm__OverflowBaseUrl\":\"\",\"LocalLlm__PrimaryMaxConcurrent\":\"$SEQS\",\"LocalLlm__MaxConcurrent\":\"$SEQS\",\"LocalLlm__MaxAckPending\":\"$SEQS\"}}"

  # 2. Restart primary vllm & llm-worker (leave vllm-overflow untouched during single-node sweep)
  run_kubectl rollout restart deployment/vllm deployment/llm-worker -n swcp
  run_kubectl rollout status deployment/llm-worker -n swcp --timeout=120s
  if ! run_kubectl rollout status deployment/vllm -n swcp --timeout=300s; then
    echo "Warning: vLLM failed to start within 300s for MEM=$MEM, SEQS=$SEQS (likely OOM). Skipping..." >&2
    continue
  fi

  # Warmup: Execute 3 requests to eliminate cold-start CUDA graph and JIT compilation latency,
  # then wait briefly after drain so c-sandbox warm pools finish refilling before full load.
  echo ">>> Warming up primary vLLM (3 requests) and allowing c-sandbox warm pool to refill..."
  run_in_container warmup --count 3 --target-url "$TARGET_URL" || true
  drain_pipeline
  sleep 12

  # 3. Record this point into SQLite (--skip-plots avoids creating per-point folders/images)
  if [ -n "$CONCURRENCY" ]; then
    ARRIVAL_ARGS=(--arrival-mode closed-loop --concurrency "$CONCURRENCY")
  else
    ARRIVAL_ARGS=(--arrival-mode stepped-poisson --rates "$RATE" --min-completions "$MIN_COMPLETIONS")
  fi

  run_in_container run \
    --suite obj3_vllm_sweep \
    --sweep-id "$SWEEP_ID" \
    --skip-plots \
    --vllm-gpu-mem "$MEM" \
    --vllm-max-seqs "$SEQS" \
    --hardware-tag "${HARDWARE_PREFIX}_mem${MEM}_seqs${SEQS}" \
    "${ARRIVAL_ARGS[@]}" \
    --step-duration-s "$DURATION" \
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
  BEST_THRESHOLD="$BEST_SEQS"
  BEST_MAX_ACK="$((BEST_SEQS * 2))"
  echo ""
  echo "=== Replicating Winning Config (MEM=$BEST_MEM, SEQS=$BEST_SEQS) to Both Primary & Overflow vLLM ==="
  run_kubectl patch configmap vllm-config -n swcp --type=merge \
    -p "{\"data\":{\"VLLM_GPU_MEMORY_UTILIZATION\":\"$BEST_MEM\",\"VLLM_MAX_NUM_SEQS\":\"$BEST_SEQS\"}}"
  run_kubectl patch configmap llm-worker-config -n swcp --type=merge \
    -p "{\"data\":{\"LocalLlm__MockEnabled\":\"false\",\"LocalLlm__OverflowBaseUrl\":\"$OVERFLOW_SERVICE_URL\",\"LocalLlm__PrimaryMaxConcurrent\":\"$BEST_THRESHOLD\",\"LocalLlm__MaxConcurrent\":\"$BEST_SEQS\",\"LocalLlm__MaxAckPending\":\"$BEST_MAX_ACK\"}}"
  if [ -n "${ORIG_SBX_READY_TIMEOUT:-}" ]; then
    run_kubectl patch configmap c-sandbox-config -n swcp --type=merge \
      -p "{\"data\":{\"SANDBOX_READY_TIMEOUT_S\":\"$ORIG_SBX_READY_TIMEOUT\"}}" || true
  else
    run_kubectl patch configmap c-sandbox-config -n swcp --type=json \
      -p '[{"op":"remove","path":"/data/SANDBOX_READY_TIMEOUT_S"}]' >/dev/null 2>&1 || true
  fi
  run_kubectl rollout restart deployment/vllm deployment/vllm-overflow deployment/llm-worker deployment/c-sandbox -n swcp
  run_kubectl rollout status deployment/llm-worker -n swcp --timeout=120s
  run_kubectl rollout status deployment/c-sandbox -n swcp --timeout=120s
  run_kubectl rollout status deployment/vllm -n swcp --timeout=300s
  run_kubectl rollout status deployment/vllm-overflow -n swcp --timeout=300s
  WINNER_APPLIED=1
  echo "✓ Both GPUs now running optimal settings: MEM=$BEST_MEM, SEQS=$BEST_SEQS, OverflowThreshold=$BEST_THRESHOLD, MaxAckPending=$BEST_MAX_ACK"

  upsert_env_var "VLLM_GPU_MEMORY_UTILIZATION" "$BEST_MEM"
  upsert_env_var "VLLM_MAX_NUM_SEQS" "$BEST_SEQS"
  upsert_env_var "VLLM_OVERFLOW_THRESHOLD" "$BEST_THRESHOLD"
  upsert_env_var "LLM_LOCAL_MAX_ACK_PENDING" "$BEST_MAX_ACK"
  echo "✓ Updated .env: MEM=$BEST_MEM, SEQS=$BEST_SEQS, VLLM_OVERFLOW_THRESHOLD=$BEST_THRESHOLD, LLM_LOCAL_MAX_ACK_PENDING=$BEST_MAX_ACK"
fi

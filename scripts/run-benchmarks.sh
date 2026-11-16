#!/usr/bin/env bash
# ==============================================================================
# run-benchmarks.sh
# Builds and runs the containerized SWCP benchmark suite on host network.
# Supports both on-cluster execution and off-cluster remote execution via
# --ssh-host <user@control-node> (out-of-band kubectl + monitoring tunnels).
# ==============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck source=scripts/lib-benchmark-cluster.sh
source "$ROOT/scripts/lib-benchmark-cluster.sh"

mkdir -p "$ROOT/benchmarks/results"

# Parse script-level options while preserving python CLI args
IS_MOCK_SUITE=0
IS_RUN_CMD=0
HAS_HELP=0
SKIP_WORKER_COOLDOWN="${SKIP_WORKER_COOLDOWN:-0}"
SWCP_CONTROL_SSH="${SWCP_CONTROL_SSH:-}"
TARGET_URL="http://localhost"
TARGET_URL_SET=0
CONTAINER_ARGS=()

i=1
argc=$#
args=("$@")
idx=0
while [ "$idx" -lt "$argc" ]; do
  arg="${args[$idx]}"
  case "$arg" in
    --skip-worker-cooldown)
      SKIP_WORKER_COOLDOWN=1
      idx=$((idx + 1))
      continue
      ;;
    --ssh-host)
      if [ $((idx + 1)) -ge "$argc" ]; then
        echo "Error: --ssh-host requires an argument (e.g. user@control-node)" >&2
        exit 1
      fi
      SWCP_CONTROL_SSH="${args[$((idx + 1))]}"
      idx=$((idx + 2))
      continue
      ;;
    --ssh-host=*)
      SWCP_CONTROL_SSH="${arg#--ssh-host=}"
      idx=$((idx + 1))
      continue
      ;;
    --target-url)
      CONTAINER_ARGS+=("$arg")
      if [ $((idx + 1)) -lt "$argc" ]; then
        TARGET_URL="${args[$((idx + 1))]}"
        TARGET_URL_SET=1
        CONTAINER_ARGS+=("$TARGET_URL")
        idx=$((idx + 2))
        continue
      fi
      ;;
    --target-url=*)
      TARGET_URL="${arg#--target-url=}"
      TARGET_URL_SET=1
      ;;
    -h|--help)
      HAS_HELP=1
      ;;
    run)
      IS_RUN_CMD=1
      ;;
    obj2_infinite_gpu|infinite_gpu|--mock-llm)
      IS_MOCK_SUITE=1
      ;;
  esac
  CONTAINER_ARGS+=("$arg")
  idx=$((idx + 1))
done
unset i argc args idx

export SWCP_CONTROL_SSH

if [ -n "$SWCP_CONTROL_SSH" ] && [ "$TARGET_URL_SET" -eq 0 ]; then
  SSH_HOST_PART="${SWCP_CONTROL_SSH#*@}"
  TARGET_URL="http://${SSH_HOST_PART}"
  if [ "$HAS_HELP" -eq 0 ]; then
    CONTAINER_ARGS+=(--target-url "$TARGET_URL")
  fi
fi

DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-90}"
WORKER_COOLDOWN_TIMEOUT="${WORKER_COOLDOWN_TIMEOUT:-360}"
MOCK_ACTIVE=0

cleanup_mock() {
  if [ "$MOCK_ACTIVE" -eq 1 ]; then
    echo ""
    echo "=== Restoring 2-Node Local LLM Mode (MockEnabled=false, MaxConcurrent=$RESTORE_SEQS, OverflowThreshold=$RESTORE_THRESHOLD, MaxAckPending=$RESTORE_MAX_ACK) ==="
    run_kubectl patch configmap llm-worker-config -n swcp --type=merge \
      -p "{\"data\":{\"LocalLlm__MockEnabled\":\"false\",\"LocalLlm__OverflowBaseUrl\":\"$RESTORE_OVERFLOW_URL\",\"LocalLlm__PrimaryMaxConcurrent\":\"$RESTORE_THRESHOLD\",\"LocalLlm__MaxConcurrent\":\"$RESTORE_SEQS\",\"LocalLlm__MaxAckPending\":\"$RESTORE_MAX_ACK\"}}" || true
    run_kubectl rollout restart deployment/llm-worker -n swcp || true
    run_kubectl rollout status deployment/llm-worker -n swcp --timeout=120s || true
    echo "✓ Restored local LLM mode on cluster."

    echo "=== Running Warmup Submissions (3 requests) after rollout ==="
    docker run --rm $DOCKER_TTY \
      --network host \
      -v "$ROOT/benchmarks/results:/app/results" \
      swcp-benchmarks:latest warmup --count 3 --target-url "$TARGET_URL" || true
    drain_pipeline
  fi
}

on_exit() {
  cleanup_mock
  cleanup_monitoring_tunnel
}
trap on_exit EXIT INT TERM

init_local_kubeconfig
setup_monitoring_endpoints
measure_inter_node_rtt

RESTORE_SEQS=$(read_env_var "VLLM_MAX_NUM_SEQS" "4")
RESTORE_THRESHOLD=$(read_env_var "VLLM_OVERFLOW_THRESHOLD" "$RESTORE_SEQS")
RESTORE_MAX_ACK=$(read_env_var "LLM_LOCAL_MAX_ACK_PENDING" "$((RESTORE_SEQS * 2))")
RESTORE_OVERFLOW_URL="http://local-llm-overflow.swcp.svc.cluster.local:8080"

echo "=== Building SWCP Benchmark Container (swcp-benchmarks:latest) ==="
docker build -t swcp-benchmarks:latest -f benchmarks/Dockerfile benchmarks/

DOCKER_TTY=""
if [ -t 0 ]; then
  DOCKER_TTY="-it"
fi

if [ "$IS_MOCK_SUITE" -eq 1 ]; then
  MOCK_ACTIVE=1
  echo ""
  echo "=== Enabling Infinite GPU (Mock LLM) Mode on Cluster ==="
  run_kubectl patch configmap llm-worker-config -n swcp --type=merge \
    -p '{"data":{"LocalLlm__MockEnabled":"true","LocalLlm__MockLatencyMs":"5","LocalLlm__MaxConcurrent":"64","LocalLlm__MaxAckPending":"128"}}'
  run_kubectl rollout restart deployment/llm-worker -n swcp
  run_kubectl rollout status deployment/llm-worker -n swcp --timeout=120s
  echo "✓ Infinite GPU (Mock LLM) mode active."

  echo "=== Running Warmup Submissions (3 requests) after rollout ==="
  docker run --rm $DOCKER_TTY \
    --network host \
    -v "$ROOT/benchmarks/results:/app/results" \
    swcp-benchmarks:latest warmup --count 3 --target-url "$TARGET_URL" || true
fi

# Drain prior work and ensure worker node starts un-warmed before running a benchmark suite
drain_pipeline
if [ "$IS_RUN_CMD" -eq 1 ] && [ "$SKIP_WORKER_COOLDOWN" -eq 0 ]; then
  wait_for_worker_baseline
fi

echo "=== Running SWCP Benchmarks (target=$TARGET_URL${SWCP_CONTROL_SSH:+, ssh=$SWCP_CONTROL_SSH}) ==="
docker run --rm $DOCKER_TTY \
  --network host \
  ${PROMETHEUS_URL:+-e PROMETHEUS_URL="$PROMETHEUS_URL"} \
  ${LOKI_URL:+-e LOKI_URL="$LOKI_URL"} \
  ${INTER_NODE_RTT_MS:+-e INTER_NODE_RTT_MS="$INTER_NODE_RTT_MS"} \
  -v "$ROOT/benchmarks/results:/app/results" \
  swcp-benchmarks:latest "${CONTAINER_ARGS[@]}"

# Drain work after test
drain_pipeline

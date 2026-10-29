#!/usr/bin/env bash
# ==============================================================================
# run-benchmarks.sh
# Builds and runs the containerized SWCP benchmark suite on host network.
# Handles cluster pipeline draining and automated mock-LLM switching for Obj 2.
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

mkdir -p "$ROOT/benchmarks/results"

DRAIN_TIMEOUT="90"
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

# Check if Objective 2 or mock LLM is requested
IS_MOCK_SUITE=0
TARGET_URL="http://localhost"
skip_next=0
for arg in "$@"; do
  if [ "$skip_next" -eq 1 ]; then
    TARGET_URL="$arg"
    skip_next=0
    continue
  fi
  if [ "$arg" = "--target-url" ]; then
    skip_next=1
  elif [[ "$arg" =~ ^--target-url=(.*)$ ]]; then
    TARGET_URL="${BASH_REMATCH[1]}"
  fi
  if [ "$arg" = "obj2_infinite_gpu" ] || [ "$arg" = "infinite_gpu" ] || [ "$arg" = "--mock-llm" ]; then
    IS_MOCK_SUITE=1
  fi
done

RESTORE_SEQS=$(grep "^VLLM_MAX_NUM_SEQS=" "$ROOT/.env" 2>/dev/null | cut -d'=' -f2 | tr -d '[:space:]' || echo "16")
RESTORE_SEQS="${RESTORE_SEQS:-16}"

cleanup_mock() {
  if [ "$IS_MOCK_SUITE" -eq 1 ]; then
    echo ""
    echo "=== Restoring Local LLM Mode on Cluster (MockEnabled=false, MaxConcurrent=$RESTORE_SEQS) ==="
    kubectl patch configmap llm-worker-config -n swcp --type=merge \
      -p "{\"data\":{\"LocalLlm__MockEnabled\":\"false\",\"LocalLlm__MaxConcurrent\":\"$RESTORE_SEQS\",\"LocalLlm__MaxAckPending\":\"$RESTORE_SEQS\"}}" || true
    kubectl rollout restart deployment/llm-worker -n swcp || true
    kubectl rollout status deployment/llm-worker -n swcp --timeout=120s || true
    echo "✓ Restored local LLM mode on cluster."

    # Warmup after restoring real LLM
    echo "=== Running Warmup Submissions (3 requests) after rollout ==="
    docker run --rm $DOCKER_TTY \
      --network host \
      -v "$ROOT/benchmarks/results:/app/results" \
      swcp-benchmarks:latest warmup --count 3 --target-url "$TARGET_URL" || true
    drain_pipeline
  fi
}

echo "=== Building SWCP Benchmark Container (swcp-benchmarks:latest) ==="
docker build -t swcp-benchmarks:latest -f benchmarks/Dockerfile benchmarks/

DOCKER_TTY=""
if [ -t 0 ]; then
  DOCKER_TTY="-it"
fi

if [ "$IS_MOCK_SUITE" -eq 1 ]; then
  trap cleanup_mock EXIT INT TERM
  echo ""
  echo "=== Enabling Infinite GPU (Mock LLM) Mode on Cluster ==="
  kubectl patch configmap llm-worker-config -n swcp --type=merge \
    -p '{"data":{"LocalLlm__MockEnabled":"true","LocalLlm__MockLatencyMs":"5","LocalLlm__MaxConcurrent":"64","LocalLlm__MaxAckPending":"128"}}'
  kubectl rollout restart deployment/llm-worker -n swcp
  kubectl rollout status deployment/llm-worker -n swcp --timeout=120s
  echo "✓ Infinite GPU (Mock LLM) mode active."

  # Warmup: Execute 3 requests to eliminate cold-start JIT noise
  echo "=== Running Warmup Submissions (3 requests) after rollout ==="
  docker run --rm $DOCKER_TTY \
    --network host \
    -v "$ROOT/benchmarks/results:/app/results" \
    swcp-benchmarks:latest warmup --count 3 --target-url "$TARGET_URL" || true
fi

# Drain prior work before test
drain_pipeline

echo "=== Running SWCP Benchmarks ==="
docker run --rm $DOCKER_TTY \
  --network host \
  -v "$ROOT/benchmarks/results:/app/results" \
  swcp-benchmarks:latest "$@"

# Drain work after test
drain_pipeline

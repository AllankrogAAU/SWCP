#!/usr/bin/env bash
# Shared cluster orchestration, SSH tunnel, and telemetry helpers for
# scripts/run-benchmarks.sh and scripts/sweep-vllm.sh.

SWCP_TUNNEL_SOCK=""

init_local_kubeconfig() {
  if [ -n "${SWCP_CONTROL_SSH:-}" ]; then
    return 0
  fi
  if [ -z "${KUBECONFIG:-}" ]; then
    if [ -f "$HOME/.kube/config" ]; then
      export KUBECONFIG="$HOME/.kube/config"
    elif [ -f "/etc/rancher/k3s/k3s.yaml" ]; then
      export KUBECONFIG="/etc/rancher/k3s/k3s.yaml"
    fi
  fi
}

run_kubectl() {
  if [ -n "${SWCP_CONTROL_SSH:-}" ]; then
    local remote_cmd='KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"; [ -f "$KUBECONFIG" ] || KUBECONFIG=/etc/rancher/k3s/k3s.yaml; export KUBECONFIG; kubectl'
    local a
    for a in "$@"; do
      remote_cmd+=" $(printf '%q' "$a")"
    done
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$SWCP_CONTROL_SSH" "$remote_cmd"
  else
    kubectl "$@"
  fi
}

_remote_env_expr() {
  local rel="${SWCP_REMOTE_ENV:-SWCP/.env}"
  if [[ "$rel" == /* ]]; then
    printf '%q' "$rel"
  else
    printf '"$HOME"/%s' "$rel"
  fi
}

read_env_var() {
  local key="$1"
  local fallback="$2"
  local val=""
  if [ -n "${SWCP_CONTROL_SSH:-}" ]; then
    local rpath
    rpath=$(_remote_env_expr)
    val=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$SWCP_CONTROL_SSH" \
      "f=${rpath}; [ -f \"\$f\" ] && grep -E '^${key}=' \"\$f\" | tail -n 1 | cut -d'=' -f2- | cut -d'#' -f1 | tr -d '[:space:]' || true" 2>/dev/null || true)
  elif [ -f "$ROOT/.env" ]; then
    val=$(grep -E "^${key}=" "$ROOT/.env" 2>/dev/null | tail -n 1 | cut -d'=' -f2- | cut -d'#' -f1 | tr -d '[:space:]' || true)
  fi
  echo "${val:-$fallback}"
}

upsert_env_var() {
  local key="$1"
  local val="$2"
  if [[ ! "$key" =~ ^[A-Za-z0-9_]+$ ]]; then
    echo "Invalid env var key: $key" >&2
    return 1
  fi
  if [ -n "${SWCP_CONTROL_SSH:-}" ]; then
    local rpath
    rpath=$(_remote_env_expr)
    ssh -o BatchMode=yes -o ConnectTimeout=5 "$SWCP_CONTROL_SSH" \
      "f=${rpath}; [ -f \"\$f\" ] || exit 0; if grep -qE '^${key}=' \"\$f\"; then sed -i 's|^${key}=.*|${key}=${val}|' \"\$f\"; else printf '\n%s=%s\n' '${key}' '${val}' >> \"\$f\"; fi"
    return 0
  fi
  if [ ! -f "$ROOT/.env" ]; then
    return 0
  fi
  if grep -qE "^${key}=" "$ROOT/.env"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ROOT/.env"
  else
    printf '\n%s=%s\n' "$key" "$val" >> "$ROOT/.env"
  fi
}

_pick_free_port() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
  else
    echo "$((20000 + RANDOM % 10000))"
  fi
}

setup_monitoring_endpoints() {
  local need_prom=0 need_loki=0
  local prom_ip="" loki_ip=""

  if [ -z "${PROMETHEUS_URL:-}" ]; then
    prom_ip=$(run_kubectl get svc prometheus -n monitoring -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)
    need_prom=1
  fi
  if [ -z "${LOKI_URL:-}" ]; then
    loki_ip=$(run_kubectl get svc loki -n monitoring -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)
    need_loki=1
  fi

  if [ -n "${SWCP_CONTROL_SSH:-}" ]; then
    local forwards=()
    if [ "$need_prom" -eq 1 ] && [ -n "$prom_ip" ]; then
      local local_prom_port
      local_prom_port=$(_pick_free_port)
      forwards+=(-L "127.0.0.1:${local_prom_port}:${prom_ip}:9090")
      PROMETHEUS_URL="http://127.0.0.1:${local_prom_port}"
    fi
    if [ "$need_loki" -eq 1 ] && [ -n "$loki_ip" ]; then
      local local_loki_port
      local_loki_port=$(_pick_free_port)
      forwards+=(-L "127.0.0.1:${local_loki_port}:${loki_ip}:3100")
      LOKI_URL="http://127.0.0.1:${local_loki_port}"
    fi

    if [ "${#forwards[@]}" -gt 0 ]; then
      SWCP_TUNNEL_SOCK="/tmp/swcp-bench-tunnel-$$.sock"
      rm -f "$SWCP_TUNNEL_SOCK"
      echo ">>> Opening out-of-band SSH monitoring tunnel via $SWCP_CONTROL_SSH..."
      ssh -f -N -M -S "$SWCP_TUNNEL_SOCK" \
        -o ExitOnForwardFailure=yes \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        "${forwards[@]}" \
        "$SWCP_CONTROL_SSH"
      echo "✓ Monitoring tunnel active (PROMETHEUS_URL=${PROMETHEUS_URL:-unset}, LOKI_URL=${LOKI_URL:-unset})"
    fi
  else
    [ "$need_prom" -eq 1 ] && [ -n "$prom_ip" ] && PROMETHEUS_URL="http://${prom_ip}:9090"
    [ "$need_loki" -eq 1 ] && [ -n "$loki_ip" ] && LOKI_URL="http://${loki_ip}:3100"
  fi
}

cleanup_monitoring_tunnel() {
  if [ -n "${SWCP_TUNNEL_SOCK:-}" ] && [ -n "${SWCP_CONTROL_SSH:-}" ]; then
    ssh -S "$SWCP_TUNNEL_SOCK" -O exit "$SWCP_CONTROL_SSH" >/dev/null 2>&1 || true
    rm -f "$SWCP_TUNNEL_SOCK"
    SWCP_TUNNEL_SOCK=""
  fi
}

measure_inter_node_rtt() {
  if [ -n "${INTER_NODE_RTT_MS:-}" ]; then
    return 0
  fi
  local worker_ip
  worker_ip=$(run_kubectl get nodes -l '!node-role.kubernetes.io/control-plane' \
    -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)
  if [ -z "$worker_ip" ]; then
    return 0
  fi
  local ping_out=""
  if [ -n "${SWCP_CONTROL_SSH:-}" ]; then
    ping_out=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$SWCP_CONTROL_SSH" \
      "ping -c 2 -W 2 -q $(printf '%q' "$worker_ip") 2>/dev/null || true" || true)
  else
    ping_out=$(ping -c 2 -W 2 -q "$worker_ip" 2>/dev/null || true)
  fi
  local avg_ms
  avg_ms=$(echo "$ping_out" | awk -F'/' 'END{if (NF>=5) print $5}' || true)
  if [[ "$avg_ms" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
    INTER_NODE_RTT_MS="$avg_ms"
    echo "✓ Baseline control<->worker (${worker_ip}) RTT: ${INTER_NODE_RTT_MS}ms"
  fi
}

drain_pipeline() {
  local timeout="${1:-${DRAIN_TIMEOUT:-90}}"
  echo ">>> Checking pipeline drain status (active DB submissions and NATS queue depth)..."
  local start_time
  start_time=$(date +%s)
  while true; do
    local nats_pending=0
    local db_pending=0

    local nats_stats
    nats_stats=$(run_kubectl exec -n swcp nats-0 -- wget -qO- 'http://localhost:8222/jsz?consumers=1' 2>/dev/null || echo "{}")
    if command -v jq >/dev/null 2>&1; then
      nats_pending=$(echo "$nats_stats" | jq '[.. | .num_ack_pending?, .num_pending? // empty] | add // 0' 2>/dev/null || echo 0)
    fi
    [[ "$nats_pending" =~ ^[0-9]+$ ]] || nats_pending=0

    local db_res
    db_res=$(run_kubectl exec -n swcp postgres-0 -- sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT count(*) FROM \"Submissions\" WHERE \"Status\" NOT IN ('\''COMPLETED'\'', '\''FAILED'\'');"' 2>/dev/null || echo 0)
    db_pending=$(echo "$db_res" | tr -d '[:space:]')
    [[ "$db_pending" =~ ^[0-9]+$ ]] || db_pending=0

    if [ "$nats_pending" -eq 0 ] && [ "$db_pending" -eq 0 ]; then
      echo "✓ Pipeline is completely idle (NATS pending: 0, DB in-flight: 0)."
      break
    fi

    local now elapsed
    now=$(date +%s)
    elapsed=$(( now - start_time ))
    if [ "$elapsed" -ge "$timeout" ]; then
      echo "Warning: Pipeline drain timed out after ${timeout}s (NATS pending: $nats_pending, DB in-flight: $db_pending). Proceeding..." >&2
      break
    fi

    echo "  [Drain] NATS in-flight/pending: $nats_pending | DB in-flight: $db_pending | Elapsed: ${elapsed}s / ${timeout}s"
    sleep 2
  done
}

# Wait until spilled compute pods on non-control-plane worker nodes have scaled back to 0.
wait_for_worker_baseline() {
  local timeout="${1:-${WORKER_COOLDOWN_TIMEOUT:-360}}"
  local control_nodes
  control_nodes=$(run_kubectl get nodes -l node-role.kubernetes.io/control-plane -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "control-node")
  local start_time
  start_time=$(date +%s)

  while true; do
    local spilled=0
    local pod_lines
    pod_lines=$(run_kubectl get pods -n swcp -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.nodeName}{" "}{.metadata.labels.app\.kubernetes\.io/name}{"\n"}{end}' 2>/dev/null || true)
    while IFS=' ' read -r _pname pnode papp; do
      [ -z "$pnode" ] && continue
      case "$papp" in
        c-sandbox|llm-worker|core-api|api-gateway)
          local is_ctrl=0 cn
          for cn in $control_nodes; do
            [ "$pnode" = "$cn" ] && is_ctrl=1 && break
          done
          if [ "$is_ctrl" -eq 0 ]; then
            spilled=$((spilled + 1))
          fi
          ;;
      esac
    done <<< "$pod_lines"

    if [ "$spilled" -eq 0 ]; then
      echo "✓ Worker node baseline clean (0 spilled compute pods outside control-plane)."
      break
    fi

    local now elapsed
    now=$(date +%s)
    elapsed=$(( now - start_time ))
    if [ "$elapsed" -ge "$timeout" ]; then
      echo "Warning: $spilled spilled pod(s) still active on worker node(s) after ${timeout}s. Proceeding..." >&2
      break
    fi

    echo "  [Cooldown] Waiting for $spilled spilled pod(s) on worker node to scale down (${elapsed}s / ${timeout}s)..."
    sleep 5
  done
}

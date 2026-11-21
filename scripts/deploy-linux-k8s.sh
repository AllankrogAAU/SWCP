#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

read_env_var() {
  local key="$1"
  [ -f "$ROOT/.env" ] || return 0
  grep -E "^[[:space:]]*${key}[[:space:]]*=" "$ROOT/.env" | tail -n1 | cut -d= -f2- | sed -E 's/[[:space:]]*#.*$//; s/^[[:space:]"'\''\r]+//; s/[[:space:]"'\''\r]+$//' || true
}

IMAGES_ONLY=0
TARGET_SERVICE=""
for arg in "$@"; do
  case "$arg" in
    --images-only)
      IMAGES_ONLY=1 ;;
    -h|--help)
      echo "Usage: bash $0 [service|all|c-sandbox|vllm] [--images-only]"
      exit 0 ;;
    *)
      if [ -z "$TARGET_SERVICE" ]; then
        TARGET_SERVICE="$arg"
      else
        echo "Unexpected argument: $arg" >&2
        exit 1
      fi ;;
  esac
done
TARGET_SERVICE="${TARGET_SERVICE:-all}"

CLUSTER_ROLE_VAL="${CLUSTER_ROLE:-$(read_env_var CLUSTER_ROLE)}"
if [ "$IMAGES_ONLY" -eq 0 ] && [ "$CLUSTER_ROLE_VAL" = "worker" ]; then
  IMAGES_ONLY=1
  echo "Detected CLUSTER_ROLE=worker; running in --images-only mode (building & importing images locally into this node)."
fi

# Find workable kubeconfig
if [ -z "${KUBECONFIG:-}" ]; then
  if [ -f "$HOME/.kube/config" ]; then
    export KUBECONFIG="$HOME/.kube/config"
  elif [ -f "/etc/rancher/k3s/k3s.yaml" ]; then
    export KUBECONFIG="/etc/rancher/k3s/k3s.yaml"
  fi
fi

if [ "$IMAGES_ONLY" -eq 0 ] && ! kubectl get nodes >/dev/null 2>&1; then
  echo "Error: Cannot connect to Kubernetes cluster using kubectl." >&2
  echo "Make sure k3s is running ('systemctl status k3s') and KUBECONFIG is readable." >&2
  echo "On a worker node, pass '--images-only' or set CLUSTER_ROLE=worker in .env to build/import local images." >&2
  echo "If you haven't run host setup yet, run: sudo bash scripts/setup-host-prereqs.sh" >&2
  exit 1
fi

# Detect docker command
DOCKER_CMD="docker"
if ! docker info >/dev/null 2>&1; then
  if sudo -n docker info >/dev/null 2>&1; then
    DOCKER_CMD="sudo docker"
  elif command -v sg >/dev/null 2>&1 && sg docker -c "docker info" >/dev/null 2>&1; then
    DOCKER_CMD="sg docker -c docker"
  else
    echo "Warning: Current user cannot access Docker daemon without sudo." >&2
    echo "Trying 'sudo docker'..." >&2
    DOCKER_CMD="sudo docker"
  fi
fi

# Detect containerd ctr command for k3s
CTR_CMD="k3s ctr"
if ! k3s ctr images list >/dev/null 2>&1; then
  CTR_CMD="sudo k3s ctr"
fi

if [ "$IMAGES_ONLY" -eq 0 ]; then
  echo "=== Kubernetes node status ==="
  kubectl get nodes -o wide

  echo "=== Labeling cluster nodes (GPU & gVisor) ==="
  GPU_NODES_VAL="${GPU_NODES:-$(read_env_var GPU_NODES)}"
  if [ -n "$GPU_NODES_VAL" ]; then
    IFS=',' read -ra gpu_node_list <<< "$GPU_NODES_VAL"
    for n in "${gpu_node_list[@]}"; do
      n="$(echo "$n" | xargs)"
      [ -n "$n" ] && kubectl label node "$n" swcp.io/gpu=true --overwrite >/dev/null || true
    done
  else
    for n in $(kubectl get nodes -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
      kubectl label node "$n" swcp.io/gpu=true --overwrite >/dev/null || true
    done
  fi

  GVISOR_NODES_VAL="${GVISOR_NODES:-$(read_env_var GVISOR_NODES)}"
  if [ -n "$GVISOR_NODES_VAL" ]; then
    IFS=',' read -ra gv_node_list <<< "$GVISOR_NODES_VAL"
    for n in "${gv_node_list[@]}"; do
      n="$(echo "$n" | xargs)"
      [ -n "$n" ] && kubectl label node "$n" swcp.io/gvisor=true --overwrite >/dev/null || true
    done
  fi

  echo "=== Ensuring metrics-server is installed ==="
  kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml >/dev/null 2>&1 || true
  if ! kubectl get deployment metrics-server -n kube-system -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null | grep -q -- '--kubelet-insecure-tls'; then
    kubectl patch deployment metrics-server -n kube-system --type='json' -p='[{"op": "add", "path": "/spec/template/spec/containers/0/args/-", "value": "--kubelet-insecure-tls"}]' || true
  fi

  if ! kubectl get crd scaledobjects.keda.sh >/dev/null 2>&1; then
    echo "=== Ensuring KEDA is installed ==="
    kubectl apply --server-side -f https://github.com/kedacore/keda/releases/download/v2.16.1/keda-2.16.1.yaml >/dev/null 2>&1 || true
  fi

  if kubectl get nodes -l swcp.io/gpu=true --no-headers 2>/dev/null | grep -q . && \
     ! kubectl get daemonset -A -o name 2>/dev/null | grep -qi "nvidia-device-plugin"; then
    echo "=== Ensuring NVIDIA device plugin is installed ==="
    kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.17.0/deployments/static/nvidia-device-plugin.yml >/dev/null 2>&1 || true
  fi

  if ! kubectl get deployment ingress-nginx-controller -n ingress-nginx >/dev/null 2>&1; then
    echo "=== Ensuring Ingress-NGINX is installed with hostNetwork ==="
    kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.12.1/deploy/static/provider/cloud/deploy.yaml >/dev/null 2>&1 || true
  fi

  if [ "$(kubectl get service ingress-nginx-controller -n ingress-nginx -o jsonpath='{.spec.type}' 2>/dev/null || true)" != "ClusterIP" ]; then
    kubectl patch service ingress-nginx-controller -n ingress-nginx --type=merge -p '{"spec":{"type":"ClusterIP"}}' || true
  fi
  if [ "$(kubectl get deployment ingress-nginx-controller -n ingress-nginx -o jsonpath='{.spec.template.spec.hostNetwork}' 2>/dev/null || true)" != "true" ]; then
    kubectl patch deployment ingress-nginx-controller -n ingress-nginx --type=json -p='[{"op":"add","path":"/spec/template/spec/hostNetwork","value":true},{"op":"add","path":"/spec/template/spec/dnsPolicy","value":"ClusterFirstWithHostNet"}]' || true
  fi
  if [ "$(kubectl get deployment ingress-nginx-controller -n ingress-nginx -o jsonpath='{.spec.strategy.type}' 2>/dev/null || true)" != "Recreate" ]; then
    kubectl patch deployment ingress-nginx-controller -n ingress-nginx --type=json -p='[{"op":"replace","path":"/spec/strategy","value":{"type":"Recreate"}}]' || true
  fi
  if [ "$(kubectl get deployment ingress-nginx-controller -n ingress-nginx -o jsonpath='{.spec.template.spec.nodeSelector.node-role\.kubernetes\.io/control-plane}' 2>/dev/null || true)" != "true" ]; then
    kubectl patch deployment ingress-nginx-controller -n ingress-nginx --type=merge -p '{"spec":{"template":{"spec":{"nodeSelector":{"node-role.kubernetes.io/control-plane":"true"}}}}}' || true
  fi

  echo "Waiting for ingress-nginx controller readiness..."
  kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx --timeout=180s || true
fi

# All defined image targets
declare -A IMAGE_MAP
IMAGE_MAP["core-api"]="ghcr.io/allankrogaau/core-api:latest|apps/coreApi/Dockerfile|."
IMAGE_MAP["api-gateway"]="ghcr.io/allankrogaau/api-gateway:latest|apps/apiGateway/Dockerfile|."
IMAGE_MAP["c-sandbox-worker"]="ghcr.io/allankrogaau/c-sandbox-worker:latest|apps/cSandbox/Dockerfile.worker|apps/cSandbox"
IMAGE_MAP["c-sandbox-runtime"]="ghcr.io/allankrogaau/c-sandbox-runtime:latest|apps/cSandbox/Dockerfile|apps/cSandbox"
IMAGE_MAP["llm-worker"]="ghcr.io/allankrogaau/llm-worker:latest|apps/llmWorker/Dockerfile|."
IMAGE_MAP["frontend"]="ghcr.io/allankrogaau/frontend:latest|apps/frontend/Dockerfile|apps/frontend"
IMAGE_MAP["postgres"]="ghcr.io/allankrogaau/postgres:latest|apps/postgres/Dockerfile|apps/postgres"

# Determine which images to build
images_to_build=()
if [ "$TARGET_SERVICE" = "all" ]; then
  images_to_build=(
    "ghcr.io/allankrogaau/core-api:latest|apps/coreApi/Dockerfile|."
    "ghcr.io/allankrogaau/api-gateway:latest|apps/apiGateway/Dockerfile|."
    "ghcr.io/allankrogaau/c-sandbox-worker:latest|apps/cSandbox/Dockerfile.worker|apps/cSandbox"
    "ghcr.io/allankrogaau/c-sandbox-runtime:latest|apps/cSandbox/Dockerfile|apps/cSandbox"
    "ghcr.io/allankrogaau/llm-worker:latest|apps/llmWorker/Dockerfile|."
    "ghcr.io/allankrogaau/frontend:latest|apps/frontend/Dockerfile|apps/frontend"
    "ghcr.io/allankrogaau/postgres:latest|apps/postgres/Dockerfile|apps/postgres"
  )
elif [ "$TARGET_SERVICE" = "c-sandbox" ]; then
  images_to_build=(
    "ghcr.io/allankrogaau/c-sandbox-worker:latest|apps/cSandbox/Dockerfile.worker|apps/cSandbox"
    "ghcr.io/allankrogaau/c-sandbox-runtime:latest|apps/cSandbox/Dockerfile|apps/cSandbox"
  )
elif [ "$TARGET_SERVICE" = "vllm" ]; then
  images_to_build=()
elif [ -n "${IMAGE_MAP[$TARGET_SERVICE]:-}" ]; then
  images_to_build=("${IMAGE_MAP[$TARGET_SERVICE]}")
else
  echo "Unknown service '$TARGET_SERVICE'. Available services: ${!IMAGE_MAP[*]}, 'c-sandbox', 'vllm', or 'all'" >&2
  exit 1
fi

worker_ssh_targets=()
if [ "$IMAGES_ONLY" -eq 0 ]; then
  WORKER_SSH_HOSTS_VAL="${WORKER_SSH_HOSTS:-$(read_env_var WORKER_SSH_HOSTS)}"
  if [ -n "$WORKER_SSH_HOSTS_VAL" ]; then
    IFS=',' read -ra raw_ssh_hosts <<< "$WORKER_SSH_HOSTS_VAL"
    for h in "${raw_ssh_hosts[@]}"; do
      h="$(echo "$h" | xargs)"
      [ -n "$h" ] && worker_ssh_targets+=("$h")
    done
  fi
fi

if [ "${#images_to_build[@]}" -gt 0 ]; then
  if [ "${#worker_ssh_targets[@]}" -gt 0 ]; then
    echo "=== Verifying SSH access to worker node(s) for image streaming ==="
    for worker_ssh in "${worker_ssh_targets[@]}"; do
      if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$worker_ssh" "sudo -n k3s ctr version >/dev/null 2>&1"; then
        echo "Error: Cannot SSH/sudo to worker '$worker_ssh' non-interactively." >&2
        echo "Ensure key-based SSH (ssh-copy-id $worker_ssh) and passwordless sudo ('sudo -n k3s ctr') are configured." >&2
        exit 1
      fi
    done
  fi

  echo "=== Building and importing images ==="
  for entry in "${images_to_build[@]}"; do
    IFS='|' read -r image dockerfile context <<< "$entry"
    echo "--> Building $image from $dockerfile..."
    $DOCKER_CMD build -t "$image" -f "$dockerfile" "$context"
    echo "--> Importing $image into $(hostname) k3s containerd..."
    sudo crictl rmi "$image" >/dev/null 2>&1 || true
    $CTR_CMD images rm "$image" >/dev/null 2>&1 || true
    $DOCKER_CMD save "$image" | $CTR_CMD images import -
    for worker_ssh in "${worker_ssh_targets[@]}"; do
      echo "--> Streaming $image to $worker_ssh..."
      $DOCKER_CMD save "$image" | ssh -o BatchMode=yes -o ConnectTimeout=10 "$worker_ssh" \
        "sudo -n sh -c 'crictl rmi \"$image\" >/dev/null 2>&1 || true; k3s ctr images rm \"$image\" >/dev/null 2>&1 || true; exec k3s ctr images import -'"
    done
  done
fi

if [ "$IMAGES_ONLY" -eq 1 ]; then
  echo "=========================================================="
  echo "Local images built and imported into $(hostname) k3s containerd."
  echo "Apply/restart deployments from the control-plane node."
  echo "=========================================================="
  exit 0
fi

echo "=== Applying namespace and storage class ==="
kubectl apply -f k8s/00-namespace.yaml

if ! kubectl get storageclass standard >/dev/null 2>&1; then
  PROVISIONER="$(kubectl get storageclass -o jsonpath='{.items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")].provisioner}' 2>/dev/null || true)"
  PROVISIONER="${PROVISIONER:-rancher.io/local-path}"
  cat <<EOF | kubectl apply -f -
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: standard
provisioner: ${PROVISIONER}
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
EOF
fi

VLLM_CONFIG_BEFORE="$(kubectl get configmap vllm-config -n swcp -o jsonpath='{.data}' 2>/dev/null || true)"

echo "=== Applying base ConfigMaps and Secrets ==="
kubectl apply -f k8s/01-configmaps-secrets.yaml

echo "=== Patching secrets and configs from .env ==="
PG_PASS=""
if [ -f "$ROOT/.env" ]; then
  # 1. Postgres password
  PG_PASS="$(read_env_var POSTGRES_PASSWORD)"
  if [ -n "$PG_PASS" ]; then
    kubectl patch secret postgres-credentials -n swcp --type=merge -p "{\"stringData\":{\"POSTGRES_PASSWORD\":\"$PG_PASS\"}}"
  fi

  # 2. Azure AI Foundry keys for llm-worker
  LLM_PATCH=""
  LLM_ENDPOINT_PATCH=""
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    if [[ "$line" =~ ^[[:space:]]*AzureAIFoundry_*Models_*([A-Za-z0-9]+)_*ApiKey[[:space:]]*=[[:space:]]*(.+)$ ]]; then
      LLM_PATCH+="\"AzureAIFoundry__Models__${BASH_REMATCH[1]}__ApiKey\":\"${BASH_REMATCH[2]}\","
    elif [[ "$line" =~ ^[[:space:]]*AzureAIFoundry_*Models_*([A-Za-z0-9]+)_*Endpoint[[:space:]]*=[[:space:]]*(.+)$ ]]; then
      AZURE_ENDPOINT="${BASH_REMATCH[2]}"
      AZURE_ENDPOINT="${AZURE_ENDPOINT%/openai/v1}"
      AZURE_ENDPOINT="${AZURE_ENDPOINT%/}"
      LLM_ENDPOINT_PATCH+="\"AzureAIFoundry__Models__${BASH_REMATCH[1]}__Endpoint\":\"$AZURE_ENDPOINT\","
    fi
  done < "$ROOT/.env"
  if [ -n "$LLM_PATCH" ]; then
    kubectl patch secret llm-worker-secrets -n swcp --type=merge -p "{\"stringData\":{${LLM_PATCH%,}}}" || true
  fi
  if [ -n "$LLM_ENDPOINT_PATCH" ]; then
    kubectl patch configmap llm-worker-config -n swcp --type=merge -p "{\"data\":{${LLM_ENDPOINT_PATCH%,}}}" || true
  fi

  # 3. Seed development users for core-api
  SEED_DEV="$(read_env_var SEED_DEVELOPMENT_USERS)"
  DEV_TEACHER_EMAIL="$(read_env_var DEV_TEACHER_USERNAME)"
  if [ -z "$DEV_TEACHER_EMAIL" ]; then
    DEV_TEACHER_EMAIL="$(read_env_var DEV_TEACHER_EMAIL)"
  fi
  DEV_TEACHER_PASS="$(read_env_var DEV_TEACHER_PASSWORD)"

  if [ -n "$SEED_DEV" ]; then
    kubectl patch configmap core-api-config -n swcp --type=merge -p "{\"data\":{\"SeedDevelopmentUsers\":\"$SEED_DEV\",\"DevelopmentUsers__TeacherUsername\":\"$DEV_TEACHER_EMAIL\"}}" || true
  fi
  if [ -n "$DEV_TEACHER_PASS" ]; then
    kubectl patch secret core-api-secrets -n swcp --type=merge -p "{\"stringData\":{\"DevelopmentUsers__TeacherPassword\":\"$DEV_TEACHER_PASS\"}}" || true
  fi

  # 4. Local vLLM and llm-worker concurrency settings from .env
  LLM_MODEL_PATH_VAL="$(read_env_var LLM_MODEL_PATH)"
  LLM_MODEL_NAME_VAL="$(read_env_var LLM_MODEL_NAME)"
  LOCAL_LLM_PORT_VAL="$(read_env_var LOCAL_LLM_PORT)"
  VLLM_QUANTIZATION_VAL="$(read_env_var VLLM_QUANTIZATION)"
  VLLM_KV_CACHE_DTYPE_VAL="$(read_env_var VLLM_KV_CACHE_DTYPE)"
  VLLM_MAX_MODEL_LEN_VAL="$(read_env_var VLLM_MAX_MODEL_LEN)"
  VLLM_GPU_MEM_VAL="$(read_env_var VLLM_GPU_MEMORY_UTILIZATION)"
  VLLM_MAX_SEQS_VAL="$(read_env_var VLLM_MAX_NUM_SEQS)"
  VLLM_OVERFLOW_THRESHOLD_VAL="$(read_env_var VLLM_OVERFLOW_THRESHOLD)"
  LLM_LOCAL_MAX_CONCURRENT_VAL="$(read_env_var LLM_LOCAL_MAX_CONCURRENT)"
  LLM_LOCAL_MAX_ACK_PENDING_VAL="$(read_env_var LLM_LOCAL_MAX_ACK_PENDING)"

  VLLM_CM_PATCH=""
  [ -n "$LLM_MODEL_PATH_VAL" ] && VLLM_CM_PATCH+="\"LLM_MODEL_PATH\":\"$LLM_MODEL_PATH_VAL\","
  [ -n "$LLM_MODEL_NAME_VAL" ] && VLLM_CM_PATCH+="\"LLM_MODEL_NAME\":\"$LLM_MODEL_NAME_VAL\","
  [ -n "$LOCAL_LLM_PORT_VAL" ] && VLLM_CM_PATCH+="\"VLLM_PORT\":\"$LOCAL_LLM_PORT_VAL\","
  [ -n "$VLLM_QUANTIZATION_VAL" ] && VLLM_CM_PATCH+="\"VLLM_QUANTIZATION\":\"$VLLM_QUANTIZATION_VAL\","
  [ -n "$VLLM_KV_CACHE_DTYPE_VAL" ] && VLLM_CM_PATCH+="\"VLLM_KV_CACHE_DTYPE\":\"$VLLM_KV_CACHE_DTYPE_VAL\","
  [ -n "$VLLM_MAX_MODEL_LEN_VAL" ] && VLLM_CM_PATCH+="\"VLLM_MAX_MODEL_LEN\":\"$VLLM_MAX_MODEL_LEN_VAL\","
  [ -n "$VLLM_GPU_MEM_VAL" ] && VLLM_CM_PATCH+="\"VLLM_GPU_MEMORY_UTILIZATION\":\"$VLLM_GPU_MEM_VAL\","
  [ -n "$VLLM_MAX_SEQS_VAL" ] && VLLM_CM_PATCH+="\"VLLM_MAX_NUM_SEQS\":\"$VLLM_MAX_SEQS_VAL\","
  if [ -n "$VLLM_CM_PATCH" ]; then
    kubectl patch configmap vllm-config -n swcp --type=merge -p "{\"data\":{${VLLM_CM_PATCH%,}}}" || true
  fi

  EFFECTIVE_MAX_CONCURRENT="${LLM_LOCAL_MAX_CONCURRENT_VAL:-${VLLM_MAX_SEQS_VAL:-}}"
  EFFECTIVE_PRIMARY_MAX="${VLLM_OVERFLOW_THRESHOLD_VAL:-${EFFECTIVE_MAX_CONCURRENT:-}}"
  EFFECTIVE_MAX_ACK_PENDING="${LLM_LOCAL_MAX_ACK_PENDING_VAL:-${EFFECTIVE_MAX_CONCURRENT:-}}"
  LLM_WORKER_CM_PATCH=""
  [ -n "$LLM_MODEL_NAME_VAL" ] && LLM_WORKER_CM_PATCH+="\"LocalLlm__ModelName\":\"$LLM_MODEL_NAME_VAL\","
  [ -n "$EFFECTIVE_MAX_CONCURRENT" ] && LLM_WORKER_CM_PATCH+="\"LocalLlm__MaxConcurrent\":\"$EFFECTIVE_MAX_CONCURRENT\","
  [ -n "$EFFECTIVE_PRIMARY_MAX" ] && LLM_WORKER_CM_PATCH+="\"LocalLlm__PrimaryMaxConcurrent\":\"$EFFECTIVE_PRIMARY_MAX\","
  [ -n "$EFFECTIVE_MAX_ACK_PENDING" ] && LLM_WORKER_CM_PATCH+="\"LocalLlm__MaxAckPending\":\"$EFFECTIVE_MAX_ACK_PENDING\","
  if [ -n "$LLM_WORKER_CM_PATCH" ]; then
    kubectl patch configmap llm-worker-config -n swcp --type=merge -p "{\"data\":{${LLM_WORKER_CM_PATCH%,}}}" || true
  fi

  # 5. Dynamic CORS allowed origins for api-gateway
  cors_origins=()
  CORS_ENV="$(read_env_var ADDITIONAL_CORS_ORIGINS)"
  if [ -n "$CORS_ENV" ]; then
    IFS=',' read -ra ADDR <<< "$CORS_ENV"
    for origin in "${ADDR[@]}"; do
      [ -n "$origin" ] && cors_origins+=("$origin")
    done
  fi

  # Auto-detect local Tailscale IP and hostname
  if command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
    TS_IP="$(tailscale ip -4 2>/dev/null || true)"
    TS_DNS="$(tailscale status --json 2>/dev/null | grep -o '"MagicDNSSuffix": *"[^"]*"' | head -n1 | cut -d'"' -f4 || true)"
    NODE_NAME="$(hostname | tr '[:upper:]' '[:lower:]')"
    [ -n "$TS_IP" ] && cors_origins+=("http://${TS_IP}" "https://${TS_IP}")
    [ -n "$NODE_NAME" ] && cors_origins+=("http://${NODE_NAME}" "https://${NODE_NAME}")
    if [ -n "$NODE_NAME" ] && [ -n "$TS_DNS" ]; then
      cors_origins+=("http://${NODE_NAME}.${TS_DNS}" "https://${NODE_NAME}.${TS_DNS}")
    fi
  fi

  if [ "${#cors_origins[@]}" -gt 0 ]; then
    CORS_PATCH=""
    idx=6
    for origin in $(printf "%s\n" "${cors_origins[@]}" | sort -u); do
      CORS_PATCH+="\"Cors__AllowedOrigins__${idx}\":\"${origin}\","
      idx=$((idx + 1))
    done
    if [ -n "$CORS_PATCH" ]; then
      kubectl patch configmap api-gateway-config -n swcp --type=merge -p "{\"data\":{${CORS_PATCH%,}}}" || true
    fi
  fi
fi

VLLM_CONFIG_AFTER="$(kubectl get configmap vllm-config -n swcp -o jsonpath='{.data}' 2>/dev/null || true)"

# Ensure sandbox runtime class matches node capability before workloads start
if kubectl get nodes -l swcp.io/gvisor=true --no-headers 2>/dev/null | grep -q .; then
  echo "gVisor detected on node; sandbox pods will use 'gvisor' RuntimeClass."
  kubectl patch configmap c-sandbox-config -n swcp --type=merge -p '{"data":{"SANDBOX_RUNTIME_CLASS":"gvisor"}}' || true
else
  echo "Warning: Node swcp.io/gvisor=true label not found; falling back to default runtime." >&2
  kubectl patch configmap c-sandbox-config -n swcp --type=merge -p '{"data":{"SANDBOX_RUNTIME_CLASS":""}}' || true
fi

echo "=== Applying workload manifests in k8s/ ==="
kubectl apply -f k8s/02-postgres.yaml
kubectl apply -f k8s/03-nats.yaml
if kubectl get job jwt-key-bootstrap -n swcp >/dev/null 2>&1 && \
   ! kubectl get secret jwt-keys -n swcp >/dev/null 2>&1; then
  echo "=== Removing stale jwt-key-bootstrap job (jwt-keys secret missing) ==="
  kubectl delete job jwt-key-bootstrap -n swcp --ignore-not-found >/dev/null 2>&1 || true
fi
kubectl apply -f k8s/04-core-api.yaml
for manifest in k8s/*.yaml; do
  case "$manifest" in
    k8s/00-namespace.yaml|k8s/01-configmaps-secrets.yaml|k8s/02-postgres.yaml|k8s/03-nats.yaml|k8s/04-core-api.yaml)
      continue ;;
  esac
  kubectl apply -f "$manifest"
done

if [ -n "$PG_PASS" ]; then
  echo "=== Syncing PostgreSQL user password with .env ==="
  kubectl rollout status statefulset/postgres -n swcp --timeout=120s
  PG_USER="$(kubectl get configmap postgres-config -n swcp -o jsonpath='{.data.POSTGRES_USER}' 2>/dev/null || echo swcp)"
  PG_DB="$(kubectl get configmap postgres-config -n swcp -o jsonpath='{.data.POSTGRES_DB}' 2>/dev/null || echo swcp)"
  printf 'ALTER USER "%s" WITH PASSWORD :'\''pass'\'';\n' "$PG_USER" | \
    kubectl exec -i -n swcp postgres-0 -- psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -v pass="$PG_PASS" >/dev/null
fi

# Ensure vllm models volume points to the repo's apps/localLLM/models directory on this host
if [ -d "$ROOT/apps/localLLM/models" ]; then
  kubectl patch deployment vllm -n swcp --type=json -p="[{\"op\":\"replace\",\"path\":\"/spec/template/spec/volumes/0/hostPath/path\",\"value\":\"$ROOT/apps/localLLM/models\"}]" >/dev/null 2>&1 || true
  kubectl patch deployment vllm-overflow -n swcp --type=json -p="[{\"op\":\"replace\",\"path\":\"/spec/template/spec/volumes/0/hostPath/path\",\"value\":\"$ROOT/apps/localLLM/models\"}]" >/dev/null 2>&1 || true
fi

VLLM_RESTARTED=0
echo "=== Restarting affected deployments ==="
if [ "$TARGET_SERVICE" = "all" ]; then
  echo "Cleaning up idle sandbox pods in swcp-sandbox..."
  kubectl delete pods -n swcp-sandbox --all --grace-period=0 --force >/dev/null 2>&1 || true
  deployments=("core-api" "api-gateway" "c-sandbox" "llm-worker" "frontend")
  for dep in "${deployments[@]}"; do
    echo "Restarting deployment/$dep..."
    kubectl rollout restart "deployment/$dep" -n swcp
  done
  if [ -n "$VLLM_CONFIG_BEFORE" ] && [ "$VLLM_CONFIG_BEFORE" != "$VLLM_CONFIG_AFTER" ]; then
    echo "vllm-config changed; restarting deployment/vllm and deployment/vllm-overflow..."
    kubectl rollout restart deployment/vllm -n swcp
    kubectl rollout restart deployment/vllm-overflow -n swcp
    VLLM_RESTARTED=1
  fi
elif [ "$TARGET_SERVICE" = "c-sandbox" ] || [ "$TARGET_SERVICE" = "c-sandbox-worker" ] || [ "$TARGET_SERVICE" = "c-sandbox-runtime" ]; then
  echo "Killing existing warm sandbox pods in swcp-sandbox..."
  kubectl delete pods -n swcp-sandbox --all --grace-period=0 --force >/dev/null 2>&1 || true
  echo "Restarting deployment/c-sandbox..."
  kubectl rollout restart deployment/c-sandbox -n swcp
elif [ "$TARGET_SERVICE" = "vllm" ]; then
  echo "Restarting deployment/vllm, deployment/vllm-overflow, and deployment/llm-worker..."
  kubectl rollout restart deployment/vllm -n swcp
  kubectl rollout restart deployment/vllm-overflow -n swcp
  kubectl rollout restart deployment/llm-worker -n swcp
  VLLM_RESTARTED=1
elif [ "$TARGET_SERVICE" != "postgres" ]; then
  echo "Restarting deployment/$TARGET_SERVICE..."
  kubectl rollout restart "deployment/$TARGET_SERVICE" -n swcp
fi

echo "=== Checking rollout status ==="
if [ "$TARGET_SERVICE" = "all" ]; then
  kubectl rollout status deployment/core-api -n swcp --timeout=180s
  kubectl rollout status deployment/api-gateway -n swcp --timeout=120s
  kubectl rollout status deployment/c-sandbox -n swcp --timeout=120s
  kubectl rollout status deployment/llm-worker -n swcp --timeout=120s
  kubectl rollout status deployment/frontend -n swcp --timeout=120s
  if [ "$VLLM_RESTARTED" -eq 1 ]; then
    kubectl rollout status deployment/vllm -n swcp --timeout=300s
    kubectl rollout status deployment/vllm-overflow -n swcp --timeout=300s
  fi
elif [ "$TARGET_SERVICE" = "c-sandbox" ] || [ "$TARGET_SERVICE" = "c-sandbox-worker" ] || [ "$TARGET_SERVICE" = "c-sandbox-runtime" ]; then
  kubectl rollout status deployment/c-sandbox -n swcp --timeout=120s
elif [ "$TARGET_SERVICE" = "vllm" ]; then
  kubectl rollout status deployment/llm-worker -n swcp --timeout=120s
  kubectl rollout status deployment/vllm -n swcp --timeout=300s
  kubectl rollout status deployment/vllm-overflow -n swcp --timeout=300s
elif [ "$TARGET_SERVICE" != "postgres" ]; then
  kubectl rollout status "deployment/$TARGET_SERVICE" -n swcp --timeout=120s
fi

echo ""
echo "=== Current Pods in swcp namespace ==="
kubectl get pods -n swcp -o wide

echo ""
echo "=========================================================="
echo "Deployment finished!"
echo "Backend and Frontend are accessible on host port 80."
echo "To share via Tailscale, run: bash scripts/expose-frontend.sh"
echo "=========================================================="

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TARGET_SERVICE="${1:-all}"

# Find workable kubeconfig
if [ -z "${KUBECONFIG:-}" ]; then
  if [ -f "$HOME/.kube/config" ]; then
    export KUBECONFIG="$HOME/.kube/config"
  elif [ -f "/etc/rancher/k3s/k3s.yaml" ]; then
    export KUBECONFIG="/etc/rancher/k3s/k3s.yaml"
  fi
fi

if ! kubectl get nodes >/dev/null 2>&1; then
  echo "Error: Cannot connect to Kubernetes cluster using kubectl." >&2
  echo "Make sure k3s is running ('systemctl status k3s') and KUBECONFIG is readable." >&2
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

echo "=== Kubernetes node status ==="
kubectl get nodes -o wide

echo "=== Ensuring metrics-server is installed ==="
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml >/dev/null 2>&1 || true
if ! kubectl get deployment metrics-server -n kube-system -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null | grep -q -- '--kubelet-insecure-tls'; then
  kubectl patch deployment metrics-server -n kube-system --type='json' -p='[{"op": "add", "path": "/spec/template/spec/containers/0/args/-", "value": "--kubelet-insecure-tls"}]' || true
fi

if ! kubectl get crd scaledobjects.keda.sh >/dev/null 2>&1; then
  echo "=== Ensuring KEDA is installed ==="
  kubectl apply --server-side -f https://github.com/kedacore/keda/releases/download/v2.16.1/keda-2.16.1.yaml >/dev/null 2>&1 || true
fi

if ! kubectl get deployment ingress-nginx-controller -n ingress-nginx >/dev/null 2>&1; then
  echo "=== Ensuring Ingress-NGINX is installed with hostNetwork ==="
  kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.12.1/deploy/static/provider/cloud/deploy.yaml >/dev/null 2>&1 || true

  if [ "$(kubectl get service ingress-nginx-controller -n ingress-nginx -o jsonpath='{.spec.type}' 2>/dev/null || true)" != "ClusterIP" ]; then
    kubectl patch service ingress-nginx-controller -n ingress-nginx --type=merge -p '{"spec":{"type":"ClusterIP"}}' || true
  fi
  if [ "$(kubectl get deployment ingress-nginx-controller -n ingress-nginx -o jsonpath='{.spec.template.spec.hostNetwork}' 2>/dev/null || true)" != "true" ]; then
    kubectl patch deployment ingress-nginx-controller -n ingress-nginx --type=json -p='[{"op":"add","path":"/spec/template/spec/hostNetwork","value":true},{"op":"add","path":"/spec/template/spec/dnsPolicy","value":"ClusterFirstWithHostNet"}]' || true
  fi
  if [ "$(kubectl get deployment ingress-nginx-controller -n ingress-nginx -o jsonpath='{.spec.strategy.type}' 2>/dev/null || true)" != "Recreate" ]; then
    kubectl patch deployment ingress-nginx-controller -n ingress-nginx --type=json -p='[{"op":"replace","path":"/spec/strategy","value":{"type":"Recreate"}}]' || true
  fi

  echo "Waiting for ingress-nginx rollout..."
  kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx --timeout=120s || true
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

if [ "${#images_to_build[@]}" -gt 0 ]; then
  echo "=== Building and importing images ==="
  for entry in "${images_to_build[@]}"; do
    IFS='|' read -r image dockerfile context <<< "$entry"
    echo "--> Building $image from $dockerfile..."
    $DOCKER_CMD build -t "$image" -f "$dockerfile" "$context"
    echo "--> Importing $image into k3s containerd..."
    sudo crictl rmi "$image" >/dev/null 2>&1 || true
    $CTR_CMD images rm "$image" >/dev/null 2>&1 || true
    $DOCKER_CMD save "$image" | $CTR_CMD images import -
  done
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

echo "=== Applying manifests in k8s/ ==="
kubectl apply -f k8s/01-configmaps-secrets.yaml
kubectl apply -f k8s/02-postgres.yaml
kubectl apply -f k8s/03-nats.yaml
kubectl apply -f k8s/04-core-api.yaml
kubectl apply -f k8s/

read_env_var() {
  local key="$1"
  grep -E "^[[:space:]]*${key}[[:space:]]*=" "$ROOT/.env" | tail -n1 | cut -d= -f2- | sed -E 's/[[:space:]]*#.*$//; s/^[[:space:]"'\''\r]+//; s/[[:space:]"'\''\r]+$//' || true
}

echo "=== Patching secrets and configs from .env ==="
if [ -f "$ROOT/.env" ]; then
  # 1. Postgres password
  PG_PASS="$(read_env_var POSTGRES_PASSWORD)"
  if [ -n "$PG_PASS" ]; then
    kubectl patch secret postgres-credentials -n swcp --type=merge -p "{\"stringData\":{\"POSTGRES_PASSWORD\":\"$PG_PASS\"}}" || true
  fi

  # 2. Azure AI Foundry keys for llm-worker
  LLM_PATCH=""
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    if [[ "$line" =~ ^[[:space:]]*AzureAIFoundry_*Models_*([A-Za-z0-9]+)_*ApiKey[[:space:]]*=[[:space:]]*(.+)$ ]]; then
      LLM_PATCH+="\"AzureAIFoundry__Models__${BASH_REMATCH[1]}__ApiKey\":\"${BASH_REMATCH[2]}\","
    fi
  done < "$ROOT/.env"
  if [ -n "$LLM_PATCH" ]; then
    kubectl patch secret llm-worker-secrets -n swcp --type=merge -p "{\"stringData\":{${LLM_PATCH%,}}}" || true
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
  EFFECTIVE_MAX_ACK_PENDING="${LLM_LOCAL_MAX_ACK_PENDING_VAL:-${EFFECTIVE_MAX_CONCURRENT:-}}"
  LLM_WORKER_CM_PATCH=""
  [ -n "$LLM_MODEL_NAME_VAL" ] && LLM_WORKER_CM_PATCH+="\"LocalLlm__ModelName\":\"$LLM_MODEL_NAME_VAL\","
  [ -n "$EFFECTIVE_MAX_CONCURRENT" ] && LLM_WORKER_CM_PATCH+="\"LocalLlm__MaxConcurrent\":\"$EFFECTIVE_MAX_CONCURRENT\","
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

# Ensure vllm models volume points to the repo's apps/localLLM/models directory on this host
if [ -d "$ROOT/apps/localLLM/models" ]; then
  kubectl patch deployment vllm -n swcp --type=json -p="[{\"op\":\"replace\",\"path\":\"/spec/template/spec/volumes/0/hostPath/path\",\"value\":\"$ROOT/apps/localLLM/models\"}]" >/dev/null 2>&1 || true
fi

# Ensure sandbox runtime class matches node capability
if kubectl get nodes -l swcp.io/gvisor=true --no-headers 2>/dev/null | grep -q .; then
  echo "gVisor detected on node; sandbox pods will use 'gvisor' RuntimeClass."
  kubectl patch configmap c-sandbox-config -n swcp --type=merge -p '{"data":{"SANDBOX_RUNTIME_CLASS":"gvisor"}}' || true
else
  echo "Warning: Node swcp.io/gvisor=true label not found; falling back to default runtime." >&2
  kubectl patch configmap c-sandbox-config -n swcp --type=merge -p '{"data":{"SANDBOX_RUNTIME_CLASS":""}}' || true
fi

echo "=== Restarting affected deployments ==="
if [ "$TARGET_SERVICE" = "all" ]; then
  echo "Cleaning up idle sandbox pods in swcp-sandbox..."
  kubectl delete pods -n swcp-sandbox --all --grace-period=0 --force >/dev/null 2>&1 || true
  deployments=("core-api" "api-gateway" "c-sandbox" "llm-worker" "frontend")
  for dep in "${deployments[@]}"; do
    echo "Restarting deployment/$dep..."
    kubectl rollout restart "deployment/$dep" -n swcp || true
  done
elif [ "$TARGET_SERVICE" = "c-sandbox" ] || [ "$TARGET_SERVICE" = "c-sandbox-worker" ] || [ "$TARGET_SERVICE" = "c-sandbox-runtime" ]; then
  echo "Killing existing warm sandbox pods in swcp-sandbox..."
  kubectl delete pods -n swcp-sandbox --all --grace-period=0 --force >/dev/null 2>&1 || true
  echo "Restarting deployment/c-sandbox..."
  kubectl rollout restart deployment/c-sandbox -n swcp || true
elif [ "$TARGET_SERVICE" = "vllm" ]; then
  echo "Restarting deployment/vllm and deployment/llm-worker..."
  kubectl rollout restart deployment/vllm -n swcp || true
  kubectl rollout restart deployment/llm-worker -n swcp || true
elif [ "$TARGET_SERVICE" != "postgres" ]; then
  echo "Restarting deployment/$TARGET_SERVICE..."
  kubectl rollout restart "deployment/$TARGET_SERVICE" -n swcp || true
fi

echo "=== Checking rollout status ==="
if [ "$TARGET_SERVICE" = "all" ]; then
  kubectl rollout status deployment/core-api -n swcp --timeout=180s || true
  kubectl rollout status deployment/api-gateway -n swcp --timeout=120s || true
  kubectl rollout status deployment/c-sandbox -n swcp --timeout=120s || true
  kubectl rollout status deployment/frontend -n swcp --timeout=120s || true
elif [ "$TARGET_SERVICE" = "c-sandbox" ] || [ "$TARGET_SERVICE" = "c-sandbox-worker" ] || [ "$TARGET_SERVICE" = "c-sandbox-runtime" ]; then
  kubectl rollout status deployment/c-sandbox -n swcp --timeout=120s || true
elif [ "$TARGET_SERVICE" = "vllm" ]; then
  kubectl rollout status deployment/llm-worker -n swcp --timeout=120s || true
  kubectl rollout status deployment/vllm -n swcp --timeout=300s || true
elif [ "$TARGET_SERVICE" != "postgres" ]; then
  kubectl rollout status "deployment/$TARGET_SERVICE" -n swcp --timeout=120s || true
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

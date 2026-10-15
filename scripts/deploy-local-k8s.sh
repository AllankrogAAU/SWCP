#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

if ! kubectl get deployment metrics-server -n kube-system -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -q -- '--kubelet-insecure-tls'; then
  kubectl patch deployment metrics-server -n kube-system --type='json' -p='[{"op": "add", "path": "/spec/template/spec/containers/0/args/-", "value": "--kubelet-insecure-tls"}]'
fi

kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.12.1/deploy/static/provider/cloud/deploy.yaml
kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx --timeout=180s

docker build -t ghcr.io/allankrogaau/core-api:latest -f apps/coreApi/Dockerfile .
docker build -t ghcr.io/allankrogaau/api-gateway:latest -f apps/apiGateway/Dockerfile .
docker build -t ghcr.io/allankrogaau/c-sandbox-worker:latest -f apps/cSandbox/Dockerfile.worker apps/cSandbox
docker build -t ghcr.io/allankrogaau/c-sandbox-runtime:latest -f apps/cSandbox/Dockerfile apps/cSandbox
docker build -t ghcr.io/allankrogaau/llm-worker:latest -f apps/llmWorker/Dockerfile .
docker build -t ghcr.io/allankrogaau/frontend:latest -f apps/frontend/Dockerfile apps/frontend
docker build -t ghcr.io/allankrogaau/postgres:latest -f apps/postgres/Dockerfile apps/postgres

kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/01-configmaps-secrets.yaml

if ! kubectl get storageclass standard >/dev/null 2>&1; then
  PROVISIONER="$(kubectl get storageclass -o jsonpath='{.items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")].provisioner}')"
  if [ -z "$PROVISIONER" ]; then
    echo "No 'standard' storage class and no default storage class to copy a provisioner from" >&2
    exit 1
  fi
  kubectl apply -f - <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: standard
provisioner: ${PROVISIONER}
reclaimPolicy: Delete
volumeBindingMode: Immediate
EOF
fi

kubectl apply -f k8s/02-postgres.yaml
kubectl apply -f k8s/03-nats.yaml
kubectl apply -f k8s/04-core-api.yaml
kubectl apply -f k8s/

if [ -f "$ROOT/.env" ]; then
  PATCH_ENTRIES=""
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    if [[ "$line" =~ ^[[:space:]]*AzureAIFoundry_*Models_*([A-Za-z0-9]+)_*ApiKey[[:space:]]*=[[:space:]]*(.+)$ ]]; then
      PATCH_ENTRIES+="\"AzureAIFoundry__Models__${BASH_REMATCH[1]}__ApiKey\":\"${BASH_REMATCH[2]}\","
    fi
  done < "$ROOT/.env"
  if [ -n "$PATCH_ENTRIES" ]; then
    kubectl patch secret llm-worker-secrets -n swcp --type=merge -p "{\"stringData\":{${PATCH_ENTRIES%,}}}"
    kubectl rollout restart deployment/llm-worker -n swcp
  fi
fi

if [ "${SWCP_GVISOR:-0}" = "1" ]; then
  echo "SWCP_GVISOR=1: sandbox pods will use the gvisor RuntimeClass (node needs runsc + label swcp.io/gvisor=true)."
else
  echo "WARNING: local mode, sandbox pods run WITHOUT gVisor. Set SWCP_GVISOR=1 on a node with gVisor installed." >&2
  kubectl patch configmap c-sandbox-config -n swcp --type=merge -p '{"data":{"SANDBOX_RUNTIME_CLASS":""}}'
fi
kubectl rollout restart deployment/c-sandbox -n swcp
kubectl rollout status deployment/c-sandbox -n swcp --timeout=120s || {
  echo "c-sandbox rollout did not complete within 120s" >&2
  kubectl get pods -n swcp -l app.kubernetes.io/name=c-sandbox >&2 || true
  exit 1
}

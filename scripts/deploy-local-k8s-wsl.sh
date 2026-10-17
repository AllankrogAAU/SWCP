#!/usr/bin/env bash
set -euo pipefail

# Brings up a *real* single-node Kubernetes cluster (k3s) with working gVisor,
# inside a normal Linux distro (WSL2 Ubuntu/Debian, or any bare Linux box) --
# unlike Docker Desktop's bundled Kubernetes, which runs in a sealed VM we
# can't register gVisor's runtime into. Run as root (the Windows wrapper
# script does this for you via `wsl -u root`).
#
# Requires: that distro's `docker` CLI talking to Docker Desktop (enable
# Docker Desktop > Settings > Resources > WSL Integration for this distro) so
# images can be built with the existing Dockerfiles, then imported into k3s's
# own containerd image store with `k3s ctr images import` (k3s does not share
# Docker Desktop's image store).

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root (e.g. 'sudo bash scripts/deploy-local-k8s-wsl.sh')." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker CLI not found. Enable Docker Desktop > Settings > Resources > WSL Integration for this distro." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "docker CLI found but cannot reach a daemon. Make sure Docker Desktop is running and WSL integration is enabled for this distro." >&2
  exit 1
fi

# Minimal-install distros (e.g. a fresh WSL Debian) are missing bzip2, which
# install-gvisor-node.sh needs to unpack gvisor.tar.bz2.
missing_tools=()
for tool in curl tar bzip2 sha512sum; do
  command -v "$tool" >/dev/null 2>&1 || missing_tools+=("$tool")
done
if [ "${#missing_tools[@]}" -gt 0 ] && command -v apt-get >/dev/null 2>&1; then
  echo "Installing missing tools: ${missing_tools[*]}..."
  apt-get update -qq
  apt-get install -y -qq "${missing_tools[@]}"
elif [ "${#missing_tools[@]}" -gt 0 ]; then
  echo "Missing required tools: ${missing_tools[*]}. Install them with your distro's package manager and re-run." >&2
  exit 1
fi

# Docker Desktop's WSL integration bind-mounts its own install directory
# into every integrated distro at /Docker/host via a 9p share. If Docker
# Desktop is installed under a path containing spaces (the default,
# "C:\Program Files\Docker\..."), that mount's options string also contains
# spaces, which breaks kubelet's /proc/mounts parser at startup (it expects
# exactly 6 fields and sees 7) -- see kubernetes/kubernetes#135554. This
# mount isn't needed for the `docker` CLI/build to keep working against
# Docker Desktop's daemon (only used for Desktop's own internal plumbing),
# so it's safe to drop for k3s's sake. It's re-created only when the distro
# itself restarts, so this is re-checked (idempotently) on every run.
if mountpoint -q /Docker/host 2>/dev/null; then
  echo "Unmounting /Docker/host (breaks kubelet's /proc/mounts parsing, not needed for docker CLI)..."
  umount /Docker/host
fi

if ! command -v k3s >/dev/null 2>&1; then
  echo "Installing k3s (bundled containerd, no Traefik -- we use our own ingress-nginx manifests)..."
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable traefik --write-kubeconfig-mode 644" sh -
fi

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

echo "Waiting for k3s node to be Ready..."
for _ in $(seq 1 60); do
  if kubectl get nodes 2>/dev/null | grep -q " Ready "; then
    break
  fi
  sleep 2
done
kubectl wait --for=condition=Ready node --all --timeout=60s

# install-gvisor-node.sh applies k8s/06-sandbox-runtime.yaml, which creates a
# ServiceAccount in the "swcp" namespace -- that namespace must already exist.
kubectl apply -f k8s/00-namespace.yaml

echo "Installing gVisor and verifying it actually works (this restarts k3s once)..."
bash "$ROOT/scripts/install-gvisor-node.sh"

echo "Waiting for k3s to be back up after the gVisor-install restart..."
for _ in $(seq 1 60); do
  if kubectl get nodes 2>/dev/null | grep -q " Ready "; then
    break
  fi
  sleep 2
done
kubectl wait --for=condition=Ready node --all --timeout=60s

kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
if ! kubectl get deployment metrics-server -n kube-system -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -q -- '--kubelet-insecure-tls'; then
  kubectl patch deployment metrics-server -n kube-system --type='json' -p='[{"op": "add", "path": "/spec/template/spec/containers/0/args/-", "value": "--kubelet-insecure-tls"}]'
fi

kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.12.1/deploy/static/provider/cloud/deploy.yaml

# k3s's bundled servicelb (klipper) fulfils the controller's LoadBalancer
# Service purely via iptables DNAT inside the WSL VM's network namespace --
# there's no real listening socket for WSL2's automatic localhost-forwarding
# to discover, so Windows can't reach it via http://localhost. Switching the
# controller to hostNetwork makes nginx bind 80/443 directly on the node
# (a real socket), which WSL2 does forward to Windows localhost. This also
# frees klipper's DaemonSet pod from those ports, so the Service must stop
# being type LoadBalancer first (klipper only manages LB-type Services).
if [ "$(kubectl get service ingress-nginx-controller -n ingress-nginx -o jsonpath='{.spec.type}')" != "ClusterIP" ]; then
  kubectl patch service ingress-nginx-controller -n ingress-nginx --type=merge -p '{"spec":{"type":"ClusterIP"}}'
fi
if [ "$(kubectl get deployment ingress-nginx-controller -n ingress-nginx -o jsonpath='{.spec.template.spec.hostNetwork}')" != "true" ]; then
  kubectl patch deployment ingress-nginx-controller -n ingress-nginx --type=json -p='[{"op":"add","path":"/spec/template/spec/hostNetwork","value":true},{"op":"add","path":"/spec/template/spec/dnsPolicy","value":"ClusterFirstWithHostNet"}]'
fi
# hostNetwork binds the node's actual port 80/443, so two replicas can never
# coexist on this single-node cluster: the default RollingUpdate strategy
# tries to surge a new pod before the old one finishes terminating, and the
# new pod then sits Pending forever ("didn't have free ports"). Recreate
# avoids that by tearing the old pod down fully before starting the new one.
if [ "$(kubectl get deployment ingress-nginx-controller -n ingress-nginx -o jsonpath='{.spec.strategy.type}')" != "Recreate" ]; then
  kubectl patch deployment ingress-nginx-controller -n ingress-nginx --type=json -p='[{"op":"replace","path":"/spec/strategy","value":{"type":"Recreate"}}]'
fi
kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx --timeout=180s

images=(
  "ghcr.io/allankrogaau/core-api:latest|apps/coreApi/Dockerfile|."
  "ghcr.io/allankrogaau/api-gateway:latest|apps/apiGateway/Dockerfile|."
  "ghcr.io/allankrogaau/c-sandbox-worker:latest|apps/cSandbox/Dockerfile.worker|apps/cSandbox"
  "ghcr.io/allankrogaau/c-sandbox-runtime:latest|apps/cSandbox/Dockerfile|apps/cSandbox"
  "ghcr.io/allankrogaau/llm-worker:latest|apps/llmWorker/Dockerfile|."
  "ghcr.io/allankrogaau/frontend:latest|apps/frontend/Dockerfile|apps/frontend"
  "ghcr.io/allankrogaau/postgres:latest|apps/postgres/Dockerfile|apps/postgres"
)
for entry in "${images[@]}"; do
  IFS='|' read -r image dockerfile context <<< "$entry"
  echo "Building $image..."
  docker build -t "$image" -f "$dockerfile" "$context"
  echo "Importing $image into k3s's containerd..."
  docker save "$image" | k3s ctr images import -
done

kubectl apply -f k8s/01-configmaps-secrets.yaml

if ! kubectl get storageclass standard >/dev/null 2>&1; then
  PROVISIONER="$(kubectl get storageclass -o jsonpath='{.items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")].provisioner}')"
  if [ -z "$PROVISIONER" ]; then
    echo "No 'standard' storage class and no default storage class to copy a provisioner from" >&2
    exit 1
  fi
  # Match the default class's volumeBindingMode rather than hardcoding
  # Immediate: k3s's bundled "local-path" provisioner requires
  # WaitForFirstConsumer (it provisions on whichever node the pod is
  # scheduled to) and errors out ("no node was specified") under Immediate.
  BINDING_MODE="$(kubectl get storageclass -o jsonpath='{.items[?(@.metadata.annotations.storageclass\.kubernetes\.io/is-default-class=="true")].volumeBindingMode}')"
  BINDING_MODE="${BINDING_MODE:-Immediate}"
  kubectl apply -f - <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: standard
provisioner: ${PROVISIONER}
reclaimPolicy: Delete
volumeBindingMode: ${BINDING_MODE}
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

kubectl rollout restart deployment/c-sandbox -n swcp
kubectl rollout status deployment/c-sandbox -n swcp --timeout=180s || {
  echo "c-sandbox rollout did not complete within 180s" >&2
  kubectl get pods -n swcp -l app.kubernetes.io/name=c-sandbox >&2 || true
  exit 1
}

echo ""
echo "Done. Open http://swcp.local (add '127.0.0.1 swcp.local' to the Windows hosts file if you haven't)."
echo "Sandbox pods run with runtimeClassName=gvisor; verify any time with:"
echo "  kubectl get pods -n swcp-sandbox -o jsonpath='{.items[*].spec.runtimeClassName}'"

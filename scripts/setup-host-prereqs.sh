#!/usr/bin/env bash
set -euo pipefail

# This script sets up host prerequisites for SWCP on native Ubuntu:
# 1. Installs docker.io and adds the target user to the docker group.
# 2. Installs k3s (with traefik disabled) and configures kubeconfig.
# 3. Configures UFW firewall rules if active.
# 4. Installs gVisor (runsc) and registers it into k3s containerd.
#
# MUST BE RUN AS ROOT: sudo bash scripts/setup-host-prereqs.sh

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: This script must be run as root (e.g. 'sudo bash scripts/setup-host-prereqs.sh')." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="/usr/local/bin:$PATH"

TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || whoami)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

echo "=== 1. Installing required host packages (docker.io, bzip2, tar, curl) ==="
apt-get update -qq
apt-get install -y -qq docker.io bzip2 tar curl

systemctl enable --now docker

if getent group docker >/dev/null; then
  usermod -aG docker "$TARGET_USER"
  echo "Added $TARGET_USER to docker group."
fi

# Allow passwordless sudo for development convenience
if [ ! -f "/etc/sudoers.d/$TARGET_USER" ]; then
  echo "$TARGET_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/$TARGET_USER"
  chmod 0440 "/etc/sudoers.d/$TARGET_USER"
  echo "Configured passwordless sudo for $TARGET_USER in /etc/sudoers.d/$TARGET_USER"
fi

echo "=== 2. Installing k3s lightweight Kubernetes ==="
if ! command -v k3s >/dev/null 2>&1; then
  curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable traefik --write-kubeconfig-mode 644" sh -
else
  echo "k3s is already installed."
fi

export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# Configure user's .kube/config
if [ -n "$TARGET_HOME" ] && [ -d "$TARGET_HOME" ]; then
  mkdir -p "$TARGET_HOME/.kube"
  cp /etc/rancher/k3s/k3s.yaml "$TARGET_HOME/.kube/config"
  chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.kube"
  chmod 600 "$TARGET_HOME/.kube/config"
  echo "Configured kubeconfig for user $TARGET_USER at $TARGET_HOME/.kube/config"
fi

echo "Waiting for k3s node to be Ready..."
for _ in $(seq 1 60); do
  if k3s kubectl get nodes 2>/dev/null | grep -q " Ready "; then
    break
  fi
  sleep 2
done
k3s kubectl wait --for=condition=Ready node --all --timeout=60s

echo "=== 3. Configuring firewall (UFW) ==="
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  echo "Allowing ports 80, 443, and 6443 in UFW..."
  ufw allow 80/tcp comment "HTTP Ingress" || true
  ufw allow 443/tcp comment "HTTPS Ingress" || true
  ufw allow 6443/tcp comment "k3s API" || true
else
  echo "UFW is inactive or not present; skipping firewall adjustment."
fi

echo "=== 4. Installing and verifying gVisor (runsc) ==="
# k8s/00-namespace.yaml is required before 06-sandbox-runtime.yaml (applied by install-gvisor-node.sh)
k3s kubectl apply -f "$ROOT/k8s/00-namespace.yaml"

bash "$ROOT/scripts/install-gvisor-node.sh"

echo "Waiting for k3s to be back up after gVisor containerd restart..."
for _ in $(seq 1 60); do
  if k3s kubectl get nodes 2>/dev/null | grep -q " Ready "; then
    break
  fi
  sleep 2
done
k3s kubectl wait --for=condition=Ready node --all --timeout=60s

echo ""
echo "=========================================================="
echo "Host prerequisites setup completed successfully!"
echo "Node $(hostname) is ready with gVisor enabled."
echo "You can now run: bash scripts/deploy-linux-k8s.sh"
echo "=========================================================="

#!/usr/bin/env bash
set -euo pipefail

# This script sets up host prerequisites for SWCP on native Ubuntu:
# 1. Installs docker.io and adds the target user to the docker group.
# 2. Installs k3s as either 'control-plane' (server) or 'worker' (agent).
# 3. Configures UFW firewall rules if active (including Flannel VXLAN & kubelet).
# 4. Installs gVisor (runsc) and registers it into k3s containerd.
#
# MUST BE RUN AS ROOT:
#   sudo bash scripts/setup-host-prereqs.sh [--role control-plane|worker]
#
# Configuration can be provided via .env, environment variables, or CLI flags:
#   CLUSTER_ROLE=control-plane|worker
#   CLUSTER_NODE_NAME=<node-name>         (optional, defaults to OS hostname)
#   CLUSTER_IFACE=enp4s0|tailscale0
#   CLUSTER_NODE_IP=<this-machine-ip>
#   CONTROL_PLANE_IP=<control-plane-ip>   (required for worker)
#   K3S_TOKEN=<node-token>                (required for worker)

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: This script must be run as root (e.g. 'sudo bash scripts/setup-host-prereqs.sh')." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="/usr/local/bin:$PATH"

# Load cluster defaults from .env if present (without overriding already-exported env vars)
if [ -f "$ROOT/.env" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      val="${BASH_REMATCH[2]}"
      val="${val%%#*}"
      val="$(echo "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
      if [ -z "${!key+x}" ] || [ -z "${!key}" ]; then
        export "$key=$val"
      fi
    fi
  done < "$ROOT/.env"
fi

CLUSTER_ROLE="${CLUSTER_ROLE:-control-plane}"
CLUSTER_NODE_NAME="${CLUSTER_NODE_NAME:-}"
CLUSTER_IFACE="${CLUSTER_IFACE:-}"
CLUSTER_NODE_IP="${CLUSTER_NODE_IP:-}"
CONTROL_PLANE_IP="${CONTROL_PLANE_IP:-}"
K3S_TOKEN="${K3S_TOKEN:-}"

usage() {
  cat <<EOF
Usage: sudo bash $0 [options]

Options:
  --role <control-plane|worker>   Node role (default: \$CLUSTER_ROLE or control-plane)
  --node-name <name>              Kubernetes node name / Grafana label (default: OS hostname)
  --iface <interface>             Network interface for k3s/Flannel (e.g. enp4s0, tailscale0)
  --node-ip <ip>                  This node's IP on the cluster network
  --control-plane-ip <ip>         Control-plane IP (required for worker)
  --token <token>                 k3s join token from control-plane (required for worker)
  -h, --help                      Show this help message
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --role)
      CLUSTER_ROLE="$2"; shift 2 ;;
    --node-name)
      CLUSTER_NODE_NAME="$2"; shift 2 ;;
    --iface)
      CLUSTER_IFACE="$2"; shift 2 ;;
    --node-ip)
      CLUSTER_NODE_IP="$2"; shift 2 ;;
    --control-plane-ip)
      CONTROL_PLANE_IP="$2"; shift 2 ;;
    --token)
      K3S_TOKEN="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1 ;;
  esac
done

if [ "$CLUSTER_ROLE" != "control-plane" ] && [ "$CLUSTER_ROLE" != "worker" ]; then
  echo "Error: Invalid role '$CLUSTER_ROLE'. Must be 'control-plane' or 'worker'." >&2
  exit 1
fi

# Auto-detect CLUSTER_NODE_IP from CLUSTER_IFACE if iface is given and node IP is unset
if [ -z "$CLUSTER_NODE_IP" ] && [ -n "$CLUSTER_IFACE" ]; then
  CLUSTER_NODE_IP="$(ip -4 -o addr show dev "$CLUSTER_IFACE" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1 || true)"
  if [ -z "$CLUSTER_NODE_IP" ]; then
    echo "Error: Could not detect IPv4 address for interface '$CLUSTER_IFACE'." >&2
    exit 1
  fi
fi

if [ "$CLUSTER_ROLE" = "worker" ]; then
  if [ -z "$CONTROL_PLANE_IP" ] || [ -z "$K3S_TOKEN" ]; then
    echo "Error: Worker setup requires CONTROL_PLANE_IP and K3S_TOKEN (via .env, env vars, or --control-plane-ip / --token)." >&2
    exit 1
  fi
fi

TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || whoami)}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"

echo "=== Host Setup: role=${CLUSTER_ROLE}, node-name=${CLUSTER_NODE_NAME:-hostname}, iface=${CLUSTER_IFACE:-auto}, node-ip=${CLUSTER_NODE_IP:-auto} ==="

echo "=== 1. Installing required host packages (docker.io, bzip2, tar, curl, gnupg) ==="
apt-get update -qq
apt-get install -y -qq docker.io bzip2 tar curl gnupg

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

echo "=== 2. Installing/configuring k3s (${CLUSTER_ROLE}) ==="
sudo_k3s_config="/etc/rancher/k3s/config.yaml"
mkdir -p /etc/rancher/k3s
PREV_NODE_NAME="$(grep -E '^node-name:' "$sudo_k3s_config" 2>/dev/null | sed -E 's/^node-name:[[:space:]]*"?([^"]*)"?/\1/' || true)"
{
  [ -n "$CLUSTER_NODE_NAME" ] && echo "node-name: \"${CLUSTER_NODE_NAME}\""
  [ -n "$CLUSTER_NODE_IP" ] && echo "node-ip: \"${CLUSTER_NODE_IP}\""
  if [ "$CLUSTER_ROLE" = "control-plane" ]; then
    [ -n "$CLUSTER_NODE_IP" ] && echo "advertise-address: \"${CLUSTER_NODE_IP}\""
    echo "disable:"
    echo "  - traefik"
    echo "write-kubeconfig-mode: \"0644\""
  fi
  [ -n "$CLUSTER_IFACE" ] && echo "flannel-iface: \"${CLUSTER_IFACE}\""
} > "$sudo_k3s_config"

strip_duplicate_k3s_unit_flags() {
  local unit_file="$1"
  local subcmd="$2"
  if [ -f "$unit_file" ] && grep -qE -- '--(node-ip|flannel-iface|node-name|advertise-address)=' "$unit_file"; then
    sed -i '/^ExecStart=/,$d' "$unit_file"
    echo "ExecStart=/usr/local/bin/k3s ${subcmd}" >> "$unit_file"
    systemctl daemon-reload
  fi
}

if [ "$CLUSTER_ROLE" = "control-plane" ]; then
  if ! systemctl list-unit-files k3s.service 2>/dev/null | grep -q k3s.service; then
    curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server" sh -
  else
    strip_duplicate_k3s_unit_flags /etc/systemd/system/k3s.service server
    echo "k3s server already installed; restarting to apply config.yaml..."
    systemctl restart k3s
  fi

  export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

  if [ -n "$TARGET_HOME" ] && [ -d "$TARGET_HOME" ]; then
    mkdir -p "$TARGET_HOME/.kube"
    cp /etc/rancher/k3s/k3s.yaml "$TARGET_HOME/.kube/config"
    chown -R "$TARGET_USER:$TARGET_USER" "$TARGET_HOME/.kube"
    chmod 600 "$TARGET_HOME/.kube/config"
    echo "Configured kubeconfig for user $TARGET_USER at $TARGET_HOME/.kube/config"
  fi

  echo "Waiting for k3s control-plane node to be Ready..."
  for _ in $(seq 1 60); do
    if k3s kubectl get nodes 2>/dev/null | grep -q " Ready "; then
      break
    fi
    sleep 2
  done
  k3s kubectl wait --for=condition=Ready node --all --timeout=60s
else
  if [ -n "$CLUSTER_NODE_NAME" ] && { [ "$PREV_NODE_NAME" != "$CLUSTER_NODE_NAME" ] || ! systemctl is-active --quiet k3s-agent 2>/dev/null; }; then
    rm -f /etc/rancher/node/password
  fi

  if ! systemctl list-unit-files k3s-agent.service 2>/dev/null | grep -q k3s-agent.service; then
    curl -sfL https://get.k3s.io | \
      K3S_URL="https://${CONTROL_PLANE_IP}:6443" \
      K3S_TOKEN="${K3S_TOKEN}" \
      INSTALL_K3S_EXEC="agent" \
      sh -
  else
    strip_duplicate_k3s_unit_flags /etc/systemd/system/k3s-agent.service agent
    echo "k3s-agent already installed; restarting to apply config.yaml..."
    systemctl restart k3s-agent
  fi
fi

echo "=== 3. Configuring firewall (UFW) ==="
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  echo "Allowing cluster ports in UFW..."
  ufw allow 80/tcp comment "HTTP Ingress" || true
  ufw allow 443/tcp comment "HTTPS Ingress" || true
  ufw allow 8472/udp comment "k3s Flannel VXLAN" || true
  ufw allow 10250/tcp comment "k3s kubelet" || true
  if [ "$CLUSTER_ROLE" = "control-plane" ]; then
    ufw allow 6443/tcp comment "k3s API" || true
  fi
else
  echo "UFW is inactive or not present; skipping firewall adjustment."
fi

echo "=== 4. Installing NVIDIA Container Toolkit (if GPU present) ==="
if (command -v nvidia-smi >/dev/null 2>&1 || lspci 2>/dev/null | grep -qi nvidia); then
  if ! command -v nvidia-ctk >/dev/null 2>&1; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
      sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
      tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
    apt-get update -qq
    apt-get install -y -qq nvidia-container-toolkit
    if [ "$CLUSTER_ROLE" = "control-plane" ]; then
      systemctl restart k3s
    else
      systemctl restart k3s-agent
    fi
  else
    echo "nvidia-container-toolkit is already installed."
  fi
else
  echo "No NVIDIA GPU detected; skipping nvidia-container-toolkit."
fi

echo "=== 5. Installing and verifying gVisor (runsc) ==="
if [ "$CLUSTER_ROLE" = "control-plane" ]; then
  k3s kubectl apply -f "$ROOT/k8s/00-namespace.yaml"
fi

echo "Waiting for k3s containerd config to be generated..."
for _ in $(seq 1 30); do
  if [ -s /var/lib/rancher/k3s/agent/etc/containerd/config.toml ]; then
    break
  fi
  sleep 2
done

NODE_NAME="${CLUSTER_NODE_NAME:-$(hostname | tr '[:upper:]' '[:lower:]')}" bash "$ROOT/scripts/install-gvisor-node.sh"

if [ "$CLUSTER_ROLE" = "control-plane" ]; then
  echo "Waiting for k3s to be back up after gVisor containerd restart..."
  for _ in $(seq 1 60); do
    if k3s kubectl get nodes 2>/dev/null | grep -q " Ready "; then
      break
    fi
    sleep 2
  done
  k3s kubectl wait --for=condition=Ready node --all --timeout=60s

  # Label nodes with swcp.io/gpu=true (from GPU_NODES in .env, or all known nodes if an NVIDIA GPU is present)
  if [ -n "${GPU_NODES:-}" ]; then
    IFS=',' read -ra gpu_node_list <<< "$GPU_NODES"
    for n in "${gpu_node_list[@]}"; do
      n="$(echo "$n" | xargs)"
      [ -n "$n" ] && k3s kubectl label node "$n" swcp.io/gpu=true --overwrite >/dev/null 2>&1 || true
    done
  elif command -v nvidia-smi >/dev/null 2>&1 || lspci 2>/dev/null | grep -qi nvidia; then
    for n in $(k3s kubectl get nodes -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
      k3s kubectl label node "$n" swcp.io/gpu=true --overwrite >/dev/null 2>&1 || true
    done
  fi

  if [ -n "${GVISOR_NODES:-}" ]; then
    IFS=',' read -ra gv_node_list <<< "$GVISOR_NODES"
    for n in "${gv_node_list[@]}"; do
      n="$(echo "$n" | xargs)"
      [ -n "$n" ] && k3s kubectl label node "$n" swcp.io/gvisor=true --overwrite >/dev/null 2>&1 || true
    done
  fi

  echo ""
  echo "=========================================================="
  echo "Control-plane setup completed on $(hostname)!"
  echo "Join token for worker nodes:"
  cat /var/lib/rancher/k3s/server/node-token
  echo "Deploy application with: bash scripts/deploy-linux-k8s.sh"
  echo "=========================================================="
else
  NODE_HOST="${CLUSTER_NODE_NAME:-$(hostname | tr '[:upper:]' '[:lower:]')}"
  echo ""
  echo "=========================================================="
  echo "Worker node setup completed on $(hostname)!"
  echo "Next steps:"
  echo "  1. Build/cache local images on this worker: bash scripts/deploy-linux-k8s.sh --images-only"
  echo "  2. On the control-plane node, run:"
  echo "     kubectl get nodes -o wide"
  echo "     kubectl label node ${NODE_HOST} swcp.io/gvisor=true swcp.io/gpu=true --overwrite"
  echo "     (or just run bash scripts/deploy-linux-k8s.sh on the control plane to auto-label GPU nodes)"
  echo "=========================================================="
fi

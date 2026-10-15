set -euo pipefail

NODE_NAME="${NODE_NAME:-$(hostname)}"
ARCH="$(uname -m)"
URL="https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"

curl -fsSLO "${URL}/runsc" -O "${URL}/runsc.sha512" \
     -O "${URL}/containerd-shim-runsc-v1" -O "${URL}/containerd-shim-runsc-v1.sha512"
sha512sum -c runsc.sha512 -c containerd-shim-runsc-v1.sha512
install -m 0755 runsc containerd-shim-runsc-v1 /usr/local/bin/

/usr/local/bin/runsc install
systemctl restart containerd

if command -v kubectl >/dev/null 2>&1; then
  kubectl label node "$NODE_NAME" swcp.io/gvisor=true --overwrite
else
  echo "kubectl not found: run 'kubectl label node ${NODE_NAME} swcp.io/gvisor=true' from a machine with cluster access."
fi
echo "gVisor installed. Verify with: kubectl apply -f k8s/06-sandbox-runtime.yaml && kubectl run -n swcp-sandbox --rm -it --restart=Never --overrides='{\"spec\":{\"runtimeClassName\":\"gvisor\"}}' t --image=busybox -- dmesg"

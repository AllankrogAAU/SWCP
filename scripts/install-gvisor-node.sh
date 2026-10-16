#!/usr/bin/env bash
set -euo pipefail

RUNTIME_HANDLER="runsc"
CONTAINERD_CONFIG="/etc/containerd/config.toml"
NODE_NAME="${NODE_NAME:-$(hostname)}"
ARCH="$(uname -m)"
URL="https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"

curl -fsSLO "${URL}/gvisor.tar.bz2" -O "${URL}/gvisor.tar.bz2.sha512"
sha512sum -c gvisor.tar.bz2.sha512
tar -xjf gvisor.tar.bz2 -C /usr/local/bin
chmod -R a+rx /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1 /usr/local/bin/gvisor-bin

if [ ! -s "$CONTAINERD_CONFIG" ]; then
  mkdir -p "$(dirname "$CONTAINERD_CONFIG")"
  containerd config default > "$CONTAINERD_CONFIG"
fi

runtimes_prefix="$(containerd config dump \
  | grep -oE '\[plugins\."[^"]*"\.containerd\.runtimes\.runc\]' \
  | sed -E 's/\.runc\]$//' \
  | head -n1)"

if [ -z "$runtimes_prefix" ]; then
  echo "Could not locate the containerd CRI runtimes table in 'containerd config dump'; aborting without touching $CONTAINERD_CONFIG" >&2
  exit 1
fi

if ! grep -qF "${runtimes_prefix}.${RUNTIME_HANDLER}]" "$CONTAINERD_CONFIG"; then
  cp "$CONTAINERD_CONFIG" "${CONTAINERD_CONFIG}.bak.$(date +%s)"
  {
    echo ""
    echo "${runtimes_prefix}.${RUNTIME_HANDLER}]"
    echo "  runtime_type = \"io.containerd.runsc.v1\""
  } >> "$CONTAINERD_CONFIG"
fi

systemctl restart containerd

if ! command -v kubectl >/dev/null 2>&1; then
  echo "gVisor binaries installed and containerd configured, but kubectl was not found." >&2
  echo "From a machine with cluster access, run:" >&2
  echo "  kubectl label node ${NODE_NAME} swcp.io/gvisor=true --overwrite" >&2
  echo "then re-run this script's verification separately; do not assume gVisor works until it is checked." >&2
  exit 0
fi

kubectl label node "$NODE_NAME" swcp.io/gvisor=true --overwrite
kubectl apply -f "${SCRIPT_DIR}/../k8s/06-sandbox-runtime.yaml" >/dev/null

echo "Verifying gVisor actually runs on ${NODE_NAME} (this creates and deletes a throwaway pod)..."
probe_name="gvisor-verify-$(date +%s)"
cat <<EOF | kubectl apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: ${probe_name}
  namespace: swcp-sandbox
  labels:
    app.kubernetes.io/name: gvisor-verify
spec:
  runtimeClassName: gvisor
  nodeSelector:
    swcp.io/gvisor: "true"
  automountServiceAccountToken: false
  restartPolicy: Never
  terminationGracePeriodSeconds: 0
  securityContext:
    runAsNonRoot: true
    runAsUser: 65534
    runAsGroup: 65534
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: verify
      image: busybox
      command: ["dmesg"]
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
EOF

cleanup_probe() {
  kubectl delete pod "$probe_name" -n swcp-sandbox --ignore-not-found --grace-period=0 --force >/dev/null 2>&1 || true
}
trap cleanup_probe EXIT

deadline=$((SECONDS + 60))
phase=""
while [ "$SECONDS" -lt "$deadline" ]; do
  phase="$(kubectl get pod "$probe_name" -n swcp-sandbox -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  if [ "$phase" = "Succeeded" ] || [ "$phase" = "Failed" ]; then
    break
  fi
  sleep 2
done

verify_output="$(kubectl logs "$probe_name" -n swcp-sandbox 2>&1 || true)"

if [ "$phase" != "Succeeded" ]; then
  kubectl label node "$NODE_NAME" swcp.io/gvisor- >/dev/null 2>&1 || true
  echo "gVisor verification FAILED on ${NODE_NAME}: probe pod never reached Succeeded (phase=${phase:-unknown})." >&2
  echo "Removed the swcp.io/gvisor label so sandbox pods are not scheduled here until this is fixed." >&2
  echo "Describe the probe pod and check containerd logs on this node for details:" >&2
  echo "  kubectl describe pod ${probe_name} -n swcp-sandbox" >&2
  echo "$verify_output" >&2
  exit 1
fi

if ! echo "$verify_output" | grep -q "Starting gVisor"; then
  kubectl label node "$NODE_NAME" swcp.io/gvisor- >/dev/null 2>&1 || true
  echo "gVisor verification FAILED on ${NODE_NAME}: pod ran, but dmesg did not show gVisor's boot banner, so it likely ran under the plain runc runtime instead of runsc." >&2
  echo "Removed the swcp.io/gvisor label so sandbox pods are not scheduled here until this is fixed." >&2
  echo "Output was:" >&2
  echo "$verify_output" >&2
  exit 1
fi

echo "gVisor verified: pods with runtimeClassName=gvisor on ${NODE_NAME} are actually running inside runsc."

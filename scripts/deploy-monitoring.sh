#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -z "${KUBECONFIG:-}" ]; then
  if [ -f "$HOME/.kube/config" ]; then
    export KUBECONFIG="$HOME/.kube/config"
  elif [ -f "/etc/rancher/k3s/k3s.yaml" ]; then
    export KUBECONFIG="/etc/rancher/k3s/k3s.yaml"
  fi
fi

echo "=== Deploying Unified Monitoring Stack (Alloy, Prometheus, Tempo, Loki, Grafana) ==="
kubectl apply -f k8s/monitoring/00-namespace.yaml
kubectl apply -f k8s/monitoring/01-loki.yaml
kubectl apply -f k8s/monitoring/02-tempo.yaml
kubectl apply -f k8s/monitoring/03-prometheus.yaml
kubectl apply -f k8s/monitoring/04-alloy.yaml

# Create/update dashboards ConfigMap directly from json file
if [ -f "$ROOT/k8s/monitoring/dashboards/pipeline-timings.json" ]; then
  kubectl create configmap grafana-dashboards-swcp -n monitoring \
    --from-file=pipeline-timings.json="$ROOT/k8s/monitoring/dashboards/pipeline-timings.json" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

kubectl apply -f k8s/monitoring/05-grafana.yaml

echo "Waiting for monitoring components to roll out..."
kubectl rollout status statefulset/loki -n monitoring --timeout=120s || true
kubectl rollout status statefulset/tempo -n monitoring --timeout=120s || true
kubectl rollout status statefulset/prometheus -n monitoring --timeout=120s || true
kubectl rollout status daemonset/alloy -n monitoring --timeout=120s || true
kubectl rollout status deployment/grafana -n monitoring --timeout=120s || true

echo ""
echo "=== Current Pods in monitoring namespace ==="
kubectl get pods -n monitoring -o wide

TS_IP="$(tailscale ip -4 2>/dev/null || echo "100.111.90.116")"
NODE_NAME="$(hostname | tr '[:upper:]' '[:lower:]')"

echo ""
echo "=========================================================="
echo "Unified Monitoring Stack is ready!"
echo "Access Grafana Web UI (anonymous admin enabled) at:"
echo "  http://${TS_IP}:3000/"
echo "  or http://${NODE_NAME}:3000/"
echo "  or http://localhost:3000/"
echo "=========================================================="


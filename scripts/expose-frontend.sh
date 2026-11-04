#!/usr/bin/env bash
set -euo pipefail

# This script configures Tailscale Serve to expose the local Kubernetes Ingress (port 80)
# to your Tailnet devices via direct peer-to-peer HTTPS.

if ! command -v tailscale >/dev/null 2>&1; then
  echo "Error: tailscale CLI not found. Please ensure Tailscale is installed." >&2
  exit 1
fi

TS_STATUS="$(tailscale status --json 2>/dev/null || true)"
if [ -z "$TS_STATUS" ]; then
  echo "Error: Tailscale does not appear to be running or logged in." >&2
  exit 1
fi

TS_IP="$(tailscale ip -4 2>/dev/null || true)"
if [ -z "$TS_IP" ]; then
  echo "Error: Could not retrieve Tailscale IPv4 address. Please ensure Tailscale is connected." >&2
  exit 1
fi

TS_DNS="$(tailscale status --json 2>/dev/null | grep -o '"MagicDNSSuffix": *"[^"]*"' | head -n1 | cut -d'"' -f4 || true)"
NODE_NAME="$(hostname | tr '[:upper:]' '[:lower:]')"
if [ -n "$TS_DNS" ]; then
  FULL_DOMAIN="${NODE_NAME}.${TS_DNS}"
else
  FULL_DOMAIN="${NODE_NAME}"
fi

echo "=== Configuring Tailscale Serve for SWCP Frontend & Backend ==="
echo "Node Name:        $NODE_NAME"
echo "Tailscale IP:     $TS_IP"
echo "Tailscale Domain: $FULL_DOMAIN"
echo ""

# Enable Tailscale Serve forwarding to local port 80
echo "Running: tailscale serve --yes --bg 80"
tailscale serve --yes --bg 80 || {
  echo ""
  echo "Note: If Tailscale Serve prompts to be enabled on your tailnet,"
  echo "visit the link printed above in your browser to authorize it once."
}

echo ""
echo "=== Current Tailscale Serve Status ==="
tailscale serve status

echo ""
echo "=========================================================="
echo "Tailscale Serve is now active!"
echo "Any device in your tailnet can access SWCP at:"
echo "  https://${FULL_DOMAIN}/"
echo "  or http://${TS_IP}/"
echo ""
echo "To disable later, run: tailscale serve reset"
echo "=========================================================="


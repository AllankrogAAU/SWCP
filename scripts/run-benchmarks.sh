#!/usr/bin/env bash
# ==============================================================================
# run-benchmarks.sh
# Builds and runs the containerized SWCP benchmark suite on host network.
# ==============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p "$ROOT/benchmarks/results"

echo "=== Building SWCP Benchmark Container (swcp-benchmarks:latest) ==="
docker build -t swcp-benchmarks:latest -f benchmarks/Dockerfile benchmarks/

DOCKER_TTY=""
if [ -t 0 ]; then
  DOCKER_TTY="-it"
fi

echo "=== Running SWCP Benchmarks ==="
docker run --rm $DOCKER_TTY \
  --network host \
  -v "$ROOT/benchmarks/results:/app/results" \
  swcp-benchmarks:latest "$@"

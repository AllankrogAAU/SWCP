#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -x "$ROOT/.venv-benchmarking2/bin/python" ]; then
  PYTHON_BIN="$ROOT/.venv-benchmarking2/bin/python"
elif [ -x "$HOME/.venvs/swcp-benchmark/bin/python" ]; then
  PYTHON_BIN="$HOME/.venvs/swcp-benchmark/bin/python"
else
  PYTHON_BIN="python3"
fi

if [ "$#" -eq 0 ]; then
  set -- complete
fi

exec "$PYTHON_BIN" -m benchmarking_2 "$@"

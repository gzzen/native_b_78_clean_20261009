#!/usr/bin/env bash
# Path: scripts/run_isolated_docker.sh
# Controller stays on host; every attempt gets its own network-none container.
set -euo pipefail

if [[ $# -lt 1 || ( "$1" != "native" && "$1" != "b" ) ]]; then
  echo "usage: $0 {native|b} [runner arguments...]" >&2
  exit 2
fi

condition="$1"
shift
solver_dir="$(cd "$(dirname "$0")/.." && pwd)"
agent_python="${AGENT_BIX_PYTHON:-${CONDA_PREFIX:?activate agent_bix or set AGENT_BIX_PYTHON}/bin/python}"
unset NATIVE_B_EXTERNAL_CONTAINER_ISOLATION
cd "$solver_dir"
exec "$agent_python" "conditions/$condition/runner.py" "$@"

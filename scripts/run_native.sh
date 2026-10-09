#!/usr/bin/env bash
# Path: scripts/run_native.sh
# Purpose: Launch a fresh isolated Native experiment with OpenCode and DeepSeek.
set -euo pipefail
cd "$(dirname "$0")/.."
run_id="${NATIVE_B_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
[[ "$run_id" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "invalid NATIVE_B_RUN_ID" >&2; exit 2; }
python conditions/native/runner.py --execute --model "${MODEL:-deepseek/deepseek-flash}" --reasoning-effort "${REASONING_EFFORT:-high}" --workers "${WORKERS:-2}" \
  --run-root "runs/hard_isolated_${run_id}_native" \
  --manifest "analysis/hard_isolated_${run_id}_native_manifest.json" \
  --output "analysis/hard_isolated_${run_id}_native_generation.json" \
  --report "reports/HARD_ISOLATED_${run_id}_NATIVE_GENERATION.md"

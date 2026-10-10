#!/usr/bin/env bash
# Path: scripts/run_b.sh
# Purpose: Launch a fresh isolated B experiment with Codex through the OpenAI relay.
set -euo pipefail
cd "$(dirname "$0")/.."
run_id="${NATIVE_B_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
[[ "$run_id" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "invalid NATIVE_B_RUN_ID" >&2; exit 2; }
python conditions/b/runner.py --execute --model "${MODEL:-openai-relay/gpt-5.6-sol}" --reasoning-effort "${REASONING_EFFORT:-high}" --workers "${WORKERS:-2}" \
  --run-root "runs/codex_${run_id}_b" \
  --manifest "analysis/codex_${run_id}_b_manifest.json" \
  --output "analysis/codex_${run_id}_b_generation.json" \
  --report "reports/CODEX_${run_id}_B_GENERATION.md"

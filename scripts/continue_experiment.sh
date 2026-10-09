#!/usr/bin/env bash
# Path: scripts/continue_experiment.sh
# Purpose: Continue all three Native/B replicates, preserve completed outcomes, recover reports, and export predictions.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
experiment_id="${1:?Usage: bash scripts/continue_experiment.sh EXPERIMENT_ID}"
if [[ ! "$experiment_id" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
  echo "Invalid experiment ID" >&2
  exit 2
fi
mkdir -p "logs/${experiment_id}" "results/${experiment_id}"
for seed in 01 02 03; do
  for condition in native b; do
    tag="${experiment_id}_${condition}_seed${seed}"
    # Also recognize the rep labels in OPENCODE_DEEPSEEK.md.
    rep_tag="${experiment_id}_${condition}_rep${seed}"
    if [[ ! -e "analysis/${tag}_manifest.json" && ! -e "analysis/${tag}_generation.json" && ! -e "runs/${tag}" &&
          ( -e "analysis/${rep_tag}_manifest.json" || -e "analysis/${rep_tag}_generation.json" || -e "runs/${rep_tag}" ) ]]; then
      tag="$rep_tag"
    fi
    python "conditions/${condition}/runner.py" --execute --resume \
      --model "${MODEL:-deepseek/deepseek-flash}" \
      --reasoning-effort "${REASONING_EFFORT:-high}" \
      --workers "${WORKERS:-2}" --timeout "${TIMEOUT:-3600}" \
      --run-root "runs/${tag}" \
      --manifest "analysis/${tag}_manifest.json" \
      --output "analysis/${tag}_generation.json" \
      --report "reports/${tag}_GENERATION.md" \
      2>&1 | tee -a "logs/${experiment_id}/${condition}_seed${seed}.log"
    if [[ ! -f "reports/${tag}_GENERATION.md" ]]; then
      python scripts/recover_generation_report.py \
        --generation "analysis/${tag}_generation.json" \
        --condition "$condition" --report "reports/${tag}_GENERATION.md"
    fi
    python scripts/export_predictions.py \
      --generation "analysis/${tag}_generation.json" --condition "$condition" \
      --output "results/${experiment_id}/${condition}_seed${seed}.jsonl" \
      --provenance "results/${experiment_id}/${condition}_seed${seed}.provenance.json" \
      --source-run "$tag"
  done
done
echo "Finished: results/${experiment_id}"

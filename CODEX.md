<!-- Path: CODEX.md; Purpose: Deploy and run Native/B with a pinned, hermetic Codex CLI through the OpenAI relay. -->
# Codex generation

Native/B generation uses the pinned Codex CLI (`codex-cli 0.158.0`, from
`environment/codex_lock.json`). The default model is `openai-relay/gpt-5.6-sol`
with high reasoning effort and 2 concurrent workers. Each of the 78 Native
questions gets an independent Codex session; B gets one session per capsule
(19 capsules). A session may make several model requests while it runs tools.

This is a new experiment. It does not reproduce the frozen OpenCode/DeepSeek
or earlier Codex predictions. The OpenCode pipeline is preserved at the git tag
`opencode-final`; check it out in a separate worktree to resume, export or
inspect runs made with it.

## What "hermetic" means here

Nothing from the machine's Codex setup reaches an attempt:

- **Pinned binaries.** `scripts/install_codex.sh` downloads the Linux Codex and
  code-mode host release archives named in `environment/codex_lock.json`,
  verifies their sha256, and installs them under `tools/codex/0.158.0/`. The
  controller refuses to start if `codex --version` differs from the lock. A
  `codex` on `PATH` (npm, Homebrew, ...) is never used.
- **Fresh `CODEX_HOME` per attempt.** It is `<workspace>/home/.codex`, holding
  only a generated `config.toml` and a one-model `model_catalog.json`, both
  mounted read-only. `~/.codex` (config, `auth.json`, memories, plugins, rules,
  history) is never mounted. Codex runs with `--strict-config` (unknown keys fail),
  `--ephemeral` and `--ignore-rules`, with AGENTS.md loading disabled.
- **Credentials stay on the host.** The container gets a short-lived attempt
  token. The host gateway swaps it for the relay key and binds the model and the
  `/responses` route. `OPENAI_API_KEY` is never read.
- **Codex as shipped, minus out-of-scope surfaces.** The model keeps its bundled
  tool mode (code mode: a JavaScript `exec` tool whose nested `exec_command` and
  `apply_patch` calls appear as ordinary `command_execution`/`file_change`
  events). For parity with the OpenCode runs, the config and catalog overrides
  turn off sub-agents, skills, plugins, apps, web search, user-input questions,
  hooks, browser/computer use, image generation and websockets. The `ultra`
  effort (automatic delegation) is rejected.

The runtime is otherwise unchanged from the OpenCode runs: the host `agent_bix`
environment is mounted read-only into a per-attempt `--network none` container.
Each `isolation_boundary.json` records the image ID, the Codex version, the
sha256 of both Codex binaries, the generated config and the catalog hashes.

## Server runbook

On the Linux host, as a non-root user with Docker access, in this checkout:

```bash
mamba activate agent_bix

# 1. Pinned Codex (downloads ~110 MB from github.com/openai/codex releases).
scripts/install_codex.sh
tools/codex/0.158.0/codex --version          # codex-cli 0.158.0

# 2. Relay credential, project-local and never committed.
test -e .env || cp .env.example .env
chmod 600 .env
# Edit .env: OPENAI_RELAY_API_KEY=your-relay-key
# Optional: OPENAI_RELAY_BASE_URL=https://xfastapi.ai (requests go to <base>/responses)

# 3. Attempt base image, as before.
docker image inspect nvidia/cuda:11.8.0-base-ubuntu22.04 >/dev/null || docker pull nvidia/cuda:11.8.0-base-ubuntu22.04

# 4. Unit tests, then the real pinned Codex against a local mock relay (no paid calls).
python -m unittest common.integrity.test_codex_agent common.integrity.test_attempt_container \
  common.integrity.test_gateway_routes common.integrity.test_project_credentials
NATIVE_B_TEST_CODEX=1 python -m unittest common.integrity.test_codex_agent.RealCodexTests

# 5. Boundary smoke test (mock model, no paid calls): mounts, network denial,
#    read-only inputs, ~/.codex / planted OPENAI_API_KEY / AGENTS.md never reach the model.
python scripts/smoke_attempt_isolation.py --report results/codex_isolation_check.json
```

If the server cannot reach github.com, run `scripts/install_codex.sh` on any
machine with access and copy `tools/codex/0.158.0/` across; the controller
checks the version, and the boundary record keeps both binaries' sha256.

An exported `OPENAI_RELAY_API_KEY` takes precedence over `.env`. The loader
reads only these keys, never executes shell expressions, and never exports them.

Restore the answer-free public release data as a sibling of this directory
(`../data/scope.json`, `../data/public/questions.jsonl`, `../data/capsules/…`).
Do not reconstruct generation inputs from the private answer key or historical
predictions.

## Run a small diagnostic

This makes real model calls.

```bash
run_tag="codex_native_diagnostic_$(date -u +%Y%m%dT%H%M%SZ)_$$"
set -o pipefail
python conditions/native/runner.py --execute --workers 1 --short-ids bix-10 \
  --run-root "runs/${run_tag}" \
  --manifest "analysis/${run_tag}_manifest.json" \
  --output "analysis/${run_tag}_generation.json" \
  --report "reports/${run_tag}.md" \
  2>&1 | tee "${run_tag}.log"
```

Check `runs/${run_tag}/0000/*/isolation_boundary.json` and `events.jsonl`.
`--short-ids` selects a capsule's questions for Native and a complete capsule for
B. Diagnostic subsets cannot be exported with the full-78 prediction exporter.
Use fresh paths for every invocation; if preflight reports a manifest mismatch,
use a new tag for all four output paths.

The runners print flushed, content-free progress lines to stderr: run totals,
attempt preparation, agent start, completed tool calls, a heartbeat every 30
seconds, attempt outcome, and completed/total counts. Detailed `events.jsonl`,
`event_timeline.json` and `stderr.log` are saved after each attempt.

## Run both conditions and three independent replicates

```bash
set -euo pipefail
experiment_id="codex_relay_$(date -u +%Y%m%dT%H%M%SZ)_$$"
mkdir -p "logs/${experiment_id}"
for replicate in 01 02 03; do
  for condition in native b; do
    tag="${experiment_id}_${condition}_rep${replicate}"
    python "conditions/${condition}/runner.py" --execute \
      --model openai-relay/gpt-5.6-sol --reasoning-effort high \
      --workers 2 --timeout 3600 \
      --run-root "runs/${tag}" \
      --manifest "analysis/${tag}_manifest.json" \
      --output "analysis/${tag}_generation.json" \
      --report "reports/${tag}_GENERATION.md" \
      2>&1 | tee "logs/${experiment_id}/${condition}_rep${replicate}.log"
  done
done
```

Replicate labels are not API random seeds. Keep model, effort, timeout,
concurrency and resource policies equal across conditions. Supported efforts are
`low`, `medium`, `high`, `xhigh` and `max`.

The convenience scripts accept `MODEL`, `REASONING_EFFORT`, `WORKERS` and
`NATIVE_B_RUN_ID`, and write `runs/codex_<id>_<condition>`:

```bash
NATIVE_B_RUN_ID=relay_rep01 WORKERS=2 scripts/run_native.sh
NATIVE_B_RUN_ID=relay_rep01 WORKERS=2 scripts/run_b.sh
```

## Check progress, continue, freeze, export

```bash
python scripts/check_experiment_progress.py --experiment-id "$experiment_id" --details
nohup bash scripts/continue_experiment.sh "$experiment_id" \
  > "logs/${experiment_id}/continue_$(date -u +%Y%m%dT%H%M%SZ).log" 2>&1 < /dev/null &
```

These work as before. The continuation reuses terminal outcomes, runs only
unfinished units in new isolated workspaces under `runs/<tag>/resume_<timestamp>/`,
freezes the combined generation, recovers missing reports and exports
predictions. Resume requires the same prompt, model, effort, timeout, scope and
resource contract.

```bash
python scripts/export_predictions.py \
  --generation "analysis/${experiment_id}_native_rep01_generation.json" \
  --condition native \
  --output "results/${experiment_id}/native_rep01.jsonl" \
  --provenance "results/${experiment_id}/native_rep01.provenance.json" \
  --source-run "${experiment_id}_native_rep01"
```

Score only after freeze, in the private evaluation environment. The scorer and
answer key must remain inaccessible during generation. To archive and then
remove an experiment's outputs, use `scripts/pack_run_artifacts.py` and
`scripts/cleanup_run_artifacts.py` (dry run by default).

## Final answers and failures

Codex emits JSON events, not schema-constrained final answers. The controller
takes the last `agent_message` of a completed turn, validates it against the
existing JSON schema, and writes `question_result.json` or `capsule_result.json`
itself; solver-created result files are rejected. A `turn.failed` event, a turn
that never completes, prose around the JSON, or a schema violation fails the
attempt with no repair call. Plain `error` events that report retried transport
errors (`Reconnecting... 1/2`) do not fail an attempt by themselves.

`Controller-passed` counts generation/compliance acceptance. If it is zero, read
each record's `failure_reason` in the generation JSON, then that workspace's
`stderr.log` and `events.jsonl`. Failure reasons name the Codex event type and an
HTTP status when one is present, never the message body. None of these is a
private correctness verdict. For runs with a policy retry, inspect both `0000`
and `0001`.

`.gitignore` excludes local secrets, caches, datasets, `tools/`, `out/`, attempt
workspaces and generated outputs.

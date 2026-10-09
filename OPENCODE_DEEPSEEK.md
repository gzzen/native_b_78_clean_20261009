<!-- Path: OPENCODE_DEEPSEEK.md; Purpose: Deploy and rerun Native/B using isolated OpenCode v2 with the DeepSeek API. -->
# OpenCode v2 + DeepSeek generation

The Native/B generation commands now use OpenCode v2. The default model is
`deepseek/deepseek-flash`, with high reasoning effort and 2 concurrent workers.
The 78 Native questions each receive an independent agent session; B receives
one session per capsule (19 capsules). A session may make multiple model
requests while executing tools. Each attempt uses `opencode run --standalone`
and a fixed title, which prevents an auxiliary model call for title generation.

The existing frozen Codex results are historical comparison material. Changing
the CLI and model creates a new experiment; it does not reproduce their exact
predictions. The current B prompt/composition also differs from some earlier runs.

## Required layout and runtime

Use a Linux host, a non-root controller account with Docker access, the Linux
`agent_bix` Conda environment, and a standalone **Linux OpenCode v2 executable**.
The macOS executable and macOS Conda packages cannot run in the Linux containers.
OpenCode v1 is not supported by this adapter.

Restore the answer-free public release data:

```text
experiment/
├── solver/                  # this generation code
└── data/
    ├── scope.json
    ├── public/questions.jsonl
    └── capsules/CapsuleFolder-…/
```

The code resolves `data/` relative to the parent of the solver directory.
The current standalone scoring checkout does not include it. Do not reconstruct
generation inputs from the private answer key or historical predictions.

```bash
mamba env create -f environment/agent_bix_environment.yml
mamba activate agent_bix
opencode --version
docker pull nvidia/cuda:11.8.0-base-ubuntu22.04

# Set this only when OpenCode's Linux binary is not on PATH:
export NATIVE_B_OPENCODE_BINARY=/absolute/path/to/linux/opencode

# Configure the credential inside this project; never commit the real key:
# Copy only if .env does not already exist.
test -e .env || cp .env.example .env
chmod 600 .env
# Edit .env and set DEEPSEEK_API_KEY=your-real-key
```

The controller reads `DEEPSEEK_API_KEY` from the project-root `.env`, regardless
of the working directory. An exported `DEEPSEEK_API_KEY` takes precedence; use
`unset DEEPSEEK_API_KEY` if you want the project file to supply it. Plain or
single/double-quoted values and comments are supported. The loader reads only
this key, does not execute shell expressions, and does not export it to child
processes. `.env` is already ignored by Git; `.env.example` is safe to commit.

The controller injects the real credential only into requests to
`https://api.deepseek.com/v1/chat/completions`. Containers receive a temporary
attempt token, a fixed model, and an isolated OpenCode configuration. Interactive
`/connect` credentials are not required or mounted. The gateway permits at most
256 model requests per attempt and retains the existing scientific-host allowlist.

## OpenAI Responses relay (`openai-relay/gpt-*`)

The same isolated pipeline can use an OpenAI-compatible Responses relay, such as
`https://xfastapi.ai`, instead of DeepSeek. Set the relay key in `.env`:

```bash
# .env (host only; never mounted into attempts)
OPENAI_RELAY_API_KEY=your-relay-key
# Optional; the default is https://xfastapi.ai. Requests go to <base>/responses.
OPENAI_RELAY_BASE_URL=https://xfastapi.ai
```

Then select the model with the `openai-relay/` prefix:

```bash
MODEL=openai-relay/gpt-5.6-sol REASONING_EFFORT=xhigh NATIVE_B_RUN_ID=relay_rep01 scripts/run_native.sh
```

This mirrors the following Codex settings. OpenCode uses its
`@opencode/ai/providers/openai/responses` package over plain HTTP:

| Codex setting | Here |
| --- | --- |
| `wire_api = "responses"`, `base_url` | gateway route `/model/responses` → `OPENAI_RELAY_BASE_URL/responses` |
| `requires_openai_auth` | `OPENAI_RELAY_API_KEY`, injected by the host gateway only |
| `model_reasoning_effort = "xhigh"` | `--reasoning-effort xhigh` (`none`, `low`, `medium`, `high`, `xhigh`) |
| `disable_response_storage = true` | OpenCode sends `store: false` and replays encrypted reasoning |
| `model_context_window = 1000000` | model limit: context 1,000,000, output 128,000 tokens |

`OPENAI_API_KEY` is never read, so an official OpenAI key cannot reach a
third-party relay by accident. The gateway binds the attempt token, the exact
model ID and the `/responses` route, and refuses `/chat/completions` for relay
runs. The 256-request limit still applies. A relay run is a separate
experiment from DeepSeek runs, and its manifest records `provider: OpenAI relay`.
OpenCode compacts automatically when the context approaches the configured
window. There is no separate setting for Codex's `model_auto_compact_token_limit`.

## Check the boundary, then run a small diagnostic

The Docker smoke test uses mock model responses; it does not contact the real
model API. `--science` additionally queries the approved mygene endpoint.

```bash
python scripts/smoke_attempt_isolation.py \
  --report results/opencode_isolation_check.json

run_tag="opencode_native_diagnostic_$(date -u +%Y%m%dT%H%M%SZ)_$$"
set -o pipefail
python conditions/native/runner.py --execute --workers 1 --short-ids bix-10 \
  --run-root "runs/${run_tag}" \
  --manifest "analysis/${run_tag}_manifest.json" \
  --output "analysis/${run_tag}_generation.json" \
  --report "reports/${run_tag}.md" \
  2>&1 | tee "${run_tag}.log"
```

The diagnostic makes real model calls. `--short-ids` selects a capsule's questions
for Native and a complete capsule for B. Diagnostic subsets cannot be exported
with the full-78 prediction exporter. Use fresh paths for every invocation.
The timestamped tag above also avoids stale manifest conflicts after changing
the timeout, model, workers, prompts or runner/adapter code. If preflight reports
an existing manifest mismatch, use a new tag for all four output paths; keep the
previous manifest and attempt artifacts intact.

Native/B print flushed progress lines to stderr: run totals, attempt preparation,
agent start, tool name/status, a heartbeat every 30 seconds while capturing the
agent, attempt outcome, and completed/total counts. The v2 CLI reports tools
after execution, so tool messages describe observed activity. Heartbeats also
appear while a long tool or model request is silent. Progress contains no prompts,
commands, tool results, answer text, credentials, or raw error messages. The final
JSON summary stays on stdout. Detailed `events.jsonl`, `event_timeline.json`, and
`stderr.log` are saved after each attempt finishes.

To keep a combined terminal log, append `2>&1 | tee diagnostic_terminal.log` to
the run command. Use `set -o pipefail` so a failing runner remains a failing shell
pipeline. Running processes must be restarted to pick up these logging changes.

## Run both conditions and three independent replicates

```bash
set -euo pipefail
experiment_id="opencode_deepseek_$(date -u +%Y%m%dT%H%M%SZ)_$$"
mkdir -p "logs/${experiment_id}"
for replicate in 01 02 03; do
  for condition in native b; do
    tag="${experiment_id}_${condition}_rep${replicate}"
    python "conditions/${condition}/runner.py" --execute \
      --model deepseek/deepseek-flash --reasoning-effort high \
      --workers 2 --timeout 3600 \
      --run-root "runs/${tag}" \
      --manifest "analysis/${tag}_manifest.json" \
      --output "analysis/${tag}_generation.json" \
      --report "reports/${tag}_GENERATION.md" \
      2>&1 | tee "logs/${experiment_id}/${condition}_rep${replicate}.log"
  done
done
```

Replicate labels are not API random seeds. Keep model, effort, timeout, concurrency
and resource policies equal across conditions. Supported effort labels are
`none`, `low`, `medium`, `high`, and `max`; enabled thinking passes the label as
DeepSeek's `reasoning_effort`. `none` disables thinking. Check the selected
model's API support before using a non-default effort.

For a single fresh run, the convenience scripts accept environment overrides:

```bash
NATIVE_B_RUN_ID=deepseek_rep01 WORKERS=2 scripts/run_native.sh
NATIVE_B_RUN_ID=deepseek_rep01 WORKERS=2 scripts/run_b.sh
```

`MODEL` and `REASONING_EFFORT` also override their defaults. The existing
`scripts/run_isolated_docker.sh {native|b}` forwards runner arguments using the
active Conda environment, or `AGENT_BIX_PYTHON` when supplied.

## Check experiment progress

Run the read-only checker on the Linux host where generation runs:

```bash
python scripts/check_experiment_progress.py \
  --experiment-id deepseek_full_20260930T090315Z_282056 --details

# Repeat every 30 seconds; Ctrl-C stops only the checker.
python scripts/check_experiment_progress.py \
  --experiment-id deepseek_full_20260930T090315Z_282056 --watch 30

# Without an ID, select the most recently modified DeepSeek experiment.
python scripts/check_experiment_progress.py --details
```

The table covers Native/B for labels 01, 02, and 03 (`seed` or `rep` paths).
It reports completed units, controller passes/failures, matching controller PIDs,
and exported prediction row counts. Native units are questions; B units are
capsules. A frozen combined generation is complete even if its report or export
is missing. Before freeze, the checker counts the latest terminal attempt per
workspace; a first policy interruption awaiting retry is not a completed unit.

`INCOMPLETE` means run artifacts exist and no matching controller is visible on
this host. It does not establish why the process stopped or whether orphaned
containers remain. If process inspection fails, status is `UNKNOWN`. Details
include unfinished workspace names, missing reports, the latest content-free
progress line, terminal-log age, and the last logged exception type. Logs and
events may not be saved until an attempt finishes. The checker never resumes
runs, edits artifacts, prints answer text, or contacts the model/evaluator.
Use `--json` for one machine-readable snapshot and `--root` for another checkout.

## Continue an interrupted experiment

After updating the Linux checkout, continue the same experiment ID:

```bash
mamba activate agent_bix
bash scripts/continue_experiment.sh deepseek_full_20260930T090315Z_282056
```

To keep the controller running after the terminal disconnects:

```bash
mkdir -p logs/deepseek_full_20260930T090315Z_282056
nohup bash scripts/continue_experiment.sh deepseek_full_20260930T090315Z_282056 \
  > "logs/deepseek_full_20260930T090315Z_282056/continue_$(date -u +%Y%m%dT%H%M%SZ).log" \
  2>&1 < /dev/null &
```

The script runs Native/B for all three labels, skips already frozen generation,
recovers missing reports, and exports predictions. Each runner supports
`--execute --resume` with the original four output paths. It reuses all terminal
outcomes, including failures, and runs only unfinished questions/capsules. A first
policy interruption gets only its remaining retry; an exhausted retry stays
failed. An interrupted tool/model session without a completed capture restarts
in a new isolated workspace rather than continuing the old OpenCode session.

Original workspaces and manifests remain intact. New attempts live under
`runs/<tag>/resume_<timestamp>/`; a controller journal beside the manifest records
source record hashes, incomplete workspace paths, the original manifest hash,
and current runner/resume-code hashes. The same prompt, model, effort, timeout,
scope and recorded resource contract are required. Only the runner code hash
may differ during resume, and that difference is recorded; ordinary launches
retain the strict manifest check. `MODEL`, `REASONING_EFFORT`, `WORKERS`, and
`TIMEOUT` environment overrides must match the original run configuration.

Resume refuses another active controller or an old attempt container using the
same run tree. Existing unfrozen combined-output files are preserved and require
a fresh `--output` path. After remaining work finishes, the runner freezes the
full combined generation using both old and new records. Saved attempt runtimes
and calls are retained; unsaved interrupted-attempt costs/runtimes are unknown,
and wall time describes the current invocation. Rerunning the continuation
script skips frozen runs and makes no new model calls for them.

## Freeze, export and score

OpenCode emits JSON events, not schema-constrained final answers. The controller
extracts final assistant text, validates the existing JSON schema, and writes
`question_result.json` or `capsule_result.json`. Failed validation fails the
attempt. The prompt distinguishes the schema definition from the answer object,
lists allowed top-level keys, and requires a JSON-only final assistant message.
The schema's `$schema` annotation is omitted from the prompt's definition; it is
not an answer field. Extra fields, prose, missing JSON, and model error events
still fail validation; there is no automatic repair or extra model call for them.
Code persistence, actual execution and source-evidence audits still apply.
Raw OpenCode events are retained under `opencode_event` in each event record;
normalized shell and file events feed the existing guards and replay tooling.
The v2 CLI reports tools after completion, so live event guards observe completed
actions. The per-attempt Docker boundary remains the access-control layer.

Generation manifests record the adapter hash and provider configuration. Each
frozen isolation record also contains the Linux binary hash, image ID and science
allowlist. Solver-created result files are rejected; the controller owns the
final result file. Default agent permissions disable subagents, skills, questions
and web search; approved scientific web fetches and shell commands remain available.

```bash
python scripts/export_predictions.py \
  --generation "analysis/${experiment_id}_native_rep01_generation.json" \
  --condition native \
  --output "results/${experiment_id}/native_rep01.jsonl" \
  --provenance "results/${experiment_id}/native_rep01.provenance.json" \
  --source-run "${experiment_id}_native_rep01"

# Run only after freeze, in the private evaluation environment:
python evaluator/score.py \
  --predictions "results/${experiment_id}/native_rep01.jsonl" \
  --answers evaluator/private/answers_78.json \
  --output "results/${experiment_id}/native_rep01_scoring.json"
```

Repeat export/scoring for B and each replicate. The scorer and answer key must
remain inaccessible during generation.

## Diagnose failed attempts

Both runners create generation-output and report parent directories before agent
execution. In older code, a missing report directory could fail after all attempts
finished. The combined generation JSON is written and frozen before the report;
if that file exists, recover only the missing report and then export predictions:

```bash
# Use the exact original run tag, without rerunning the completed generation.
tag="your_original_native_seed01_tag"
python scripts/recover_generation_report.py \
  --generation "analysis/${tag}_generation.json" \
  --condition native \
  --report "reports/${tag}_GENERATION.md"
```

The recovery command checks the freeze status and summary against records,
preserves the generation JSON, and refuses an existing report. It also supports
`--condition b`. Continue remaining runs with their own unused paths; completed
generation must not be launched again into its nonempty run root.

`Controller-passed` counts generation/compliance acceptance. If it is zero, read
each record's `failure_reason` in the combined generation JSON, then inspect that
record's workspace `stderr.log` and `events.jsonl`. A nonzero OpenCode exit is
reported with its error name and HTTP status when an error event provides them;
raw error bodies are retained only in the detailed logs. Exit code 1 alone does
not establish an API, transport, or provider compatibility cause. None of these
failures is a private correctness verdict. Use a fresh tag after updating code.

A quoted literal exclusion such as `grep -v '/tmp/'` filters text and is not a
shared-temp access. The guard permits that pattern while rejecting actual shared
temp file operands, redirections, pattern files, and command substitutions.
Peer resets/broken pipes during relay forwarding close that connection without
a worker traceback; this alone does not establish the cause of a CLI failure.

For runs with a policy retry, inspect both `0000` and `0001`: the first attempt's
policy interruption and the retry's final CLI exit can have different causes.
If `Rscript` is found but reports a missing sibling Conda prefix, that wrapper's
target is unavailable inside the container; finding the wrapper is not an R
runtime availability check.

## Verification and Git hygiene

Run the adapter and boundary tests in an environment containing `jsonschema`:

```bash
python -m unittest common.integrity.test_opencode_capture \
  common.integrity.test_attempt_relay common.integrity.test_isolation_audit \
  common.integrity.test_generation_outputs \
  common.integrity.test_experiment_progress \
  common.integrity.test_resume \
  common.integrity.test_progress \
  common.integrity.test_project_credentials \
  common.integrity.test_gateway_body common.integrity.test_gateway_routes \
  common.integrity.test_attempt_container common.integrity.test_controller_retry_policy

# Optional real CLI against a local fake model, with zero paid calls:
NATIVE_B_TEST_OPENCODE_BINARY=/absolute/path/to/opencode \
  python -m unittest common.integrity.test_opencode_capture
```

`.gitignore` excludes local secrets, caches, datasets, attempt workspaces and
future generated output files. Existing bytecode caches have been removed from
the working tree and Git index. Packaged historical experiment artifacts remain
tracked. Ignoring or deleting a file does not remove it from Git history.

Official references: [OpenCode v2 CLI](https://opencode.ai/v2/docs/cli/),
[provider configuration](https://opencode.ai/v2/docs/providers/), and
[DeepSeek integration](https://api-docs.deepseek.com/guides/coding_agents/).

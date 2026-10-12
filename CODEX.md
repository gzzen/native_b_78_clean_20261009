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

- **Pinned binaries, installed offline.** The generation server never downloads
  Codex. On any machine with internet, `scripts/make_codex_bundle.sh` packs the
  Linux Codex and code-mode host release archives named in
  `environment/codex_lock.json` into one tar file. On the server,
  `scripts/install_codex.sh BUNDLE` installs them under `tools/codex/0.158.0/`
  with no network access, checking each archive's and each binary's sha256
  against the lock. Before every attempt the controller re-checks both binaries'
  sha256 and the reported version, so a wrong or corrupted copy is refused
  however it got there. A `codex` on `PATH` (npm, Homebrew, ...) is never used.
- **No network at runtime.** Attempts run with `--network none`; the only path
  out is the host gateway to the relay and the science allowlist. Update checks,
  analytics and remote model lists are off, and the controller reads the model
  catalog from the binary itself (`codex debug models --bundled`).
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

On a machine with internet (once per Codex version), build the offline bundle
and copy it to the server alongside this checkout:

```bash
scripts/make_codex_bundle.sh      # -> out/codex-offline-0.158.0-x86_64-unknown-linux-musl.tar (~136 MB)
scp out/codex-offline-0.158.0-x86_64-unknown-linux-musl.tar server:path/to/this/checkout/out/
```

On the Linux host, as a non-root user with Docker access, in this checkout
(no internet needed):

```bash
mamba activate agent_bix

# 1. Pinned Codex from the offline bundle (no network; sha256-checked against the lock).
scripts/install_codex.sh out/codex-offline-0.158.0-x86_64-unknown-linux-musl.tar
#    -> prints codex-cli 0.158.0

# 2. Relay credential, project-local and never committed.
test -e .env || cp .env.example .env
chmod 600 .env
# Edit .env: OPENAI_RELAY_API_KEY=your-relay-key
# Optional: OPENAI_RELAY_BASE_URL=https://xfastapi.ai (requests go to <base>/responses)

# 3. Attempt base image, as before (already present for the OpenCode runs).
docker image inspect nvidia/cuda:11.8.0-base-ubuntu22.04 --format '{{.Id}}'

# 4. Unit tests, then the real pinned Codex against a local mock relay (no paid calls).
python -m unittest common.integrity.test_codex_agent common.integrity.test_attempt_container \
  common.integrity.test_gateway_routes common.integrity.test_project_credentials
NATIVE_B_TEST_CODEX=1 python -m unittest common.integrity.test_codex_agent.RealCodexTests

# 5. Boundary smoke test (mock model, no paid calls): mounts, network denial,
#    read-only inputs, ~/.codex / planted OPENAI_API_KEY / AGENTS.md never reach the model.
python scripts/smoke_attempt_isolation.py --report results/codex_isolation_check.json
```

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
private correctness verdict. For runs with a policy or relay retry, inspect every
attempt directory (`0000`, `0001`, ...) listed in `attempt_history`.

## Relay capacity: monitor and re-attempts

The relay sometimes refuses work with a "busy"/capacity error (an SSE `error`
event, or HTTP 429), and occasionally drops a stream mid-generation. Codex
reports the refusal as `Selected model is at capacity` and does not retry it, so
two layers handle it. Neither changes what the model sees.
[RELAY_CAPACITY.md](RELAY_CAPACITY.md) explains exactly how both layers work.

**Relay monitor (gateway).** One `RelayMonitor` (`common/integrity/relay_monitor.py`)
is shared by every attempt gateway in a controller process:

- Upstream `/responses` requests are paced by an AIMD in-flight limit that starts
  at `--workers`. A refusal halves the limit and starts a jittered cooldown
  (5 s doubling to 120 s, longer when the relay sends `Retry-After`).
- Three refusals in a row open a circuit breaker. After the cooldown, one real
  request goes through as a probe; the breaker closes when the relay accepts it.
  There are no paid health checks.
- A refused request is resent upstream **only while Codex has received nothing**
  from it: HTTP 429/502/503/504/52x/529, connection errors, or an SSE
  `error`/`response.failed` that arrives before the first output event. The
  gateway holds back at most 30 s / 256 KiB of the stream to see this. The whole
  wait-and-retry window ends 200 s after the request arrived, below the 300 s idle
  limits; after that the refusal is forwarded verbatim. Codex never sees an
  absorbed refusal, and the request body it sent is resent byte for byte.
- Non-streamed model requests are refused (`gateway_stream_required`): the relay's
  CDN cuts them off at about 125 s (Cloudflare 524).
- Each attempt writes content-free counters to `relay_monitor.json`: requests,
  upstream tries, transparent retries, refusal signals, short error codes,
  admission wait, time to first byte, completed streams and mid-stream drops. The
  run totals appear under `generation_summary.relay_monitor`.

**Controller re-attempts.** A failed turn whose final error is a relay capacity
refusal (`relay_capacity`) or a relay stream drop that Codex could not reconnect
(`relay_stream_drop`) is re-run from scratch in a fresh workspace, container and
gateway. No state carries over. The default is up to 3 re-attempts
(`--relay-retries 0..5`). Before every attempt, the controller waits while the
relay is cooling down, for up to `--relay-outage-wait` seconds (default 1800).
This means an outage holds work back instead of spending re-attempts. That wait
happens before the container starts, so it does not count against `--timeout`.
Timeouts, schema or compliance failures and any other model-side outcome are
never re-attempted; `"retry": false` in `conditions/*/config.json` still means no
scientific or repair retries.

Records carry:
- `relay_failure` (the class, or null);
- `relay_retry_count` and `policy_retry_count`;
- `retry_reason` (for example `prior_attempt_relay_capacity`);
- `failure_reason` per attempt in `attempt_history`.

When the budget is spent, the final status is `failed_relay_retries_exhausted`.
The generation summary adds `relay_failed_attempts`, `relay_retry_count` and
`relay_failed_final`, and the manifest's `retry_policy` records the budget.
`--resume` re-runs relay failures that still have budget and reuses everything
else.

Progress logs show `relay failure class=<class>; re-attempt N/M` and
`relay cooling down; waiting Ns`. The runners refuse to start a paid run if the
controller cannot import `jsonschema`; activate `agent_bix` first.

Runs started before this change have a different manifest and cannot be resumed
with this code. Start them again under a fresh tag.

`.gitignore` excludes local secrets, caches, datasets, `tools/`, `out/`, attempt
workspaces and generated outputs.

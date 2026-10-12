<!-- Path: RELAY_CAPACITY.md; Purpose: How the relay monitor and relay re-attempts keep relay capacity failures from costing questions. -->
# Relay capacity handling

This covers the two layers that stop relay capacity failures from costing
questions:

- the **relay monitor**, inside the host gateway;
- **relay re-attempts**, in the controller.

[CODEX.md](CODEX.md) has the runbook. This file explains how the two layers
work and how to check them.

## Why

In the bix-10 diagnostic (`codex_native_diagnostic_20261012T025554Z_1835731`,
`--workers 1`), 4 of 7 questions failed with Codex `turn.failed` "Selected model
is at capacity. Please try a different model.":

- q2, q5 and q7 failed after 3–28 s, before doing any work.
- q4 recovered from one `Reconnecting... 1/2` stream drop, then failed at 46 s.

The attempts ran one at a time, so the refusals came from the relay itself, not
from our load. The message is Codex's own text for an overloaded server, and
Codex never retries it, so one brief refusal cost a whole question.

What we know about the relay:

- It reports "busy" as an SSE error, or as HTTP 429 when not streaming.
- Non-streamed `/responses` is cut off by its CDN at about 125 s (Cloudflare 524).
- It sometimes fails mid-generation; two long jobs died after about 260 s and 490 s.

The diagnostic's other 3 questions finished their work but failed because the
controller could not import `jsonschema`. That was a host environment problem,
not the relay; the runners now refuse to start a paid run in that state.

## Where the monitor sits

```
Codex (container) → container relay → Unix socket → attempt gateway ──→ xfastapi relay
                                                         │
                                         one RelayMonitor shared by every
                                         attempt gateway in the runner process
```

- Each attempt has its own gateway (`common/integrity/attempt_gateway.py`).
- All of them share one `RelayMonitor` (`common/integrity/relay_monitor.py`).
- The runner creates it at startup with `relay_monitor.configure(--workers)`.
- Native and B run as separate processes, so each has its own monitor.
- Only model requests (`/model/responses`) go through the monitor. Requests to
  the science-host allowlist are unchanged.

## One model request, step by step

1. **Checks.**
   - The gateway checks the attempt token, the model and the 256-call cap, as
     before.
   - It also requires `"stream": true`. A non-streamed request gets 400
     `gateway_stream_required`.
   - It then sets a deadline 200 s after the request arrived.
2. **Admission (`monitor.admit`).** The request waits while any of these is
   true:
   - the relay is cooling down;
   - a probe request (see the circuit breaker below) is still pending;
   - the number of requests already in flight has reached the current limit.

   At the deadline it is let through anyway. Admission never rejects a request.
3. **Upstream try.** The gateway sends the request body Codex sent, byte for
   byte.
   - **Connection error:** treated as a refusal (`connect_error`).
   - **Status other than 200:** the gateway reads up to 64 KiB of the body.
     - 429, 502, 503, 504, 520–524 and 529 are refusals.
     - Any other status (for example a 400) goes to Codex unchanged.
   - **Status 200:** a background thread reads the stream into a queue. The
     gateway **holds it back** for up to 30 s or 256 KiB, skipping the opening
     events (`response.created`, `response.in_progress`, `response.queued`).
     The first other event decides what happens:

| First real event | Outcome |
|---|---|
| `error` / `response.failed` mentioning capacity, busy, overloaded, rate limit, too many requests or slow down | refusal `sse_capacity` |
| `error` / `response.failed` mentioning server error, timeout, bad gateway or upstream | refusal `sse_server_error` |
| anything else (normal output) | **commit** |
| hold-back window runs out with only opening events | commit |
| read error before any decision | refusal `sse_stream_error` |
| stream ends after only opening events | refusal `sse_early_eof` |

4. **On a refusal.** `monitor.refused()` records it and returns when the
   cooldown ends.
   - If that leaves at least 10 s before the deadline, the gateway closes the
     upstream connection and goes back to step 2, which waits out the cooldown.
     Codex has seen nothing.
   - Otherwise the refusal goes to Codex exactly as the relay sent it. Codex
     fails the turn, and the controller re-attempt (below) takes over.
5. **On commit.**
   - `monitor.committed()` marks the relay as healthy.
   - The gateway sends the headers and the held-back bytes, then passes the rest
     straight through.
   - It watches for the end of the stream: `response.completed` counts as a
     completed request; an error event, or the stream ending without
     completion, counts as a drop.
   - **After commit, nothing is ever retried by the gateway.** For a mid-stream
     drop, Codex first tries its own reconnects (`stream_max_retries = 2`). If
     those fail, the controller re-attempts the whole question.

The 200 s window keeps every silent wait below the 300 s idle limits of
`attempt_relay.relay_stream` and Codex's `stream_idle_timeout_ms`.

## Monitor state

- **In-flight limit.**
  - It starts at `--workers` and never goes above it.
  - Each refusal halves it, and so does each mid-stream drop (never below 1).
  - Each completed request adds 1, but only if there has been no refusal or drop
    in the last 60 s.
- **Cooldown.**
  - Each refusal sets `cooldown_until = now + delay`. The delay is
    `min(120, 5 × 2^(n−1))` seconds with ±25 % jitter, where `n` counts refusals
    in a row.
  - A `Retry-After` header can lengthen the delay, still capped at 120 s.
  - `n` resets the first time a request commits.
- **Circuit breaker.**
  - It opens after 3 refusals in a row.
  - Once the cooldown ends, the next request becomes the **probe**, a real
    request. Others wait behind it until their own deadline.
  - If the probe commits, the breaker closes. If it is refused, the breaker
    reopens with the next, longer backoff.
  - There are no separate paid health checks.

All of this is shared, so a refusal seen by one question slows down every
question running in that process.

**Silent retries per request during a sustained refusal streak** (ignoring jitter
and relay latency):

| Time (s) | Event | Cooldown |
|---|---|---|
| 0 | refused | 5 s |
| 5 | retried, refused | 10 s |
| 15 | refused | 20 s (breaker opens) |
| 35 | refused | 40 s |
| 75 | refused | 80 s |
| 155 | refused | would end at 275 s, past the window |

At that point the refusal goes to Codex. That is about 6 tries per request. A
one-off refusal costs only about 5 s.

## Controller re-attempts

`common/integrity/relay_failures.py` classifies each failed attempt by its final
`turn.failed` (or its last `error` when there is none):

- **`relay_capacity`:** "at capacity", busy, overloaded, too many requests, or
  Codex giving up on status 429/503/529.
- **`relay_stream_drop`:** "stream disconnected before completion", Codex giving
  up on a 5xx or 524, or a gateway upstream error.
- **None:** anything else, including timeouts, completed turns, and model-side
  failures (schema, compliance, context).

Only the class is stored, never the message.

`run_attempt_chain` (`common/integrity/resume.py`) runs each question (Native) or
capsule (B):

- **Waiting first.** Before every attempt, `monitor.wait_ready(--relay-outage-wait)`
  blocks while the relay is cooling down, logging
  `relay cooling down; waiting Ns` every 30 s. The container hasn't started yet,
  so this wait doesn't count against `--timeout`.
- **Re-running.** A relay failure with budget left is re-run from scratch in a
  fresh workspace, container and gateway (`0000`, `0001`, …). Nothing carries
  over. The log shows `relay failure class=<class>; re-attempt N/M`.
- **Separate budgets.** The relay budget (`--relay-retries`, default 3) is
  separate from the policy-violation budget (1).
- **Running out.** When the budget is spent, the status is
  `failed_relay_retries_exhausted`.
- **Resuming.** `--resume` re-runs relay failures that still have budget and
  reuses everything else.

`"retry": false` in `conditions/*/config.json` still means no scientific or
repair retries.

## Run flags

Both runners accept:

| Flag | Default | Meaning |
|---|---|---|
| `--relay-retries N` | 3 (range 0–5) | fresh re-attempts after a relay failure |
| `--relay-outage-wait SECONDS` | 1800 | longest wait for a cooling-down relay before an attempt starts |

`--workers` also sets the monitor's starting in-flight limit.

- The relay budget is saved in the manifest's `retry_policy`, so resume with the
  same `--relay-retries` you started with. `scripts/continue_experiment.sh` and
  the convenience scripts don't pass these flags, so they assume the defaults.
- Runs started before this feature have a different manifest and cannot be
  resumed with this code. Start them again under a fresh tag.

## What's recorded

**Per attempt, in `relay_monitor.json`** (content-free):
- Codex requests, upstream tries and silent retries;
- forced admissions, refusals passed on to Codex, completed streams and
  mid-stream drops;
- client disconnects, total admission wait, and time to first byte (count, max,
  mean);
- refusal counts by signal (`http_429`, `sse_capacity`, …);
- short error codes such as `server_is_overloaded`, kept only if they match
  `^[a-z0-9_.-]{1,64}$`.

No message text or bodies are stored.

**Per record, in the generation JSON:**
- `relay_failure`;
- `relay_retry_count` and `policy_retry_count`;
- `retry_reason`;
- `attempt_history` entries, each with its own `relay_failure`, `failure_reason`
  and `retry_reason`.

**Run summary:**
- `relay_failed_attempts`, `relay_retry_count` and `relay_failed_final`;
- `relay_monitor`: totals, plus the monitor's final state and limit.

## Checking it on the server

```bash
mamba activate agent_bix
python -m unittest common.integrity.test_relay_failures common.integrity.test_relay_monitor \
  common.integrity.test_gateway_relay_retry common.integrity.test_controller_retry_policy \
  common.integrity.test_resume
# Real pinned Codex against a local relay that answers "busy" first (no paid calls):
NATIVE_B_TEST_CODEX=1 python -m unittest common.integrity.test_codex_agent.RealCodexTests
```

Then re-run the bix-10 diagnostic from CODEX.md under a fresh tag and check:

- each workspace's `relay_monitor.json`: `transparent_retries`, `signals` and
  `error_codes`;
- `attempt_history` and `relay_retry_count` in the generation JSON;
- `python scripts/check_experiment_progress.py --details`.

## Limits

- **Matching is by keyword.** Refusals are detected by status codes and keywords.
  The relay's real "busy" response has not been captured yet. The `error_codes`
  from the first real run will show what it sends. A refusal the patterns miss
  reaches Codex, and the controller re-attempt still catches it.
- **Up to 30 s extra delay.** Holding back the start of a reply can delay a
  healthy reply by up to 30 s. It only does so when the relay sends nothing but
  opening events in that time.
- **No coordination between processes.** Two runner processes started at the
  same time against the same relay don't share cooldowns.

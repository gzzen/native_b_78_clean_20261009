#!/usr/bin/env python3
# Path: common/integrity/network_probe_conditions.py
# Purpose: Explicitly launch an isolated OpenCode diagnostic against an allowlisted science endpoint.
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from common.benchmark.a84_contract import controlled_env
from common.integrity.attempt_container import isolated_attempt
from common.integrity.evaluator_access_policy import answer_blind_env, generation_event_guard
from common.integrity.opencode_capture import (
    DEFAULT_MODEL, DEFAULT_REASONING_EFFORT, capture_opencode, opencode_command, write_result,
)


def main(default_condition="native"):
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="make real DeepSeek model calls")
    parser.add_argument("--condition", choices=("native", "a", "b"), default=default_condition)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--run-root", type=Path)
    args = parser.parse_args()
    if not args.execute:
        print("Diagnostic requires --execute and makes model calls. For a mock-only check use scripts/smoke_attempt_isolation.py.")
        return
    tag = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    workspace = (args.run_root or ROOT / "runs" / args.condition / f"opencode_network_probe_{tag}").resolve()
    (workspace / "data").mkdir(parents=True, exist_ok=False)
    environment = answer_blind_env(controlled_env(workspace))
    environment["BASH_ENV"] = str(ROOT / "conditions" / args.condition / "agent_bix_bash_env.sh")
    schema = ROOT / "conditions/native/question_result_no_id.schema.json"
    prompt = ('Run curl -fsSL "https://mygene.info/v3/gene/1017?fields=symbol" '
              '> network_probe.json in this workspace. Return unresolved with no candidates '
              'and describe whether the network query succeeded in summary.')
    command = opencode_command(prompt, schema, args.model)
    with isolated_attempt(command, environment, workspace, schema=schema,
                          reasoning_effort=args.reasoning_effort) as (argv, env):
        code, events, stderr, runtime = capture_opencode(argv, env, 120,
                                                        event_guard=generation_event_guard(workspace))
    (workspace / "events.jsonl").write_text("".join(json.dumps(record["event"]) + "\n" for record in events))
    (workspace / "stderr.log").write_text(stderr)
    if code == 0:
        write_result(events, schema, workspace / "question_result.json")
    probe = workspace / "network_probe.json"
    success = False
    if code == 0 and probe.is_file():
        success = json.loads(probe.read_text()).get("symbol") == "CDK2"
    report = {"condition": args.condition, "workspace": str(workspace), "command": command,
              "return_code": code, "runtime_seconds": runtime, "science_query_success": success}
    (workspace / "probe_manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

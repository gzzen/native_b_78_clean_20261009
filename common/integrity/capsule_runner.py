# Path: common/integrity/capsule_runner.py
# Purpose: Answer-blind generic one-call capsule execution harness with workspace isolation and freezing.
from __future__ import annotations
import concurrent.futures, json, time
from pathlib import Path
from typing import Any, Callable
from common.benchmark.load_bixbench import CAPSULES, capsule_groups
from common.integrity.environment import MODEL, REASONING_EFFORT, TIMEOUT_SECONDS, controlled_env, environment_manifest
from common.integrity.freeze import freeze_files
from common.integrity.hashing import write_json
from common.integrity.isolation_audit import audit_events_v121 as audit_events
from common.benchmark.a84_contract import workspace_temp_environment
from common.integrity.attempt_container import isolated_attempt
from common.integrity.opencode_capture import opencode_command, capture_opencode, write_result
from common.integrity.evaluator_access_policy import generation_event_guard, answer_blind_env

def _workspace(root: Path, rows: list[dict[str, Any]], environment: dict[str, Any]) -> Path:
    workspace = root / "0000" / f"{rows[0]['short_id']}__{rows[0]['capsule_uuid']}"
    workspace.mkdir(parents=True, exist_ok=False)
    write_json(workspace / "questions.json", rows)
    write_json(workspace / "environment_contract.json", environment)
    import shutil
    shutil.copytree(CAPSULES / f"CapsuleFolder-{rows[0]['capsule_uuid']}", workspace / "data", copy_function=shutil.copy2)
    return workspace

def _capture(command: list[str], env: dict[str, str], timeout: int) -> tuple[int | None, list[dict[str, Any]], str, float]:
    return capture_opencode(command, env, timeout)

def _validate(result: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    expected = [row["question_id"] for row in rows]
    got = [row.get("question_id") for row in result.get("results", [])]
    if got != expected:
        raise ValueError("result question IDs are not the exact public capsule order")

def run_one_shot(*, condition: str, prompt: Path, bash_env: Path, schema: Path, run_root: Path, execute: bool, workers: int, persistence_check: Callable[[Path, list[dict[str, Any]]], dict[str, Any]] | None = None, timeout: int = TIMEOUT_SECONDS) -> dict[str, Any]:
    if not 1 <= workers <= 59: raise ValueError("workers must be between 1 and 59")
    groups = capsule_groups(); environment = environment_manifest(bash_env, timeout)
    if not execute:
        return {"condition": condition, "status": "preflight_ok", "capsules": len(groups), "questions": sum(len(rows) for _, rows in groups)}
    if run_root.exists() and any(run_root.iterdir()): raise FileExistsError(run_root)
    prompt_text = prompt.read_text(encoding="utf-8")
    def one(rows: list[dict[str, Any]]) -> dict[str, Any]:
        workspace = _workspace(run_root, rows, environment)
        command = opencode_command(prompt_text, schema, MODEL)
        environment = answer_blind_env(controlled_env(bash_env))
        environment.update(workspace_temp_environment(workspace))
        with isolated_attempt(command, environment, workspace, schema=schema, reasoning_effort=REASONING_EFFORT) as (argv, env):
            code, events, stderr, elapsed = capture_opencode(argv, env, timeout, event_guard=generation_event_guard(workspace))
        write_json(workspace / "event_timeline.json", events); (workspace / "events.jsonl").write_text("".join(json.dumps(event["event"]) + "\n" for event in events)); (workspace / "stderr.log").write_text(stderr)
        record: dict[str, Any] = {"condition": condition, "short_id": rows[0]["short_id"], "capsule_uuid": rows[0]["capsule_uuid"], "question_ids": [row["question_id"] for row in rows], "workspace": str(workspace), "calls": 1, "retry": False, "runtime_seconds": elapsed, "return_code": code, "accepted": False}
        try:
            if code != 0: raise RuntimeError(f"OpenCode exit={code}")
            result = write_result(events, schema, workspace / "capsule_result.json")
            if persistence_check: record["code_provenance"] = persistence_check(workspace, events)
            record["isolation_audit"] = audit_events(events, workspace)
            _validate(result, rows)
            record["accepted"] = True
        except Exception as exc: record["failure_reason"] = f"{type(exc).__name__}: {exc}"
        write_json(workspace / "run_manifest.json", record); return record
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool: records = list(pool.map(lambda group: one(group[1]), groups))
    frozen = []
    for record in records:
        workspace = Path(record["workspace"])
        files = [workspace / name for name in ("capsule_result.json", "event_timeline.json", "events.jsonl", "stderr.log", "run_manifest.json", "isolation_boundary.json")]
        files += list((workspace / "analysis" / "scripts").rglob("*")) if (workspace / "analysis" / "scripts").is_dir() else []
        frozen += freeze_files(run_root.parents[1], files)
    return {"condition": condition, "generation": {"status": "frozen_before_private_scoring", "capsules": len(groups), "questions": sum(len(rows) for _, rows in groups), "calls": len(groups), "retry": False, "accepted_capsules": sum(record["accepted"] for record in records), "wall_seconds": time.monotonic() - started}, "environment": environment, "records": records, "freeze": frozen}

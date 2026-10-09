#!/usr/bin/env python3
# Path: conditions/native/runner.py
# Purpose: Run the raw Native baseline on the standalone 78-question scope with at most 59 concurrent workers.
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import common.benchmark.a84_contract as substrate
from common.benchmark.a84_contract import (
    CAPSULES,
    MODEL,
    QUESTIONS,
    REASONING_EFFORT,
    TIMEOUT_SECONDS,
    controlled_env,
    environment_manifest,
    load_rows,
    public_question,
    sha256_file,
    write_json,
)
from common.integrity.opencode_capture import (
    capture_opencode, command_metrics, opencode_command, write_result, agent_manifest, generation_failure,
)
from common.integrity.attempt_container import isolated_attempt
from common.integrity.progress import RunProgress, progress_log
from common.integrity.resume import finalize_attempts, frozen_summary, prepare_resume
from common.integrity.evaluator_access_policy import (
    MAX_POLICY_RETRIES,
    POLICY_EXIT_CODE,
    answer_blind_command,
    answer_blind_env,
    generation_event_guard,
    filesystem_boundary,
    policy_violation,
)
from conditions.a.runtime.compliance_audit import compliance_audit

SCHEMA = ROOT / "conditions/native/question_result_no_id.schema.json"
MANIFEST = ROOT / "analysis/native_78_manifest.json"
OUT = ROOT / "analysis/native_78_generation.json"
REPORT = ROOT / "reports/NATIVE_78_GENERATION.md"
PROMPT_CONTRACT = ROOT / "conditions/native/prompt.md"
RUNNABLE_SUFFIXES = {".py", ".r", ".sh"}
substrate.NATIVE_BASH_ENV = ROOT / "conditions/native/agent_bix_bash_env.sh"


def rows(selected_short_ids: set[str] | None = None,
         selected_capsule_uuids: set[str] | None = None) -> list[dict]:
    result = [public_question(row) for row in load_rows()]
    if len(result) != 78 or len({row["question_id"] for row in result}) != 78:
        raise RuntimeError("expected 78 unique public questions")
    if selected_short_ids is not None:
        found = {row["short_id"] for row in result}
        missing = selected_short_ids - found
        if missing:
            raise ValueError(f"unknown short IDs: {sorted(missing)}")
        result = [row for row in result if row["short_id"] in selected_short_ids]
    if selected_capsule_uuids is not None:
        found = {row["capsule_uuid"] for row in result}
        missing = selected_capsule_uuids - found
        if missing:
            raise ValueError(f"unknown capsule UUIDs: {sorted(missing)}")
        result = [row for row in result if row["capsule_uuid"] in selected_capsule_uuids]
    return result


def solver_prompt(row: dict) -> str:
    contract = PROMPT_CONTRACT.read_text(encoding="utf-8").strip()
    return row["question"].rstrip() + "\n\n" + contract + "\n"


def prompt_sha256s(items: list[dict]) -> dict[str, str]:
    return {
        row["question_id"]: hashlib.sha256(solver_prompt(row).encode()).hexdigest()
        for row in items
    }


def preflight(items: list[dict], timeout: int, workers: int, *, resume: bool = False) -> dict:
    question_count = len(items)
    manifest = {
        "experiment": "Native question-level executable-code one-shot",
        "granularity": "one independent OpenCode context per question",
        "question_count": question_count,
        "calls": question_count,
        "max_concurrent_workers": workers,
        "prompt_policy": "public question followed by the fixed Native executable-code contract",
        "identity_policy": "question_id is controller-bound metadata and is absent from model output",
        "question_prompt_sha256s": prompt_sha256s(items),
        "prompt_contract_sha256": sha256_file(PROMPT_CONTRACT),
        "runner_sha256": sha256_file(Path(__file__)),
        "schema_sha256": sha256_file(SCHEMA),
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "timeout_seconds": timeout,
        "ephemeral": True,
        "ignore_user_config": True,
        "agent": agent_manifest(MODEL, REASONING_EFFORT),
        "question_source_sha256": sha256_file(QUESTIONS),
        "environment": environment_manifest(),
        "retry_policy": {
            "ordinary_max_attempts": 1,
            "policy_violation_max_retries": MAX_POLICY_RETRIES,
            "policy_violation_action": "interrupt_and_fresh_workspace_retry",
            "second_policy_violation_action": "fail_closed",
            "answer_blind": True,
        },
        "evaluator_api_access": "forbidden",
        "private_evaluator_filesystem_boundary": filesystem_boundary(),
        "requires_task_code_persistence": True,
        "temporary_directory_policy": {
            "scope": "one private workspace-local tmp tree per question attempt",
            "environment_variables": [
                "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "MPLCONFIGDIR",
                "NUMBA_CACHE_DIR", "JOBLIB_TEMP_FOLDER",
            ],
            "shared_tmp": "forbidden",
        },
        "code_acceptance_policy": {
            "source_location": "analysis/scripts/",
            "runnable_suffixes": sorted(RUNNABLE_SUFFIXES),
            "persisted_source_required_for_every_question": True,
            "persisted_source_execution_required": True,
            "resolved_candidate_must_cite_persisted_source": True,
            "inline_or_temporary_only_substantive_code": "reject",
        },
        "reflection": False,
        "multihead": False,
        "generation_scorer_or_evaluator": False,
        "questions": [{key: row[key] for key in ("question_id", "short_id", "capsule_uuid")} for row in items],
    }
    if MANIFEST.exists():
        existing = json.loads(MANIFEST.read_text(encoding="utf-8"))
        old, new = json.loads(json.dumps(existing)), json.loads(json.dumps(manifest))
        for candidate in (old, new):
            candidate["environment"].pop("sha256", None)
            candidate["environment"]["values"].pop("PATH", None)
            if resume:
                # Resume changes scheduling only; its current code hash is
                # recorded separately while the original manifest is retained.
                candidate.pop("runner_sha256", None)
        if old != new:
            changed = sorted(key for key in old.keys() | new.keys()
                             if key not in old or key not in new or old[key] != new[key])
            raise RuntimeError(
                f"existing Native executable-code manifest mismatch: {MANIFEST}\n"
                f"Changed fields: {', '.join(changed)}. "
                "Use a fresh run tag for --manifest, --run-root, --output and --report; "
                "previous experiment artifacts must not be overwritten."
            )
        return existing
    write_json(MANIFEST, manifest)
    return manifest


def prepare_workspace(run_root: Path, row: dict, attempt_index: int) -> Path:
    workspace = run_root / f"{attempt_index:04d}" / row["question_id"]
    if workspace.exists():
        raise FileExistsError(f"refusing existing workspace: {workspace}")
    workspace.mkdir(parents=True)
    temp_environment = substrate.workspace_temp_environment(workspace)
    write_json(workspace / "question.json", row)
    environment_contract = dict(environment_manifest()["values"])
    environment_contract.update(temp_environment)
    environment_contract["temporary_directory_policy"] = {
        "scope": "one private workspace-local temp tree per question attempt",
        "required_usage": "use TMPDIR/TMP/TEMP; never use shared /tmp or /var/tmp",
        "environment_keys": list(substrate.WORKSPACE_TEMP_ENV_KEYS),
    }
    write_json(workspace / "environment_contract.json", environment_contract)
    source = CAPSULES / f"CapsuleFolder-{row['capsule_uuid']}"
    if not source.is_dir():
        raise FileNotFoundError(source)
    if os.environ.get("NATIVE_B_EXTERNAL_CONTAINER_ISOLATION") == "1":
        os.symlink(source, workspace / "data", target_is_directory=True)
    else:
        shutil.copytree(source, workspace / "data", copy_function=substrate.hardlink_or_copy)
    return workspace


def validate(payload: dict, row: dict) -> None:
    candidates = payload.get("candidates")
    if payload.get("status") not in {"resolved", "unresolved"} or not isinstance(candidates, list) or len(candidates) > 3:
        raise ValueError("status or candidate policy mismatch")
    if (payload["status"] == "resolved") != bool(candidates):
        raise ValueError("resolved/candidate mismatch")
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {"answer", "method", "evidence"}:
            raise ValueError("candidate shape mismatch")
        if not all(isinstance(candidate[key], str) and candidate[key].strip() for key in candidate):
            raise ValueError("empty candidate field")


def audit_and_enforce_code(workspace: Path, events: list[dict], payload: dict, question_id: str) -> tuple[dict, list[str]]:
    """Return the audit only when this one-question Native run is reproducible."""
    audit_result = {
        "question_id": question_id,
        "status": payload["status"],
        "candidates": payload["candidates"],
    }
    audit = compliance_audit(workspace, events, [audit_result])
    runnable_sources = [
        relative for relative in audit["persisted_source_files"]
        if Path(relative).suffix.lower() in RUNNABLE_SUFFIXES
    ]
    if audit["fatal"]:
        raise RuntimeError("fatal compliance breach: " + json.dumps(audit["fatal"], ensure_ascii=False))
    if not runnable_sources:
        raise RuntimeError("Native requires at least one runnable persisted source under analysis/scripts/")
    if not audit["executed_persisted_source"]:
        raise RuntimeError("Native requires actual execution of a persisted analysis/scripts source")
    if question_id in audit["question_local"]:
        raise RuntimeError(
            "question-local code provenance failure: "
            + json.dumps(audit["question_local"][question_id], ensure_ascii=False)
        )
    return audit, runnable_sources


def run_question_attempt(row: dict, run_root: Path, timeout: int, attempt_index: int) -> dict:
    label = f"Native/{row['short_id']}/{row['question_id']}/attempt{attempt_index + 1}"
    progress_log(label, "preparing workspace and isolated container")
    workspace = prepare_workspace(run_root, row, attempt_index)
    command = opencode_command(solver_prompt(row), SCHEMA, MODEL)
    with isolated_attempt(command, answer_blind_env(controlled_env(workspace)), workspace,
                          schema=SCHEMA, reasoning_effort=REASONING_EFFORT) as (argv, env):
        return_code, events, stderr, runtime = capture_opencode(
            argv, env, timeout, event_guard=generation_event_guard(workspace),
            policy_exit_code=POLICY_EXIT_CODE,
            progress_label=label,
        )
    write_json(workspace / "event_timeline.json", events)
    (workspace / "events.jsonl").write_text("".join(json.dumps(event["event"]) + "\n" for event in events), encoding="utf-8")
    (workspace / "stderr.log").write_text(stderr, encoding="utf-8")
    record = {
        "question_id": row["question_id"], "short_id": row["short_id"], "capsule_uuid": row["capsule_uuid"],
        "workspace": str(workspace), "attempt_index": attempt_index,
        "return_code": return_code, "runtime_seconds": runtime,
        "accepted": False, "controller_status": "failed", "event_metrics": command_metrics(events),
    }
    violation = policy_violation(events)
    if violation is not None:
        record.update({
            "controller_status": "interrupted_policy_violation",
            "policy_violation": violation,
            "failure_reason": "generation safety policy violation: " + str(violation.get("reason")),
            "isolation_audit": False,
        })
        write_json(workspace / "run_manifest.json", record)
        return record
    try:
        if return_code != 0:
            raise RuntimeError(generation_failure(return_code, events))
        payload = write_result(events, SCHEMA, workspace / "question_result.json")
        validate(payload, row)
        audit, runnable_sources = audit_and_enforce_code(workspace, events, payload, row["question_id"])
        write_json(workspace / "analysis/compliance_audit.json", audit)
        record.update({"accepted": True, "controller_status": "passed", "isolation_audit": True,
                       "identity_binding": "controller_workspace_record",
                       "resolved": payload["status"] == "resolved", "candidate_count": len(payload["candidates"]),
                       "path_audit_evidence_count": len(audit["path_audit_evidence"]),
                       "question_local_failures": audit["question_local"],
                       "soft_warnings": audit["soft"],
                       "code_provenance": {
                           "source_files": [
                               {"path": path, "sha256": sha256_file(workspace / path)}
                               for path in audit["persisted_source_files"]
                           ],
                           "runnable_source_files": runnable_sources,
                           "executed_persisted_source": audit["executed_persisted_source"],
                       }})
    except Exception as exc:
        record.update({"failure_reason": f"{type(exc).__name__}: {exc}", "isolation_audit": False})
    write_json(workspace / "run_manifest.json", record)
    return record


def run_question(row: dict, run_root: Path, timeout: int, *, prior_attempts: list[dict] | None = None) -> dict:
    attempts: list[dict] = list(prior_attempts or [])
    if attempts and (len(attempts) != 1 or "policy_violation" not in attempts[0]):
        raise ValueError("resume requires exactly one prior policy interruption")
    final: dict | None = None
    for attempt_index in range(len(attempts), MAX_POLICY_RETRIES + 1):
        current = run_question_attempt(row, run_root, timeout, attempt_index)
        attempts.append(current)
        final = current
        status = "passed" if current["accepted"] else "policy interrupted" if "policy_violation" in current else "failed"
        progress_log(f"Native/{row.get('question_id', 'question')}", f"attempt={attempt_index + 1} status={status}; logs saved")
        if "policy_violation" not in current:
            break
    assert final is not None
    final = finalize_attempts(attempts, MAX_POLICY_RETRIES)
    write_json(Path(final["workspace"]) / "run_manifest.json", final)
    return final


def main() -> None:
    global MANIFEST, MODEL, OUT, REASONING_EFFORT, REPORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true", help="reuse terminal outcomes and continue unfinished work in fresh workspaces")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--short-ids", help="comma-separated explicit capsule subset")
    parser.add_argument("--capsule-uuids", help="comma-separated exact capsule UUID subset")
    parser.add_argument("--timeout", type=int, default=TIMEOUT_SECONDS)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--reasoning-effort", default=REASONING_EFFORT)
    parser.add_argument("--run-root", type=Path, default=ROOT / "runs/native/native_78")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--report", type=Path, default=REPORT)
    args = parser.parse_args()
    if not 1 <= args.workers <= 59:
        raise SystemExit("--workers must be between 1 and 59")
    MANIFEST, OUT, REPORT = args.manifest.resolve(), args.output.resolve(), args.report.resolve()
    MODEL, REASONING_EFFORT = args.model, args.reasoning_effort
    substrate.MODEL, substrate.REASONING_EFFORT = MODEL, REASONING_EFFORT
    if args.resume and args.execute:
        summary = frozen_summary(OUT)
        if summary is not None:
            progress_log("Native", "generation already frozen; no agent calls made")
            print(json.dumps(summary, indent=2))
            return
    selected_short_ids = ({item.strip() for item in args.short_ids.split(",") if item.strip()}
                          if args.short_ids else None)
    selected_capsule_uuids = ({item.strip() for item in args.capsule_uuids.split(",") if item.strip()}
                              if args.capsule_uuids else None)
    items = rows(selected_short_ids, selected_capsule_uuids)
    manifest = (preflight(items, args.timeout, args.workers, resume=True) if args.resume
                else preflight(items, args.timeout, args.workers))
    if not args.execute:
        print(json.dumps({"status": "preflight_passed", "questions": len(items), "calls": len(items),
                          "workers": args.workers, "granularity": "question",
                          "evaluator_api_access": "forbidden",
                          "policy_violation_max_retries": MAX_POLICY_RETRIES}, indent=2))
        return
    nonempty = args.run_root.exists() and any(args.run_root.iterdir())
    if nonempty and not args.resume:
        raise SystemExit("refusing nonempty run root")
    # Detect unavailable output directories before any paid agent calls.
    OUT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    work_root = args.run_root.resolve()
    plan = {"completed": {}, "prior": {}}
    if nonempty:
        work_root, plan = prepare_resume(work_root, ROOT, "native", items, MANIFEST, Path(__file__), OUT)
        progress_log("Native", f"resume reusing={len(plan['completed'])} remaining={len(items) - len(plan['completed'])}")
    started = time.monotonic()
    progress = RunProgress("Native", len(items), args.workers)
    def execute(row):
        key = row["question_id"]
        if key in plan["completed"]:
            return progress.complete(plan["completed"][key])
        if key in plan["prior"]:
            return progress.complete(run_question(row, work_root, args.timeout, prior_attempts=plan["prior"][key]))
        return progress.complete(run_question(row, work_root, args.timeout))
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        records = list(pool.map(execute, items))
    progress_log("Native", "all questions finished; freezing artifacts and writing report")
    artifacts = []
    for record in records:
        for attempt in record["attempt_history"]:
            workspace = Path(attempt["workspace"])
            for path in (workspace / "question_result.json", workspace / "event_timeline.json", workspace / "events.jsonl",
                         workspace / "stderr.log", workspace / "run_manifest.json", workspace / "isolation_boundary.json", workspace / "analysis/compliance_audit.json",
                         *sorted((workspace / "analysis/scripts").rglob("*") if (workspace / "analysis/scripts").is_dir() else [])):
                if not path.is_file():
                    continue
                artifacts.append({"path": str(path.relative_to(ROOT)), "sha256": sha256_file(path) if path.is_file() else None})
    total_calls = sum(record["total_agent_calls"] for record in records)
    summary = {"questions": len(items), "calls": total_calls, "workers": min(args.workers, len(items)),
               "controller_passed_questions": sum(record["accepted"] for record in records),
               "retry_rate": (total_calls - len(items)) / len(items),
               "policy_interrupted_attempts": sum(
                   1 for record in records for attempt in record["attempt_history"]
                   if attempt["policy_violation"] is not None
               ),
               "runtime_seconds_all_attempts": sum(
                   attempt["runtime_seconds"] for record in records for attempt in record["attempt_history"]
               ),
               "wall_runtime_seconds": time.monotonic() - started,
               "scorer_or_evaluator_used_during_generation": False}
    write_json(OUT, {"manifest": manifest, "records": records,
                     "freeze": {"status": "frozen_before_private_scoring", "at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                                 "manifest_sha256": sha256_file(MANIFEST), "artifacts": artifacts},
                     "generation_summary": summary, "scoring": {"status": "not_started"},
                     **({"resume": plan["journal"]} if "journal" in plan else {})})
    REPORT.write_text("# Native Executable-code Question-level Generation\n\n" + f"> Path: `{REPORT}`\n\n" +
                      f"> Purpose: {len(items)} independent question calls; every accepted question must persist and execute runnable code. Question IDs are controller-bound.\n\n" +
                      f"Controller-passed questions: {summary['controller_passed_questions']}/{len(items)}.\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

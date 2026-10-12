# Path: common/integrity/resume.py
# Purpose: Run and resume attempt chains with policy and relay-infrastructure retry budgets.
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Callable

from common.integrity.progress import progress_log
from common.integrity.relay_failures import MAX_RELAY_RETRIES, RELAY_FAILURE_CLASSES
from common.integrity.relay_monitor import shared as shared_relay_monitor

POLICY_VIOLATION = "policy_violation"
RETRY_REASONS = {POLICY_VIOLATION: "prior_attempt_generation_safety_policy_violation",
                 **{name: "prior_attempt_" + name for name in RELAY_FAILURE_CLASSES}}
# A relay outage holds new attempts back instead of spending their retry budget.
RELAY_OUTAGE_WAIT_SECONDS = 1800


def retry_cause(record: dict) -> str | None:
    """Why an attempt may be re-run in a fresh workspace, if at all."""
    if "policy_violation" in record:
        return POLICY_VIOLATION
    cause = record.get("relay_failure")
    return cause if cause in RELAY_FAILURE_CLASSES else None


def _counts(attempts: list[dict]) -> tuple[int, int]:
    causes = [retry_cause(item) for item in attempts]
    return causes.count(POLICY_VIOLATION), sum(cause in RELAY_FAILURE_CLASSES for cause in causes)


def next_retry(attempts: list[dict], max_policy_retries: int, max_relay_retries: int) -> str | None:
    """The retry cause of the last attempt while its budget lasts; None when the chain is final."""
    cause = retry_cause(attempts[-1])
    policy, relay = _counts(attempts)
    if cause == POLICY_VIOLATION and policy <= max_policy_retries:
        return cause
    if cause in RELAY_FAILURE_CLASSES and relay <= max_relay_retries:
        return cause
    return None


def finalize_attempts(attempts: list[dict], max_policy_retries: int,
                      max_relay_retries: int = MAX_RELAY_RETRIES) -> dict:
    final = copy.deepcopy(attempts[-1])
    final["attempt_history"] = [{
        **{key: item.get(key) for key in (
            "attempt_index", "workspace", "return_code", "runtime_seconds", "controller_status",
            "policy_violation", "relay_failure", "failure_reason")},
        "retry_reason": RETRY_REASONS.get(retry_cause(attempts[index - 1])) if index else None,
    } for index, item in enumerate(attempts)]
    retried = [retry_cause(item) for item in attempts[:-1]]
    final.update(attempt_count=len(attempts), total_agent_calls=len(attempts), retry_attempted=len(attempts) > 1,
                 policy_retry_count=retried.count(POLICY_VIOLATION),
                 relay_retry_count=sum(cause in RELAY_FAILURE_CLASSES for cause in retried))
    if len(attempts) > 1:
        final["retry_reason"] = RETRY_REASONS[retried[-1]]
    policy, relay = _counts(attempts)
    if "policy_violation" in final and policy == max_policy_retries + 1:
        final["controller_status"] = "failed_closed_repeated_policy_violation"
    elif retry_cause(final) in RELAY_FAILURE_CLASSES and relay == max_relay_retries + 1:
        final["controller_status"] = "failed_relay_retries_exhausted"
    return final


def run_attempt_chain(run_attempt: Callable[[int], dict], label: str, prior_attempts: list[dict] | None = None,
                      *, max_policy_retries: int, max_relay_retries: int = MAX_RELAY_RETRIES,
                      relay_outage_wait: float = RELAY_OUTAGE_WAIT_SECONDS) -> dict:
    """Run fresh attempts until one is final; only policy and relay failures earn another."""
    attempts: list[dict] = list(prior_attempts or [])
    if attempts and next_retry(attempts, max_policy_retries, max_relay_retries) is None:
        raise ValueError("resume requires a pending policy or relay retry")
    while True:
        shared_relay_monitor().wait_ready(
            relay_outage_wait, on_wait=lambda remaining: progress_log(
                label, f"relay cooling down; waiting {remaining:.0f}s"))
        current = run_attempt(len(attempts))
        attempts.append(current)
        status = ("passed" if current["accepted"] else "policy interrupted"
                  if "policy_violation" in current else "failed")
        progress_log(label, f"attempt={len(attempts)} status={status}; logs saved")
        cause = next_retry(attempts, max_policy_retries, max_relay_retries)
        if cause is None:
            break
        if cause in RELAY_FAILURE_CLASSES:
            progress_log(label, f"relay failure class={cause}; re-attempt {_counts(attempts)[1]}/{max_relay_retries}")
    return finalize_attempts(attempts, max_policy_retries, max_relay_retries)


def attempt_workspaces(run_root: Path) -> list[Path]:
    stages = [run_root, *sorted(run_root.glob("resume_*"))]
    return [path for stage in stages for path in sorted(stage.glob("[0-9][0-9][0-9][0-9]/*")) if path.is_dir()]


def resume_plan(run_root: Path, condition: str, items: list[dict], max_policy_retries: int = 1,
                max_relay_retries: int = MAX_RELAY_RETRIES) -> dict:
    from common.integrity.attempt_container import validate_workspace_outputs
    key = "question_id" if condition == "native" else "capsule_uuid"
    expected = {item[key]: item for item in items}
    grouped: dict[str, list[dict]] = {}
    sources, interrupted = [], []
    for workspace in attempt_workspaces(run_root):
        if not workspace.resolve().is_relative_to(run_root.resolve()):
            raise RuntimeError("resume workspace escapes the original run root")
        path = workspace / "run_manifest.json"
        # Apply the same host output boundary before consuming old checkpoints.
        if path.exists() or path.is_symlink():
            validate_workspace_outputs(workspace)
        if not path.is_file():
            interrupted.append(str(workspace))
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            # A torn controller write is unfinished; preserve its original bytes.
            interrupted.append(str(workspace))
            continue
        if not isinstance(record, dict) or record.get(key) not in expected:
            raise RuntimeError(f"resume record identity mismatch: {path}")
        item = expected[record[key]]
        if any(record.get(field) != item.get(field) for field in ("short_id", "capsule_uuid")):
            raise RuntimeError(f"resume record scope mismatch: {path}")
        if condition == "b" and record.get("question_ids") != item.get("question_ids"):
            raise RuntimeError(f"resume B question coverage mismatch: {path}")
        required = {"accepted", "return_code", "runtime_seconds", "controller_status", "workspace", "attempt_index"}
        if not required <= record.keys():
            raise RuntimeError(f"incomplete resume record metadata: {path}")
        if Path(record["workspace"]).resolve() != workspace.resolve():
            raise RuntimeError(f"resume workspace binding mismatch: {path}")
        if record["attempt_index"] != int(workspace.parent.name):
            raise RuntimeError(f"resume attempt index mismatch: {path}")
        if not isinstance(record["accepted"], bool):
            raise RuntimeError(f"invalid acceptance metadata: {path}")
        if not all((workspace / name).is_file() for name in ("events.jsonl", "stderr.log", "event_timeline.json", "isolation_boundary.json")):
            raise RuntimeError(f"resume record lacks saved capture logs: {path}")
        result_name = "question_result.json" if condition == "native" else "capsule_result.json"
        if record["accepted"] and not (workspace / result_name).is_file():
            raise RuntimeError(f"accepted resume record lacks result: {path}")
        grouped.setdefault(record[key], []).append(record)
        sources.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    completed, prior = {}, {}
    for identity, records in grouped.items():
        attempts = []
        for record in records:
            if attempts and next_retry(attempts, max_policy_retries, max_relay_retries) is None:
                raise RuntimeError(f"duplicate terminal outcome during resume: {identity}")
            if record["attempt_index"] != len(attempts):
                raise RuntimeError(f"invalid retry history during resume: {identity}")
            attempts.append(record)
        if next_retry(attempts, max_policy_retries, max_relay_retries) is None:
            # Reconstruct a final wrapper write lost immediately after capture.
            completed[identity] = finalize_attempts(attempts, max_policy_retries, max_relay_retries)
        else:
            prior[identity] = attempts
    return {"completed": completed, "prior": prior, "source_records": sources,
            "interrupted_workspaces": interrupted}


def ensure_idle(run_root: Path, root: Path) -> None:
    from scripts.check_experiment_progress import runner_processes
    processes, error = runner_processes(root)
    if error:
        raise RuntimeError("cannot verify whether the original controller is still running")
    if any(pid != os.getpid() for pid in processes.get(str(run_root.resolve()), [])):
        raise RuntimeError("another controller is still running for this run root")
    result = subprocess.run(["docker", "ps", "--filter", "name=native-b-attempt-", "--format", "{{.ID}}"],
                            capture_output=True, text=True, timeout=15, check=True)
    ids = result.stdout.split()
    if ids:
        result = subprocess.run(["docker", "inspect", *ids], capture_output=True, text=True, timeout=15, check=True)
        for container in json.loads(result.stdout):
            if any(Path(mount.get("Source", "/")).resolve().is_relative_to(run_root.resolve())
                   for mount in container.get("Mounts", [])):
                raise RuntimeError("an attempt container is still using this run root: " + container["Id"][:12])


def prepare_resume(run_root: Path, root: Path, condition: str, items: list[dict], manifest: Path,
                   runner: Path, output: Path, max_relay_retries: int = MAX_RELAY_RETRIES) -> tuple[Path, dict]:
    if output.exists():
        raise RuntimeError("existing generation output is not frozen; preserve it and choose a new --output")
    ensure_idle(run_root, root)
    plan = resume_plan(run_root, condition, items, max_relay_retries=max_relay_retries)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + f"_{os.getpid()}"
    stage = run_root / f"resume_{stamp}"
    journal = {"status": "resume_prepared", "source_run_root": str(run_root.resolve()),
               "stage": str(stage.resolve()), "original_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
               "current_runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
               "resume_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "reused_terminal_units": sorted(plan["completed"]),
               "policy_retries_pending": sorted(key for key, attempts in plan["prior"].items()
                                                if retry_cause(attempts[-1]) == POLICY_VIOLATION),
               "relay_retries_pending": sorted(key for key, attempts in plan["prior"].items()
                                               if retry_cause(attempts[-1]) in RELAY_FAILURE_CLASSES),
               "source_records": plan["source_records"],
               "interrupted_workspaces": plan["interrupted_workspaces"],
               "interrupted_attempt_cost_and_runtime": "unknown when capture logs were not saved"}
    journal_path = manifest.with_name(manifest.stem + f"_resume_{stamp}.json")
    with journal_path.open("x", encoding="utf-8") as stream:
        json.dump(journal, stream, indent=2)
        stream.write("\n")
    plan["journal"] = {"path": str(journal_path), "sha256": hashlib.sha256(journal_path.read_bytes()).hexdigest()}
    return stage, plan


def frozen_summary(output: Path) -> dict | None:
    if not output.exists():
        return None
    payload = json.loads(output.read_text())
    if payload.get("freeze", {}).get("status") == "frozen_before_private_scoring":
        return payload["generation_summary"]
    return None

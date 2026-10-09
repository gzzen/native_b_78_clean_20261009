#!/usr/bin/env python3
# Path: conditions/b/runner.py
# Purpose: Condition A plus compatibility-first grounding and operation contracts.

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from conditions.a import runner as condition_a
from conditions.b import components as b_components
from conditions.b.operation_contract import audit_operation_contract
from conditions.b.operation_discovery import discover_operation_hints
from conditions.b.grounding.consistency import validate_consistency
from common.integrity.evaluator_access_policy import (
    MAX_POLICY_RETRIES,
    POLICY_EXIT_CODE,
    answer_blind_command,
    answer_blind_env,
    generation_event_guard,
    filesystem_boundary,
    policy_violation,
)
from common.integrity.attempt_container import isolated_attempt
from common.integrity.progress import RunProgress, progress_log
from common.integrity.resume import finalize_attempts, frozen_summary, prepare_resume
from common.integrity.opencode_capture import opencode_command, write_result, agent_manifest, generation_failure


# B = complete A execution/compliance substrate + B-only semantic grounding.
BASE_CONDITION = condition_a

MODEL = BASE_CONDITION.MODEL
NATIVE_SCHEMA = BASE_CONDITION.NATIVE_SCHEMA
QUESTIONS = BASE_CONDITION.QUESTIONS
REASONING_EFFORT = BASE_CONDITION.REASONING_EFFORT
TIMEOUT_SECONDS = BASE_CONDITION.TIMEOUT_SECONDS

candidate_policy = BASE_CONDITION.candidate_policy
controlled_env = BASE_CONDITION.controlled_env
environment_manifest = BASE_CONDITION.environment_manifest
load_rows = BASE_CONDITION.load_rows
public_question = BASE_CONDITION.public_question
sha256_file = BASE_CONDITION.sha256_file
write_json = BASE_CONDITION.write_json
prepare_workspace = BASE_CONDITION.prepare_workspace
validate_native_result = BASE_CONDITION.validate_native_result
capture_opencode = BASE_CONDITION.capture_opencode
command_metrics = BASE_CONDITION.command_metrics
compliance_audit = BASE_CONDITION.compliance_audit

COMPONENT_PROMPT = ROOT / "conditions/b/prompt.md"
SOLVER_PROMPT = b_components.compose_solver_prompt(
    BASE_CONDITION.SOLVER_PROMPT,
    COMPONENT_PROMPT,
)

GROUNDING_SCHEMA = ROOT / "conditions/b/grounding/grounding.schema.json"

GROUNDING_FIELDS = (
    "data_object",
    "observation_unit",
    "comparison",
    "metric_or_outcome",
    "unit_or_scale",
    "denominator_or_population",
    "method_or_estimator",
)

MANIFEST = ROOT / "analysis/b_78_manifest.json"
OUT = ROOT / "analysis/b_78_generation.json"
REPORT = ROOT / "reports/B_78_GENERATION.md"


def capsules(selected_short_ids: set[str] | None = None,
             selected_capsule_uuids: set[str] | None = None):
    items = BASE_CONDITION.caps()

    if selected_short_ids is not None:
        found = {item["short_id"] for item in items}
        missing = selected_short_ids - found
        if missing:
            raise ValueError(f"unknown short IDs: {sorted(missing)}")
        items = [item for item in items if item["short_id"] in selected_short_ids]
    if selected_capsule_uuids is not None:
        found = {item["capsule_uuid"] for item in items}
        missing = selected_capsule_uuids - found
        if missing:
            raise ValueError(f"unknown capsule UUIDs: {sorted(missing)}")
        items = [item for item in items if item["capsule_uuid"] in selected_capsule_uuids]
    return items


def preflight(items, timeout, selected_short_ids, selected_capsule_uuids=None, *, resume=False):
    b_components.validate_component_prompt(COMPONENT_PROMPT)

    selected_uuids = {
        item["capsule_uuid"]
        for item in items
    }

    rows = [
        public_question(row)
        for row in load_rows()
        if row["capsule_uuid"] in selected_uuids
    ]

    capsule_count = len(items)
    question_count = len(rows)

    scope = (
        "standalone-78"
        if selected_short_ids is None and selected_capsule_uuids is None
        else "explicit-diagnostic-subset"
    )

    manifest = {
        "experiment": (
            f"B-compatibility = A + compatibility-first selective semantic grounding "
            f"{scope} one-shot"
        ),
        "extends_condition": "a",

        "base_runner_path": str(
            Path(BASE_CONDITION.__file__).resolve().relative_to(ROOT)
        ),
        "base_runner_sha256": sha256_file(
            Path(BASE_CONDITION.__file__).resolve()
        ),

        "base_solver_prompt_sha256": hashlib.sha256(
            BASE_CONDITION.SOLVER_PROMPT.encode()
        ).hexdigest(),

        "component_prompt_path": str(
            COMPONENT_PROMPT.relative_to(ROOT)
        ),
        "component_prompt_sha256": sha256_file(
            COMPONENT_PROMPT
        ),

        "composed_solver_prompt_sha256": hashlib.sha256(
            SOLVER_PROMPT.encode()
        ).hexdigest(),

        "grounding_schema_sha256": sha256_file(
            GROUNDING_SCHEMA
        ),

        "scope": scope,
        "selected_short_ids": (
            sorted(selected_short_ids)
            if selected_short_ids is not None
            else None
        ),
        "selected_capsule_uuids": (
            sorted(selected_capsule_uuids)
            if selected_capsule_uuids is not None
            else None
        ),

        "runner_sha256": sha256_file(
            Path(__file__)
        ),

        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "timeout_seconds": timeout,
        "ephemeral": True,
        "ignore_user_config": True,
        "agent": agent_manifest(MODEL, REASONING_EFFORT),

        "question_source_sha256": sha256_file(
            QUESTIONS
        ),

        "public_question_envelope_sha256": hashlib.sha256(
            json.dumps(
                rows,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),

        "environment": environment_manifest(),
        "candidate_policy": candidate_policy(),

        "calls_per_capsule": 1,
        "retry_policy": {
            "ordinary_max_attempts": 1,
            "policy_violation_max_retries": MAX_POLICY_RETRIES,
            "policy_violation_action": "interrupt_and_fresh_workspace_retry",
            "second_policy_violation_action": "fail_closed",
        },
        "evaluator_api_access": "forbidden",
        "private_evaluator_filesystem_boundary": filesystem_boundary(),
        "reflection": False,
        "multihead": False,

        "inherits_a_graded_compliance": True,
        "requires_task_code_persistence": True,
        "temporary_directory_policy": {
            "scope": "one private workspace-local tmp tree per capsule attempt",
            "environment_variables": [
                "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "MPLCONFIGDIR",
                "NUMBA_CACHE_DIR", "JOBLIB_TEMP_FOLDER",
            ],
            "shared_tmp": "forbidden",
        },

        "grounding_mode": "selective",
        "grounding_fields": [
            "question_id",
            *GROUNDING_FIELDS,
        ],

        "interpretations_required": (
            "only_when_genuine_ambiguity"
        ),
        "interpretations_if_present": "2-3",
        "selection_evidence_required_if_contrastive": True,

        "execution_record_required": False,
        "consistency_checker_required": False,
        "analysis_plan_required": False,

        "generation_scorer_or_evaluator": False,

        "capsule_count": capsule_count,
        "question_count": question_count,
        "capsules": items,
    }

    if MANIFEST.exists():
        existing = json.loads(
            MANIFEST.read_text(encoding="utf-8")
        )

        old = json.loads(json.dumps(existing))
        new = json.loads(json.dumps(manifest))

        for candidate in (old, new):
            candidate["environment"].pop(
                "sha256",
                None,
            )
            candidate["environment"]["values"].pop(
                "PATH",
                None,
            )
            if resume:
                candidate.pop("runner_sha256", None)

        if old != new:
            raise RuntimeError(
                "existing B-compatibility manifest mismatch"
            )

        manifest = existing

    else:
        write_json(
            MANIFEST,
            manifest,
        )

    return manifest


def grounding_record(workspace, rows):
    path = workspace / "analysis/grounding.json"

    if not path.is_file():
        raise ValueError(
            "missing analysis/grounding.json"
        )

    payload = json.loads(
        path.read_text(encoding="utf-8")
    )

    expected = [
        row["question_id"]
        for row in rows
    ]

    if payload.get("capsule_uuid") != rows[0]["capsule_uuid"]:
        raise ValueError(
            "grounding capsule identity mismatch"
        )

    questions = payload.get("questions")

    if not isinstance(questions, list):
        raise ValueError(
            "grounding questions must be a list"
        )

    if [
        question.get("question_id")
        for question in questions
    ] != expected:
        raise ValueError(
            "grounding question coverage/order mismatch"
        )

    for question in questions:
        qid = question.get("question_id")

        status = question.get(
            "grounding_status"
        )

        rationale = question.get(
            "selection_rationale"
        )

        if status not in {"resolved", "partially_resolved", "unresolved"}:
            raise ValueError(
                f"invalid grounding status for {qid}"
            )

        if (
            not isinstance(rationale, str)
            or not rationale.strip()
        ):
            raise ValueError(
                f"missing grounding rationale for {qid}"
            )

        for field in GROUNDING_FIELDS:
            value = question.get(field)

            if (
                not isinstance(value, str)
                or not value.strip()
            ):
                raise ValueError(
                    f"incomplete grounding "
                    f"{field} for {qid}"
                )

        # Grounding confidence and answer production are independent axes.
        # An unresolved semantic choice may still record the concrete object,
        # unit, comparison, and method that were actually analysed.
        result_status = question.get("result_status")
        if result_status is not None and result_status not in {
            "answered", "unresolved", "execution_failed",
        }:
            raise ValueError(f"invalid result status for {qid}")

        interpretations = question.get(
            "interpretations"
        )

        # Clear questions need no contrastive alternatives.
        if interpretations is None:
            if (
                status == "resolved"
                and question.get(
                    "selected_interpretation_id"
                )
                not in {None, ""}
            ):
                raise ValueError(
                    f"{qid} selects an interpretation "
                    f"but has no interpretations list"
                )

            continue

        # If the solver explicitly declares ambiguity,
        # require 2-3 real alternatives.
        if (
            not isinstance(interpretations, list)
            or len(interpretations) not in {2, 3}
        ):
            raise ValueError(
                f"optional interpretations for {qid} "
                f"must contain 2 or 3 items"
            )

        ids: list[str] = []

        for interpretation in interpretations:
            if not isinstance(
                interpretation,
                dict,
            ):
                raise ValueError(
                    f"invalid interpretation for {qid}"
                )

            identifier = interpretation.get("id")
            evidence = interpretation.get("evidence")

            if (
                not isinstance(identifier, str)
                or not identifier.strip()
                or identifier in ids
            ):
                raise ValueError(
                    f"invalid interpretation ID for {qid}"
                )

            ids.append(identifier)

            if (
                not isinstance(evidence, str)
                or not evidence.strip()
            ):
                raise ValueError(
                    f"missing interpretation evidence "
                    f"for {qid}"
                )

            for field in GROUNDING_FIELDS:
                value = interpretation.get(field)

                if (
                    not isinstance(value, str)
                    or not value.strip()
                ):
                    raise ValueError(
                        f"incomplete interpretation "
                        f"{field} for {qid}"
                    )

        selected = question.get(
            "selected_interpretation_id"
        )

        if status == "resolved":
            if selected not in ids:
                raise ValueError(
                    f"selected interpretation invalid "
                    f"for {qid}"
                )

            chosen = next(
                item
                for item in interpretations
                if item["id"] == selected
            )

            for field in GROUNDING_FIELDS:
                if (
                    question[field].strip()
                    != chosen[field].strip()
                ):
                    raise ValueError(
                        f"locked grounding differs from "
                        f"selected interpretation for "
                        f"{qid}: {field}"
                    )

        else:
            if selected not in {None, ""}:
                raise ValueError(
                    f"non-resolved grounding cannot select "
                    f"an interpretation for {qid}"
                )

    return {
        "path": "analysis/grounding.json",
        "sha256": sha256_file(path),
    }


def audit_grounding_component(workspace, rows):
    """Audit B grounding without invalidating an otherwise valid A result.

    B is additive: A's native result and graded compliance are authoritative
    for capsule viability.  A malformed B annotation is therefore recorded as
    a question-local B failure, while the already generated A-compatible
    candidates remain available.  This function never invents or repairs
    grounding content.
    """
    path = workspace / "analysis/grounding.json"
    expected = [row["question_id"] for row in rows]
    try:
        artifact = grounding_record(workspace, rows)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            "passed": True,
            "artifact": artifact,
            "payload": payload,
            "question_local_failures": {},
        }
    except Exception as exc:
        message = f"B grounding audit failed: {type(exc).__name__}: {exc}"
        mentioned = [qid for qid in expected if qid in str(exc)]
        affected = mentioned or expected
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                payload = {"questions": []}
        except (OSError, json.JSONDecodeError):
            payload = {"questions": []}
        return {
            "passed": False,
            "artifact": {
                "path": "analysis/grounding.json",
                "sha256": sha256_file(path) if path.is_file() else None,
            },
            "payload": payload,
            "question_local_failures": {qid: message for qid in affected},
        }


def audit_operation_component(workspace, rows, grounding, results):
    """Run B's deterministic operation audit behind an additive boundary."""
    try:
        report = audit_operation_contract(workspace, rows, grounding, results)
        return report, {}
    except Exception as exc:
        message = f"B operation audit failed: {type(exc).__name__}: {exc}"
        return {
            "checker": "b_additive_named_operation_contract_v1",
            "passed": False,
            "audit_error": message,
            "question_local_failures": {
                row["question_id"]: message for row in rows
            },
        }, {row["question_id"]: message for row in rows}


def audit_consistency_component(workspace, rows):
    """Run the optional locked-grounding/execution check without gating A."""
    execution_path = workspace / "analysis/execution.json"
    destination = workspace / "analysis/consistency.json"
    if not execution_path.is_file():
        report = {
            "checker": "deterministic_contrastive_locked_binding_v3",
            "status": "unavailable_no_execution_record",
            "passed": True,
            "question_local_failures": {},
        }
        write_json(destination, report)
        return report, {}
    try:
        report = validate_consistency(workspace, rows, output_path=destination)
        return report, {}
    except Exception as exc:
        # The check is additive and question-local. Preserve A's candidate and
        # report any conflict for B scoring/audit rather than making the whole
        # capsule fail or attempting an unrequested semantic retry.
        message = f"B consistency audit failed: {type(exc).__name__}: {exc}"
        try:
            report = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {
                "checker": "deterministic_contrastive_locked_binding_v3",
                "passed": False,
                "audit_error": message,
            }
            write_json(destination, report)
        failures = {
            str(item.get("question_id")): message
            for item in report.get("questions", [])
            if isinstance(item, dict) and item.get("consistent") is False
        }
        if not failures:
            # A malformed execution record has no reliable question scope;
            # conservatively mark the B component local to this capsule while
            # preserving every A candidate. This is visible in the manifest,
            # never silently reported as a passed B audit.
            failures = {row["question_id"]: message for row in rows}
            report["question_local_failures"] = failures
            write_json(destination, report)
        return report, failures


def canonicalize_result_identity(payload, rows):
    """Repair only a provable UUID/short-id envelope transposition.

    A's validator remains authoritative for question coverage and candidate
    shape. This B-only transport step never changes results or accepts an
    arbitrary identity: it succeeds only when the two expected identity values
    were swapped in the output envelope for the already-bound workspace.
    """
    expected_uuid = rows[0]["capsule_uuid"]
    expected_short_id = rows[0]["short_id"]
    observed_uuid = payload.get("capsule_uuid")
    observed_short_id = payload.get("short_id")
    if observed_uuid == expected_uuid and observed_short_id == expected_short_id:
        return payload, None
    if observed_uuid == expected_uuid and observed_short_id == expected_uuid:
        repaired = dict(payload)
        repaired["short_id"] = expected_short_id
        return repaired, {
            "field": "short_id",
            "from": observed_short_id,
            "to": expected_short_id,
            "reason": "short_id duplicated capsule_uuid",
        }
    if observed_uuid == expected_uuid and observed_short_id in {
        expected_uuid.split("-", 1)[0],
        expected_uuid[:8],
    }:
        repaired = dict(payload)
        repaired["short_id"] = expected_short_id
        return repaired, {
            "field": "short_id",
            "from": observed_short_id,
            "to": expected_short_id,
            "reason": "short_id was a capsule_uuid prefix",
        }
    if observed_uuid == expected_short_id and observed_short_id == expected_short_id:
        repaired = dict(payload)
        repaired["capsule_uuid"] = expected_uuid
        return repaired, {
            "field": "capsule_uuid",
            "from": observed_uuid,
            "to": expected_uuid,
            "reason": "capsule_uuid duplicated short_id",
        }
    slug = f"{expected_short_id}__{expected_uuid}"
    if observed_uuid == slug and observed_short_id == expected_short_id:
        repaired = dict(payload)
        repaired["capsule_uuid"] = expected_uuid
        return repaired, {
            "field": "capsule_uuid",
            "from": observed_uuid,
            "to": expected_uuid,
            "reason": "capsule_uuid was the bound workspace slug",
        }
    return payload, None


def run_capsule_attempt(
    item,
    rows,
    run_root,
    timeout,
    attempt_index,
):
    label = f"B/{item['short_id']}/attempt{attempt_index + 1}"
    progress_log(label, "preparing workspace and isolated container")
    workspace, _ = prepare_workspace(
        f"{attempt_index:04d}",
        run_root,
        rows,
    )

    write_json(
        workspace / "operation_hints.json",
        discover_operation_hints(rows),
    )

    command = opencode_command(SOLVER_PROMPT, NATIVE_SCHEMA, MODEL)

    with isolated_attempt(command, answer_blind_env(controlled_env(workspace)), workspace,
                          schema=NATIVE_SCHEMA, reasoning_effort=REASONING_EFFORT) as (argv, env):
        return_code, events, stderr, runtime = capture_opencode(
            argv, env, timeout, event_guard=generation_event_guard(workspace),
            policy_exit_code=POLICY_EXIT_CODE,
            progress_label=label,
        )

    write_json(
        workspace / "event_timeline.json",
        events,
    )

    (
        workspace / "events.jsonl"
    ).write_text(
        "".join(
            json.dumps(event["event"]) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )

    (
        workspace / "stderr.log"
    ).write_text(
        stderr,
        encoding="utf-8",
    )

    record = {
        key: item[key]
        for key in (
            "short_id",
            "capsule_uuid",
            "question_ids",
        )
    }

    record.update({
        "workspace": str(workspace),
        "attempt_index": attempt_index,
        "total_agent_calls": 1,
        "attempt_count": 1,
        "retry_attempted": False,
        "return_code": return_code,
        "runtime_seconds": runtime,
        "controller_status": "failed",
        "accepted": False,
        "event_metrics": command_metrics(events),
    })

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

        result = write_result(events, NATIVE_SCHEMA, workspace / "capsule_result.json")

        result, identity_repair = canonicalize_result_identity(result, rows)
        if identity_repair is not None:
            write_json(workspace / "capsule_result.json", result)

        results = validate_native_result(
            result,
            rows,
        )

        # A's complete graded compliance remains active.
        audit = compliance_audit(
            workspace,
            events,
            results,
        )

        write_json(
            workspace
            / "analysis/compliance_audit.json",
            audit,
        )

        if audit["fatal"]:
            raise RuntimeError(
                "fatal A compliance breach: "
                + json.dumps(
                    audit["fatal"],
                    ensure_ascii=False,
                )
            )

        # B components are additive question-local audits. They cannot erase
        # an A-valid native result or turn it into a capsule-level failure.
        grounding_audit = audit_grounding_component(
            workspace,
            rows,
        )

        operation_audit, operation_internal_failures = audit_operation_component(
            workspace,
            rows,
            grounding_audit["payload"],
            results,
        )
        consistency_audit, consistency_failures = audit_consistency_component(
            workspace,
            rows,
        )

        scripts = (
            sorted(
                (
                    workspace
                    / "analysis/scripts"
                ).rglob("*")
            )
            if (
                workspace
                / "analysis/scripts"
            ).is_dir()
            else []
        )

        scripts = [
            path
            for path in scripts
            if path.is_file()
        ]

        local_failures = audit[
            "question_local"
        ]
        local_failures = {
            **local_failures,
            **operation_audit["question_local_failures"],
            **operation_internal_failures,
        }

        b_local_failures = {
            **operation_audit["question_local_failures"],
            **operation_internal_failures,
        }

        record.update({
            "controller_status": "passed",
            "accepted": True,
            "isolation_audit": True,

            "base_condition": "a",
            "a_compliance_passed": True,
            "identity_transport_repair": identity_repair,
            "a_question_local_failures": audit["question_local"],
            "b_component_status": (
                "passed" if not b_local_failures else "question_local_failed"
            ),
            "b_grounding_status": (
                "passed"
                if grounding_audit["passed"]
                else "audit_failed_metadata_only"
            ),
            "b_question_local_failures": b_local_failures,
            "b_grounding_failures": grounding_audit["question_local_failures"],
            "b_consistency_failures": consistency_failures,
            "consistency_component": {
                "status": consistency_audit.get("status", "completed"),
                "passed": consistency_audit.get("passed", False),
                "question_local_failures": consistency_failures,
            },

            "path_audit_evidence_count": len(
                audit["path_audit_evidence"]
            ),

            "question_local_failures": (
                local_failures
            ),

            "soft_warnings": audit["soft"],

            "operation_contract": operation_audit,

            "grounding": grounding_audit["artifact"],
            "grounding_component": {
                key: value
                for key, value in grounding_audit.items()
                if key != "payload"
            },

            "code_provenance": {
                "source_files": [
                    {
                        "path": str(
                            path.relative_to(
                                workspace
                            )
                        ),
                        "sha256": sha256_file(
                            path
                        ),
                    }
                    for path in scripts
                ]
            },

            "question_completion": len(
                results
            ),

            "resolved": sum(
                result_item["status"]
                == "resolved"
                and result_item["question_id"]
                not in local_failures
                for result_item in results
            ),

            "unresolved": sum(
                result_item["status"]
                == "unresolved"
                or result_item["question_id"]
                in local_failures
                for result_item in results
            ),
        })

    except Exception as exc:
        record.update({
            "failure_reason": (
                f"{type(exc).__name__}: {exc}"
            ),
            "isolation_audit": False,
        })

    write_json(
        workspace / "run_manifest.json",
        record,
    )

    return record


def run_capsule(item, rows, run_root, timeout, *, prior_attempts=None):
    attempts = list(prior_attempts or [])
    if attempts and (len(attempts) != 1 or "policy_violation" not in attempts[0]):
        raise ValueError("resume requires exactly one prior policy interruption")
    final = None
    for attempt_index in range(len(attempts), MAX_POLICY_RETRIES + 1):
        current = run_capsule_attempt(item, rows, run_root, timeout, attempt_index)
        attempts.append(current)
        final = current
        status = "passed" if current["accepted"] else "policy interrupted" if "policy_violation" in current else "failed"
        progress_log(f"B/{item.get('short_id', 'capsule')}", f"attempt={attempt_index + 1} status={status}; logs saved")
        if "policy_violation" not in current:
            break
    assert final is not None
    final = finalize_attempts(attempts, MAX_POLICY_RETRIES)
    write_json(Path(final["workspace"]) / "run_manifest.json", final)
    return final


def main():
    global MANIFEST, MODEL, OUT, REASONING_EFFORT, REPORT

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--execute",
        action="store_true",
    )
    parser.add_argument("--resume", action="store_true", help="reuse terminal outcomes and continue unfinished work in fresh workspaces")

    parser.add_argument(
        "--workers",
        type=int,
        default=2,
    )

    parser.add_argument(
        "--short-ids",
        help=(
            "comma-separated explicit diagnostic "
            "subset; default is full 78"
        ),
    )
    parser.add_argument(
        "--capsule-uuids",
        help="comma-separated exact capsule UUID subset",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=TIMEOUT_SECONDS,
    )

    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--reasoning-effort", default=REASONING_EFFORT)

    parser.add_argument(
        "--run-root",
        type=Path,
        default=(
            ROOT
            / "runs/b/"
            "b_v73_additive_baseline_full205"
        ),
    )

    parser.add_argument(
        "--manifest",
        type=Path,
        default=MANIFEST,
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=OUT,
    )

    parser.add_argument(
        "--report",
        type=Path,
        default=REPORT,
    )

    args = parser.parse_args()

    MANIFEST = args.manifest.resolve()
    OUT = args.output.resolve()
    REPORT = args.report.resolve()
    MODEL = args.model
    REASONING_EFFORT = args.reasoning_effort
    BASE_CONDITION.MODEL = MODEL
    BASE_CONDITION.REASONING_EFFORT = REASONING_EFFORT
    BASE_CONDITION.substrate.MODEL = MODEL
    BASE_CONDITION.substrate.REASONING_EFFORT = REASONING_EFFORT
    if args.resume and args.execute:
        summary = frozen_summary(OUT)
        if summary is not None:
            progress_log("B", "generation already frozen; no agent calls made")
            print(json.dumps(summary, indent=2))
            return

    if args.workers < 1:
        raise SystemExit(
            "workers must be positive"
        )

    selected_short_ids = (
        {
            item.strip()
            for item
            in args.short_ids.split(",")
            if item.strip()
        }
        if args.short_ids
        else None
    )
    selected_capsule_uuids = (
        {item.strip() for item in args.capsule_uuids.split(",") if item.strip()}
        if args.capsule_uuids
        else None
    )

    items = capsules(
        selected_short_ids,
        selected_capsule_uuids,
    )

    manifest = preflight(
        items,
        args.timeout,
        selected_short_ids,
        selected_capsule_uuids,
        **({"resume": True} if args.resume else {}),
    )

    question_count = sum(
        len(item["question_ids"])
        for item in items
    )

    if not args.execute:
        print(
            json.dumps({
                "status": "preflight_passed",
                "capsules": len(items),
                "questions": question_count,
                "ordinary_max_attempts": 1,
                "policy_violation_max_retries": MAX_POLICY_RETRIES,
                "evaluator_api_access": "forbidden",
                "condition": "B-compatibility-first-selective-grounding",
            }, indent=2)
        )
        return

    nonempty = (
        args.run_root.exists()
        and any(args.run_root.iterdir())
    )
    if nonempty and not args.resume:
        raise SystemExit(
            "refusing nonempty run root"
        )

    # Detect unavailable output directories before any paid agent calls.
    OUT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    work_root = args.run_root.resolve()
    plan = {"completed": {}, "prior": {}}
    if nonempty:
        work_root, plan = prepare_resume(work_root, ROOT, "b", items, MANIFEST, Path(__file__), OUT)
        progress_log("B", f"resume reusing={len(plan['completed'])} remaining={len(items) - len(plan['completed'])}")
    started = time.monotonic()
    all_rows = load_rows()

    tasks = [
        (
            item,
            [
                public_question(row)
                for row in all_rows
                if row["capsule_uuid"]
                == item["capsule_uuid"]
            ],
        )
        for item in items
    ]

    progress = RunProgress("B", len(tasks), args.workers)
    def execute(task):
        item, task_rows = task
        key = item["capsule_uuid"]
        if key in plan["completed"]:
            return progress.complete(plan["completed"][key])
        if key in plan["prior"]:
            return progress.complete(run_capsule(item, task_rows, work_root, args.timeout, prior_attempts=plan["prior"][key]))
        return progress.complete(run_capsule(item, task_rows, work_root, args.timeout))
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(
            args.workers,
            len(tasks),
        )
    ) as pool:
        records = list(
            pool.map(
                execute,
                tasks,
            )
        )

    progress_log("B", "all capsules finished; freezing artifacts and writing report")
    artifacts = []

    for record in records:
        for attempt in record["attempt_history"]:
            workspace = Path(attempt["workspace"])

            files = [
                workspace / name
                for name in (
                    "capsule_result.json",
                    "event_timeline.json",
                    "events.jsonl",
                    "stderr.log",
                    "run_manifest.json",
                    "isolation_boundary.json",
                )
            ]

            files += [
                workspace / "operation_hints.json",

                workspace
                / "analysis/grounding.json",

                workspace
                / "analysis/compliance_audit.json",

                workspace
                / "analysis/operation_contract.json",

                workspace
                / "analysis/consistency.json",
            ]

            if (
                workspace
                / "analysis/scripts"
            ).is_dir():
                files += [
                    path
                    for path in (
                        workspace
                        / "analysis/scripts"
                    ).rglob("*")
                    if path.is_file()
                ]

            files = list(
                dict.fromkeys(files)
            )

            artifacts += [
                {
                    "path": str(
                        path.relative_to(ROOT)
                    ),
                    "sha256": (
                        sha256_file(path)
                        if path.is_file()
                        else None
                    ),
                }
                for path in files
            ]

    total_calls = sum(record["total_agent_calls"] for record in records)

    summary = {
        "capsules": len(items),
        "questions": question_count,

        "accepted_capsules": sum(
            bool(record["accepted"])
            for record in records
        ),

        "total_agent_calls": total_calls,
        "retry_rate": (total_calls - len(items)) / len(items),
        "mean_attempts_per_capsule": total_calls / len(items),
        "policy_interrupted_attempts": sum(
            1 for record in records for attempt in record["attempt_history"]
            if attempt["policy_violation"] is not None
        ),

        "runtime_seconds_all_attempts": sum(
            float(attempt["runtime_seconds"])
            for record in records for attempt in record["attempt_history"]
        ),

        "wall_runtime_seconds": (
            time.monotonic()
            - started
        ),

        "scorer_or_evaluator_used_during_generation": False,
    }

    write_json(
        OUT,
        {
            "manifest": manifest,
            "episodes": records,

            "freeze": {
                "status": (
                    "frozen_before_private_scoring"
                ),
                "at_utc": dt.datetime.now(
                    dt.timezone.utc
                ).isoformat(),

                "manifest_sha256": sha256_file(
                    MANIFEST
                ),

                "artifacts": artifacts,
            },

            "generation_summary": summary,
            **({"resume": plan["journal"]} if "journal" in plan else {}),

            "scoring": {
                "status": "not_started"
            },
        },
    )

    REPORT.write_text(
        "# B compatibility-first: A + Selective Semantic Grounding\n\n"
        + f"> Path: `{REPORT}`\n\n"
        + "> Purpose: fresh one-shot B generation built "
          "from complete Condition A plus only selective "
          "semantic grounding. Private scoring occurs "
          "only after freeze.\n\n"
        + f"Controller-passed capsules: "
          f"{summary['accepted_capsules']}/{len(items)}.\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            summary,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# Path: conditions/b/grounding/consistency.py
# Purpose: B-v5 locked-grounding versus actual-execution consistency check.
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

FIELDS = (
    "data_object",
    "observation_unit",
    "comparison",
    "metric_or_outcome",
    "unit_or_scale",
    "denominator_or_population",
    "method_or_estimator",
)


def _validate_contrastive_selection(records: Sequence[Mapping[str, Any]]) -> None:
    """Validate that grounding contains evidence-backed alternatives and a locked choice."""
    for item in records:
        qid = item.get("question_id")
        alternatives = item.get("interpretations")
        # Most questions are unambiguous and intentionally omit alternatives.
        # Contrastive validation applies only when the solver supplied them.
        if alternatives is None:
            if item.get("selected_interpretation_id") is not None:
                raise ValueError(f"grounding {qid} selects an absent interpretation")
            if item.get("grounding_status") not in {"resolved", "partially_resolved", "unresolved"}:
                raise ValueError(f"grounding {qid} has invalid grounding status")
            if not isinstance(item.get("selection_rationale"), str) or not item["selection_rationale"].strip():
                raise ValueError(f"grounding {qid} lacks selection rationale")
            continue
        if not isinstance(alternatives, list) or len(alternatives) not in {2, 3}:
            raise ValueError(f"grounding {qid} must contain 2 or 3 interpretations")
        ids: list[str] = []
        for alternative in alternatives:
            if not isinstance(alternative, Mapping):
                raise ValueError(f"grounding {qid} interpretation is not an object")
            identifier = alternative.get("id")
            if not isinstance(identifier, str) or not identifier.strip() or identifier in ids:
                raise ValueError(f"grounding {qid} interpretation IDs are invalid or duplicated")
            ids.append(identifier)
            if not all(isinstance(alternative.get(field), str) and alternative[field].strip() for field in FIELDS):
                raise ValueError(f"grounding {qid} interpretation has an empty semantic field")
            _validate_method_specificity(alternative["method_or_estimator"], str(qid), "grounding")
            if not isinstance(alternative.get("evidence"), str) or not alternative["evidence"].strip():
                raise ValueError(f"grounding {qid} interpretation lacks evidence")
        status = item.get("grounding_status")
        selected = item.get("selected_interpretation_id")
        rationale = item.get("selection_rationale")
        if status not in {"resolved", "partially_resolved", "unresolved"} or not isinstance(rationale, str) or not rationale.strip():
            raise ValueError(f"grounding {qid} has invalid selection metadata")
        if status == "resolved":
            if selected not in ids:
                raise ValueError(f"grounding {qid} has no selected interpretation")
            chosen = next(alternative for alternative in alternatives if alternative["id"] == selected)
            if any(item[field].strip() != chosen[field].strip() for field in FIELDS):
                raise ValueError(f"grounding {qid} locked fields differ from selected interpretation")
        elif selected is not None:
            raise ValueError(f"grounding {qid} non-resolved selection cannot lock an interpretation")


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _validate_method_specificity(value: str, qid: str, label: str) -> None:
    """Require model-based methods to record their design, not just a package name."""
    normalized = _normalized(value)
    if "deseq2" in normalized and not re.search(r"\bdesign\s*(?:formula)?\s*~", normalized):
        raise ValueError(f"{label} {qid} DESeq2 method must state its design formula")


def _load(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _ordered_records(payload: Mapping[str, Any], expected_ids: Sequence[str], label: str) -> list[Mapping[str, Any]]:
    questions = payload.get("questions")
    if not isinstance(questions, list) or not all(isinstance(item, Mapping) for item in questions):
        raise ValueError(f"{label} questions must be an ordered object list")
    if [item.get("question_id") for item in questions] != list(expected_ids):
        raise ValueError(f"{label} question coverage/order mismatch")
    for item in questions:
        for field in FIELDS:
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"{label} {item.get('question_id')} has empty {field}")
        _validate_method_specificity(item["method_or_estimator"], str(item.get("question_id")), label)
    return questions


def validate_consistency(
    workspace: Path,
    expected_rows: Sequence[Mapping[str, Any]],
    *,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Validate compact execution evidence and compare it to pre-computation grounding."""
    workspace = workspace.resolve()
    grounding_path = workspace / "analysis/grounding.json"
    execution_path = workspace / "analysis/execution.json"
    grounding = _load(grounding_path)
    execution = _load(execution_path)
    expected_ids = [str(row["question_id"]) for row in expected_rows]
    capsule_uuid = str(expected_rows[0]["capsule_uuid"])
    for label, payload in (("grounding", grounding), ("execution", execution)):
        if payload.get("capsule_uuid") != capsule_uuid:
            raise ValueError(f"{label} capsule identity mismatch")
    grounded = _ordered_records(grounding, expected_ids, "grounding")
    _validate_contrastive_selection(grounded)
    executed = _ordered_records(execution, expected_ids, "execution")

    reports: list[dict[str, Any]] = []
    evidence_paths: set[str] = set()
    conflicts: list[str] = []
    for before, after in zip(grounded, executed):
        qid = str(before["question_id"])
        status = after.get("status")
        if status not in {"resolved", "unresolved"}:
            raise ValueError(f"execution {qid} has invalid status")
        evidence = after.get("evidence_files")
        if not isinstance(evidence, list) or not all(isinstance(item, str) and item.strip() for item in evidence):
            raise ValueError(f"execution {qid} has invalid evidence_files")
        if status == "resolved" and not evidence:
            raise ValueError(f"execution {qid} has no actual-analysis evidence")
        for relative in evidence:
            candidate = (workspace / relative).resolve()
            try:
                candidate.relative_to(workspace)
            except ValueError as exc:
                raise ValueError(f"execution {qid} evidence escapes workspace: {relative}") from exc
            if not candidate.is_file():
                raise ValueError(f"execution {qid} evidence missing: {relative}")
            if not relative.startswith("analysis/") or relative in {
                "analysis/grounding.json", "analysis/execution.json", "analysis/consistency.json"
            } or relative.startswith("analysis/framework/"):
                raise ValueError(f"execution {qid} evidence is not an analysis artifact: {relative}")
            evidence_paths.add(str(candidate.relative_to(workspace)))

        checks = {
            "observation_unit": _normalized(before["observation_unit"]) == _normalized(after["observation_unit"]),
            "population_or_denominator": _normalized(before["denominator_or_population"]) == _normalized(after["denominator_or_population"]),
            "metric_or_outcome": _normalized(before["metric_or_outcome"]) == _normalized(after["metric_or_outcome"]),
            "unit_or_scale": _normalized(before["unit_or_scale"]) == _normalized(after["unit_or_scale"]),
            "data_object_or_filter": _normalized(before["data_object"]) == _normalized(after["data_object"]),
            "comparison": _normalized(before["comparison"]) == _normalized(after["comparison"]),
            "method_or_estimator": _normalized(before["method_or_estimator"]) == _normalized(after["method_or_estimator"]),
        }
        consistent = all(checks.values())
        if status == "resolved" and not consistent:
            conflicts.append(qid)
        reports.append({
            "question_id": qid,
            "execution_status": status,
            "checks": checks,
            "consistent": consistent,
            "disposition": "accepted" if consistent else ("unresolved_semantics" if status == "unresolved" else "conflict"),
        })

    report = {
        "capsule_uuid": capsule_uuid,
        "checker": "deterministic_contrastive_locked_binding_v3",
        "questions": reports,
        "resolved_conflicts": conflicts,
        "passed": not conflicts,
        "evidence_files": sorted(evidence_paths),
    }
    destination = output_path or workspace / "analysis/consistency.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if conflicts:
        raise ValueError(f"resolved grounding/execution conflicts: {conflicts}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("grounding", type=Path)
    parser.add_argument("execution", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    grounding = _load(args.grounding)
    questions = grounding.get("questions")
    if not isinstance(questions, list):
        raise SystemExit("grounding questions missing")
    rows = [{"capsule_uuid": grounding.get("capsule_uuid"), "question_id": item.get("question_id")} for item in questions]
    workspace = args.grounding.resolve().parents[1]
    try:
        report = validate_consistency(workspace, rows, output_path=args.output.resolve())
    except Exception as exc:
        raise SystemExit(f"consistency check failed: {type(exc).__name__}: {exc}") from exc
    print(json.dumps({"passed": report["passed"], "questions": len(report["questions"])}, indent=2))


if __name__ == "__main__":
    main()

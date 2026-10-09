# Path: common/integrity/capsule_workspace.py
# Purpose: Byte-preserving native capsule workspace construction and result validation for migrated A84.
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Mapping
from common.benchmark.a84_contract import environment_manifest, materialize_common_native_inputs
from common.integrity.hashing import write_json
def validate_native_result(payload: Mapping[str, Any], rows: list[dict[str, Any]]) -> list[Mapping[str, Any]]:
    """Match the frozen native generator's mechanical result and candidate-policy checks."""
    expected_ids = [row["question_id"] for row in rows]
    if payload.get("capsule_uuid") != rows[0]["capsule_uuid"] or payload.get("short_id") != rows[0]["short_id"]: raise ValueError("native capsule identity mismatch")
    results = payload.get("results")
    if not isinstance(results, list) or [item.get("question_id") for item in results] != expected_ids: raise ValueError("native question coverage/order mismatch")
    for item in results:
        candidates = item.get("candidates")
        if item.get("status") not in {"resolved", "unresolved"} or not isinstance(candidates, list) or len(candidates) > 3: raise ValueError("native status or candidate policy mismatch")
        if (item["status"] == "resolved") != bool(candidates): raise ValueError("native resolved/candidate mismatch")
        for candidate in candidates:
            if not isinstance(candidate, Mapping) or set(candidate) != {"answer", "method", "evidence"}: raise ValueError("native candidate shape mismatch")
            if not all(isinstance(candidate[key], str) and candidate[key].strip() for key in candidate): raise ValueError("native candidate is empty")
    return results
def prepare_workspace(arm: str, run_root: Path, rows: list[dict[str, Any]]) -> tuple[Path, Any]:
    workspace = run_root / arm / f"{rows[0]['short_id']}__{rows[0]['capsule_uuid']}"
    if workspace.exists(): raise FileExistsError(f"refusing existing workspace: {workspace}")
    environment = environment_manifest()["values"]; materialize_common_native_inputs(workspace, rows, environment)
    return workspace, None

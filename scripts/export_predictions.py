#!/usr/bin/env python3
# Path: scripts/export_predictions.py
# Purpose: Export one frozen Native or B generation into normalized answer-blind prediction JSONL.
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def unresolved(question_id: str) -> dict[str, Any]:
    return {"question_id": question_id, "status": "unresolved", "candidates": []}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--condition", choices=("native", "b"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--source-run", required=True)
    args = parser.parse_args()

    generation = load_json(args.generation)
    if generation.get("freeze", {}).get("status") != "frozen_before_private_scoring":
        raise RuntimeError("generation is not frozen before private scoring")
    records = generation["records" if args.condition == "native" else "episodes"]
    rows: list[dict[str, Any]] = []
    for record in records:
        workspace = Path(record["workspace"])
        local_failures = record.get("question_local_failures") or {}
        if args.condition == "native":
            question_ids = [record["question_id"]]
            result_path = workspace / "question_result.json"
            payload = load_json(result_path) if result_path.is_file() else unresolved(question_ids[0])
            results = [payload]
        else:
            question_ids = list(record["question_ids"])
            result_path = workspace / "capsule_result.json"
            payload = load_json(result_path) if result_path.is_file() else {"results": []}
            by_id = {
                item.get("question_id"): item
                for item in payload.get("results", [])
                if isinstance(item, dict) and isinstance(item.get("question_id"), str)
            }
            results = [by_id.get(question_id, unresolved(question_id)) for question_id in question_ids]
        for question_id, result in zip(question_ids, results, strict=True):
            candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
            status = result.get("status") if result.get("status") in {"resolved", "unresolved"} else "unresolved"
            accepted = bool(record.get("accepted")) and question_id not in local_failures
            rows.append({
                "condition": args.condition,
                "question_id": question_id,
                "short_id": record["short_id"],
                "capsule_uuid": record["capsule_uuid"],
                "status": status,
                "candidates": candidates,
                "controller_accepted": accepted,
                "controller_status": record.get("controller_status"),
                "question_local_failure": local_failures.get(question_id) if isinstance(local_failures, dict) else None,
                "return_code": record.get("return_code"),
                "runtime_seconds": record.get("runtime_seconds"),
                "source_run": args.source_run,
            })

    if len(rows) != 78 or len({row["question_id"] for row in rows}) != 78:
        raise RuntimeError("expected exactly 78 unique exported questions")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    provenance = {
        "condition": args.condition,
        "source_run": args.source_run,
        "source_generation": str(args.generation.resolve()),
        "source_generation_sha256": sha256(args.generation),
        "source_manifest_sha256": generation["freeze"].get("manifest_sha256"),
        "questions": 78,
        "model": generation["manifest"].get("model"),
        "reasoning_effort": generation["manifest"].get("reasoning_effort"),
        "generation_scorer_or_evaluator": False,
        "freeze_status": generation["freeze"]["status"],
        "predictions_sha256": sha256(args.output),
    }
    args.provenance.parent.mkdir(parents=True, exist_ok=True)
    args.provenance.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"condition": args.condition, "questions": len(rows), "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()

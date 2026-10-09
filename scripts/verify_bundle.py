#!/usr/bin/env python3
"""Verify the public 19-capsule / 78-question generation bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


SOLVER_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = SOLVER_ROOT.parent
DATA_ROOT = BUNDLE_ROOT / "data"


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def jsonl_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()

    scope = json.loads((DATA_ROOT / "scope.json").read_text(encoding="utf-8"))
    questions = jsonl_rows(DATA_ROOT / "public/questions.jsonl")
    assert len(questions) == 78
    assert len({row["question_id"] for row in questions}) == 78
    assert len({row["capsule_uuid"] for row in questions}) == 19
    assert not ({row["short_id"] for row in questions} & set(scope["excluded_short_ids"]))
    question_ids = {row["question_id"] for row in questions}

    prediction_paths = (
        SOLVER_ROOT / "results/native/predictions.jsonl",
        SOLVER_ROOT / "results/b/predictions.jsonl",
        SOLVER_ROOT / "results/fresh_envfixed_20260921/native/predictions.jsonl",
        SOLVER_ROOT / "results/fresh_envfixed_20260921/b/predictions.jsonl",
    )
    for path in prediction_paths:
        rows = jsonl_rows(path)
        assert len(rows) == 78, path
        assert {row["question_id"] for row in rows} == question_ids, path
        forbidden = {"correct", "verdict", "ideal_answer", "reference_answer"}
    assert not any(forbidden & set(row) for row in rows), path

    assert not (SOLVER_ROOT / "evaluator").exists()
    assert not (SOLVER_ROOT / "private").exists()

    if not args.metadata_only:
        manifest = json.loads((DATA_ROOT / "data_manifest.json").read_text(encoding="utf-8"))
        for item in manifest["files"]:
            path = BUNDLE_ROOT / item["path"]
            assert path.stat().st_size == item["bytes"], item["path"]
            assert digest(path) == item["sha256"], item["path"]

    print("PASS: solver + data contain 19 capsules / 78 questions; solver contains no private evaluator")


if __name__ == "__main__":
    main()

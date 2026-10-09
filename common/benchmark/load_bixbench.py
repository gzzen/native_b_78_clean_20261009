# Path: common/benchmark/load_bixbench.py
# Purpose: Answer-blind loading and grouping of the public BixBench question file.
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = ROOT
BUNDLE_ROOT = ROOT.parent
DATA_ROOT = BUNDLE_ROOT / "data"
QUESTIONS = DATA_ROOT / "public/questions.jsonl"
CAPSULES = DATA_ROOT / "capsules"
PUBLIC_FIELDS = ("id", "tag", "version", "question", "capsule_uuid", "short_id", "question_id", "categories", "paper", "data_folder", "eval_mode")

def load_rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]

def public_question(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in PUBLIC_FIELDS if key in row}

def capsule_groups() -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in load_rows():
        groups[row["capsule_uuid"]].append(public_question(row))
    if len(groups) != 19 or sum(map(len, groups.values())) != 78:
        raise RuntimeError("expected 78 questions in 19 capsules")
    return sorted(groups.items(), key=lambda item: (item[1][0]["short_id"], item[0]))

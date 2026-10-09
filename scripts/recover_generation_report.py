#!/usr/bin/env python3
# Path: scripts/recover_generation_report.py
# Purpose: Restore a missing report from frozen generation without rerunning or modifying attempts.
from __future__ import annotations

import argparse
import json
from pathlib import Path


def recover_report(generation: Path, condition: str, report: Path) -> dict:
    payload = json.loads(generation.read_text(encoding="utf-8"))
    if payload.get("freeze", {}).get("status") != "frozen_before_private_scoring":
        raise ValueError("generation is not frozen before private scoring")
    if condition not in {"native", "b"}:
        raise ValueError("condition must be native or b")
    summary = payload["generation_summary"]
    records = payload["records" if condition == "native" else "episodes"]
    total = summary["questions" if condition == "native" else "capsules"]
    passed = summary["controller_passed_questions" if condition == "native" else "accepted_capsules"]
    if len(records) != total or sum(bool(record.get("accepted")) for record in records) != passed:
        raise ValueError("generation summary does not match frozen records")
    unit = "questions" if condition == "native" else "capsules"
    text = (f"# {condition.title()} Generation Report\n\n"
            f"> Path: `{report.resolve()}`\n\n"
            f"> Recovered from frozen generation: `{generation.resolve()}`.\n\n"
            f"Controller-passed {unit}: {passed}/{total}.\n")
    report.parent.mkdir(parents=True, exist_ok=True)
    # A report recovery must never replace existing artifacts or its own input.
    with report.open("x", encoding="utf-8") as stream:
        stream.write(text)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--condition", choices=("native", "b"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    summary = recover_report(args.generation, args.condition, args.report)
    print(json.dumps({"report": str(args.report), "generation_summary": summary}, indent=2))


if __name__ == "__main__":
    main()

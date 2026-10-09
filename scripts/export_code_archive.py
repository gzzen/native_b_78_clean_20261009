#!/usr/bin/env python3
"""Export frozen generated code into a relocatable, answer-blind release archive."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from replay_solution_code import cited_scripts, executed_commands, native_solution_entrypoints

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT.parent / "data/capsules"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--condition", choices=("native", "b"), required=True)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    generation = json.loads(args.generation.read_text(encoding="utf-8"))
    if generation.get("freeze", {}).get("status") != "frozen_before_private_scoring":
        raise RuntimeError("generation is not frozen")
    if args.output.exists():
        raise RuntimeError(f"refusing existing archive: {args.output}")
    args.output.mkdir(parents=True)

    records = generation["records" if args.condition == "native" else "episodes"]
    units = []
    for record in records:
        source = Path(record["workspace"])
        unit_id = record.get("question_id") or f"{record['short_id']}__{record['capsule_uuid']}"
        destination = args.output / "units" / unit_id
        scripts_source = source / "analysis/scripts"
        scripts_destination = destination / "analysis/scripts"
        scripts_destination.parent.mkdir(parents=True)
        shutil.copytree(scripts_source, scripts_destination)
        metadata_names = (
            ("question.json", "question_result.json", "environment_contract.json", "event_timeline.json")
            if args.condition == "native"
            else ("questions.json", "capsule_result.json", "environment_contract.json", "operation_hints.json", "event_timeline.json")
        )
        for name in metadata_names:
            path = source / name
            if path.is_file():
                shutil.copy2(path, destination / name)
        capsule_data = DATA / f"CapsuleFolder-{record['capsule_uuid']}"
        relative_data = os.path.relpath(capsule_data, destination)
        os.symlink(relative_data, destination / "data", target_is_directory=True)

        if args.condition == "native":
            source_status, entrypoints = native_solution_entrypoints(source)
        else:
            source_status, entrypoints = "capsule", cited_scripts(source, "capsule_result.json")
        commands = executed_commands(source, entrypoints)
        portable_commands = {
            relative: {
                **payload,
                "command": str(payload["command"]).replace(str(source), "."),
            }
            for relative, payload in commands.items()
        }
        all_sources = sorted(path for path in scripts_destination.rglob("*") if path.is_file())
        units.append({
            "unit_id": unit_id,
            "question_id": record.get("question_id"),
            "short_id": record["short_id"],
            "capsule_uuid": record["capsule_uuid"],
            "source_status": source_status,
            "controller_accepted": bool(record.get("accepted")),
            "directory": str(destination.relative_to(args.output)),
            "data_symlink": relative_data,
            "all_generated_sources": [
                {"path": str(path.relative_to(destination)), "sha256": digest(path)}
                for path in all_sources
            ],
            "replayed_entrypoints": entrypoints,
            "frozen_execution": portable_commands,
        })

    manifest = {
        "condition": args.condition,
        "answer_blind": True,
        "source_generation": str(args.generation.resolve()),
        "source_generation_sha256": digest(args.generation),
        "units": units,
        "unit_count": len(units),
        "generated_source_files": sum(len(unit["all_generated_sources"]) for unit in units),
        "replayed_entrypoints": sum(len(unit["replayed_entrypoints"]) for unit in units),
        "instructions": "Run frozen_execution commands from each unit directory; data is a relative symlink to the release data tree.",
    }
    write_json(args.output / "code_manifest.json", manifest)
    print(json.dumps({key: manifest[key] for key in ("condition", "unit_count", "generated_source_files", "replayed_entrypoints")}, indent=2))


if __name__ == "__main__":
    main()

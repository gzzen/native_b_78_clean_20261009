#!/usr/bin/env python3
# Path: scripts/pack_run_artifacts.py
# Purpose: Pack the per-question artifacts of one experiment's run trees into out/<experiment_id>.zip.
from __future__ import annotations

import argparse
import hashlib
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Controller-owned records written into each attempt workspace (Native and B).
WORKSPACE_FILES = (
    "question.json", "questions.json", "environment_contract.json", "operation_hints.json",
    "question_result.json", "capsule_result.json", "run_manifest.json", "isolation_boundary.json",
    "event_timeline.json", "events.jsonl", "stderr.log",
)
# Generated scripts, audits and outputs. data/, tmp/ and home/ (OpenCode config/state) are never packed.
WORKSPACE_DIRS = ("analysis",)
# Checkout-level directories holding per-experiment manifests, reports, predictions and logs.
OUTPUT_DIRS = ("analysis", "reports", "results", "logs", "diagnostics")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_roots(root: Path, experiment: str) -> list[Path]:
    runs = root / "runs"
    roots = [runs / experiment] if (runs / experiment).is_dir() else []
    roots += sorted(path for path in runs.glob(f"{experiment}_*") if path.is_dir())
    return roots


def workspaces(run_root: Path) -> list[Path]:
    stages = [run_root, *sorted(run_root.glob("resume_*"))]
    return [path for stage in stages for path in sorted(stage.glob("[0-9][0-9][0-9][0-9]/*"))
            if path.is_dir() and not path.is_symlink()]


def regular_files(directory: Path) -> list[Path]:
    found = []
    for current, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not (Path(current) / name).is_symlink())
        found += [Path(current) / name for name in sorted(files)]
    return found


def belongs_to(name: str, experiment: str) -> bool:
    # Tags are <experiment>_<condition>_<label>; reports may be upper-cased (HARD_ISOLATED_..._GENERATION.md).
    name, key = name.lower(), experiment.lower()
    return name == key or name.startswith((key + "_", key + "."))


def is_answer_derived(path: Path) -> bool:
    """Private-evaluator scoring output; kept out of the answer-blind archive."""
    return "scoring" in path.name.lower()


def experiment_outputs(root: Path, experiment: str) -> list[Path]:
    """Files outside runs/ that carry this experiment's answers, reports or diagnostic logs."""
    files: list[Path] = []
    for directory in OUTPUT_DIRS:
        base = root / directory
        entries = sorted(base.iterdir()) if base.is_dir() else []
        for entry in entries:
            if entry.is_symlink() or not belongs_to(entry.name, experiment):
                continue
            files += regular_files(entry) if entry.is_dir() else [entry]
    # Terminal logs tee'd into the checkout root, e.g. <run_tag>.log.
    files += sorted(path for path in root.glob("*.log") if belongs_to(path.name, experiment))
    return [path for path in files if path.is_file() and not path.is_symlink()]


def workspace_artifacts(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for run_root in roots:
        for workspace in workspaces(run_root):
            files += [workspace / name for name in WORKSPACE_FILES]
            files += [path for name in WORKSPACE_DIRS if (workspace / name).is_dir()
                      for path in regular_files(workspace / name)]
    return [path for path in files if path.is_file() and not path.is_symlink()]


def candidates(root: Path, experiment: str, roots: list[Path]) -> list[Path]:
    """Everything the archive should hold: workspace artifacts plus answer-blind experiment outputs."""
    outputs = [path for path in experiment_outputs(root, experiment) if not is_answer_derived(path)]
    return workspace_artifacts(roots) + outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Pack per-question run artifacts into out/<experiment_id>.zip.")
    parser.add_argument("experiment_id", help="runs/<experiment_id> or the prefix of runs/<experiment_id>_<condition>_<label>")
    parser.add_argument("--root", type=Path, default=ROOT, help="checkout containing runs/ (default: this checkout)")
    parser.add_argument("--output-dir", type=Path, default=None, help="default: <root>/out")
    parser.add_argument("--max-file-mb", type=float, default=50.0, help="skip larger files and list them in the manifest")
    parser.add_argument("--force", action="store_true", help="replace an existing zip")
    args = parser.parse_args()

    experiment = args.experiment_id.strip("/").removeprefix("runs/")
    if not experiment or "/" in experiment or experiment.startswith("."):
        raise SystemExit(f"invalid experiment id: {args.experiment_id}")
    root = args.root.resolve()
    roots = run_roots(root, experiment)
    if not roots:
        raise SystemExit(f"no run trees for {experiment} under {root / 'runs'}")
    output_dir = (args.output_dir or root / "out").resolve()
    output = output_dir / f"{experiment}.zip"
    if output.exists() and not args.force:
        raise SystemExit(f"refusing existing archive (use --force): {output}")

    limit = int(args.max_file_mb * 1024 * 1024)
    packed, skipped, seen = [], [], set()
    for path in candidates(root, experiment, roots):
        if path in seen or path.is_symlink() or not path.is_file():
            continue
        seen.add(path)
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        if size > limit:
            skipped.append({"path": relative, "bytes": size, "reason": f"larger than {args.max_file_mb} MB"})
            continue
        packed.append((path, relative, size))

    manifest = {
        "experiment_id": experiment,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_root": str(root),
        "run_roots": [path.relative_to(root).as_posix() for path in roots],
        "workspaces": sum(len(workspaces(path)) for path in roots),
        "excluded_workspace_content": ["data/", "tmp/", "home/", "symlinks"],
        "file_count": len(packed),
        "total_bytes": sum(size for _, _, size in packed),
        "files": [{"path": relative, "bytes": size, "sha256": sha256(path)} for path, relative, size in packed],
        "skipped": skipped,
        "excluded_answer_derived": [path.relative_to(root).as_posix() for path in experiment_outputs(root, experiment)
                                    if is_answer_derived(path)],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(".zip.partial")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for path, relative, _ in packed:
            archive.write(path, f"{experiment}/{relative}")
        archive.writestr(f"{experiment}/PACK_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    partial.replace(output)
    print(json.dumps({"output": str(output), "run_roots": manifest["run_roots"], "workspaces": manifest["workspaces"],
                      "files": manifest["file_count"], "skipped": len(skipped),
                      "zip_bytes": output.stat().st_size}, indent=2))


if __name__ == "__main__":
    main()

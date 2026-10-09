#!/usr/bin/env python3
# Path: scripts/cleanup_run_artifacts.py
# Purpose: Delete one experiment's run trees and answer-leaking outputs after verifying out/<experiment_id>.zip.
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path

from check_experiment_progress import runner_processes
from pack_run_artifacts import (ROOT, OUTPUT_DIRS, belongs_to, experiment_outputs, is_answer_derived,
                               run_roots, sha256, workspace_artifacts)


def tree_bytes(path: Path) -> int:
    total = 0
    for current, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(current) / name).lstat().st_size
            except OSError:
                pass
    return total


def remove_tree(path: Path) -> None:
    # Make directories writable so read-only copies can be unlinked. Files are never chmodded:
    # hardlinked capsule data shares its mode with the original dataset.
    for current, dirs, _ in os.walk(path, followlinks=False):
        for name in dirs:
            directory = Path(current) / name
            if not directory.is_symlink():
                directory.chmod(directory.stat().st_mode | 0o700)
    path.chmod(path.stat().st_mode | 0o700)
    shutil.rmtree(path)


def prune_empty_dirs(root: Path, experiment: str) -> None:
    for directory in OUTPUT_DIRS:
        base = root / directory
        for entry in sorted(base.iterdir()) if base.is_dir() else []:
            if not entry.is_dir() or entry.is_symlink() or not belongs_to(entry.name, experiment):
                continue
            for current, _, _ in sorted(os.walk(entry, followlinks=False), reverse=True):
                if not any(Path(current).iterdir()):
                    Path(current).rmdir()


def verify_archive(archive_path: Path, experiment: str) -> dict:
    try:
        return read_verified_manifest(archive_path, experiment)
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise SystemExit(f"archive is not a valid pack for {experiment}: {type(exc).__name__}: {exc}")


def read_verified_manifest(archive_path: Path, experiment: str) -> dict:
    with zipfile.ZipFile(archive_path) as archive:
        broken = archive.testzip()
        if broken is not None:
            raise SystemExit(f"corrupt archive member: {broken}")
        manifest = json.loads(archive.read(f"{experiment}/PACK_MANIFEST.json"))
        if manifest.get("experiment_id") != experiment:
            raise SystemExit(f"archive belongs to {manifest.get('experiment_id')!r}, not {experiment!r}")
        for entry in manifest["files"]:
            digest = hashlib.sha256()
            with archive.open(f"{experiment}/{entry['path']}") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
            if digest.hexdigest() != entry["sha256"]:
                raise SystemExit(f"archive content does not match its manifest: {entry['path']}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete runs/<experiment_id>* and the experiment's analysis/, reports/, results/, logs/, "
                    "diagnostics/ and terminal-log outputs after verifying out/<experiment_id>.zip.")
    parser.add_argument("experiment_id")
    parser.add_argument("--root", type=Path, default=ROOT, help="checkout containing runs/ (default: this checkout)")
    parser.add_argument("--archive", type=Path, default=None, help="default: <root>/out/<experiment_id>.zip")
    parser.add_argument("--runs-only", action="store_true",
                        help="delete only runs/<experiment_id>*, keeping analysis/reports/results/logs/diagnostics outputs")
    parser.add_argument("--allow-unpacked-loss", action="store_true",
                        help="also delete files the archive does not hold (oversized skips, answer-derived scoring)")
    parser.add_argument("--execute", action="store_true", help="actually delete (default: dry run)")
    args = parser.parse_args()

    experiment = args.experiment_id.strip("/").removeprefix("runs/")
    if not experiment or "/" in experiment or experiment.startswith("."):
        raise SystemExit(f"invalid experiment id: {args.experiment_id}")
    root = args.root.resolve()
    runs = (root / "runs").resolve()
    roots = run_roots(root, experiment)
    outputs = [] if args.runs_only else experiment_outputs(root, experiment)
    if not roots and not outputs:
        raise SystemExit(f"nothing to clean for {experiment} under {root}")
    for run_root in roots:
        if run_root.is_symlink() or run_root.resolve().parent != runs:
            raise SystemExit(f"refusing run root outside {runs}: {run_root}")

    archive_path = (args.archive or root / "out" / f"{experiment}.zip").resolve()
    if not archive_path.is_file():
        raise SystemExit(f"missing archive; run pack_run_artifacts.py first: {archive_path}")
    manifest = verify_archive(archive_path, experiment)
    packed = {entry["path"]: entry["sha256"] for entry in manifest["files"]}

    def relative(path: Path) -> str:
        return path.relative_to(root).as_posix()

    # Changed or newly created files always block: the archive no longer reflects the run.
    # Files the pack knowingly left out (oversized, answer-derived scoring) need --allow-unpacked-loss.
    known_unpacked = {entry["path"] for entry in manifest["skipped"]}
    known_unpacked |= {relative(path) for path in outputs if is_answer_derived(path)}
    problems = []
    checked = workspace_artifacts(roots) + [path for path in outputs if not is_answer_derived(path)]
    for path in checked:
        name = relative(path)
        if name in known_unpacked:
            continue
        if name not in packed:
            problems.append(f"not in archive (created after packing?): {name}")
        elif sha256(path) != packed[name]:
            problems.append(f"changed since packing: {name}")
    if not args.allow_unpacked_loss:
        problems += [f"not in archive, would be lost: {name}" for name in sorted(known_unpacked)
                     if (root / name).is_file() and (not args.runs_only or name.startswith("runs/"))]

    processes, process_error = runner_processes(root)
    if process_error:
        problems.append(process_error)
    for run_root in roots:
        if pids := processes.get(str(run_root.resolve())):
            problems.append(f"controller still running on {relative(run_root)}: pids {pids}")

    if problems:
        print("Refusing cleanup:")
        print("\n".join(f"  - {problem}" for problem in problems))
        print("Re-pack after the run settles; pass --allow-unpacked-loss only to delete files the archive omits.")
        raise SystemExit(1)

    targets: list[tuple[Path, int]] = [(run_root, tree_bytes(run_root)) for run_root in roots]
    targets += [(path, path.stat().st_size) for path in outputs]
    print(f"Archive verified: {archive_path} ({manifest['file_count']} files)")
    print("Deleting:" if args.execute else "Dry run; would delete:")
    for path, size in targets:
        note = "" if path.is_dir() or relative(path) in packed else "  [not in archive]"
        print(f"  {relative(path)}  ({size / 1024 / 1024:.1f} MB){note}")
    print(f"Total: {sum(size for _, size in targets) / 1024 / 1024:.1f} MB")
    if not args.execute:
        print("Re-run with --execute to delete.")
        return

    for path, _ in targets:
        remove_tree(path) if path.is_dir() else path.unlink()
    if not args.runs_only:
        prune_empty_dirs(root, experiment)
    print("Done.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# Path: scripts/cleanup_run_artifacts.py
# Purpose: Delete one experiment's run trees after verifying they are fully packed in out/<experiment_id>.zip.
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path

from check_experiment_progress import runner_processes
from pack_run_artifacts import ROOT, candidates, run_roots, sha256


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
    parser = argparse.ArgumentParser(description="Delete runs/<experiment_id>* after verifying out/<experiment_id>.zip.")
    parser.add_argument("experiment_id")
    parser.add_argument("--root", type=Path, default=ROOT, help="checkout containing runs/ (default: this checkout)")
    parser.add_argument("--archive", type=Path, default=None, help="default: <root>/out/<experiment_id>.zip")
    parser.add_argument("--include-metadata", action="store_true",
                        help="also delete the packed analysis/, reports/, logs/ and results/ files")
    parser.add_argument("--allow-skipped-loss", action="store_true",
                        help="proceed although the pack skipped oversized files")
    parser.add_argument("--execute", action="store_true", help="actually delete (default: dry run)")
    args = parser.parse_args()

    experiment = args.experiment_id.strip("/").removeprefix("runs/")
    if not experiment or "/" in experiment or experiment.startswith("."):
        raise SystemExit(f"invalid experiment id: {args.experiment_id}")
    root = args.root.resolve()
    runs = (root / "runs").resolve()
    roots = run_roots(root, experiment)
    if not roots:
        raise SystemExit(f"no run trees for {experiment} under {runs}")
    for run_root in roots:
        if run_root.is_symlink() or run_root.resolve().parent != runs:
            raise SystemExit(f"refusing run root outside {runs}: {run_root}")

    archive_path = (args.archive or root / "out" / f"{experiment}.zip").resolve()
    if not archive_path.is_file():
        raise SystemExit(f"missing archive; run pack_run_artifacts.py first: {archive_path}")
    manifest = verify_archive(archive_path, experiment)
    packed = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    skipped = {entry["path"] for entry in manifest["skipped"]}

    problems = []
    current = {path.relative_to(root).as_posix(): path for path in candidates(root, experiment, roots)
               if path.is_file() and not path.is_symlink()}
    for relative, path in sorted(current.items()):
        if relative in skipped:
            continue
        if relative not in packed:
            problems.append(f"not in archive (created after packing?): {relative}")
        elif sha256(path) != packed[relative]:
            problems.append(f"changed since packing: {relative}")
    if skipped and not args.allow_skipped_loss:
        problems += [f"skipped by pack, would be lost: {path}" for path in sorted(skipped)]

    processes, process_error = runner_processes(root)
    if process_error:
        problems.append(process_error)
    for run_root in roots:
        if pids := processes.get(str(run_root.resolve())):
            problems.append(f"controller still running on {run_root.relative_to(root)}: pids {pids}")

    if problems:
        print("Refusing cleanup:")
        print("\n".join(f"  - {problem}" for problem in problems))
        raise SystemExit(1)

    targets: list[tuple[Path, int]] = [(run_root, tree_bytes(run_root)) for run_root in roots]
    if args.include_metadata:
        targets += [(path, path.stat().st_size) for relative, path in sorted(current.items())
                    if not relative.startswith("runs/") and relative in packed]
    print(f"Archive verified: {archive_path} ({manifest['file_count']} files)")
    print(("Deleting:" if args.execute else "Dry run; would delete:"))
    for path, size in targets:
        print(f"  {path.relative_to(root)}  ({size / 1024 / 1024:.1f} MB)")
    print(f"Total: {sum(size for _, size in targets) / 1024 / 1024:.1f} MB")
    if not args.execute:
        print("Re-run with --execute to delete.")
        return

    for path, _ in targets:
        remove_tree(path) if path.is_dir() else path.unlink()
    for directory in (root / "logs" / experiment, root / "results" / experiment):
        if args.include_metadata and directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    print("Done.")


if __name__ == "__main__":
    main()

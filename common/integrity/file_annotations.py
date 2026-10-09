#!/usr/bin/env python3
# Path: common/integrity/file_annotations.py
# Purpose: Generate the non-destructive, repository-wide path-and-purpose index for experiment artifacts.
"""Regenerate FILE_ANNOTATIONS.md without modifying frozen artifacts."""
from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "FILE_ANNOTATIONS.md"


def purpose(rel: Path, is_link: bool) -> str:
    p = rel.as_posix()
    if is_link:
        return "Workspace input link or formal-condition run artifact link."
    if p.startswith("runs/"):
        return "Formal-condition run output, provenance, executable source, or frozen artifact."
    if p.startswith("Native/history/") or p.startswith("archive/"):
        return "Historical archive artifact retained without byte modification."
    if p.startswith("conditions/"):
        return "Formal condition package source, protocol, configuration, or runtime component."
    if p.startswith("common/"):
        return "Shared experiment-integrity substrate source or schema; contains no condition capability."
    if p.startswith("analysis/") or p.startswith("reports/"):
        return "Analysis, scoring, manifest, or report artifact."
    if p.startswith("protocols/"):
        return "Experiment-wide protocol and integrity documentation."
    if p.startswith("public/"):
        return "Public benchmark or public experiment input artifact."
    return "Experiment-tree artifact; see parent directory context."


def main() -> None:
    rows: list[tuple[str, str, str]] = []
    for base, dirs, names in os.walk(ROOT, followlinks=False):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in names:
            path = Path(base) / name
            if path == INDEX:
                continue
            rel = path.relative_to(ROOT)
            is_link = path.is_symlink()
            rows.append((rel.as_posix(), "symbolic link" if is_link else "regular file", purpose(rel, is_link)))
        # os.walk places symlinked directories in dirs; index them as links too.
        for name in dirs:
            path = Path(base) / name
            if path.is_symlink():
                rel = path.relative_to(ROOT)
                rows.append((rel.as_posix(), "symbolic link", purpose(rel, True)))
    lines = [
        "# File Annotation Index",
        "",
        "> Path: `minimal_projection_experiments_20260909/FILE_ANNOTATIONS.md`",
        "",
        "> Purpose: non-destructive, generated index of every regular file and symbolic link in this experiment tree. It supplies the path-and-purpose annotation required for formats that cannot safely contain a source comment, and for frozen historical artifacts whose bytes and hashes must remain unchanged.",
        "",
        "This index deliberately excludes itself to avoid a recursive entry. Source and documentation files that are still maintained are additionally annotated in their own native comment syntax. Regenerate it after adding or removing files with `python common/integrity/file_annotations.py`.",
        "",
        "## Inventory",
        "",
        "| Path | Type | Purpose |",
        "|---|---|---|",
    ]
    lines.extend(f"| `{path}` | {kind} | {why} |" for path, kind, why in sorted(rows))
    INDEX.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"indexed {len(rows)} paths in {INDEX}")


if __name__ == "__main__":
    main()

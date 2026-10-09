# Path: common/integrity/freeze.py
# Purpose: Hash answer-blind generation artifacts before any private post-hoc scoring.
from __future__ import annotations
from pathlib import Path
from .hashing import sha256_file

def freeze_files(root: Path, files: list[Path]) -> list[dict[str, str]]:
    return [{"path": str(path.relative_to(root)), "sha256": sha256_file(path)} for path in files if path.is_file()]

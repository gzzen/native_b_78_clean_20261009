# Path: common/integrity/hashing.py
# Purpose: Deterministic file hashing and JSON writing for answer-blind provenance.
from __future__ import annotations
import hashlib, json
from pathlib import Path
from typing import Any

def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

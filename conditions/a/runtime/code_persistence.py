# Path: conditions/a/runtime/code_persistence.py
# Purpose: A-condition rule that records substantive task code and rejects inline-only execution.
from __future__ import annotations
from pathlib import Path
from typing import Any
from common.integrity.hashing import sha256_file

INLINE_MARKERS = ("python - <<", "python3 - <<", "Rscript -e", "R -q -e", "node -e")

def provenance(workspace: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    scripts = workspace / "analysis" / "scripts"
    source_files = sorted(path for path in scripts.rglob("*") if path.is_file()) if scripts.is_dir() else []
    commands = [str(item.get("event", {}).get("item", {}).get("command", "")) for item in events]
    inline = [command for command in commands if any(marker in command for marker in INLINE_MARKERS)]
    if inline:
        raise ValueError("A requires task-specific code to be persisted under analysis/scripts")
    return {"source_files": [{"path": str(path.relative_to(workspace)), "sha256": sha256_file(path)} for path in source_files], "inline_code_commands": inline}

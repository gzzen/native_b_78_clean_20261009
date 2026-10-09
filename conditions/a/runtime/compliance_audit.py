# Path: conditions/a/runtime/compliance_audit.py
# Purpose: A-only graded compliance audit: fatal capsule breaches, question-local evidence failures, and soft diagnostics.
from __future__ import annotations
import re
from pathlib import Path
from typing import Any
from common.integrity.isolation_audit import audit_events_v121

INLINE = ("python - <<", "python3 - <<", "Rscript - <<", "R -q -e", "Rscript -e", "node -e")
PROBE = re.compile(r"packageVersion\(|importlib\.util\.find_spec|sys\.executable|sys\.version|--version|pip\s+(?:show|list)|conda\s+(?:list|info)|sessionInfo\(", re.I)
INSPECTION = re.compile(r"\b(?:head|sed|rg|grep)\b|\.(?:shape|columns|dtypes)\b|\b(?:nrow|ncol|colnames|names)\(", re.I)
SUBSTANTIVE = re.compile(r"read_(?:csv|table)|pandas|numpy|scipy|statsmodels|DESeq|glm\(|lm\(|wilcox|t\.test|cor\(|p\.adjust|ggplot|matplotlib|sklearn|data/", re.I)
PATH_REF = re.compile(r"(?:analysis/(?:scripts|artifacts)/[^\s,;`'\"\)]+)")

def _canonical_evidence_path(relative: str) -> str:
    """Normalize plain or Markdown-linked workspace-relative evidence paths."""
    # `[analysis/scripts/solve.py](...)` is semantically the same citation as
    # the bare relative path.  PATH_REF intentionally scans prose broadly, so
    # trim the Markdown link boundary here before checking the filesystem.
    return relative.split("](", 1)[0].rstrip("].,:;!?")

def _commands(events: list[dict[str, Any]]) -> list[str]:
    return [str(record["event"]["item"].get("command", "")) for record in events
            if record.get("event", {}).get("type") in (None, "item.completed")
            and record["event"].get("item", {}).get("type", "command_execution") == "command_execution"
            and record["event"]["item"].get("status") != "error"
            and record["event"]["item"].get("exit_code") in (None, 0)]

def compliance_audit(workspace: Path, events: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify only audit facts; it never judges scientific correctness."""
    fatal: list[dict[str, str]] = []
    soft: list[dict[str, str]] = []
    question_local: dict[str, list[str]] = {}
    try:
        path_evidence = audit_events_v121(events, workspace)
    except ValueError as exc:
        path_evidence = []
        fatal.append({"kind": "isolation_breach", "detail": str(exc)})
    commands = _commands(events)
    persisted = sorted(path for path in (workspace / "analysis" / "scripts").rglob("*") if path.is_file()) if (workspace / "analysis" / "scripts").is_dir() else []
    executed_persisted_source = any("analysis/scripts/" in command for command in commands)
    for command in commands:
        if not any(marker in command for marker in INLINE):
            continue
        if PROBE.search(command):
            soft.append({"kind": "environment_probe", "detail": command})
        elif INSPECTION.search(command) and not SUBSTANTIVE.search(command):
            soft.append({"kind": "inspection", "detail": command})
        elif SUBSTANTIVE.search(command) and not executed_persisted_source:
            fatal.append({"kind": "inline_only_substantive_computation", "detail": command})
        else:
            soft.append({"kind": "inline_with_persisted_source", "detail": command})
    for result in results:
        question_id = str(result.get("question_id", ""))
        cited_sources: list[str] = []
        for candidate in result.get("candidates", []):
            evidence = str(candidate.get("evidence", ""))
            for raw_relative in PATH_REF.findall(evidence):
                relative = _canonical_evidence_path(raw_relative)
                if not (workspace / relative).is_file():
                    question_local.setdefault(question_id, []).append(f"cited evidence missing: {relative}")
                elif relative.startswith("analysis/scripts/"):
                    cited_sources.append(relative)
        if result.get("status") == "resolved" and not cited_sources:
            question_local.setdefault(question_id, []).append("resolved result lacks a cited persisted analysis/scripts source")
    return {"fatal": fatal, "question_local": question_local, "soft": soft, "path_audit_evidence": path_evidence, "persisted_source_files": [str(path.relative_to(workspace)) for path in persisted], "executed_persisted_source": executed_persisted_source}

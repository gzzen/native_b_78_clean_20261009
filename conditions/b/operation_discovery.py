"""Deterministic discovery of installed implementations for named metrics.

The input is public question wording only.  Output contains environment/docs
matches, never benchmark answers or evaluator information.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import os
import sys
import sysconfig
from pathlib import Path
from typing import Any, Mapping, Sequence

AGENT_BIN = Path(os.environ.get("AGENT_BIX_BIN", str(Path(sys.executable).resolve().parent)))
SITE_PACKAGES = Path(sysconfig.get_paths()["purelib"])
_MARKER = r"(?:score|index|rate|ratio)s?"
_STOP = {"what", "is", "the", "a", "an", "of", "for", "from", "between", "mean", "median", "average", "absolute", "difference", "per", "gene"}
_GENERIC_STATISTICAL_PHRASES = {
    "odds ratio",
    "risk ratio",
    "rate ratio",
    "mean ratio",
    "percent ratio",
}


def metric_phrases(question: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", question.casefold())
    phrases: set[str] = set()
    for i, word in enumerate(words):
        if not re.fullmatch(_MARKER, word):
            continue
        stem = word[:-1] if word.endswith("s") else word
        prefix = [w for w in words[max(0, i - 3):i] if w not in _STOP]
        for size in range(1, min(3, len(prefix)) + 1):
            candidate = " ".join(prefix[-size:] + [stem])
            if (
                len(candidate.split()) >= 2
                and candidate not in _GENERIC_STATISTICAL_PHRASES
                and not candidate.endswith(" reduction in odds ratio")
            ):
                phrases.add(candidate)
    return sorted(phrases, key=lambda value: (-len(value.split()), value))[:4]


def _run(command: list[str], timeout: int = 12) -> dict[str, Any]:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
        return {"command": command, "exit_code": result.returncode, "stdout": result.stdout[:5000], "stderr": result.stderr[:2000]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "exit_code": None, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}


def discover_operation_hints(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    records = []
    for row in rows:
        for phrase in metric_phrases(str(row.get("question", ""))):
            pattern = phrase.replace(" ", "[_ -]+")
            search = _run(["rg", "-l", "-i", "-m", "1", pattern, str(SITE_PACKAGES)])
            matches = [line for line in search["stdout"].splitlines() if line][:12]
            candidates: list[str] = []
            for match in matches:
                path = Path(match)
                try:
                    relative = path.relative_to(SITE_PACKAGES)
                except ValueError:
                    continue
                top = relative.parts[0].split("-")[0].replace("_", "-")
                for name in {top, top.replace("-", "_"), top.replace("-", "")}:
                    executable = AGENT_BIN / name
                    if executable.is_file() and name not in candidates:
                        candidates.append(name)
            help_records = []
            subcommand = phrase.replace(" ", "_")
            for name in candidates[:3]:
                executable = shutil.which(name, path=str(AGENT_BIN)) or str(AGENT_BIN / name)
                help_records.append(_run([executable, subcommand, "--help"]))
            excerpts = []
            for match in matches[:4]:
                excerpts.append(_run(["rg", "-n", "-i", "-m", "3", pattern, match], timeout=5))
            records.append({
                "question_id": row.get("question_id"),
                "metric_phrase": phrase,
                "site_package_matches": matches,
                "candidate_executables": candidates[:3],
                "subcommand_help": help_records,
                "documentation_excerpts": excerpts,
            })
    return {
        "source": "public question wording plus installed environment only",
        "contains_answers_or_evaluator_data": False,
        "records": records,
    }

#!/usr/bin/env python3
"""Merge accepted infrastructure recoveries into a frozen B generation.

This is a pre-scoring transport operation: it never reads answers or evaluator
state and never replaces a semantically failed episode.  A recovery episode is
eligible only when it is controller-accepted and has a successful one-shot
generation.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _is_infrastructure_failure(episode: dict[str, Any]) -> bool:
    """Recognise only failures that are safe to replace after generation.

    A service recovery must be keyed to an actually failed base episode.  In
    particular, a successful recovery for a UUID that already succeeded in
    the base run is *not* an alternative sample and must never overwrite it.
    Semantic, grounding, format, or controller failures are intentionally not
    classified here.
    """
    if episode.get("accepted"):
        return False
    reason = str(episode.get("failure_reason") or "").lower()
    if episode.get("return_code") is None:
        return any(token in reason for token in ("timeout", "timed out", "network", "connection", "service"))
    if episode.get("return_code") != 0:
        return any(token in reason for token in ("generation exit=", "timeout", "network", "connection", "service"))
    return False


def _eligible(episode: dict[str, Any], base_episode: dict[str, Any] | None) -> bool:
    """Return true only for a clean one-shot recovery of a failed base UUID."""
    return (
        base_episode is not None
        and _is_infrastructure_failure(base_episode)
        and bool(episode.get("accepted"))
        and episode.get("return_code") == 0
        and not episode.get("retry_attempted")
    )


def _artifact_paths(workspace: Path) -> list[Path]:
    names = (
        "capsule_result.json",
        "event_timeline.json",
        "events.jsonl",
        "stderr.log",
        "run_manifest.json",
        "operation_hints.json",
        "analysis/grounding.json",
        "analysis/compliance_audit.json",
        "analysis/operation_contract.json",
        "analysis/consistency.json",
    )
    paths = [workspace / name for name in names]
    scripts = workspace / "analysis/scripts"
    if scripts.is_dir():
        paths.extend(path for path in scripts.rglob("*") if path.is_file())
    return list(dict.fromkeys(paths))


def merge(base_path: Path, recovery_path: Path, output_path: Path, report_path: Path) -> dict[str, Any]:
    base = _read(base_path)
    recovery = _read(recovery_path)
    base_eps = {episode["capsule_uuid"]: episode for episode in base["episodes"]}
    replacements: list[str] = []
    rejected: list[dict[str, Any]] = []
    for episode in recovery["episodes"]:
        uuid = episode["capsule_uuid"]
        if uuid not in base_eps:
            raise ValueError(f"recovery capsule not present in base generation: {uuid}")
        if _eligible(episode, base_eps[uuid]):
            base_eps[uuid] = episode
            replacements.append(uuid)
        else:
            base_episode = base_eps[uuid]
            rejected.append({
                "capsule_uuid": uuid,
                "short_id": episode.get("short_id"),
                "reason": (
                    "base episode already accepted or was not an infrastructure failure"
                    if base_episode.get("accepted") or not _is_infrastructure_failure(base_episode)
                    else "recovery episode was not a clean accepted one-shot"
                ),
                "base_accepted": base_episode.get("accepted"),
                "base_failure_reason": base_episode.get("failure_reason"),
                "accepted": episode.get("accepted"),
                "return_code": episode.get("return_code"),
                "failure_reason": episode.get("failure_reason"),
            })

    ordered = [base_eps[episode["capsule_uuid"]] for episode in base["episodes"]]
    merged = dict(base)
    merged["episodes"] = ordered
    accepted = sum(bool(episode.get("accepted")) for episode in ordered)
    merged["generation_summary"] = dict(base.get("generation_summary", {}))
    base_calls = int(base.get("generation_summary", {}).get("total_agent_calls", base.get("generation_summary", {}).get("total_codex_calls", len(base["episodes"]))))
    recovery_calls = int(
        recovery.get("generation_summary", {}).get(
            "total_agent_calls",
            recovery.get("generation_summary", {}).get("total_codex_calls", len(recovery["episodes"])),
        )
    )
    prior_recovery_calls = int(
        base.get("generation_summary", {}).get("service_recovery_calls", 0)
    )
    prior_replacements = int(
        base.get("generation_summary", {}).get(
            "service_recovery_accepted_replacements",
            len(base.get("service_recovery", {}).get("accepted_replacements", [])),
        )
    )
    merged["generation_summary"].update({
        "accepted_capsules": accepted,
        # Every recovery episode is a real agent call, even when it times out,
        # fails compliance, or is rejected because the base UUID succeeded.
        "total_codex_calls": base_calls + recovery_calls,
        "total_agent_calls": base_calls + recovery_calls,
        "service_recovery_calls": prior_recovery_calls + recovery_calls,
        "service_recovery_accepted_replacements": prior_replacements + len(replacements),
        "service_recovery_merge": True,
        "scorer_or_evaluator_used_during_generation": False,
    })
    recovery_record = {
        "base_generation": str(base_path),
        "recovery_generation": str(recovery_path),
        "calls": recovery_calls,
        "accepted_replacements": replacements,
        "rejected_recoveries": rejected,
        "semantic_failures_preserved": True,
    }
    merged["service_recovery"] = recovery_record
    merged["service_recovery_history"] = [
        *base.get("service_recovery_history", []),
        recovery_record,
    ]

    artifacts = []
    for episode in ordered:
        workspace = Path(episode["workspace"])
        for path in _artifact_paths(workspace):
            artifacts.append({
                "path": str(path),
                "sha256": _sha(path),
            })
    merged["freeze"] = {
        "status": "frozen_before_private_scoring",
        "at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "manifest_sha256": merged["freeze"].get("manifest_sha256"),
        "artifacts": artifacts,
        "service_recovery_merge": True,
    }
    merged["scoring"] = {"status": "not_started"}
    output_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(
        "# B service-recovery merge\n\n"
        f"Accepted replacements: {len(replacements)}\n\n"
        f"Rejected/non-service recoveries: {len(rejected)}\n\n"
        "Semantic failures from the base generation were preserved.\n",
        encoding="utf-8",
    )
    return {
        "episodes": len(ordered),
        "accepted_capsules": accepted,
        "replacements": len(replacements),
        "rejected_recoveries": len(rejected),
        "output": str(output_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--recovery", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(merge(args.base, args.recovery, args.output, args.report), indent=2))


if __name__ == "__main__":
    main()

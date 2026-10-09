"""Controller-side scoring boundary for generation conditions.

Native and B are answer-blind conditions.  Their child processes receive no
scoring endpoint or credential, and an observed command that attempts to reach
an external scoring service is treated as a hard policy violation.
"""
from __future__ import annotations

import os
import re
import shutil
from typing import Any
from pathlib import Path


POLICY_EXIT_CODE = 86
POLICY_NAME = "evaluator_api_forbidden"
MAX_POLICY_RETRIES = 1
EXTERNAL_CONTAINER_ENV = "NATIVE_B_EXTERNAL_CONTAINER_ISOLATION"
PRIVATE_EVALUATION_ROOT = Path(__file__).resolve().parents[2].parent / "private"
LEGACY_PRIVATE_ROOTS = (
    Path("/mnt/files1/wangji/agent_agent/native_b_78_standalone_20260921/evaluator"),
    Path("/mnt/files1/wangji/agent_agent/open_reflection_10q_20260821/private"),
)


def private_evaluation_roots() -> list[Path]:
    bundle = Path(__file__).resolve().parents[3]
    relocated = bundle.parent / bundle.name.replace("_release_", "_private_")
    configured = tuple(Path(value).resolve() for value in
                       os.environ.get("NATIVE_B_PRIVATE_ROOTS", "").split(os.pathsep) if value)
    candidates = (PRIVATE_EVALUATION_ROOT, *LEGACY_PRIVATE_ROOTS, *configured)
    if relocated != bundle:
        candidates += (relocated,)
    return list(dict.fromkeys(path.resolve() for path in candidates if path.is_dir()))


def external_container_isolation() -> bool:
    """Validate the explicit Docker-only outer-isolation mode.

    Codex's inner Linux sandbox cannot create a nested namespace under the
    release's bubblewrap boundary on this host.  The Docker runner instead
    mounts only solver/, data/, the frozen environment, and Codex credentials.
    Inner sandbox bypass is permitted only when Docker is positively detected
    and no private evaluator root is visible inside the container.
    """
    requested = os.environ.get(EXTERNAL_CONTAINER_ENV)
    if requested is None:
        return False
    if requested != "1":
        raise RuntimeError(f"{EXTERNAL_CONTAINER_ENV} must be exactly 1")
    if not Path("/.dockerenv").is_file():
        raise RuntimeError("external container isolation requested outside Docker")
    visible = private_evaluation_roots()
    if visible:
        raise RuntimeError(f"private evaluator roots visible in Docker: {visible}")
    return True


def codex_sandbox_args() -> list[str]:
    """Return the Codex sandbox CLI arguments for the active hard boundary."""
    if external_container_isolation():
        return ["--dangerously-bypass-approvals-and-sandbox"]
    return ["--sandbox", "workspace-write"]

EVALUATOR_ENV_KEYS = (
    "EVALUATOR_API_URL",
    "EVALUATOR_API_TOKEN",
)

# These are deliberately narrow.  We inspect only actual command-execution
# events, not model prose, so discussion of the experiment does not trigger a
# retry.  The patterns cover credential variable names, local-service probes,
# and generic private scoring artifacts.
FORBIDDEN_COMMAND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("evaluator_environment", re.compile(r"\bEVALUATOR_API_(?:URL|TOKEN)\b", re.I)),
    ("local_scoring_service", re.compile(r"\b(?:127\.0\.0\.1|localhost):[0-9]{2,5}\b", re.I)),
    ("private_answer_key", re.compile(r"\b(?:answers?(?:_[0-9]+)?|answer_key)\.json\b", re.I)),
    ("private_evaluator_code", re.compile(r"\b(?:open_)?evaluator\b", re.I)),
    ("private_api_secret", re.compile(r"\btoken_config\.json\b", re.I)),
    ("private_bundle_path", re.compile(r"(?:/private|\.\./private)(?:/|\b)", re.I)),
)


def answer_blind_env(source: dict[str, str]) -> dict[str, str]:
    """Return a copy with every evaluator address and credential removed."""
    result = dict(source)
    for key in list(result):
        if key.upper().startswith("EVALUATOR_API_") or key == "NATIVE_B_PRIVATE_ROOTS":
            result.pop(key, None)
    result["GENERATION_EVALUATOR_ACCESS"] = "forbidden"
    return result


def answer_blind_command(command: list[str]) -> list[str]:
    """Hide the sibling private bundle from the entire Native/B process tree.

    When the private sibling exists, bubblewrap overlays it with an empty
    tmpfs.  Absence of bubblewrap is a fail-closed controller error.  A public
    checkout without a private sibling needs no overlay.
    """
    if external_container_isolation():
        return list(command)
    hidden_roots = private_evaluation_roots()
    if not hidden_roots:
        return list(command)
    bubblewrap = shutil.which("bwrap")
    if bubblewrap is None:
        raise RuntimeError("bwrap is required while a sibling private evaluator exists")
    wrapped = [
        bubblewrap,
        "--bind", "/", "/",
    ]
    for path in hidden_roots:
        wrapped.extend(("--tmpfs", str(path)))
    wrapped.extend((
        "--unshare-pid",
        "--proc", "/proc",
        "--die-with-parent",
        "--",
        *command,
    ))
    return wrapped


def filesystem_boundary() -> dict[str, Any]:
    from common.integrity.attempt_container import boundary_manifest
    return boundary_manifest()


def legacy_filesystem_boundary() -> dict[str, Any]:
    if external_container_isolation():
        return {
            "hidden_roots": [],
            "mechanism": "docker_allowlist_mounts_solver_data_environment_and_codex_auth_only",
            "private_roots_visible_in_container": False,
            "codex_inner_sandbox": "disabled_due_to_nested_namespace_incompatibility",
        }
    return {
        "hidden_roots": [
            str(path) for path in private_evaluation_roots()
        ],
        "mechanism": "bubblewrap_empty_tmpfs_overlay_and_private_pid_namespace_when_present",
        "fail_closed_if_bubblewrap_unavailable": True,
    }


def command_from_event(event: Any) -> str | None:
    if not isinstance(event, dict):
        return None
    if event.get("type") not in {"item.started", "item.completed"}:
        return None
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") not in {"command_execution", "file_access"}:
        return None
    command = item.get("command")
    return command if isinstance(command, str) else None


def evaluator_api_event_guard(event: Any) -> dict[str, str] | None:
    """Return violation metadata when a real shell command touches the API."""
    command = command_from_event(event)
    if command is None:
        return None
    for reason, pattern in FORBIDDEN_COMMAND_PATTERNS:
        if pattern.search(command):
            return {
                "policy": POLICY_NAME,
                "reason": reason,
                "command": command,
            }
    return None


def generation_event_guard(workspace: Path):
    """Live answer-blind API and parseable-path checks, shared by Native/B.

    Events are observations, not syscall interception: this stops the attempt
    when a violation is observed, but cannot undo an already executed command.
    Retain the final compliance audit and deployment isolation as separate layers.
    """
    from common.integrity.isolation_audit import audit_command

    def guard(event: Any) -> dict[str, Any] | None:
        violation = evaluator_api_event_guard(event)
        if violation is not None:
            return violation
        command = command_from_event(event)
        if command is None:
            return None
        item = event.get("item", {})
        paths = list(item.get("paths", []))
        if item.get("cwd"):
            paths.append(item["cwd"])
        for raw in paths:
            resolved = (workspace / raw).resolve()
            if not resolved.is_relative_to(workspace.resolve()):
                return {"policy": "generation_isolation_forbidden", "reason": "fatal_workspace_escape",
                        "command": command, "path": raw}
        for finding in audit_command(command, workspace):
            if finding["verdict"].startswith("fatal_") or finding["verdict"] == "ambiguous_path_resolution":
                return {"policy": "generation_isolation_forbidden",
                        "reason": finding["verdict"], "command": command,
                        "finding": finding}
        return None

    return guard


def policy_violation(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in records:
        event = record.get("event")
        if isinstance(event, dict) and event.get("type") == "controller.policy_violation":
            return event
    return None

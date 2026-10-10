# Path: common/integrity/codex_agent.py
# Purpose: Codex CLI command, hermetic per-attempt configuration and audited result capture.
from __future__ import annotations

import copy
import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any

from common.integrity.codex_capture import capture_codex, command_metrics
from common.integrity.progress import AttemptProgress, progress_log

CODEX_LOCK = Path(__file__).resolve().parents[2] / "environment/codex_lock.json"
CODEX_VERSION = json.loads(CODEX_LOCK.read_text(encoding="utf-8"))["version"]
AGENT_LABEL = f"codex-cli-{CODEX_VERSION}"
DEFAULT_MODEL = "openai-relay/gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "high"

# Model-provider route. The container only ever sees the gateway; the host-side
# upstream and credential live in attempt_gateway.
PROVIDERS = {
    # OpenAI Responses API through a third-party relay (e.g. https://xfastapi.ai).
    "openai-relay": {"pattern": r"gpt-[a-zA-Z0-9._-]+", "name": "OpenAI relay", "wire_api": "responses"},
}
# "ultra" is "maximum reasoning with automatic task delegation", i.e. sub-agents.
EXCLUDED_EFFORTS = ("ultra",)
# Checked before any attempt; model_catalog() re-checks against the image's catalog.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
GATEWAY_BASE_URL = "http://127.0.0.1:8765/model"
# Settings Codex requires as inline tables rather than [sections].
INLINE_TABLES = {"experimental_request_user_input"}

# Applied to the pinned binary's bundled catalog entry for the selected model.
# Codex's own tool mode (code mode), base instructions and limits are kept as shipped.
CATALOG_OVERRIDES = {
    "multi_agent_version": None,              # no sub-agents, as with OpenCode
    "include_apps_usage_instructions": False,
    "include_plugin_usage_instructions": False,
    "include_skills_usage_instructions": False,
    "prefer_websockets": False,               # the gateway proxies plain HTTP only
}
# Stable features that default on in 0.158.0 but fall outside a benchmark attempt.
DISABLED_FEATURES = (
    "enable_request_compression", "apps", "browser_use", "browser_use_external",
    "browser_use_full_cdp_access", "computer_use", "daemon_auto_start", "goals",
    "guardian_approval", "hooks", "image_generation", "in_app_browser", "in_app_chat",
    "in_app_dictation", "in_app_local_automation", "in_app_updates", "multi_agent",
    "multi_agent_v2", "plugin_sharing", "plugins", "realtime_conversation", "remote_plugin",
    "skill_mcp_dependency_install", "skill_search", "tool_suggest", "tool_call_mcp_elicitation",
    "workspace_dependencies", "worktrees", "auth_elicitation", "unbounded_connection_retries",
    "fast_mode",
)


def model_route(model: str) -> tuple[str, str]:
    """Return (provider, model ID) for an allowlisted model; the gateway binds both."""
    provider, _, identifier = model.partition("/")
    route = PROVIDERS.get(provider)
    if route is None or not re.fullmatch(route["pattern"], identifier):
        raise ValueError("--model must be openai-relay/gpt-<name>, e.g. openai-relay/gpt-5.6-sol")
    return provider, identifier


def model_id(model: str) -> str:
    return model_route(model)[1]


def solver_message(prompt: str, schema: Path) -> str:
    # The schema is given to the model as text and validated on the controller;
    # Codex's --output-schema is not used.
    contract = json.loads(schema.read_text(encoding="utf-8"))
    # This annotation identifies the schema dialect, not an answer property.
    contract.pop("$schema", None)
    keys = ", ".join(contract.get("properties", {}))
    return (prompt.rstrip()
            + "\n\nOutput JSON Schema (a definition, not the answer object):\n"
            + json.dumps(contract)
            + "\n\nFinal response requirements:\n"
            + "Return exactly one JSON object that is an INSTANCE of this schema. "
            + f"The only allowed top-level keys are: {keys}. "
            + "Do not copy schema-definition keywords such as $schema, properties, "
            + "required, or additionalProperties into your answer. "
            + "The final assistant message must contain the complete JSON object only, "
            + "with no prose, markdown fences, or text before or after it. "
            + "Return it in the final message; do not write question_result.json or "
            + "capsule_result.json, which are created by the controller.")


def codex_command(prompt: str, schema: Path, model: str) -> list[str]:
    identifier = model_id(model)
    # Docker is the external sandbox, so Codex's inner sandbox is bypassed (it
    # cannot nest namespaces under --cap-drop ALL). The working directory is the
    # container's --workdir, the attempt workspace.
    return ["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--strict-config",
            "--ignore-rules", "--dangerously-bypass-approvals-and-sandbox",
            "--model", identifier, solver_message(prompt, schema)]


def model_catalog(bundled: dict, model: str, effort: str) -> dict:
    """One-model catalog derived from the image's bundled catalog."""
    identifier = model_id(model)
    entry = next((item for item in bundled.get("models", []) if item.get("slug") == identifier), None)
    if entry is None:
        raise ValueError(f"model {identifier} is not in the pinned Codex {CODEX_VERSION} catalog")
    efforts = [level["effort"] for level in entry.get("supported_reasoning_levels", [])
               if level.get("effort") not in EXCLUDED_EFFORTS]
    if effort not in efforts:
        raise ValueError(f"--reasoning-effort for {identifier} must be one of: {', '.join(efforts)}")
    return {"models": [{**copy.deepcopy(entry), **CATALOG_OVERRIDES}]}


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)  # JSON string escapes are valid TOML basic strings.
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items()) + " }"
    raise TypeError(f"unsupported TOML value: {value!r}")


def codex_settings(model: str, effort: str, catalog_path: str) -> dict:
    """Complete Codex configuration; written as the attempt's only config.toml."""
    identifier = model_id(model)
    return {
        "model": identifier,
        "model_provider": "native_b_gateway",
        "model_catalog_json": catalog_path,
        "model_reasoning_effort": effort,
        "model_reasoning_summary": "auto",
        "approval_policy": "never",
        "sandbox_mode": "danger-full-access",
        "check_for_update_on_startup": False,
        "project_doc_max_bytes": 0,             # no AGENTS.md
        "web_search": "disabled",
        "include_apps_instructions": False,
        "include_collaboration_mode_instructions": False,
        "tools": {"experimental_request_user_input": {"enabled": False}},
        "history": {"persistence": "none"},
        "analytics": {"enabled": False},
        "feedback": {"enabled": False},
        "shell_environment_policy": {"inherit": "all"},  # Docker already allowlists the env.
        "skills": {"include_instructions": False, "bundled": {"enabled": False}},
        "model_providers": {"native_b_gateway": {
            "name": "Native/B attempt gateway",
            "base_url": GATEWAY_BASE_URL,
            "env_key": "NATIVE_B_GATEWAY_TOKEN",
            "wire_api": "responses",
            "supports_websockets": False,
            "request_max_retries": 2,
            "stream_max_retries": 2,
            "stream_idle_timeout_ms": 300_000,
        }},
        "features": {name: False for name in DISABLED_FEATURES},
    }


def codex_config(model: str, effort: str, catalog_path: str) -> str:
    """Render codex_settings() as TOML: scalars first, then one table per section."""
    lines: list[str] = []

    def emit(prefix: str, table: dict) -> None:
        scalars = {key: value for key, value in table.items()
                   if not isinstance(value, dict) or key in INLINE_TABLES}
        if prefix and scalars:
            lines.extend(["", f"[{prefix}]"])
        lines.extend(f"{key} = {_toml_value(value)}" for key, value in scalars.items())
        for key, value in table.items():
            if key not in scalars:
                emit(f"{prefix}.{key}" if prefix else key, value)

    emit("", codex_settings(model, effort, catalog_path))
    return "\n".join(lines) + "\n"


def _workspace_relative(path: str, workspace: Path | None) -> str:
    """Report in-workspace paths relative to it, as the path guards resolve them, so
    the checkout's own location never reaches command-text policy patterns."""
    if workspace is not None and Path(path).is_absolute():
        candidate = Path(path)
        if candidate == workspace or candidate.is_relative_to(workspace):
            return str(candidate.relative_to(workspace)) or "."
    return path


def normalize_event(raw: Any, workspace: Path | None = None) -> dict[str, Any]:
    """Map Codex exec JSON items onto the audit event shape, retaining the original.

    Codex already reports shell runs as command_execution items with the command,
    output and exit code. apply_patch edits arrive as file_change items with
    absolute paths; they become file_access items so path guards see them.
    """
    if not isinstance(raw, dict):
        return {"type": "codex.unknown", "codex_event": raw}
    event = {**raw, "codex_event": raw}
    item = raw.get("item")
    if not isinstance(item, dict):
        return event
    kind = item.get("type")
    if kind == "command_execution":
        event["item"] = {**item, "agent_tool": "shell"}
    elif kind == "file_change":
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        paths = [_workspace_relative(change["path"], workspace) for change in changes
                 if isinstance(change, dict) and isinstance(change.get("path"), str)]
        event["item"] = {**item, "type": "file_access", "codex_item_type": "file_change",
                         "agent_tool": "apply_patch", "paths": paths,
                         "command": "cat " + " ".join(shlex.quote(path) for path in paths)}
    elif kind in {"mcp_tool_call", "web_search", "collab_tool_call"}:
        event["item"] = {**item, "agent_tool": "other"}
    return event


def capture_agent(command, env, timeout, event_guard=None, policy_exit_code=86,
                  *, progress_label=None):
    progress_label = progress_label or "Codex"
    # The attempt container's --workdir is the workspace (absent for unit-test commands).
    workspace = Path(command[command.index("--workdir") + 1]) if "--workdir" in command else None
    progress = AttemptProgress(progress_label)
    progress_log(progress_label, f"agent started timeout={timeout}s")
    try:
        result = capture_codex(command, env, timeout, event_guard=event_guard,
                               policy_exit_code=policy_exit_code,
                               event_transform=lambda raw: normalize_event(raw, workspace),
                               progress_callback=progress)
    except Exception as exc:
        progress_log(progress_label, f"agent capture failed type={type(exc).__name__}")
        raise
    code, records, _, runtime = result
    status = "timeout" if code is None else "policy interrupted" if code == policy_exit_code else f"exit={code}"
    progress_log(progress_label, f"agent finished {status} elapsed={runtime:.1f}s events={len(records)}; validating and saving logs")
    return result


def _raw(record: dict) -> Any:
    event = record.get("event", {})
    return event.get("codex_event", event) if isinstance(event, dict) else event


def write_result(records: list[dict], schema: Path, destination: Path) -> dict:
    """Reject failed turns and malformed output before writing a controller result."""
    from jsonschema import Draft202012Validator

    messages: list[str] = []
    completed = False
    for record in records:
        raw = _raw(record)
        if not isinstance(raw, dict):
            continue
        if raw.get("type") == "turn.failed":
            raise ValueError("Codex reported a failed turn")
        # Plain "error" events also report retried transport errors
        # ("Reconnecting... 1/2"); only the turn outcome is decisive.
        if raw.get("type") == "turn.completed":
            completed = True
        item = raw.get("item")
        if (raw.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
            messages.append(item["text"])
    if not completed:
        raise ValueError("Codex turn did not complete")
    if not messages:
        raise ValueError("Codex produced no final assistant message")
    text = messages[-1].strip()
    # A single presentation fence is harmless; surrounding prose/multiple objects fail.
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    payload = json.loads(text)
    validator = Draft202012Validator(json.loads(schema.read_text(encoding="utf-8")))
    validator.validate(payload)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("result is controller-owned; refusing a solver-created result file")
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def generation_failure(return_code: int | None, records: list[dict]) -> str:
    """Summarize the failing event type and HTTP status without copying messages or answers."""
    if return_code is None:
        return "generation timeout; inspect events.jsonl and stderr.log"
    detail = f"generation exit={return_code}"
    for record in reversed(records):
        raw = _raw(record)
        if not isinstance(raw, dict) or raw.get("type") not in {"turn.failed", "error"}:
            continue
        detail += f"; Codex event={raw['type']}"
        error = raw.get("error") if isinstance(raw.get("error"), dict) else raw
        message = error.get("message") if isinstance(error, dict) else None
        status = re.search(r"\b(?:status|HTTP)[ :=]*([1-5]\d\d)\b", message or "", re.I)
        if status:
            detail += f"; HTTP status={status.group(1)}"
        break
    return detail + "; inspect events.jsonl and stderr.log"


def agent_manifest(model: str, effort: str) -> dict:
    route = PROVIDERS[model_route(model)[0]]
    if effort not in EFFORTS:
        raise ValueError(f"--reasoning-effort must be one of: {', '.join(EFFORTS)}")
    return {"cli": "codex", "cli_version": CODEX_VERSION,
            "capture_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "codex_lock_sha256": hashlib.sha256(CODEX_LOCK.read_bytes()).hexdigest(),
            "provider": route["name"], "wire_api": route["wire_api"], "route": "host_attempt_gateway",
            "tool_mode": "as shipped in the pinned catalog (code mode for gpt-5.6-*)",
            "catalog_overrides": CATALOG_OVERRIDES,
            "config": codex_settings(model, effort, "$CODEX_HOME/model_catalog.json")}


__all__ = ["AGENT_LABEL", "CODEX_VERSION", "DEFAULT_MODEL", "DEFAULT_REASONING_EFFORT", "agent_manifest",
           "capture_agent", "codex_command", "codex_config", "command_metrics", "generation_failure",
           "model_catalog", "model_id", "model_route", "normalize_event", "write_result"]

# Path: common/integrity/opencode_capture.py
# Purpose: OpenCode v2 command, isolated provider configuration and audited result capture.
from __future__ import annotations

import json
import hashlib
import re
import shlex
from pathlib import Path
from typing import Any

from common.integrity.codex_capture import capture_codex, command_metrics
from common.integrity.progress import AttemptProgress, progress_log

DEFAULT_MODEL = "deepseek/deepseek-flash"
DEFAULT_REASONING_EFFORT = "high"

# Model-provider routes. The container only ever sees the gateway; the host-side
# upstream and credential for each provider live in attempt_gateway.
PROVIDERS = {
    "deepseek": {"pattern": r"deepseek-[a-zA-Z0-9._-]+", "efforts": ("none", "low", "medium", "high", "max"),
                 "name": "DeepSeek", "wire_api": "chat/completions"},
    # OpenAI Responses API through a third-party relay (e.g. https://xfastapi.ai).
    "openai-relay": {"pattern": r"gpt-[a-zA-Z0-9._-]+", "efforts": ("none", "low", "medium", "high", "xhigh"),
                     "name": "OpenAI relay", "wire_api": "responses"},
}
RELAY_LIMIT = {"context": 1_000_000, "output": 128_000}


def model_route(model: str) -> tuple[str, str]:
    """Return (provider, model ID) for an allowlisted direct model; the gateway binds both."""
    provider, _, identifier = model.partition("/")
    route = PROVIDERS.get(provider)
    if route is None or not re.fullmatch(route["pattern"], identifier):
        raise ValueError("--model must be deepseek/deepseek-<name> or openai-relay/gpt-<name>, "
                         "e.g. deepseek/deepseek-flash")
    return provider, identifier


def model_id(model: str) -> str:
    return model_route(model)[1]


def opencode_command(prompt: str, schema: Path, model: str) -> list[str]:
    model_route(model)
    # OpenCode's --format controls the event stream, not the answer schema.
    # Supply the same schema to the model, then validate on the controller.
    contract = json.loads(schema.read_text(encoding="utf-8"))
    # This annotation identifies the schema dialect, not an answer property.
    contract.pop("$schema", None)
    keys = ", ".join(contract.get("properties", {}))
    message = (prompt.rstrip()
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
    return ["opencode", "run", "--standalone", "--auto", "--agent", "benchmark",
            "--model", model, "--format", "json", "--title", "Benchmark attempt", message]


def provider_config(model: str, effort: str, base_url: str = "http://127.0.0.1:8765/model") -> dict:
    provider, identifier = model_route(model)
    efforts = PROVIDERS[provider]["efforts"]
    if effort not in efforts:
        raise ValueError(f"--reasoning-effort for {provider} must be one of: {', '.join(efforts)}")
    if provider == "deepseek":
        body: dict[str, Any] = {"thinking": {"type": "disabled" if effort == "none" else "enabled"}}
        if effort != "none":
            body["reasoning_effort"] = effort
        provider_entry = {
            "env": ["NATIVE_B_GATEWAY_TOKEN"],
            "package": "@opencode/ai/providers/deepseek",
            "settings": {"baseURL": base_url},
            "models": {identifier: {"body": body}},
        }
    else:
        # Stateless Responses: OpenCode sends store=false and replays encrypted
        # reasoning. The gateway proxies plain HTTP only, never WebSockets.
        settings: dict[str, Any] = {"baseURL": base_url, "transport": "http", "reasoningEffort": effort}
        if effort != "none":
            settings["reasoningSummary"] = "auto"
        provider_entry = {
            "env": ["NATIVE_B_GATEWAY_TOKEN"],
            "package": "@opencode/ai/providers/openai/responses",
            "settings": settings,
            "models": {identifier: {"limit": dict(RELAY_LIMIT)}},
        }
    permissions = [
        {"action": "*", "resource": "*", "effect": "deny"},
        *[{"action": action, "resource": "*", "effect": "allow"}
          for action in ("read", "edit", "glob", "grep", "shell", "webfetch")],
        {"action": "external_directory", "resource": "*", "effect": "deny"},
    ]
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "default_agent": "benchmark",
        "snapshots": False,
        "providers": {provider: provider_entry},
        "agents": {"benchmark": {
            "description": "Answer-blind benchmark solver in one isolated attempt",
            "mode": "primary",
            "model": model,
            "permissions": permissions,
        }},
    }


def normalize_event(raw: Any) -> dict[str, Any]:
    """Adapt v2 CLI tool_use/text events to the existing audit event shape.

    The complete original event is retained. The v2 CLI reports tools after
    completion; these observations are not a pre-execution security boundary.
    """
    if not isinstance(raw, dict):
        return {"type": "opencode.unknown", "opencode_event": raw}
    event = {**raw, "opencode_event": raw}
    part = raw.get("part")
    if raw.get("type") != "tool_use" or not isinstance(part, dict):
        return event
    state = part.get("state", {})
    if not isinstance(state, dict):
        return event
    inputs = state.get("input", {})
    if not isinstance(inputs, dict):
        inputs = {}
    tool = part.get("tool")
    status = state.get("status")
    item: dict[str, Any] = {"id": part.get("id"), "opencode_tool": tool, "status": status}
    if tool in {"shell", "bash"} and isinstance(inputs.get("command"), str):
        metadata = state.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        metadata = metadata.get("metadata", metadata)
        if not isinstance(metadata, dict):
            metadata = {}
        item.update(type="command_execution", command=inputs["command"],
                    aggregated_output=state.get("output", ""),
                    exit_code=metadata.get("exit", metadata.get("exitCode")),
                    cwd=inputs.get("workdir"))
    elif tool in {"read", "write", "edit", "patch", "apply_patch", "glob", "grep"}:
        paths = [inputs[key] for key in ("filePath", "file_path", "path")
                 if isinstance(inputs.get(key), str)]
        patch = inputs.get("patchText", inputs.get("patch", ""))
        if isinstance(patch, str):
            paths.extend(re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.M))
            paths.extend(re.findall(r"^\*\*\* Move to: (.+)$", patch, re.M))
        item.update(type="file_access", paths=paths,
                    command="cat " + " ".join(shlex.quote(path) for path in paths))
    else:
        item.update(type="opencode_tool")
    event.update(type="item.completed" if status in {"completed", "error"} else "item.started", item=item)
    return event


def capture_opencode(command, env, timeout, event_guard=None, policy_exit_code=86,
                     *, progress_label=None):
    if progress_label is None:
        workspace = Path(command[command.index("--workdir") + 1]) if "--workdir" in command else None
        progress_label = f"{workspace.parent.name}/{workspace.name}" if workspace else "OpenCode"
    progress = AttemptProgress(progress_label)
    progress_log(progress_label, f"agent started timeout={timeout}s")
    try:
        result = capture_codex(command, env, timeout, event_guard=event_guard,
                               policy_exit_code=policy_exit_code, event_transform=normalize_event,
                               progress_callback=progress)
    except Exception as exc:
        progress_log(progress_label, f"agent capture failed type={type(exc).__name__}")
        raise
    code, records, _, runtime = result
    status = "timeout" if code is None else "policy interrupted" if code == policy_exit_code else f"exit={code}"
    progress_log(progress_label, f"agent finished {status} elapsed={runtime:.1f}s events={len(records)}; validating and saving logs")
    return result


def write_result(records: list[dict], schema: Path, destination: Path) -> dict:
    """Reject model errors and malformed output before writing a controller result."""
    from jsonschema import Draft202012Validator

    messages: dict[str, list[str]] = {}
    for record in records:
        raw = record["event"].get("opencode_event", record["event"])
        if not isinstance(raw, dict):
            continue
        if raw.get("type") == "error":
            raise ValueError("OpenCode reported a model/session error")
        part = raw.get("part", {})
        if not isinstance(part, dict):
            continue
        if raw.get("type") == "text" and isinstance(part.get("text"), str):
            messages.setdefault(part.get("messageID", "final"), []).append(part["text"])
    if not messages:
        raise ValueError("OpenCode produced no final assistant text")
    text = "".join(next(reversed(messages.values()))).strip()
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
    """Summarize CLI error type/status without copying error bodies or answers."""
    if return_code is None:
        return "generation timeout; inspect events.jsonl and stderr.log"
    detail = f"generation exit={return_code}"
    for record in reversed(records):
        event = record.get("event", {})
        if not isinstance(event, dict):
            continue
        raw = event.get("opencode_event", event)
        if not isinstance(raw, dict) or raw.get("type") != "error":
            continue
        error = raw.get("error", {})
        if not isinstance(error, dict):
            break
        name = error.get("name")
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", name):
            detail += f"; OpenCode error={name}"
        data = error.get("data", {})
        status = data.get("statusCode") if isinstance(data, dict) else None
        if isinstance(status, int) and 100 <= status <= 599:
            detail += f"; HTTP status={status}"
        break
    return detail + "; inspect events.jsonl and stderr.log"


def agent_manifest(model: str, effort: str) -> dict:
    route = PROVIDERS[model_route(model)[0]]
    return {"cli": "opencode", "cli_major_version": 2,
            "capture_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "provider": route["name"], "wire_api": route["wire_api"], "route": "host_attempt_gateway",
            "config": provider_config(model, effort)}

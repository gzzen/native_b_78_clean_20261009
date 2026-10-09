# Path: common/integrity/codex_capture.py
# Purpose: Byte-preserving Codex event capture and command metrics extracted from the A84 runner dependencies.
from __future__ import annotations
import json, os, queue, re, signal, subprocess, threading, time
from typing import Any, Callable
SCIENTIFIC_SCRIPT = re.compile(r"\b(?:python|python3)\s+(?:analysis|scripts)/[^\s]+|\bRscript\s+|\bjupyter\s+nbconvert")
FRAMEWORK_ONLY = re.compile(r"(?:episode_plan_schema|question_result_schema|artifact_manifest_schema|plan_revision_schema|jsonschema|py_compile|artifact_manifest|episode_result|plan_revisions)")
TASK_DATA_INSPECTION = re.compile(r"(?:\bdata/|questions\.json|\bscipy\b|\bstats\b)")
TASK_WORKSPACE_INVENTORY = re.compile(r"(?:task_episode\.json|\brg\b|\bls\b|\bfind\b)")
def capture_codex(
    command: list[str],
    env: dict[str, str],
    timeout: int,
    event_guard: Callable[[Any], dict[str, Any] | None] | None = None,
    policy_exit_code: int = 86,
    event_transform: Callable[[Any], dict[str, Any]] | None = None,
    progress_callback: Callable[[float, dict[str, Any] | None], None] | None = None,
) -> tuple[int | None, list[dict[str, Any]], str, float]:
    """Capture actual event arrival times while preserving raw Codex JSONL."""
    started = time.monotonic(); process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, start_new_session=True); stderr: list[str] = []; stdout: queue.Queue[tuple[float, str]] = queue.Queue()
    def terminate_tree() -> None:
        if process.poll() is None:
            try: os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError: pass
    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout: stdout.put((time.monotonic(), line))
    thread = threading.Thread(target=lambda: stderr.extend(process.stderr.readlines()), daemon=True); out_thread = threading.Thread(target=read_stdout, daemon=True); thread.start(); out_thread.start(); records: list[dict[str, Any]] = []; timed_out = False; policy_stopped = False
    try:
        while process.poll() is None or out_thread.is_alive() or not stdout.empty():
            if progress_callback is not None:
                progress_callback(time.monotonic() - started, None)
            if time.monotonic() - started > timeout:
                terminate_tree(); timed_out = True; break
            try: observed, line = stdout.get(timeout=0.25)
            except queue.Empty:
                if time.monotonic() - started > timeout: terminate_tree(); timed_out = True; break
                continue
            try: event = json.loads(line)
            except json.JSONDecodeError: event = {"type": "non_json_stdout", "text": line.rstrip()}
            if event_transform is not None:
                event = event_transform(event)
            elapsed = round(observed - started, 3)
            records.append({"elapsed_seconds": elapsed, "event": event})
            if progress_callback is not None:
                progress_callback(elapsed, event)
            violation = event_guard(event) if event_guard is not None else None
            if violation is not None:
                records.append({
                    "elapsed_seconds": elapsed,
                    "event": {
                        "type": "controller.policy_violation",
                        **violation,
                        "action": "interrupt_attempt",
                    },
                })
                terminate_tree()
                policy_stopped = True
                break
        if timed_out or policy_stopped:
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait()
            return_code = policy_exit_code if policy_stopped else None
        else: return_code = process.wait(timeout=1)
    finally:
        thread.join(timeout=5); out_thread.join(timeout=5)
        if process.stdout is not None: process.stdout.close()
        if process.stderr is not None: process.stderr.close()
    return return_code, records, "".join(stderr), round(time.monotonic() - started, 3)
def command_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify captured commands conservatively; mixed commands remain uncertain."""
    completed = []
    for record in records:
        event, item = record["event"], record["event"].get("item", {}) if isinstance(record["event"], dict) else {}
        if event.get("type") == "item.completed" and item.get("type") == "command_execution": completed.append((record["elapsed_seconds"], str(item.get("command", ""))))
    classified: dict[str, list[tuple[float, str]]] = {"task_inspection_and_planning": [], "scientific_computation": [], "framework_bookkeeping_and_validation": [], "uncertain": []}
    for elapsed, command in completed:
        scientific = bool(SCIENTIFIC_SCRIPT.search(command))
        framework = bool(FRAMEWORK_ONLY.search(command))
        task_data = bool(TASK_DATA_INSPECTION.search(command))
        task_inventory = bool(TASK_WORKSPACE_INVENTORY.search(command))
        if scientific and not framework:
            bucket = "scientific_computation"
        elif task_data:
            bucket = "task_inspection_and_planning"
        elif framework and not scientific:
            bucket = "framework_bookkeeping_and_validation"
        elif task_inventory:
            bucket = "task_inspection_and_planning"
        else:
            bucket = "uncertain"
        classified[bucket].append((elapsed, command))
    task_actions = classified["task_inspection_and_planning"] + classified["scientific_computation"]; scientific = classified["scientific_computation"]
    return {"command_count": len(completed), "time_to_first_task_action_seconds": min((item[0] for item in task_actions), default=None), "time_to_first_scientific_computation_seconds": scientific[0][0] if scientific else None, "task_inspection_and_planning_command_count": len(classified["task_inspection_and_planning"]), "scientific_computation_command_count": len(scientific), "framework_bookkeeping_and_validation_command_count": len(classified["framework_bookkeeping_and_validation"]), "uncertain_command_count": len(classified["uncertain"]), "command_classification": [{"elapsed_seconds": elapsed, "command": command, "class": bucket} for bucket, commands in classified.items() for elapsed, command in commands]}

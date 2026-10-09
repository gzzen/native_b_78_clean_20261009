#!/usr/bin/env python3
# Path: scripts/replay_solution_code.py
# Purpose: Replay Native entrypoints or final-answer-cited B solution scripts twice without an LLM.
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

CITATION = re.compile(r"analysis/scripts/[^\s,;`\)]+")
RUNNABLE = {".py", ".r", ".sh"}
VOLATILE_SUMMARY_FIELD = re.compile(r"(?m)^(Date|Time):\s+.*$")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_text(value: str, workspace: Path | None = None) -> str:
    """Ignore presentation-only wall-clock fields emitted by statsmodels summaries."""
    if workspace is not None:
        value = value.replace(str(workspace), "<workspace>")
    return VOLATILE_SUMMARY_FIELD.sub(lambda match: f"{match.group(1)}: <normalized>", value)


def normalize_json(value: Any, workspace: Path | None = None) -> Any:
    if isinstance(value, dict):
        return {
            key: "<temporary directory>" if key == "temporary_workdir" and isinstance(item, str)
            else normalize_json(item, workspace)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalize_json(item, workspace) for item in value]
    if isinstance(value, str):
        return normalize_text(value, workspace)
    return value


def result_digest(path: Path, workspace: Path) -> str:
    if path.suffix.lower() == ".json":
        try:
            normalized = json.dumps(normalize_json(json.loads(path.read_text(encoding="utf-8")), workspace), sort_keys=True)
            return hashlib.sha256(normalized.encode()).hexdigest()
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    try:
        normalized = normalize_text(path.read_text(encoding="utf-8"), workspace)
        return hashlib.sha256(normalized.encode()).hexdigest()
    except UnicodeDecodeError:
        return digest(path)


def stdout_digest(value: str, workspace: Path) -> str:
    value = value.replace(str(workspace), "<workspace>")
    try:
        normalized = json.dumps(normalize_json(json.loads(value)), sort_keys=True)
    except json.JSONDecodeError:
        normalized = normalize_text(value)
    return hashlib.sha256(normalized.encode()).hexdigest()


def artifact_hashes(workspace: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    analysis = workspace / "analysis"
    if not analysis.is_dir():
        return result
    for path in sorted(analysis.rglob("*")):
        if path.is_file() and "scripts" not in path.relative_to(analysis).parts:
            result[str(path.relative_to(workspace))] = result_digest(path, workspace)
    return result


def command_for(script: Path, python: str, rscript: str) -> list[str]:
    suffix = script.suffix.lower()
    if suffix == ".py":
        return [python, str(script)]
    if suffix == ".r":
        return [rscript, str(script)]
    if suffix == ".sh":
        return ["/bin/bash", str(script)]
    raise ValueError(f"not runnable: {script}")


def cited_scripts(workspace: Path, result_name: str) -> list[str]:
    payload = json.loads((workspace / result_name).read_text(encoding="utf-8"))
    cited: set[str] = set()
    results = payload.get("results", [payload])
    for result in results:
        for candidate in result.get("candidates", []):
            for raw in CITATION.findall(str(candidate.get("evidence", ""))):
                relative = raw.split("](", 1)[0].rstrip("].,:;!?`")
                if Path(relative).suffix.lower() in RUNNABLE:
                    cited.add(relative)
    return sorted(cited)


def native_entrypoints(workspace: Path) -> list[str]:
    script_root = workspace / "analysis/scripts"
    if not script_root.is_dir():
        return []
    return sorted(
        str(path.relative_to(workspace))
        for path in script_root.rglob("*")
        if path.is_file() and path.suffix.lower() in RUNNABLE
    )


def native_solution_entrypoints(workspace: Path) -> tuple[str, list[str]]:
    payload = json.loads((workspace / "question_result.json").read_text(encoding="utf-8"))
    status = str(payload.get("status", "unresolved"))
    scripts = cited_scripts(workspace, "question_result.json") if status == "resolved" else []
    # A failed controller record may have a resolved payload without a valid
    # citation.  Replay every runnable persisted source in that fail-closed case.
    return status, scripts or native_entrypoints(workspace)


def invokes_script(command: str, relative: str) -> bool:
    return bool(re.search(
        r"(?<![A-Za-z0-9_])(?:[^\s'\"]*/)?(?:python(?:\d+(?:\.\d+)?)?|Rscript|bash|sh)\s+(?:-[A-Za-z]+\s+)*['\"]?"
        + re.escape(relative),
        command,
    ))


def pipeline_entrypoints(workspace: Path) -> list[str]:
    """Order every actually invoked frozen source by its final invocation."""
    scripts = native_entrypoints(workspace)
    events = json.loads((workspace / "event_timeline.json").read_text(encoding="utf-8"))
    last_position: dict[str, tuple[int, int]] = {}
    for position, record in enumerate(events):
        event = record.get("event", {})
        if event.get("type") != "item.completed":
            continue
        command = str(event.get("item", {}).get("command", ""))
        for relative in scripts:
            if invokes_script(command, relative):
                last_position[relative] = (position, command.find(relative))
    return sorted(last_position, key=lambda relative: (last_position[relative], relative))


def executed_commands(workspace: Path, scripts: list[str]) -> dict[str, dict[str, Any]]:
    events = json.loads((workspace / "event_timeline.json").read_text(encoding="utf-8"))
    commands: dict[str, dict[str, Any]] = {}
    for relative in scripts:
        completed = []
        for record in events:
            event = record.get("event", {})
            item = event.get("item", {}) if isinstance(event, dict) else {}
            command = str(item.get("command", ""))
            if event.get("type") == "item.completed" and invokes_script(command, relative):
                completed.append({"command": command, "exit_code": item.get("exit_code")})
        if completed:
            clean = [item for item in completed if "apply_patch" not in item["command"] and "*** Begin Patch" not in item["command"]]
            # Commands that combine patching and execution are replayed from
            # the frozen final source directly; patching it again is invalid.
            selected = (clean or completed)[-1]
            commands[relative] = {
                "command": selected["command"] if clean else None,
                "exit_code": selected["exit_code"],
            }
    return commands


def pipeline_commands(workspace: Path, scripts: list[str]) -> dict[str, dict[str, Any]]:
    """Use the final standalone invocation; run compound steps from frozen source."""
    events = json.loads((workspace / "event_timeline.json").read_text(encoding="utf-8"))
    result: dict[str, dict[str, Any]] = {}
    for relative in scripts:
        standalone = []
        for record in events:
            event = record.get("event", {})
            if event.get("type") != "item.completed":
                continue
            item = event.get("item", {})
            command = str(item.get("command", ""))
            if not invokes_script(command, relative):
                continue
            invocations = [script for script in scripts if invokes_script(command, script)]
            if len(invocations) == 1 and "apply_patch" not in command and "*** Begin Patch" not in command:
                standalone.append({"command": command, "exit_code": item.get("exit_code")})
        result[relative] = standalone[-1] if standalone else {"command": None, "exit_code": 0}
    return result


def prepare_trial(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for name in ("question.json", "questions.json", "environment_contract.json", "operation_hints.json"):
        path = source / name
        if path.is_file():
            shutil.copy2(path, destination / name)
    source_data = source / "data"
    if source_data.is_symlink():
        os.symlink(source_data.resolve(), destination / "data", target_is_directory=True)
    else:
        shutil.copytree(source_data, destination / "data", copy_function=shutil.copy2)
    scripts = destination / "analysis/scripts"
    scripts.parent.mkdir(parents=True)
    shutil.copytree(source / "analysis/scripts", scripts, copy_function=shutil.copy2)
    grounding = source / "analysis/grounding.json"
    if grounding.is_file():
        shutil.copy2(grounding, destination / "analysis/grounding.json")
    # Keep the original workspace's directory layout without copying generated
    # result files into the replay. Scripts that write analysis/results need
    # their output directory to exist, but must regenerate its contents.
    source_analysis = source / "analysis"
    if source_analysis.is_dir():
        for directory in source_analysis.rglob("*"):
            if directory.is_dir() and "scripts" not in directory.relative_to(source_analysis).parts:
                (destination / directory.relative_to(source)).mkdir(parents=True, exist_ok=True)


def run_trial(source: Path, destination: Path, scripts: list[str], recorded_commands: dict[str, dict[str, Any]], python: str, rscript: str, timeout: int) -> dict[str, Any]:
    prepare_trial(source, destination)
    env = dict(os.environ)
    for key in list(env):
        if any(marker in key.upper() for marker in ("OPENAI", "ANTHROPIC", "CODEX")):
            env.pop(key, None)
    env.update({
        "REPLAY_NO_LLM": "1",
        "PYTHONHASHSEED": "0",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
        "CONDA_PREFIX": str(Path(python).resolve().parents[1]),
    })
    temp_dir = destination / "tmp"
    temp_dir.mkdir()
    for key in ("TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "MPLCONFIGDIR", "NUMBA_CACHE_DIR", "JOBLIB_TEMP_FOLDER"):
        env[key] = str(temp_dir)
    records = []
    for relative in scripts:
        script = destination / relative
        recorded = recorded_commands.get(relative)
        command = command_for(script, python, rscript)
        shell = False
        executable = None
        expected_exit_code = 0
        if recorded is not None:
            expected_exit_code = recorded.get("exit_code")
        if recorded is not None and recorded.get("command") is not None:
            command = str(recorded["command"]).replace(str(source), str(destination))
            shell = True
            executable = "/bin/bash"
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=destination,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
                shell=shell,
                executable=executable,
            )
            return_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            return_code = None
            stdout = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            timed_out = True
        records.append({
            "script": relative,
            "source_sha256": digest(script),
            "return_code": return_code,
            "generation_exit_code": expected_exit_code,
            "timed_out": timed_out,
            "replay_command": command,
            "runtime_seconds": time.monotonic() - started,
            "stdout_sha256": stdout_digest(stdout, destination),
            "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
            "stdout": stdout,
            "stderr": stderr,
        })
    return {"scripts": records, "artifact_sha256s": artifact_hashes(destination)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--condition", choices=("native", "b"), required=True)
    parser.add_argument("--network-policy", default="caller_enforced")
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--python", default="/home/wangji/miniconda3/envs/agent_bix/bin/python3.11")
    parser.add_argument("--rscript", default="/home/wangji/miniconda3/envs/agent_bix/bin/Rscript")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--question-ids", help="comma-separated Native question IDs for a focused replay audit")
    parser.add_argument("--short-ids", help="comma-separated B capsule short IDs for a focused replay audit")
    parser.add_argument("--pipeline", action="store_true", help="replay every originally invoked B script in dependency order")
    parser.add_argument("--include-stdout", action="store_true", help="retain replay stdout in the audit for output-level review")
    args = parser.parse_args()
    args.generation = args.generation.resolve()
    args.output_root = args.output_root.resolve()
    args.report = args.report.resolve()
    generation = json.loads(args.generation.read_text(encoding="utf-8"))
    if generation.get("freeze", {}).get("status") != "frozen_before_private_scoring":
        raise RuntimeError("generation is not frozen")
    if args.output_root.exists() and any(args.output_root.iterdir()):
        raise RuntimeError("refusing nonempty replay output root")
    args.output_root.mkdir(parents=True, exist_ok=True)

    source_records = generation["records" if args.condition == "native" else "episodes"]
    if args.question_ids:
        if args.condition != "native":
            raise ValueError("--question-ids is only supported for Native")
        selected = {item.strip() for item in args.question_ids.split(",") if item.strip()}
        found = {record["question_id"] for record in source_records}
        if selected - found:
            raise ValueError(f"unknown question IDs: {sorted(selected - found)}")
        source_records = [record for record in source_records if record["question_id"] in selected]
    if args.short_ids:
        if args.condition != "b":
            raise ValueError("--short-ids is only supported for B")
        selected = {item.strip() for item in args.short_ids.split(",") if item.strip()}
        found = {record["short_id"] for record in source_records}
        if selected - found:
            raise ValueError(f"unknown short IDs: {sorted(selected - found)}")
        source_records = [record for record in source_records if record["short_id"] in selected]
    if args.pipeline and args.condition != "b":
        raise ValueError("--pipeline is only supported for B")
    units = []
    for record in source_records:
        source = Path(record["workspace"])
        if args.condition == "native":
            source_status, scripts = native_solution_entrypoints(source)
        else:
            source_status = "capsule"
            scripts = pipeline_entrypoints(source) if args.pipeline else cited_scripts(source, "capsule_result.json")
        recorded_commands = pipeline_commands(source, scripts) if args.pipeline else executed_commands(source, scripts)
        trials = []
        for trial_number in (1, 2):
            destination = args.output_root / f"trial{trial_number}" / source.name
            trials.append(run_trial(source, destination, scripts, recorded_commands, args.python, args.rscript, args.timeout))
        first = {row["script"]: row for row in trials[0]["scripts"]}
        second = {row["script"]: row for row in trials[1]["scripts"]}
        script_checks = []
        for relative in scripts:
            a, b = first[relative], second[relative]
            check = {
                "script": relative,
                "source_sha256": a["source_sha256"],
                "trial1_return_code": a["return_code"],
                "trial2_return_code": b["return_code"],
                "trial1_timed_out": a["timed_out"],
                "trial2_timed_out": b["timed_out"],
                "both_succeeded": a["return_code"] == b["return_code"] == 0,
                "generation_exit_code": a["generation_exit_code"],
                "both_matched_generation_exit": not (a["timed_out"] or b["timed_out"]) and a["return_code"] == b["return_code"] == a["generation_exit_code"],
                "stdout_identical": a["stdout_sha256"] == b["stdout_sha256"],
                "stdout_sha256": a["stdout_sha256"] if a["stdout_sha256"] == b["stdout_sha256"] else None,
                "trial1_runtime_seconds": a["runtime_seconds"],
                "trial2_runtime_seconds": b["runtime_seconds"],
                "trial1_stderr": a["stderr"],
                "trial2_stderr": b["stderr"],
            }
            if args.include_stdout:
                check["trial1_stdout"] = a["stdout"]
                check["trial2_stdout"] = b["stdout"]
            script_checks.append(check)
        units.append({
            "question_id": record.get("question_id"),
            "short_id": record["short_id"],
            "capsule_uuid": record["capsule_uuid"],
            "source_workspace": str(source),
            "source_status": source_status,
            "cited_solution_scripts": scripts,
            "script_checks": script_checks,
            "artifacts_identical": trials[0]["artifact_sha256s"] == trials[1]["artifact_sha256s"],
            "trial1_artifact_sha256s": trials[0]["artifact_sha256s"],
            "trial2_artifact_sha256s": trials[1]["artifact_sha256s"],
        })

    checks = [check for unit in units for check in unit["script_checks"]]
    summary = {
        "status": "pass" if all(c["both_matched_generation_exit"] and c["stdout_identical"] for c in checks)
        and all(unit["artifacts_identical"] for unit in units)
        and bool(checks) else "fail",
        "condition": args.condition,
        "llm_calls": 0,
        "network_policy": args.network_policy,
        "comparison_policy": "exact after normalizing trial workspace paths and statsmodels Date/Time presentation fields",
        "selection_policy": "all_originally_invoked_B_scripts" if args.pipeline else "final_answer_cited_scripts",
        "units": len(units),
        "units_with_solution_code": sum(bool(unit["cited_solution_scripts"]) for unit in units),
        "solution_entrypoints": len(checks),
        "scripts_succeeded_twice": sum(c["both_succeeded"] for c in checks),
        "scripts_matching_generation_exit_twice": sum(c["both_matched_generation_exit"] for c in checks),
        "scripts_with_identical_stdout": sum(c["stdout_identical"] for c in checks),
        "units_with_identical_artifacts": sum(unit["artifacts_identical"] for unit in units),
    }
    result = {"summary": summary, "units": units}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if summary["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

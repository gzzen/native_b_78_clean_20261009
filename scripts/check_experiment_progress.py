#!/usr/bin/env python3
# Path: scripts/check_experiment_progress.py
# Purpose: Read-only progress inspection of three-seed Native/B generation and matching controller processes.
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
PROGRESS = re.compile(r"^\[\d{2}:\d{2}:\d{2}Z\] \[(?:Native|B)[A-Za-z0-9._/:-]*\] "
    r"(?:starting total=\d+ workers=\d+|completed=\d+/\d+ passed=\d+ failed=\d+|"
    r"running elapsed=\d+s events=\d+ tools=\d+|tool=[a-z_]+ status=[a-z]+ elapsed=\d+s|"
    r"preparing workspace and isolated container|agent started timeout=\d+s|"
    r"assistant output received elapsed=\d+s|agent error received elapsed=\d+s; inspect saved logs|"
    r"agent finished (?:exit=-?\d+|timeout|policy interrupted) elapsed=[\d.]+s events=\d+; validating and saving logs|"
    r"attempt=\d+ status=(?:passed|failed|policy interrupted); logs saved|"
    r"resume reusing=\d+ remaining=\d+|generation already frozen; no agent calls made|"
    r"all (?:questions|capsules) finished; freezing artifacts and writing report)$")


def read_json(path: Path, warnings: list[str]):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warnings.append(f"{path.name}: {type(exc).__name__} (possibly being written)")
        return None


def runner_processes(root: Path) -> tuple[dict[str, list[int]], str | None]:
    """Inspect process metadata; never print argv, prompts, or credentials."""
    try:
        result = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                                text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return {}, "process inspection unavailable; running/stopped status is unknown"
    found: dict[str, list[int]] = {}
    for line in result.stdout.splitlines():
        try:
            pid_text, command = line.strip().split(None, 1)
            tokens = shlex.split(command)
            if not tokens or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(tokens[0]).name):
                continue
            if not any(token.endswith(("conditions/native/runner.py", "conditions/b/runner.py"))
                       and Path(token).name == "runner.py" for token in tokens):
                continue
            run_path = next((tokens[i + 1] for i, token in enumerate(tokens[:-1])
                             if token == "--run-root"), None)
            if run_path is None:
                run_path = next((token.split("=", 1)[1] for token in tokens
                                 if token.startswith("--run-root=")), None)
            if run_path is None:
                continue
            pid = int(pid_text)
            try:
                cwd = Path(f"/proc/{pid}/cwd").resolve(strict=True)
            except OSError:
                cwd = root
            resolved = (cwd / run_path).resolve()
            found.setdefault(str(resolved), []).append(pid)
        except (ValueError, IndexError):
            continue
    return found, None


def log_status(path: Path, now: float) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 262144))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
        progress = next((line for line in reversed(lines) if PROGRESS.fullmatch(line)), None)
        error = next((match.group(1) for line in reversed(lines)
                      if (match := re.match(r"^([A-Za-z_][\w.]*(?:Error|Exception)):", line))), None)
        return {"path": str(path), "age_seconds": max(0, int(now - path.stat().st_mtime)),
                "last_progress": progress, "exception_type": error}
    except OSError:
        return {"path": str(path), "warning": "log unavailable"}


def inspect_run(root: Path, experiment: str, condition: str, seed: str,
                processes: dict[str, list[int]], process_error: str | None, now: float) -> dict:
    # Support seed labels from the full-run command and rep labels from the docs.
    tags = [f"{experiment}_{condition}_{style}{seed}" for style in ("seed", "rep")]
    tag = next((tag for tag in tags if (root / "runs" / tag).exists()
                or (root / "analysis" / f"{tag}_manifest.json").exists()
                or (root / "analysis" / f"{tag}_generation.json").exists()), tags[0])
    run_root = root / "runs" / tag
    warnings: list[str] = []
    manifest_path = root / "analysis" / f"{tag}_manifest.json"
    manifest = read_json(manifest_path, warnings)
    expected = 78 if condition == "native" else 19
    if isinstance(manifest, dict):
        expected = manifest.get("question_count" if condition == "native" else "capsule_count", expected)
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        warnings.append("invalid manifest scope count; using the full experiment scope")
        expected = 78 if condition == "native" else 19
    generation_path = root / "analysis" / f"{tag}_generation.json"
    generation = read_json(generation_path, warnings)
    latest: dict[str, tuple[int, Path, dict | None]] = {}
    stages = [run_root, *sorted(run_root.glob("resume_*"))]
    workspaces = [workspace for stage in stages for workspace in sorted(stage.glob("[0-9][0-9][0-9][0-9]/*"))]
    for workspace in workspaces:
        if workspace.is_dir():
            latest[workspace.name] = (int(workspace.parent.name), workspace,
                                     read_json(workspace / "run_manifest.json", warnings))
    finished: dict[str, dict] = {}
    for name, (index, _, record) in latest.items():
        if isinstance(record, dict) and ("policy_violation" not in record
                or index >= 1 or "attempt_history" in record):
            finished[name] = record
    pids = processes.get(str(run_root.resolve()), [])
    freeze = generation.get("freeze", {}) if isinstance(generation, dict) else {}
    frozen = isinstance(freeze, dict) and freeze.get("status") == "frozen_before_private_scoring"
    if frozen:
        records = generation.get("records" if condition == "native" else "episodes", [])
        if isinstance(records, list) and all(isinstance(record, dict) for record in records):
            finished = {str(index): record for index, record in enumerate(records)}
        else:
            frozen = False
            warnings.append("frozen generation has invalid records")
    completed = len(finished)
    passed = sum(bool(record.get("accepted")) for record in finished.values())
    if frozen:
        state = "COMPLETE" if completed == expected else "SCOPE MISMATCH"
    elif pids:
        state = "RUNNING"
    elif latest or generation_path.exists():
        state = "INCOMPLETE" if process_error is None else "UNKNOWN"
    elif manifest_path.exists() or run_root.exists():
        state = "PREPARED" if process_error is None else "UNKNOWN"
    else:
        state = "NOT STARTED" if process_error is None else "UNKNOWN"
    report = root / "reports" / f"{tag}_GENERATION.md"
    export = root / "results" / experiment / f"{condition}_seed{seed}.jsonl"
    if not export.exists():
        export = root / "results" / experiment / f"{condition}_rep{seed}.jsonl"
    export_rows = None
    if export.is_file():
        try:
            with export.open(encoding="utf-8") as stream:
                export_rows = sum(bool(line.strip()) for line in stream)
        except OSError:
            warnings.append("prediction export could not be read")
    if frozen and not report.is_file():
        warnings.append("report missing; frozen generation can be recovered without model calls")
    if export_rows is not None and export_rows != 78:
        warnings.append(f"prediction export contains {export_rows} rows; full scope expects 78")
    log = root / "logs" / experiment / f"{condition}_seed{seed}.log"
    if not log.exists():
        log = root / "logs" / experiment / f"{condition}_rep{seed}.log"
    return {"condition": condition, "seed": seed, "tag": tag, "state": state,
            "unit": "questions" if condition == "native" else "capsules", "expected": expected,
            "completed": completed, "passed": passed, "failed": completed - passed,
            "unfinished_workspaces": [name for name in latest if name not in finished] if not frozen else [],
            "runner_pids": pids, "frozen": frozen, "generation": str(generation_path),
            "report_exists": report.is_file(), "export_rows": export_rows,
            "failure_status_counts": dict(Counter(str(record.get("controller_status", "failed"))
                for record in finished.values() if not record.get("accepted"))),
            "log": log_status(log, now), "warnings": warnings}


def latest_experiment(root: Path) -> str:
    candidates: dict[str, float] = {}
    for path in (root / "analysis").glob("*.json"):
        match = re.fullmatch(r"((?:deepseek_full|opencode_deepseek)_[A-Za-z0-9_.-]+)_(?:native|b)_(?:seed|rep)\d{2}_(?:manifest|generation)\.json", path.name)
        if match:
            candidates[match.group(1)] = max(candidates.get(match.group(1), 0), path.stat().st_mtime)
    if not candidates:
        raise ValueError("no DeepSeek experiment found; pass --experiment-id explicitly")
    return max(candidates, key=candidates.get)


def print_status(snapshot: dict, details: bool) -> None:
    print(f"\n{snapshot['checked_at_utc']}  {snapshot['experiment_id']}")
    print(f"{'Run':<15} {'State':<15} {'Completed':<20} {'Passed':>6} {'Failed':>6}  Runner PID  Export")
    for run in snapshot["runs"]:
        count = f"{run['completed']}/{run['expected']} {run['unit']}"
        pids = ",".join(map(str, run["runner_pids"])) or "—"
        exported = "missing" if run["export_rows"] is None else f"{run['export_rows']} rows"
        print(f"{run['condition'] + '_seed' + run['seed']:<15} {run['state']:<15} {count:<20} "
              f"{run['passed']:>6} {run['failed']:>6}  {pids:<10}  {exported}")
    if snapshot["process_warning"]:
        print("Warning: " + snapshot["process_warning"])
    print("B counts are capsules covering 78 questions; passed/failed are controller outcomes.")
    print("INCOMPLETE means artifacts exist and no matching controller was found at this check.")
    for run in snapshot["runs"]:
        if details or run["warnings"]:
            print(f"\n{run['tag']}:")
            for warning in run["warnings"]:
                print("  " + warning)
            if details:
                print("  Frozen: " + str(run["frozen"]) + "; report: " + ("present" if run["report_exists"] else "missing"))
                print("  Generation: " + run["generation"])
                if run["unfinished_workspaces"]:
                    print("  Unfinished workspaces: " + ", ".join(run["unfinished_workspaces"]))
                if run["failure_status_counts"]:
                    print("  Failure statuses: " + json.dumps(run["failure_status_counts"], sort_keys=True))
                log = run["log"]
                if log:
                    print(f"  Log: {log['path']} (updated {log.get('age_seconds', '?')}s ago)")
                    if log.get("last_progress"):
                        print("  " + log["last_progress"])
                    if log.get("exception_type"):
                        print("  Last logged exception type: " + log["exception_type"])
    print(flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check experiment progress without starting or changing any run.")
    parser.add_argument("--experiment-id", help="default: most recently modified DeepSeek experiment")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--details", action="store_true")
    parser.add_argument("--json", action="store_true", help="print one machine-readable snapshot")
    parser.add_argument("--watch", type=float, metavar="SECONDS", help="repeat status at this interval")
    args = parser.parse_args()
    if args.watch is not None and (args.watch < 1 or args.json):
        parser.error("--watch must be at least 1 second and cannot be combined with --json")
    root = args.root.resolve()
    try:
        experiment = args.experiment_id or latest_experiment(root)
    except ValueError as exc:
        parser.error(str(exc))
    if not SAFE_ID.fullmatch(experiment):
        parser.error("experiment ID must contain only letters, numbers, dots, underscores and hyphens")
    try:
        while True:
            processes, process_error = runner_processes(root)
            now = time.time()
            snapshot = {"experiment_id": experiment, "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                        "process_warning": process_error,
                        "runs": [inspect_run(root, experiment, condition, seed, processes, process_error, now)
                                 for seed in ("01", "02", "03") for condition in ("native", "b")]}
            if args.json:
                print(json.dumps(snapshot, indent=2))
            else:
                print_status(snapshot, args.details)
            if args.watch is None:
                break
            time.sleep(args.watch)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

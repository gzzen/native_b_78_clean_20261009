# Path: common/benchmark/a84_contract.py
# Purpose: Byte-preserving public benchmark/environment contract required by the migrated A84 runner.
"""Standalone answer-blind benchmark and execution substrate for Native/B."""
from __future__ import annotations

import hashlib
import errno
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence
from common.integrity.opencode_capture import DEFAULT_MODEL, DEFAULT_REASONING_EFFORT

ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = ROOT
BUNDLE_ROOT = ROOT.parent
DATA_ROOT = BUNDLE_ROOT / "data"
QUESTIONS = DATA_ROOT / "public/questions.jsonl"
# The sibling data directory carries exactly the current 19-capsule / 78-question scope.
CAPSULES = DATA_ROOT / "capsules"
NATIVE_PROMPT = ROOT / "conditions/a/prompt.md"
NATIVE_SCHEMA = ROOT / "common/schemas/capsule_result.schema.json"
NATIVE_BASH_ENV = ROOT / "conditions/a/agent_bix_bash_env.sh"
AGENT_BIN = Path(os.environ.get("AGENT_BIX_BIN", str(Path(sys.executable).resolve().parent)))
MODEL = DEFAULT_MODEL
REASONING_EFFORT = DEFAULT_REASONING_EFFORT
TIMEOUT_SECONDS = 3600
PUBLIC_FIELDS = ("id", "tag", "version", "question", "capsule_uuid", "short_id", "question_id",
                 "categories", "paper", "data_folder", "eval_mode")
WORKSPACE_TEMP_ENV_KEYS = (
    "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "MPLCONFIGDIR",
    "NUMBA_CACHE_DIR", "JOBLIB_TEMP_FOLDER",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip()]


def public_question(row: Mapping[str, Any]) -> dict[str, Any]:
    """Exact public question serialization used by the frozen capsule native generator."""
    return {field: row[field] for field in PUBLIC_FIELDS if field in row}


def rows_for_capsule(short_id: str, capsule_uuid: str) -> list[dict[str, Any]]:
    rows = [public_question(row) for row in load_rows()
            if row["short_id"] == short_id and row["capsule_uuid"] == capsule_uuid]
    if not rows:
        raise ValueError(f"unknown frozen capsule: {short_id} {capsule_uuid}")
    if len({row["capsule_uuid"] for row in rows}) != 1:
        raise ValueError(f"capsule selection is not unique: {short_id} {capsule_uuid}")
    return rows


def workspace_temp_environment(workspace: Path, *, create: bool = True) -> dict[str, str]:
    """Return process-local temp/cache paths rooted in exactly one workspace."""
    root = workspace.resolve()
    temp_root = (root / "tmp").resolve()
    if not temp_root.is_relative_to(root):
        raise RuntimeError(f"workspace temp escaped its workspace: {temp_root}")
    paths = {
        "TMPDIR": temp_root,
        "TMP": temp_root,
        "TEMP": temp_root,
        "XDG_CACHE_HOME": temp_root / "xdg-cache",
        "MPLCONFIGDIR": temp_root / "matplotlib",
        "NUMBA_CACHE_DIR": temp_root / "numba",
        "JOBLIB_TEMP_FOLDER": temp_root / "joblib",
    }
    if create:
        temp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp_root.chmod(0o700)
        for path in set(paths.values()) - {temp_root}:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
    return {key: str(value) for key, value in paths.items()}


def controlled_env(workspace: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "PATH": f"{AGENT_BIN}:{env.get('PATH', '')}", "CONDA_PREFIX": str(AGENT_BIN.parent),
        "BASH_ENV": str(NATIVE_BASH_ENV), "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1",
        "PYTHONHASHSEED": "0",
    })
    if workspace is not None:
        env.update(workspace_temp_environment(workspace))
    return env


def environment_manifest() -> dict[str, Any]:
    env = controlled_env()
    selected = {key: env[key] for key in ("PATH", "CONDA_PREFIX", "BASH_ENV", "OMP_NUM_THREADS",
               "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
               "VECLIB_MAXIMUM_THREADS", "PYTHONHASHSEED")}
    selected.update({"model": MODEL, "reasoning_effort": REASONING_EFFORT,
                     "timeout_seconds": TIMEOUT_SECONDS, "agent": "opencode-v2", "sandbox": "per-attempt Docker hard boundary",
                     "resource_access_policy": "network-none; host gateway scientific exact-host allowlist",
                     "expected_python": str((AGENT_BIN / "python").resolve()),
                     "bash_env": str(NATIVE_BASH_ENV.resolve()), "status": "parity_contract"})
    canonical = json.dumps(selected, sort_keys=True, separators=(",", ":"))
    return {"values": selected, "sha256": hashlib.sha256(canonical.encode()).hexdigest()}


def data_file_hashes(capsule_uuid: str) -> dict[str, str]:
    source = CAPSULES / f"CapsuleFolder-{capsule_uuid}"
    if not source.is_dir():
        raise FileNotFoundError(source)
    return {str(path.relative_to(source)): sha256_file(path) for path in sorted(source.rglob("*")) if path.is_file()}


def hardlink_or_copy(source: str, destination: str) -> str:
    """Hardlink capsule bytes when possible; copy across isolated mounts."""
    try:
        os.link(source, destination)
        return destination
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        return shutil.copy2(source, destination)


def materialize_common_native_inputs(workspace: Path, rows: Sequence[Mapping[str, Any]], environment: Mapping[str, Any]) -> None:
    """Create the native-visible files identically in both arms; data stays one official symlink."""
    workspace.mkdir(parents=True, exist_ok=False)
    temp_environment = workspace_temp_environment(workspace)
    write_json(workspace / "questions.json", [public_question(row) for row in rows])
    environment_contract = dict(environment)
    environment_contract.update(temp_environment)
    environment_contract["temporary_directory_policy"] = {
        "scope": "one private workspace-local temp tree per capsule attempt",
        "required_usage": "use TMPDIR/TMP/TEMP; never use shared /tmp or /var/tmp",
        "environment_keys": list(WORKSPACE_TEMP_ENV_KEYS),
    }
    write_json(workspace / "environment_contract.json", environment_contract)
    source = CAPSULES / f"CapsuleFolder-{rows[0]['capsule_uuid']}"
    if os.environ.get("NATIVE_B_EXTERNAL_CONTAINER_ISOLATION") == "1":
        # The Docker runner mounts the canonical data tree read-only and does
        # not mount private/.  A symlink avoids cross-mount hardlink failures
        # and prevents Native's question-level workspaces from duplicating the
        # same multi-gigabyte capsule once per question.
        os.symlink(source, workspace / "data", target_is_directory=True)
        return
    # Hard links are ordinary discoverable files in a capsule-local directory,
    # while all official bytes remain owned by the one common canonical store.
    shutil.copytree(source, workspace / "data", copy_function=hardlink_or_copy)


def native_solver_object(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [public_question(row) for row in rows]


def a_solver_object(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Extract only the public question object from A's additional episode envelope."""
    return [public_question(row) for row in rows]


def candidate_policy() -> dict[str, Any]:
    return {"maximum_ranked_candidates": 3, "official_open_loop_rank": 1,
            "candidate_2_3_usage": "not used for rescue, selection, post-hoc scoring, or independent heads"}

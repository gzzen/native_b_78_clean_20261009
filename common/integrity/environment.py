# Path: common/integrity/environment.py
# Purpose: Fixed answer-blind execution environment contract for all conditions.
from __future__ import annotations
import hashlib, json, os, sys
from pathlib import Path
from typing import Any
from common.integrity.opencode_capture import DEFAULT_MODEL, DEFAULT_REASONING_EFFORT

AGENT_BIN = Path(os.environ.get("AGENT_BIX_BIN", str(Path(sys.executable).resolve().parent)))
MODEL = DEFAULT_MODEL
REASONING_EFFORT = DEFAULT_REASONING_EFFORT
TIMEOUT_SECONDS = 3600

def controlled_env(bash_env: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"PATH": f"{AGENT_BIN}:{env.get('PATH', '')}", "CONDA_PREFIX": str(AGENT_BIN.parent), "BASH_ENV": str(bash_env), "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1", "PYTHONHASHSEED": "0"})
    return env

def environment_manifest(bash_env: Path, timeout: int = TIMEOUT_SECONDS) -> dict[str, Any]:
    env = controlled_env(bash_env)
    values = {key: env[key] for key in ("PATH", "CONDA_PREFIX", "BASH_ENV", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "PYTHONHASHSEED")}
    values.update({"model": MODEL, "reasoning_effort": REASONING_EFFORT, "timeout_seconds": timeout, "agent": "opencode-v2", "sandbox": "per-attempt Docker hard boundary", "expected_python": str((AGENT_BIN / "python").resolve())})
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return {"values": values, "sha256": hashlib.sha256(canonical.encode()).hexdigest()}

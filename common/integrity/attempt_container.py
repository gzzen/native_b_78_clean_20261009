# Path: common/integrity/attempt_container.py
# Purpose: Isolate each Codex question/capsule attempt behind a host-only gateway.
"""One hard Docker boundary per Native question / B capsule attempt.

The host's agent_bix environment and the pinned Codex binaries are mounted
read-only. Nothing from the host user's Codex setup (~/.codex, PATH, auth) is
mounted or inherited: each attempt gets a fresh CODEX_HOME.
"""
from __future__ import annotations
import contextlib
import functools
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from common.integrity.attempt_gateway import AttemptGateway, GatewayConfig
from common.integrity.codex_agent import (AGENT_LABEL, CODEX_LOCK, CODEX_VERSION, DEFAULT_REASONING_EFFORT,
                                          codex_config, model_catalog, model_route)

ROOT = Path(__file__).resolve().parents[2]
RELAY = Path(__file__).with_name("attempt_relay.py")
# Codex starts its code-mode host from the directory of its own executable,
# so both binaries are mounted side by side here.
CONTAINER_CODEX_DIR = Path("/opt/codex/bin")


def validate_workspace_outputs(workspace):
    """Reject output symlinks that could redirect host audit reads/writes.

    Runtime temp/home contents are never consumed by host audits; Codex keeps its
    state and helper links there. Audit/output trees must stay internal.
    """
    root = workspace.resolve()
    def fail_walk(error):
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail_walk):
        base = Path(directory)
        for name in dirs + files:
            path = base / name
            if path.is_symlink():
                target = path.resolve()
                if not target.is_relative_to(root) or any(target.is_relative_to(root / name) for name in ("tmp", "home")):
                    raise RuntimeError("unsafe output symlink outside audited attempt tree: " + str(path.relative_to(root)))
        if base == root:
            for name in ("tmp", "home"):
                if name in dirs:
                    dirs.remove(name)


def boundary_manifest():
    return {"mechanism": "per_attempt_docker_network_none_with_host_unix_gateway",
            "mounts": "current_workspace_rw_current_capsule_data_ro_environment_codex_schema_config_ro",
            "model_credentials": "host_gateway_only_short_lived_attempt_token_in_container",
            "network": "none; scientific exact-host allowlist through gateway",
            "siblings_history_private_home_docker_socket": "not_mounted",
            "host_user_codex_home": "never_mounted; fresh per-attempt CODEX_HOME",
            "fail_closed": True,
            "controller_code_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                       for name in ("attempt_container.py", "attempt_gateway.py", "attempt_relay.py", "codex_agent.py")}}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@functools.lru_cache(maxsize=4)
def _verified_sha256(path: Path, size: int, mtime_ns: int) -> str:
    return sha256_file(path)


def codex_binaries() -> dict[str, Path]:
    """The pinned Linux binaries installed offline by scripts/install_codex.sh.

    Each must match the binary sha256 in codex_lock.json, however it was copied here.
    """
    default = ROOT / "tools/codex" / CODEX_VERSION
    paths = {"codex": os.environ.get("NATIVE_B_CODEX_BINARY") or default / "codex",
             "codex-code-mode-host": os.environ.get("NATIVE_B_CODEX_CODE_MODE_HOST") or default / "codex-code-mode-host"}
    binaries = {name: Path(path).resolve() for name, path in paths.items()}
    missing = [name for name, path in binaries.items() if not path.is_file()]
    if missing:
        raise RuntimeError(f"missing pinned Codex binaries {missing}; install the offline bundle "
                           "with scripts/install_codex.sh (see CODEX.md)")
    pinned = json.loads(CODEX_LOCK.read_text(encoding="utf-8"))["binaries"]
    for name, path in binaries.items():
        stat = path.stat()
        if _verified_sha256(path, stat.st_size, stat.st_mtime_ns) != pinned[name]["binary_sha256"]:
            raise RuntimeError(f"{path} does not match the {name} sha256 pinned in {CODEX_LOCK.name}")
    return binaries


@functools.lru_cache(maxsize=2)
def codex_metadata(codex: Path) -> dict:
    """Version and bundled model catalog, read from the pinned binary with an empty CODEX_HOME."""
    with tempfile.TemporaryDirectory(prefix="native-b-codex-home-") as home:
        env = {"HOME": home, "CODEX_HOME": home, "PATH": "/usr/bin:/bin"}
        version = subprocess.run([str(codex), "--version"], capture_output=True, text=True,
                                 timeout=60, check=True, env=env).stdout.strip()
        catalog = subprocess.run([str(codex), "debug", "models", "--bundled"], capture_output=True, text=True,
                                 timeout=120, check=True, env=env).stdout
    if version != f"codex-cli {CODEX_VERSION}":
        raise RuntimeError(f"Codex binary reports {version!r}; {CODEX_LOCK.name} pins {CODEX_VERSION}")
    return {"version": version, "bundled_catalog": json.loads(catalog),
            "bundled_catalog_sha256": hashlib.sha256(catalog.encode()).hexdigest()}


def write_codex_home(workspace: Path, model: str, reasoning_effort: str, bundled_catalog: dict) -> tuple[Path, Path, str]:
    """Fresh CODEX_HOME holding only the generated config and one-model catalog."""
    # Resolved, because the container mounts the resolved workspace path.
    codex_home = workspace.resolve() / "home/.codex"
    codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    catalog_path = codex_home / "model_catalog.json"
    catalog_path.write_text(json.dumps(model_catalog(bundled_catalog, model, reasoning_effort), indent=2) + "\n")
    config = codex_config(model, reasoning_effort, str(catalog_path))
    config_path = codex_home / "config.toml"
    config_path.write_text(config)
    return config_path, catalog_path, config


def docker_command(command, environment, workspace, socket_dir, name, token, binaries, image,
                   *, schema, config_path, catalog_path):
    """Construct allowlisted mounts/env. Never inherit the controller environment."""
    workspace = workspace.resolve()
    prefix = Path(environment["CONDA_PREFIX"]).resolve()
    if not prefix.is_dir() or prefix in (Path("/"), Path.home()):
        raise RuntimeError("invalid isolated environment prefix")
    schema = Path(schema).resolve()
    bash_env = Path(environment["BASH_ENV"]).resolve()
    config_path, catalog_path = Path(config_path).resolve(), Path(catalog_path).resolve()
    if not schema.is_file() or not bash_env.is_file() or not all(path.is_file() for path in binaries.values()):
        raise RuntimeError("missing isolated runtime/schema/tool input")
    home = workspace / "home"
    argv = list(command)
    argv[0] = str(CONTAINER_CODEX_DIR / "codex")
    env = {key: value for key, value in environment.items() if key in {
        "CONDA_PREFIX", "BASH_ENV", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "MPLCONFIGDIR",
        "NUMBA_CACHE_DIR", "JOBLIB_TEMP_FOLDER", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "PYTHONHASHSEED"}}
    env.update(HOME=str(home), CODEX_HOME=str(home / ".codex"), PWD=str(workspace),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local/share"), XDG_STATE_HOME=str(home / ".local/state"),
        NATIVE_B_GATEWAY_TOKEN=token,
        GENERATION_EVALUATOR_ACCESS="forbidden", HTTP_PROXY="http://127.0.0.1:8765",
        HTTPS_PROXY="http://127.0.0.1:8765", http_proxy="http://127.0.0.1:8765",
        https_proxy="http://127.0.0.1:8765", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
        PATH=f"{prefix}/bin:{CONTAINER_CODEX_DIR}:/usr/local/bin:/usr/bin:/bin")
    result = ["docker", "run", "--rm", "--name", name, "--runtime", "runc", "--network", "none",
        "--user", f"{os.getuid()}:{os.getgid()}", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "512",
        "--cpus", "1", "--memory", os.environ.get("NATIVE_B_ATTEMPT_MEMORY", "16g"),
        "--memory-swap", os.environ.get("NATIVE_B_ATTEMPT_MEMORY", "16g"),
        "--tmpfs", "/tmp:ro,noexec,nosuid,size=1m", "--tmpfs", "/var/tmp:ro,noexec,nosuid,size=1m",
        "--workdir", str(workspace)]
    mounts = [(workspace, workspace, False), (workspace / "data", workspace / "data", True),
        (prefix, prefix, True), (schema, schema, True),
        (bash_env, bash_env, True), (socket_dir, Path("/boundary/socket"), True),
        (RELAY, Path("/boundary/relay.py"), True),
        (workspace / "isolation_boundary.json", workspace / "isolation_boundary.json", True),
        (config_path, config_path, True), (catalog_path, catalog_path, True),
        *[(path, CONTAINER_CODEX_DIR / name, True) for name, path in binaries.items()]]
    for source, destination, readonly in mounts:
        source = source.resolve()
        if not source.exists():
            raise RuntimeError(f"missing isolated input: {source}")
        result += ["--mount", f"type=bind,src={source},dst={destination}" + (",readonly" if readonly else "")]
    for key, value in env.items():
        result += ["--env", f"{key}={value}"]
    return result + [image, str(prefix / "bin/python"), "/boundary/relay.py", *argv]


@contextlib.contextmanager
def isolated_attempt(command, environment, workspace, *, schema,
                     reasoning_effort=DEFAULT_REASONING_EFFORT):
    if os.environ.get("NATIVE_B_EXTERNAL_CONTAINER_ISOLATION"):
        raise RuntimeError("legacy whole-solver Docker mode is disabled; run controller on host")
    if os.getuid() == 0:
        raise RuntimeError("run the controller as a non-root user")
    if not shutil.which("docker"):
        raise RuntimeError("Docker is required for per-attempt isolation")
    if not (Path(environment["CONDA_PREFIX"]) / "conda-meta").is_dir():
        raise RuntimeError("activate the dedicated agent_bix Conda environment before generation")
    if sys.platform != "linux":
        raise RuntimeError("generation requires a Linux host and a Linux agent_bix environment")
    binaries = codex_binaries()
    metadata = codex_metadata(binaries["codex"])
    # codex_command passes the bare model ID; the relay is the only provider.
    model = "openai-relay/" + command[command.index("--model") + 1]
    provider, identifier = model_route(model)
    workspace = Path(workspace)
    config_path, catalog_path, config_text = write_codex_home(
        workspace, model, reasoning_effort, metadata["bundled_catalog"])
    token = secrets.token_urlsafe(32)
    config = GatewayConfig.from_host(token, identifier, provider)
    name = "native-b-attempt-" + secrets.token_hex(10)
    image = os.environ.get("NATIVE_B_DOCKER_IMAGE", "nvidia/cuda:11.8.0-base-ubuntu22.04")
    inspected = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                               capture_output=True, text=True, timeout=15, check=True)
    image_id = inspected.stdout.strip()
    if not image_id.startswith("sha256:"):
        raise RuntimeError("cannot resolve a locally installed image digest")
    with tempfile.TemporaryDirectory(prefix="native-b-gateway-") as directory:
        gateway = AttemptGateway(Path(directory) / "gateway.sock", config)
        gateway.start()
        try:
            manifest = {**boundary_manifest(), "image_id": image_id,
                        "science_hosts": list(config.science_hosts), "memory": os.environ.get("NATIVE_B_ATTEMPT_MEMORY", "16g"),
                        "agent": AGENT_LABEL, "model": model, "reasoning_effort": reasoning_effort,
                        "codex_version": metadata["version"],
                        "codex_binary_sha256": {name: _verified_sha256(path, path.stat().st_size, path.stat().st_mtime_ns)
                                                for name, path in binaries.items()},
                        "codex_config": config_text,
                        "codex_bundled_catalog_sha256": metadata["bundled_catalog_sha256"],
                        "codex_model_catalog_sha256": sha256_file(catalog_path)}
            (workspace / "isolation_boundary.json").write_text(json.dumps(manifest, indent=2) + "\n")
            argv = docker_command(command, environment, workspace, Path(directory), name, token, binaries, image_id,
                                  schema=schema, config_path=config_path, catalog_path=catalog_path)
            yield argv, {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())}
        finally:
            try:
                # Killing the docker client alone does not kill its container.
                subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30, check=False)
                remaining = subprocess.run(["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
                                           capture_output=True, text=True, timeout=15, check=True)
                if remaining.stdout.strip():
                    raise RuntimeError("attempt container cleanup failed; stop generation")
            finally:
                gateway.close()
            validate_workspace_outputs(workspace)

# Path: common/integrity/attempt_container.py
# Purpose: Isolate each OpenCode question/capsule attempt behind a host-only gateway.
"""One hard Docker boundary per Native question / B capsule attempt."""
from __future__ import annotations
import contextlib
import json
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from common.integrity.attempt_gateway import AttemptGateway, GatewayConfig
from common.integrity.opencode_capture import DEFAULT_REASONING_EFFORT, model_route, provider_config

RELAY = Path(__file__).with_name("attempt_relay.py")


def validate_workspace_outputs(workspace):
    """Reject output symlinks that could redirect host audit reads/writes.

    Runtime temp/home contents are never consumed by host audits; OpenCode creates
    container-local helper links there. Audit/output trees must stay internal.
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
            "mounts": "current_workspace_rw_current_capsule_data_ro_environment_tools_schema_ro",
            "model_credentials": "host_gateway_only_short_lived_attempt_token_in_container",
            "network": "none; scientific exact-host allowlist through gateway",
            "siblings_history_private_home_docker_socket": "not_mounted", "fail_closed": True,
            "controller_code_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                       for name in ("attempt_container.py", "attempt_gateway.py", "attempt_relay.py", "opencode_capture.py")}}


def docker_command(command, environment, workspace, socket_dir, name, token, binary, image,
                   *, schema, reasoning_effort=DEFAULT_REASONING_EFFORT):
    """Construct allowlisted mounts/env. Never inherit the controller environment."""
    workspace = workspace.resolve()
    prefix = Path(environment["CONDA_PREFIX"]).resolve()
    if not prefix.is_dir() or prefix in (Path("/"), Path.home()):
        raise RuntimeError("invalid isolated environment prefix")
    schema = Path(schema).resolve()
    bash_env = Path(environment["BASH_ENV"]).resolve()
    if not schema.is_file() or not bash_env.is_file() or not binary.is_file():
        raise RuntimeError("missing isolated runtime/schema/tool input")
    home = workspace / "home"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_dir = home / ".config/opencode"
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = config_dir / "opencode.json"
    config_path.write_text(json.dumps(provider_config(
        command[command.index("--model") + 1], reasoning_effort), indent=2) + "\n")
    argv = list(command)
    argv[0] = "/opt/opencode/opencode"
    env = {key: value for key, value in environment.items() if key in {
        "CONDA_PREFIX", "BASH_ENV", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "MPLCONFIGDIR",
        "NUMBA_CACHE_DIR", "JOBLIB_TEMP_FOLDER", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "PYTHONHASHSEED"}}
    env.update(HOME=str(home), PWD=str(workspace), XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local/share"), XDG_STATE_HOME=str(home / ".local/state"),
        NATIVE_B_GATEWAY_TOKEN=token,
        GENERATION_EVALUATOR_ACCESS="forbidden", HTTP_PROXY="http://127.0.0.1:8765",
        HTTPS_PROXY="http://127.0.0.1:8765", http_proxy="http://127.0.0.1:8765",
        https_proxy="http://127.0.0.1:8765", NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
        PATH=f"{prefix}/bin:/opt/opencode:/usr/local/bin:/usr/bin:/bin")
    result = ["docker", "run", "--rm", "--name", name, "--runtime", "runc", "--network", "none",
        "--user", f"{os.getuid()}:{os.getgid()}", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--pids-limit", "512",
        "--cpus", "1", "--memory", os.environ.get("NATIVE_B_ATTEMPT_MEMORY", "16g"),
        "--memory-swap", os.environ.get("NATIVE_B_ATTEMPT_MEMORY", "16g"),
        "--tmpfs", "/tmp:ro,noexec,nosuid,size=1m", "--tmpfs", "/var/tmp:ro,noexec,nosuid,size=1m",
        "--workdir", str(workspace)]
    mounts = [(workspace, workspace, False), (workspace / "data", workspace / "data", True),
        (prefix, prefix, True), (binary, Path("/opt/opencode/opencode"), True), (schema, schema, True),
        (bash_env, bash_env, True), (socket_dir, Path("/boundary/socket"), True),
        (RELAY, Path("/boundary/relay.py"), True),
        (workspace / "isolation_boundary.json", workspace / "isolation_boundary.json", True),
        (config_path, config_path, True)]
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
    executable = os.environ.get("NATIVE_B_OPENCODE_BINARY") or shutil.which("opencode")
    if not executable or not Path(executable).is_file():
        raise RuntimeError("install OpenCode v2 or set NATIVE_B_OPENCODE_BINARY to its Linux executable")
    binary = Path(executable).resolve()
    token = secrets.token_urlsafe(32)
    model = command[command.index("--model") + 1]
    provider, identifier = model_route(model)
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
                        "agent": "opencode-v2", "model": model, "reasoning_effort": reasoning_effort,
                        "opencode_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                        "opencode_config": provider_config(model, reasoning_effort)}
            (workspace / "isolation_boundary.json").write_text(json.dumps(manifest, indent=2) + "\n")
            argv = docker_command(command, environment, workspace, Path(directory), name, token, binary, image_id,
                                  schema=schema, reasoning_effort=reasoning_effort)
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

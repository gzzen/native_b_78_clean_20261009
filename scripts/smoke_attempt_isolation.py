#!/usr/bin/env python3
# Path: scripts/smoke_attempt_isolation.py
# Purpose: Verify isolated Codex transport, hermeticity and science access using mock model responses.
"""No paid model calls: exercise real Docker mounts/network/cleanup for both arms."""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.benchmark.a84_contract import controlled_env
from common.integrity.attempt_container import isolated_attempt
from common.integrity.attempt_gateway import AttemptGateway, GatewayConfig
from common.integrity.codex_agent import CODEX_VERSION, DEFAULT_MODEL, capture_agent, codex_command, write_result
from common.integrity.evaluator_access_policy import generation_event_guard

# Planted on the controller host; none of these may reach the attempt or the model.
PLANTED_HOST_KEY = "planted-host-openai-key"
WORKSPACE_SENTINEL = "PLANTED-AGENTS-MD-SENTINEL"

PROBE = r'''
import json,os,socket,subprocess,urllib.request
from pathlib import Path
workspace=Path.cwd()
checks={}
for name,path in json.loads(os.environ['PROBE_FORBIDDEN_PATHS']).items():
    checks['cannot_see_'+name]=not Path(path).exists()
checks['current_data_readable']=(workspace/'data/input.txt').read_text()=='public-input'
try:
    (workspace/'data/input.txt').write_text('mutation')
    checks['data_readonly']=False
except OSError:
    checks['data_readonly']=True
try:
    Path('/tmp/leak').write_text('x')
    checks['shared_tmp_readonly']=False
except OSError:
    checks['shared_tmp_readonly']=True
(Path(os.environ['TMPDIR'])/'probe.txt').write_text('local')
checks['private_tmp_writable']=True
try:
    (workspace/'isolation_boundary.json').write_text('{}')
    checks['boundary_record_readonly']=False
except OSError:
    checks['boundary_record_readonly']=True
codex_home=Path(os.environ['CODEX_HOME'])
checks['codex_home_is_per_attempt']=codex_home.is_relative_to(workspace)
checks['codex_home_has_only_generated_files']=sorted(p.name for p in codex_home.iterdir())==['config.toml','model_catalog.json']
try:
    (codex_home/'config.toml').write_text('model = "other"\n')
    checks['codex_config_readonly']=False
except OSError:
    checks['codex_config_readonly']=True
checks['no_codex_auth_file']=not (codex_home/'auth.json').exists()
checks['no_model_or_evaluator_secret_env']=not any(
    k.startswith(('OPENAI_','DEEPSEEK_','ANTHROPIC_','EVALUATOR_API_')) or k=='CODEX_API_KEY' for k in os.environ)
evaluator_port=int(os.environ['PROBE_EVALUATOR_PORT'])
for label,host,port in [('direct_public_network','1.1.1.1',443),('host_service','172.17.0.1',evaluator_port),
('loopback_service','127.0.0.1',evaluator_port)]:
    try:
        socket.create_connection((host,port),timeout=2).close()
        checks[label+'_blocked']=False
    except OSError:
        checks[label+'_blocked']=True
import http.client
for label,target in [('unknown_endpoint','http://example.com/'),('private_endpoint','http://127.0.0.1:54321/')]:
    conn=http.client.HTTPConnection('127.0.0.1',8765,timeout=5)
    conn.request('GET',target)
    checks[label+'_denied_by_gateway']=conn.getresponse().status==403
    conn.close()
conn=http.client.HTTPConnection('127.0.0.1',8765,timeout=5)
conn.request('POST','/model/responses',body='{"model":"unused"}')
checks['model_without_token_denied']=conn.getresponse().status==403
conn.close()
checks['pinned_codex_executable']=os.environ['PROBE_CODEX_VERSION'] in subprocess.check_output(['/opt/codex/bin/codex','--version'],text=True)
checks['code_mode_host_present']=Path('/opt/codex/bin/codex-code-mode-host').is_file()
checks['python_is_mounted_agent_bix']=subprocess.check_output(['python','-c','import sys;print(sys.prefix)'],text=True).strip()==os.environ['CONDA_PREFIX']
if os.environ.get('PROBE_SCIENCE')=='1':
    with urllib.request.urlopen('https://mygene.info/v3/gene/1017?fields=symbol',timeout=40) as response:
        checks['allowlisted_science_works']=json.load(response).get('symbol')=='CDK2'
print(json.dumps(checks),flush=True)
assert all(checks.values()),checks
'''


def mock_model_handler(requests):
    """Mock Responses upstream; records what Codex sent for the hermeticity assertions."""
    def build(gateway):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                requests.append({"method": "GET", "path": self.path})
                self.send_error(404)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                requests.append({"method": "POST", "path": self.path, "authorization": self.headers.get("Authorization"),
                                 "encoding": self.headers.get("Content-Encoding"), "body": raw.decode(errors="replace")})
                if self.path != "/model/responses" or self.headers.get("Authorization") != "Bearer " + gateway.config.token:
                    self.send_error(403); return
                body = json.loads(raw)
                assert body["model"] == gateway.config.model
                text = '{"status":"unresolved","candidates":[],"summary":"Mock transport only."}'
                item = {"type": "message", "id": "msg_mock", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}
                usage = {"input_tokens": 1, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 1,
                         "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 2}
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                for kind, data in (("response.created", {"response": {"id": "resp_mock"}}),
                                   ("response.output_item.done", {"output_index": 0, "item": item}),
                                   ("response.completed", {"response": {"id": "resp_mock", "usage": usage}})):
                    self.wfile.write(f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n".encode())
                    self.wfile.flush()
                self.close_connection = True
        return Handler
    return build


def probe_argv(argv: list[str], program: str, extra_env: dict[str, str]) -> list[str]:
    """Replace the agent command after the relay and add probe-only environment."""
    relay = argv.index("/boundary/relay.py")
    argv = argv[:relay + 1] + [sys.executable, "-c", program]
    image_pos = relay - 2
    for key, value in extra_env.items():
        argv[image_pos:image_pos] = ["--env", f"{key}={value}"]
    return argv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--science", action="store_true", help="also test the public mygene endpoint")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    class MockEvaluator(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'no-real-answers-test-only')
    evaluator = ThreadingHTTPServer(("0.0.0.0", 0), MockEvaluator)
    threading.Thread(target=evaluator.serve_forever, daemon=True).start()
    os.environ["OPENAI_API_KEY"] = PLANTED_HOST_KEY
    runs = ROOT / "runs"
    runs.mkdir(exist_ok=True)
    reports = []
    with tempfile.TemporaryDirectory(prefix="security-probe-", dir=runs) as directory:
        root = Path(directory)
        hidden = root / "private/answer_sentinel.txt"
        hidden.parent.mkdir(); hidden.write_text("host-only-sentinel")
        history = root / "old/predictions.json"
        history.parent.mkdir(); history.write_text("host-only-history")
        (root / "AGENTS.md").write_text(WORKSPACE_SENTINEL + " parent\n")
        for condition in ("native", "b"):
            workspace = root / condition
            (workspace / "data").mkdir(parents=True)
            (workspace / "data/input.txt").write_text("public-input")
            (workspace / "AGENTS.md").write_text(WORKSPACE_SENTINEL + " workspace\n")
            environment = controlled_env(workspace)
            schema = ROOT / "conditions/native/question_result_no_id.schema.json"
            command = codex_command("No model invocation", schema, DEFAULT_MODEL)
            forbidden = {"private": str(hidden), "history": str(history),
                         "host_codex_home": str(Path.home() / ".codex"),
                         "solver_runs": str(ROOT / "runs/20260924_native_b_3seeds"),
                         "docker_socket": "/var/run/docker.sock"}
            probe_env = {"PROBE_FORBIDDEN_PATHS": json.dumps(forbidden), "PROBE_SCIENCE": str(int(args.science)),
                         "PROBE_EVALUATOR_PORT": str(evaluator.server_port), "PROBE_CODEX_VERSION": CODEX_VERSION}
            with isolated_attempt(command, environment, workspace, schema=schema) as (argv, env):
                result = subprocess.run(probe_argv(argv, PROBE, probe_env), env=env, capture_output=True,
                                        text=True, timeout=180)
                if result.returncode:
                    raise RuntimeError(f"{condition} probe failed: {result.stdout} {result.stderr}")
                checks = json.loads(result.stdout.strip().splitlines()[-1])
                boundary = json.loads((workspace / "isolation_boundary.json").read_text())
                reports.append({"condition": condition, "status": "pass", "checks": checks,
                                "image_id": boundary["image_id"], "codex_binary_sha256": boundary["codex_binary_sha256"]})
            # A command-event violation must kill the attempt container, not only its docker client.
            with isolated_attempt(command, environment, workspace, schema=schema) as (argv, env):
                name = argv[argv.index("--name") + 1]
                event = {"type": "item.started", "item": {"type": "command_execution", "command": "cat ../old/predictions.json"}}
                program = "import json,time; print(json.dumps(" + repr(event) + "),flush=True); time.sleep(30)"
                code, records, _, _ = capture_agent(probe_argv(argv, program, {}), env, timeout=90,
                                                    event_guard=generation_event_guard(workspace))
                assert code == 86, code
            remaining = subprocess.run(["docker", "ps", "-a", "--filter", f"name=^{name}", "--format", "{{.Names}}"],
                                       capture_output=True, text=True, check=True)
            assert not remaining.stdout.strip(), remaining.stdout
            reports[-1]["interrupted_container_removed"] = True
            # Exercise the real Codex provider/relay/SSE chain using only mock responses.
            requests: list[dict] = []
            transport_command = codex_command("Return unresolved with no candidates.", schema, DEFAULT_MODEL)
            with patch.object(GatewayConfig, "from_host", side_effect=lambda token, model, provider="openai-relay": GatewayConfig(
                    token, model, "https://relay.invalid", "unused-mock-host-secret")), \
                 patch.object(AttemptGateway, "handler", mock_model_handler(requests)):
                with isolated_attempt(transport_command, environment, workspace, schema=schema) as (argv, env):
                    code, records, stderr, _ = capture_agent(argv, env, 180)
                if code:
                    raise RuntimeError(f"{condition} mock Codex transport failed: {stderr[-2000:]}")
                (workspace / "mock_result.json").unlink(missing_ok=True)
                write_result(records, schema, workspace / "mock_result.json")
            posts = [request for request in requests if request["method"] == "POST"]
            hermetic = {
                "only_responses_route": {request["path"] for request in requests} == {"/model/responses"},
                "attempt_token_only": all(request["authorization"] and PLANTED_HOST_KEY not in request["authorization"]
                                          for request in posts),
                "planted_host_key_absent": all(PLANTED_HOST_KEY not in request["body"] for request in posts),
                "agents_md_ignored": all(WORKSPACE_SENTINEL not in request["body"] for request in posts),
                "uncompressed_store_false": all(request["encoding"] is None and json.loads(request["body"]).get("store") is False
                                                for request in posts),
            }
            if not all(hermetic.values()):
                raise RuntimeError(f"{condition} hermeticity check failed: {hermetic}")
            reports[-1]["actual_codex_transport_with_mock_model"] = True
            reports[-1]["hermeticity"] = hermetic
    evaluator.shutdown(); evaluator.server_close()
    report = {"status": "pass", "llm_calls": 0, "live_mock_host_service_tested": True, "conditions": reports}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

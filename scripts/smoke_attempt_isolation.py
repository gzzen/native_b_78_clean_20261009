#!/usr/bin/env python3
# Path: scripts/smoke_attempt_isolation.py
# Purpose: Verify isolated OpenCode transport and science access using mock model responses.
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
from common.integrity.opencode_capture import capture_opencode, opencode_command, write_result, DEFAULT_MODEL
from common.integrity.evaluator_access_policy import generation_event_guard

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
checks['no_real_model_auth_file']=not (Path(os.environ['XDG_DATA_HOME'])/'opencode/auth.json').exists()
checks['no_model_or_evaluator_secret_env']=not any(k.startswith(('OPENAI_','DEEPSEEK_','EVALUATOR_API_')) for k in os.environ)
evaluator_port=int(os.environ['PROBE_EVALUATOR_PORT'])
for label,host,port in [('direct_public_network','1.1.1.1',443),('host_service','172.17.0.1',evaluator_port),('loopback_service','127.0.0.1',evaluator_port)]:
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
conn.request('POST','/model/chat/completions',body='{"model":"unused"}')
checks['model_without_token_denied']=conn.getresponse().status==403
conn.close()
checks['opencode_executable']='2.' in subprocess.check_output(['/opt/opencode/opencode','--version'],text=True)
if os.environ.get('PROBE_SCIENCE')=='1':
    with urllib.request.urlopen('https://mygene.info/v3/gene/1017?fields=symbol',timeout=40) as response:
        checks['allowlisted_science_works']=json.load(response).get('symbol')=='CDK2'
print(json.dumps(checks),flush=True)
assert all(checks.values()),checks
'''


def mock_model_handler(gateway):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path != "/model/chat/completions" or self.headers.get("Authorization") != "Bearer " + gateway.config.token:
                self.send_error(403); return
            assert body["model"] == gateway.config.model
            text = '{"status":"unresolved","candidates":[],"summary":"Mock transport only."}'
            self.send_response(200); self.send_header("Content-Type","text/event-stream"); self.end_headers()
            for delta, finish in (({"role":"assistant", "content":text}, None), ({}, "stop")):
                chunk = {"id":"mock", "object":"chat.completion.chunk", "created":1,
                         "model":gateway.config.model,
                         "choices":[{"index":0,"delta":delta,"finish_reason":finish}]}
                self.wfile.write(("data: "+json.dumps(chunk)+"\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.close_connection = True
    return Handler


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
    runs = ROOT / "runs"
    runs.mkdir(exist_ok=True)
    reports = []
    with tempfile.TemporaryDirectory(prefix="security-probe-", dir=runs) as directory:
        root = Path(directory)
        hidden = root / "private/answer_sentinel.txt"
        hidden.parent.mkdir(); hidden.write_text("host-only-sentinel")
        history = root / "old/predictions.json"
        history.parent.mkdir(); history.write_text("host-only-history")
        for condition in ("native", "b"):
            workspace = root / condition
            (workspace / "data").mkdir(parents=True)
            (workspace / "data/input.txt").write_text("public-input")
            environment = controlled_env(workspace)
            schema = ROOT / "conditions/native/question_result_no_id.schema.json"
            command = opencode_command("No model invocation", schema, DEFAULT_MODEL)
            forbidden = {"private":str(hidden), "history":str(history),
                         "host_opencode_home":str(Path.home()/".local/share/opencode/auth.json"),
                         "solver_runs":str(ROOT/"runs/20260924_native_b_3seeds"),
                         "docker_socket":"/var/run/docker.sock"}
            with isolated_attempt(command, environment, workspace, schema=schema) as (argv, env):
                relay = argv.index("/boundary/relay.py")
                argv = argv[:relay + 1] + [sys.executable, "-c", PROBE]
                image_pos = relay - 2
                argv[image_pos:image_pos] = ["--env", "PROBE_FORBIDDEN_PATHS="+json.dumps(forbidden),
                                           "--env", "PROBE_SCIENCE="+str(int(args.science)),
                                           "--env", "PROBE_EVALUATOR_PORT="+str(evaluator.server_port)]
                result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=90)
                if result.returncode:
                    raise RuntimeError(f"{condition} probe failed: {result.stdout} {result.stderr}")
                checks = json.loads(result.stdout.strip().splitlines()[-1])
                reports.append({"condition":condition, "status":"pass", "checks":checks})
            # A command-event violation must kill the attempt container, not only its docker client.
            with isolated_attempt(command, environment, workspace, schema=schema) as (argv, env):
                name = argv[argv.index("--name") + 1]
                relay = argv.index("/boundary/relay.py")
                event = {"type":"item.started","item":{"type":"command_execution","command":"cat ../old/predictions.json"}}
                program = "import json,time; print(json.dumps("+repr(event)+"),flush=True); time.sleep(30)"
                argv = argv[:relay + 1] + [sys.executable, "-c", program]
                code, records, _, _ = capture_opencode(argv, env, timeout=45, event_guard=generation_event_guard(workspace))
                assert code == 86, code
            remaining = subprocess.run(["docker","ps","-a","--filter",f"name=^{name}$","--format","{{.Names}}"],
                                       capture_output=True, text=True, check=True)
            assert not remaining.stdout.strip(), remaining.stdout
            reports[-1]["interrupted_container_removed"] = True
            # Exercise the actual OpenCode provider/relay/SSE chain using only mock responses.
            transport_command = opencode_command("Return unresolved with no candidates.", schema, DEFAULT_MODEL)
            with patch.object(GatewayConfig, "from_host", side_effect=lambda token, model, provider="deepseek": GatewayConfig(
                    token, model, "https://api.deepseek.com/v1", "unused-mock-host-secret")), \
                 patch.object(AttemptGateway, "handler", mock_model_handler):
                with isolated_attempt(transport_command, environment, workspace, schema=schema) as (argv, env):
                    code, records, stderr, _ = capture_opencode(argv, env, 90)
                if code:
                    raise RuntimeError(f"{condition} mock OpenCode transport failed: {stderr}")
                write_result(records, schema, workspace/"mock_result.json")
                reports[-1]["actual_opencode_transport_with_mock_model"] = True
    evaluator.shutdown(); evaluator.server_close()
    report = {"status":"pass", "llm_calls":0, "live_mock_host_service_tested":True, "conditions":reports}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

# Path: common/integrity/test_opencode_capture.py
# Purpose: Verify OpenCode event auditing, schema validation and optional real CLI mock transport.
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from common.integrity.opencode_capture import (
    DEFAULT_MODEL, agent_manifest, capture_opencode, model_id, normalize_event, opencode_command,
    provider_config, write_result, generation_failure,
)
from common.integrity.evaluator_access_policy import generation_event_guard, policy_violation
from common.integrity.attempt_gateway import GatewayConfig
from conditions.native.runner import audit_and_enforce_code

ROOT = Path(__file__).resolve().parents[2]
RELAY_MODEL = "openai-relay/gpt-5.6-sol"
SCHEMA = ROOT / "conditions/native/question_result_no_id.schema.json"
RESULT = {"status": "unresolved", "candidates": [], "summary": "Mock transport only."}


def text_event(text, message="m1"):
    return {"type": "text", "part": {"type": "text", "text": text, "messageID": message}}


def tool_event(tool, inputs, status="completed", **state):
    return {"type": "tool_use", "part": {"tool": tool, "id": "tool-1",
            "state": {"status": status, "input": inputs, **state}}}


class OpenCodeCaptureTests(unittest.TestCase):
    def test_command_uses_v2_private_server_and_preserves_prompt(self):
        command = opencode_command("Original scientific prompt", SCHEMA, DEFAULT_MODEL)
        self.assertEqual(command[:3], ["opencode", "run", "--standalone"])
        self.assertIn("--auto", command)
        self.assertNotIn("--output-schema", command)
        self.assertTrue(command[-1].startswith("Original scientific prompt\n\n"))
        self.assertIn("INSTANCE of this schema", command[-1])
        self.assertIn("only allowed top-level keys are: status, candidates, summary", command[-1])
        definition = command[-1].split("\n\nOutput JSON Schema (a definition, not the answer object):\n", 1)[1].split("\n\nFinal response requirements:", 1)[0]
        self.assertNotIn("$schema", json.loads(definition))
        self.assertIn("$schema", json.loads(SCHEMA.read_text()), "the original schema must remain intact")
        with self.assertRaises(ValueError):
            model_id("openrouter/deepseek/model")

    def test_config_binds_token_and_disables_subagents(self):
        config = provider_config(DEFAULT_MODEL, "high")
        provider = config["providers"]["deepseek"]
        self.assertEqual(provider["env"], ["NATIVE_B_GATEWAY_TOKEN"])
        self.assertEqual(provider["models"]["deepseek-flash"]["body"]["reasoning_effort"], "high")
        rules = config["agents"]["benchmark"]["permissions"]
        self.assertEqual(rules[0], {"action": "*", "resource": "*", "effect": "deny"})
        self.assertFalse(any(rule["action"] == "subagent" and rule["effect"] == "allow" for rule in rules))
        self.assertNotIn("reasoning_effort", provider_config(DEFAULT_MODEL, "none")["providers"]["deepseek"]["models"]["deepseek-flash"]["body"])

    def test_relay_config_uses_stateless_responses_over_http(self):
        config = provider_config(RELAY_MODEL, "xhigh")
        provider = config["providers"]["openai-relay"]
        self.assertEqual(list(config["providers"]), ["openai-relay"])
        self.assertEqual(provider["env"], ["NATIVE_B_GATEWAY_TOKEN"])
        self.assertEqual(provider["package"], "@opencode/ai/providers/openai/responses")
        self.assertEqual(provider["settings"], {"baseURL": "http://127.0.0.1:8765/model", "transport": "http",
                                                "reasoningEffort": "xhigh", "reasoningSummary": "auto"})
        self.assertEqual(provider["models"]["gpt-5.6-sol"]["limit"], {"context": 1_000_000, "output": 128_000})
        self.assertEqual(config["agents"]["benchmark"]["model"], RELAY_MODEL)
        self.assertNotIn("reasoningSummary", provider_config(RELAY_MODEL, "none")["providers"]["openai-relay"]["settings"])
        manifest = agent_manifest(RELAY_MODEL, "xhigh")
        self.assertEqual((manifest["provider"], manifest["wire_api"]), ("OpenAI relay", "responses"))
        self.assertEqual(agent_manifest(DEFAULT_MODEL, "high")["wire_api"], "chat/completions")

    def test_models_and_efforts_are_bound_to_their_provider(self):
        self.assertEqual(model_id(RELAY_MODEL), "gpt-5.6-sol")
        for model in ("openai/gpt-5.6-sol", "openai-relay/deepseek-flash", "deepseek/gpt-5.6-sol",
                      "openai-relay/gpt-5.6-sol/extra", "openai-relay/"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                model_id(model)
        for model, effort in ((RELAY_MODEL, "max"), (DEFAULT_MODEL, "xhigh")):
            with self.subTest(model=model, effort=effort), self.assertRaises(ValueError):
                provider_config(model, effort)
        command = opencode_command("Prompt", SCHEMA, RELAY_MODEL)
        self.assertEqual(command[command.index("--model") + 1], RELAY_MODEL)

    def test_gateway_requires_host_deepseek_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "host-only"}, clear=True):
            config = GatewayConfig.from_host("attempt-token", "deepseek-flash")
        self.assertEqual(config.upstream, "https://api.deepseek.com/v1")
        self.assertEqual(config.credential, "host-only")
        with tempfile.TemporaryDirectory() as directory:
            with patch("common.integrity.attempt_gateway.PROJECT_ENV", Path(directory) / ".env"), \
                    patch.dict(os.environ, {"OPENAI_API_KEY": "unrelated"}, clear=True), \
                    self.assertRaises(RuntimeError):
                GatewayConfig.from_host("attempt-token", "deepseek-flash")

    def test_shell_execution_retains_raw_event_and_exit_status(self):
        raw = tool_event("shell", {"command": "python analysis/scripts/solve.py"},
                         output="42", metadata={"metadata": {"exit": 0}})
        event = normalize_event(raw)
        self.assertEqual(event["opencode_event"], raw)
        self.assertEqual(event["item"]["exit_code"], 0)
        self.assertEqual(event["item"]["type"], "command_execution")

    def test_file_tools_and_shell_workdir_cannot_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = generation_event_guard(Path(directory))
            for raw in (
                tool_event("read", {"filePath": "../old/predictions.json"}),
                tool_event("write", {"filePath": "/etc/host-secret"}),
                tool_event("patch", {"patchText": "*** Begin Patch\n*** Update File: ../private/key\n"}),
                tool_event("shell", {"command": "ls", "workdir": ".."}),
            ):
                self.assertIsNotNone(guard(normalize_event(raw)))
            self.assertIsNone(guard(normalize_event(tool_event("read", {"filePath": "data/input.csv"}))))

    def test_completed_grep_exclusion_does_not_interrupt_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            guard = generation_event_guard(workspace)
            command = (f"cd {workspace} && find . -type f \\( -name '*.py' -o -name '*.json' \\) "
                       "2>/dev/null | grep -v '/tmp/' | grep -v '/analysis/' | head -50")
            self.assertIsNone(guard(normalize_event(tool_event("shell", {"command": command}))))
            for raw in (tool_event("read", {"path": "/tmp/private-file"}),
                        tool_event("shell", {"command": command + "; cat /tmp/private-file"})):
                with self.subTest(raw=raw):
                    self.assertIsNotNone(guard(normalize_event(raw)))

    def test_reading_or_failing_a_source_does_not_count_as_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            script = workspace / "analysis/scripts/solve.py"
            script.parent.mkdir(parents=True)
            script.write_text("print(42)\n")
            for raw in (tool_event("read", {"filePath": "analysis/scripts/solve.py"}),
                        tool_event("shell", {"command": "python analysis/scripts/solve.py"}, status="error"),
                        tool_event("shell", {"command": "python analysis/scripts/solve.py"}, metadata={"exit": 1})):
                with self.assertRaisesRegex(RuntimeError, "actual execution"):
                    audit_and_enforce_code(workspace, [{"event": normalize_event(raw)}], RESULT, "q1")

    def test_stream_capture_stops_on_an_opencode_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = tool_event("shell", {"command": "cat ../old/predictions.json"})
            program = "import json,time; print(json.dumps(" + repr(raw) + "),flush=True); time.sleep(30)"
            started = time.monotonic()
            code, events, _, _ = capture_opencode([sys.executable, "-c", program], dict(os.environ), 35,
                                                event_guard=generation_event_guard(Path(directory)))
            self.assertEqual(code, 86)
            self.assertIsNotNone(policy_violation(events))
            self.assertLess(time.monotonic() - started, 5)

    def test_final_answer_is_validated_and_earlier_commentary_is_ignored(self):
        records = [{"event": text_event("Inspecting the data", "m0")},
                   {"event": text_event(json.dumps(RESULT)[:20])},
                   {"event": text_event(json.dumps(RESULT)[20:])}]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            self.assertEqual(write_result(records, SCHEMA, output), RESULT)
            with self.assertRaises(FileExistsError):
                write_result(records, SCHEMA, output)

    def test_malformed_schema_output_errors_and_missing_text_fail_closed(self):
        from jsonschema import ValidationError
        cases = [([], ValueError),
                 ([{"event": text_event("{}") }], ValidationError),
                 ([{"event": text_event(json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", **RESULT}))}], ValidationError),
                 ([{"event": text_event("prose " + json.dumps(RESULT))}], ValueError),
                 ([{"event": text_event(json.dumps(RESULT))}, {"event": {"type": "error"}}], ValueError)]
        with tempfile.TemporaryDirectory() as directory:
            for records, error in cases:
                with self.assertRaises(error):
                    write_result(records, SCHEMA, Path(directory) / "result.json")

    def test_generation_failure_reports_error_type_and_status_without_payload(self):
        record = {"event": {"opencode_event": {"type": "error", "error": {
            "name": "APIError", "data": {"statusCode": 400,
            "message": "confidential-error", "responseBody": "confidential-body"}}}}}
        reason = generation_failure(1, [record])
        self.assertIn("OpenCode error=APIError", reason)
        self.assertIn("HTTP status=400", reason)
        self.assertIn("stderr.log", reason)
        self.assertNotIn("confidential", reason)
        self.assertIn("generation timeout", generation_failure(None, []))
        self.assertIn("generation exit=1", generation_failure(1, []))

    def test_short_lived_process_output_is_fully_drained(self):
        events = [text_event(str(i)) for i in range(100)]
        program = "import json; print('\\n'.join(map(json.dumps," + repr(events) + ")))"
        code, records, _, _ = capture_opencode([sys.executable, "-c", program], dict(os.environ), 5)
        self.assertEqual(code, 0)
        self.assertEqual(len(records), 100)

    def run_real_cli(self, mock_handler, model, effort):
        """Run the real CLI in a private workspace against a local mock model server."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), mock_handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                home = workspace / "home"
                config = home / ".config/opencode/opencode.json"
                config.parent.mkdir(parents=True)
                configuration = provider_config(model, effort, f"http://127.0.0.1:{server.server_port}/model")
                configuration["experimental"] = {"portable_shell_scanner": True}
                config.write_text(json.dumps(configuration))
                script = workspace / "analysis/scripts/solve.py"
                script.parent.mkdir(parents=True)
                script.write_text("print(42)\n")
                env = {"HOME": str(home), "PWD": str(workspace), "PATH": os.environ["PATH"],
                       "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
                       "XDG_CACHE_HOME": str(workspace / "tmp/cache"), "TMPDIR": str(workspace / "tmp"),
                       "NATIVE_B_GATEWAY_TOKEN": "mock-attempt-token"}
                (workspace / "tmp").mkdir()
                command = opencode_command("Run the persisted mock script, then return unresolved.", SCHEMA, model)
                command[0] = os.environ["NATIVE_B_TEST_OPENCODE_BINARY"]
                code, records, stderr, _ = capture_opencode(command, env, 90)
                self.assertEqual(code, 0, stderr + repr(records[-3:]))
                self.assertEqual(write_result(records, SCHEMA, workspace / "question_result.json"), RESULT)
                self.assertTrue(any(record["event"].get("item", {}).get("type") == "command_execution"
                                    and record["event"]["item"].get("status") == "completed"
                                    and record["event"]["item"].get("exit_code") in (None, 0) for record in records), repr(records))
                audit_and_enforce_code(workspace, records, RESULT, "q1")
        finally:
            server.shutdown()
            server.server_close()

    @unittest.skipUnless(os.environ.get("NATIVE_B_TEST_OPENCODE_BINARY"), "set binary path for real CLI mock test")
    def test_real_opencode_cli_against_local_mock_model(self):
        requests = []
        errors = []

        class MockModel(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, self.headers.get("Authorization"), payload))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                tool_result = any(message.get("role") == "tool" for message in payload["messages"])
                if not tool_result:
                    names = [tool["function"]["name"] for tool in payload.get("tools", [])]
                    shell = next((name for name in names if name in {"shell", "bash"}), None)
                    if not shell:
                        errors.append("No shell tool: " + repr(names))
                    delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_mock",
                             "type": "function", "function": {"name": shell or "shell",
                             "arguments": json.dumps({"command": "python3 analysis/scripts/solve.py",
                                                      "description": "Run persisted mock source"})}}]}
                    finish = "tool_calls"
                else:
                    delta = {"role": "assistant", "content": json.dumps(RESULT)}
                    finish = "stop"
                for delta_part, reason in (({"reasoning_content": "Mock reasoning only."}, None), (delta, None), ({}, finish)):
                    chunk = {"id": "mock", "object": "chat.completion.chunk", "created": 1,
                             "model": "deepseek-flash", "choices": [{"index": 0, "delta": delta_part,
                             "finish_reason": reason}]}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        self.run_real_cli(MockModel, DEFAULT_MODEL, "high")
        self.assertEqual(errors, [])
        self.assertEqual(len(requests), 2, "a fixed title must prevent auxiliary title-model calls")
        for path, auth, payload in requests:
            self.assertEqual(path, "/model/chat/completions")
            self.assertEqual(auth, "Bearer mock-attempt-token")
            self.assertEqual(payload["model"], "deepseek-flash")
            self.assertEqual(payload["reasoning_effort"], "high")
            self.assertEqual(payload["thinking"], {"type": "enabled"})
        assistant_turns = [message for message in requests[-1][2]["messages"]
                           if message.get("role") == "assistant"]
        self.assertTrue(assistant_turns)
        self.assertTrue(all(message.get("reasoning_content") == "Mock reasoning only."
                            for message in assistant_turns),
                        "DeepSeek requires reasoning_content to be retained across tool calls")

    @unittest.skipUnless(os.environ.get("NATIVE_B_TEST_OPENCODE_BINARY"), "set binary path for real CLI mock test")
    def test_real_opencode_cli_against_local_responses_relay(self):
        requests = []
        errors = []

        class MockRelay(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def event(self, kind, **data):
                self.wfile.write(f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n".encode())

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, self.headers.get("Authorization"), payload))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                response = {"id": "resp_mock", "object": "response", "created_at": 1,
                            "model": payload["model"], "status": "in_progress", "output": []}
                self.event("response.created", response=response, sequence_number=0)
                reasoning = {"id": "rs_mock", "type": "reasoning", "summary": [],
                             "encrypted_content": "encrypted-mock-reasoning"}
                self.event("response.output_item.added", output_index=0, item=reasoning, sequence_number=1)
                self.event("response.output_item.done", output_index=0, item=reasoning, sequence_number=2)
                if not any(item.get("type") == "function_call_output" for item in payload["input"]):
                    names = [tool.get("name") for tool in payload.get("tools", [])]
                    shell = next((name for name in names if name in {"shell", "bash"}), None)
                    if not shell:
                        errors.append("No shell tool: " + repr(names))
                    item = {"id": "fc_mock", "type": "function_call", "call_id": "call_mock",
                            "name": shell or "shell", "status": "completed",
                            "arguments": json.dumps({"command": "python3 analysis/scripts/solve.py",
                                                     "description": "Run persisted mock source"})}
                    self.event("response.output_item.added", output_index=1,
                               item={**item, "arguments": ""}, sequence_number=3)
                    self.event("response.function_call_arguments.done", output_index=1, item_id="fc_mock",
                               arguments=item["arguments"], sequence_number=4)
                else:
                    text = json.dumps(RESULT)
                    item = {"id": "msg_mock", "type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": text, "annotations": []}]}
                    self.event("response.output_item.added", output_index=1,
                               item={**item, "content": [], "status": "in_progress"}, sequence_number=3)
                    self.event("response.output_text.delta", output_index=1, item_id="msg_mock",
                               content_index=0, delta=text, sequence_number=4)
                self.event("response.output_item.done", output_index=1, item=item, sequence_number=5)
                usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                         "input_tokens_details": {"cached_tokens": 0},
                         "output_tokens_details": {"reasoning_tokens": 2}}
                self.event("response.completed", sequence_number=6,
                           response={**response, "status": "completed", "output": [reasoning, item], "usage": usage})
                self.wfile.flush()
                self.close_connection = True

        self.run_real_cli(MockRelay, RELAY_MODEL, "xhigh")
        self.assertEqual(errors, [])
        self.assertEqual(len(requests), 2, "a fixed title must prevent auxiliary title-model calls")
        for path, auth, payload in requests:
            self.assertEqual(path, "/model/responses")
            self.assertEqual(auth, "Bearer mock-attempt-token")
            self.assertEqual(payload["model"], "gpt-5.6-sol")
            self.assertEqual(payload["reasoning"]["effort"], "xhigh")
            self.assertIs(payload["store"], False)
            self.assertIn("reasoning.encrypted_content", payload["include"])
        replayed = [item for item in requests[-1][2]["input"] if item.get("type") == "reasoning"]
        self.assertEqual([item.get("encrypted_content") for item in replayed], ["encrypted-mock-reasoning"],
                         "stateless Responses requests must replay encrypted reasoning across tool calls")

if __name__ == "__main__":
    unittest.main()

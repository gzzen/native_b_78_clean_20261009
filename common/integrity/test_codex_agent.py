# Path: common/integrity/test_codex_agent.py
# Purpose: Verify Codex configuration, event auditing, schema validation and optional real CLI mock transport.
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

from common.integrity.codex_agent import (
    CATALOG_OVERRIDES, DEFAULT_MODEL, agent_manifest, capture_agent, codex_command, codex_config,
    codex_settings, generation_failure, model_catalog, model_id, normalize_event, write_result,
)
from common.integrity.evaluator_access_policy import generation_event_guard, policy_violation
from common.integrity.attempt_gateway import AttemptGateway, GatewayConfig
from conditions.native.runner import audit_and_enforce_code

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "conditions/native/question_result_no_id.schema.json"
RESULT = {"status": "unresolved", "candidates": [], "summary": "Mock transport only."}
CATALOG = {"models": [{"slug": "gpt-5.6-sol", "tool_mode": "code_mode_only", "multi_agent_version": "v2",
                       "prefer_websockets": True, "include_apps_usage_instructions": True,
                       "supported_reasoning_levels": [{"effort": e} for e in ("low", "high", "xhigh", "max", "ultra")]}]}


def item_event(item, kind="item.completed"):
    return {"type": kind, "item": item}


def command_event(command, status="completed", exit_code=0, kind="item.completed"):
    return item_event({"id": "item_0", "type": "command_execution", "command": command,
                       "aggregated_output": "42\n", "exit_code": exit_code, "status": status}, kind)


def file_change_event(*paths, kind="item.completed"):
    return item_event({"id": "item_1", "type": "file_change", "status": "completed",
                       "changes": [{"path": path, "kind": "add"} for path in paths]}, kind)


def message_event(text):
    return item_event({"id": "item_2", "type": "agent_message", "text": text})


def turn(records, completed=True):
    return [{"event": normalize_event(raw)} for raw in
            [{"type": "turn.started"}, *records, *([{"type": "turn.completed", "usage": {}}] if completed else [])]]


class CodexConfigurationTests(unittest.TestCase):
    def test_command_is_non_interactive_strict_and_preserves_prompt(self):
        command = codex_command("Original scientific prompt", SCHEMA, DEFAULT_MODEL)
        self.assertEqual(command[:3], ["codex", "exec", "--json"])
        for flag in ("--ephemeral", "--strict-config", "--ignore-rules", "--skip-git-repo-check",
                     "--dangerously-bypass-approvals-and-sandbox"):
            self.assertIn(flag, command)
        self.assertNotIn("--output-schema", command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-sol")
        self.assertTrue(command[-1].startswith("Original scientific prompt\n\n"))
        self.assertIn("only allowed top-level keys are: status, candidates, summary", command[-1])
        definition = command[-1].split("\n\nOutput JSON Schema (a definition, not the answer object):\n", 1)[1].split("\n\nFinal response requirements:", 1)[0]
        self.assertNotIn("$schema", json.loads(definition))
        self.assertIn("$schema", json.loads(SCHEMA.read_text()), "the original schema must remain intact")

    def test_only_relay_models_and_supported_efforts_are_accepted(self):
        self.assertEqual(model_id(DEFAULT_MODEL), "gpt-5.6-sol")
        for model in ("deepseek/deepseek-flash", "openai/gpt-5.6-sol", "openai-relay/deepseek-flash",
                      "openai-relay/gpt-5.6-sol/extra", "openai-relay/"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                model_id(model)
        for effort in ("none", "ultra", "minimal"):
            with self.subTest(effort=effort), self.assertRaises(ValueError):
                agent_manifest(DEFAULT_MODEL, effort)

    def test_config_is_hermetic_and_routes_only_through_the_gateway(self):
        text = codex_config(DEFAULT_MODEL, "xhigh", "/w/home/.codex/model_catalog.json")
        config = tomllib.loads(text)
        self.assertEqual(config, codex_settings(DEFAULT_MODEL, "xhigh", "/w/home/.codex/model_catalog.json"))
        provider = config["model_providers"][config["model_provider"]]
        self.assertEqual(provider["base_url"], "http://127.0.0.1:8765/model")
        self.assertEqual(provider["env_key"], "NATIVE_B_GATEWAY_TOKEN")
        self.assertEqual((provider["wire_api"], provider["supports_websockets"]), ("responses", False))
        self.assertEqual(config["project_doc_max_bytes"], 0)
        self.assertEqual(config["history"], {"persistence": "none"})
        self.assertFalse(config["skills"]["bundled"]["enabled"])
        self.assertFalse(config["tools"]["experimental_request_user_input"]["enabled"])
        for feature in ("multi_agent", "multi_agent_v2", "plugins", "apps", "hooks", "computer_use",
                        "enable_request_compression", "image_generation"):
            self.assertIs(config["features"][feature], False, feature)
        self.assertNotIn("auth", text.lower().replace("auth_elicitation", ""))

    def test_catalog_keeps_the_shipped_tool_mode_and_removes_subagents(self):
        catalog = model_catalog(CATALOG, DEFAULT_MODEL, "high")
        [entry] = catalog["models"]
        self.assertEqual(entry["tool_mode"], "code_mode_only")
        for key, value in CATALOG_OVERRIDES.items():
            self.assertEqual(entry[key], value)
        self.assertEqual(CATALOG["models"][0]["multi_agent_version"], "v2", "the bundled catalog must not be mutated")
        with self.assertRaises(ValueError):
            model_catalog(CATALOG, DEFAULT_MODEL, "ultra")
        with self.assertRaises(ValueError):
            model_catalog(CATALOG, "openai-relay/gpt-6-sol", "high")

    def test_gateway_requires_host_relay_key_and_ignores_openai_key(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("common.integrity.attempt_gateway.PROJECT_ENV", Path(directory) / ".env"):
            with patch.dict(os.environ, {"OPENAI_RELAY_API_KEY": "host-only"}, clear=True):
                config = GatewayConfig.from_host("attempt-token", "gpt-5.6-sol")
            self.assertEqual((config.credential, config.model_path), ("host-only", "/responses"))
            with patch.dict(os.environ, {"OPENAI_API_KEY": "unrelated"}, clear=True), self.assertRaises(RuntimeError):
                GatewayConfig.from_host("attempt-token", "gpt-5.6-sol")
            with self.assertRaises(ValueError):
                GatewayConfig.from_host("attempt-token", "deepseek-flash", "deepseek")


class CodexEventTests(unittest.TestCase):
    def test_command_execution_retains_raw_event_and_exit_status(self):
        raw = command_event("/bin/bash -lc 'python3 analysis/scripts/solve.py'")
        event = normalize_event(raw)
        self.assertEqual(event["codex_event"], raw)
        self.assertEqual((event["item"]["type"], event["item"]["exit_code"], event["item"]["agent_tool"]),
                         ("command_execution", 0, "shell"))

    def test_file_changes_become_guarded_file_access(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            guard = generation_event_guard(workspace)
            # macOS temp directories live under /private, which command-text policy
            # patterns flag; in-workspace paths must therefore be reported relative.
            inside = normalize_event(file_change_event(str(workspace / "analysis/scripts/solve.py")), workspace)
            self.assertEqual(inside["item"]["type"], "file_access")
            self.assertEqual(inside["item"]["paths"], ["analysis/scripts/solve.py"])
            self.assertIsNone(guard(inside))
            for path in ("/etc/host-secret", str(workspace.parent / "old/predictions.json")):
                with self.subTest(path=path):
                    self.assertIsNotNone(guard(normalize_event(file_change_event(path, kind="item.started"), workspace)))

    def test_escaping_shell_commands_are_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            guard = generation_event_guard(Path(directory))
            for command in ("/bin/bash -lc 'cat ../old/predictions.json'", "/bin/bash -lc 'cat /tmp/private-file'"):
                with self.subTest(command=command):
                    self.assertIsNotNone(guard(normalize_event(command_event(command, kind="item.started"))))
            self.assertIsNone(guard(normalize_event(command_event("/bin/bash -lc 'ls data'"))))

    def test_failing_a_source_does_not_count_as_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            script = workspace / "analysis/scripts/solve.py"
            script.parent.mkdir(parents=True)
            script.write_text("print(42)\n")
            # A failed run is not execution. (Acceptance still treats any successful command
            # naming analysis/scripts/ as execution, e.g. `cat`; see the replay tooling.)
            for raw in (command_event("/bin/bash -lc 'python3 analysis/scripts/solve.py'", status="failed", exit_code=1),):
                with self.subTest(raw=raw), self.assertRaisesRegex(RuntimeError, "actual execution"):
                    audit_and_enforce_code(workspace, [{"event": normalize_event(raw)}], RESULT, "q1")
            audit_and_enforce_code(workspace, [{"event": normalize_event(
                command_event("/bin/bash -lc 'python3 analysis/scripts/solve.py'"))}], RESULT, "q1")

    def test_stream_capture_stops_on_a_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = command_event("/bin/bash -lc 'cat ../old/predictions.json'", kind="item.started")
            program = "import json,time; print(json.dumps(" + repr(raw) + "),flush=True); time.sleep(30)"
            started = time.monotonic()
            code, events, _, _ = capture_agent([sys.executable, "-c", program], dict(os.environ), 35,
                                               event_guard=generation_event_guard(Path(directory)))
            self.assertEqual(code, 86)
            self.assertIsNotNone(policy_violation(events))
            self.assertLess(time.monotonic() - started, 5)

    def test_last_agent_message_is_the_validated_answer(self):
        records = turn([message_event("Inspecting the data"),
                        {"type": "error", "message": "Reconnecting... 1/2"},
                        message_event(json.dumps(RESULT))])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            self.assertEqual(write_result(records, SCHEMA, output), RESULT)
            with self.assertRaises(FileExistsError):
                write_result(records, SCHEMA, output)

    def test_failed_incomplete_or_malformed_turns_fail_closed(self):
        from jsonschema import ValidationError
        answer = message_event(json.dumps(RESULT))
        cases = [(turn([]), ValueError),
                 (turn([answer], completed=False), ValueError),
                 (turn([answer, {"type": "turn.failed", "error": {"message": "x"}}]), ValueError),
                 (turn([message_event("{}")]), ValidationError),
                 (turn([message_event("prose " + json.dumps(RESULT))]), ValueError)]
        with tempfile.TemporaryDirectory() as directory:
            for records, error in cases:
                with self.subTest(records=records), self.assertRaises(error):
                    write_result(records, SCHEMA, Path(directory) / "result.json")

    def test_generation_failure_reports_event_and_status_without_message(self):
        record = {"event": normalize_event({"type": "turn.failed", "error": {
            "message": "unexpected status 429 Too Many Requests: confidential-body"}})}
        reason = generation_failure(1, [record])
        self.assertIn("Codex event=turn.failed", reason)
        self.assertIn("HTTP status=429", reason)
        self.assertNotIn("confidential", reason)
        self.assertIn("generation timeout", generation_failure(None, []))
        self.assertIn("generation exit=1", generation_failure(1, []))

    def test_short_lived_process_output_is_fully_drained(self):
        events = [message_event(str(i)) for i in range(100)]
        program = "import json; print('\\n'.join(map(json.dumps," + repr(events) + ")))"
        code, records, _, _ = capture_agent([sys.executable, "-c", program], dict(os.environ), 5)
        self.assertEqual(code, 0)
        self.assertEqual(len(records), 100)


def code_mode_relay(requests):
    """Mock Responses upstream: one code-mode exec running the persisted script, then the answer."""
    js = ("const r = await tools.exec_command({cmd: 'python3 analysis/scripts/solve.py'});\n"
          "text(JSON.stringify(r));")

    def build(gateway):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                requests.append(("GET", self.path, None))
                self.send_error(404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(("POST", self.path, self.headers.get("Authorization")))
                answered = any(item.get("type", "").endswith("_output") for item in body.get("input", []))
                item = ({"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                         "content": [{"type": "output_text", "text": json.dumps(RESULT), "annotations": []}]}
                        if answered else
                        {"type": "custom_tool_call", "id": "ctc_1", "call_id": "call_1", "name": "exec",
                         "namespace": "functions", "input": js, "status": "completed"})
                usage = {"input_tokens": 1, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 1,
                         "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 2}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for kind, data in (("response.created", {"response": {"id": "resp"}}),
                                   ("response.output_item.done", {"output_index": 0, "item": item}),
                                   ("response.completed", {"response": {"id": "resp", "usage": usage}})):
                    self.wfile.write(f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n".encode())
                    self.wfile.flush()
                self.close_connection = True
        return Handler
    return build


@unittest.skipUnless(os.environ.get("NATIVE_B_TEST_CODEX"),
                     "set NATIVE_B_TEST_CODEX=1 on the Linux host (Docker, agent_bix, scripts/install_codex.sh)")
class RealCodexTests(unittest.TestCase):
    def test_real_codex_runs_persisted_code_through_the_gateway(self):
        from common.benchmark.a84_contract import controlled_env
        from common.integrity.attempt_container import isolated_attempt
        requests: list = []
        (ROOT / "runs").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "runs") as directory:
            workspace = Path(directory) / "q1"
            (workspace / "data").mkdir(parents=True)
            script = workspace / "analysis/scripts/solve.py"
            script.parent.mkdir(parents=True)
            script.write_text("print(42)\n")
            command = codex_command("Run the persisted mock script, then return unresolved.", SCHEMA, DEFAULT_MODEL)
            with patch.object(GatewayConfig, "from_host", side_effect=lambda token, model, provider="openai-relay":
                              GatewayConfig(token, model, "https://relay.invalid", "unused-mock-host-secret")), \
                    patch.object(AttemptGateway, "handler", code_mode_relay(requests)):
                with isolated_attempt(command, controlled_env(workspace), workspace, schema=SCHEMA,
                                      reasoning_effort="high") as (argv, env):
                    code, records, stderr, _ = capture_agent(argv, env, 240,
                                                             event_guard=generation_event_guard(workspace))
            self.assertEqual(code, 0, stderr[-2000:] + repr(records[-3:]))
            self.assertEqual(write_result(records, SCHEMA, workspace / "question_result.json"), RESULT)
            audit_and_enforce_code(workspace, records, RESULT, "q1")
            self.assertEqual({path for _, path, _ in requests}, {"/model/responses"})
            self.assertEqual(len(requests), 2)
            self.assertTrue(all(auth and auth.startswith("Bearer ") for _, _, auth in requests))


if __name__ == "__main__":
    unittest.main()

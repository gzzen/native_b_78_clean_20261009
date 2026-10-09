from __future__ import annotations

import json
import os
import sys
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from common.integrity.codex_capture import capture_codex
from common.integrity.evaluator_access_policy import (
    EVALUATOR_ENV_KEYS,
    LEGACY_PRIVATE_ROOTS,
    POLICY_EXIT_CODE,
    PRIVATE_EVALUATION_ROOT,
    answer_blind_command,
    answer_blind_env,
    codex_sandbox_args,
    evaluator_api_event_guard,
    policy_violation,
    generation_event_guard,
    private_evaluation_roots,
)


class EvaluatorAccessPolicyTests(unittest.TestCase):
    def test_all_api_prefixed_environment_keys_are_removed(self) -> None:
        clean = answer_blind_env({"EVALUATOR_API_SECRET": "secret", "EVALUATOR_API_ENDPOINT": "url",
                                  "NATIVE_B_PRIVATE_ROOTS": "/private/path", "KEEP": "yes"})
        self.assertEqual(clean, {"KEEP": "yes", "GENERATION_EVALUATOR_ACCESS": "forbidden"})

    def test_configured_private_location_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"NATIVE_B_PRIVATE_ROOTS": directory}):
                self.assertIn(Path(directory).resolve(), private_evaluation_roots())

    def test_live_guard_rejects_paths_but_allows_science(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "question"
            workspace.mkdir()
            guard = generation_event_guard(workspace)
            for command in ("cat ../other/predictions.json", "ls /tmp", "cat /var/tmp/old.json",
                            "python /mnt/other/run/solve.py", "cat ../private/answers.json"):
                event = {"type": "item.started", "item": {"type": "command_execution", "command": command}}
                self.assertIsNotNone(guard(event), command)
            for command in ("python analysis/scripts/solve.py", "curl https://mygene.info/v3/query"):
                event = {"type": "item.started", "item": {"type": "command_execution", "command": command}}
                self.assertIsNone(guard(event), command)

    def test_live_capture_interrupts_path_violation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event = {"type": "item.started", "item": {"type": "command_execution", "command": "cat ../other/result.json"}}
            script = "import json,time; print(json.dumps(" + repr(event) + "),flush=True); time.sleep(30)"
            started = time.monotonic()
            rc, records, _, _ = capture_codex([sys.executable, "-c", script], dict(os.environ), timeout=35,
                                             event_guard=generation_event_guard(Path(directory)))
            self.assertEqual(rc, POLICY_EXIT_CODE)
            self.assertEqual(policy_violation(records)["policy"], "generation_isolation_forbidden")
            self.assertLess(time.monotonic() - started, 5)

    def test_default_codex_sandbox_is_workspace_write(self) -> None:
        self.assertEqual(codex_sandbox_args(), ["--sandbox", "workspace-write"])

    def test_external_container_mode_fails_closed_outside_docker(self) -> None:
        old = os.environ.get("NATIVE_B_EXTERNAL_CONTAINER_ISOLATION")
        os.environ["NATIVE_B_EXTERNAL_CONTAINER_ISOLATION"] = "invalid"
        try:
            with self.assertRaises(RuntimeError):
                codex_sandbox_args()
        finally:
            if old is None:
                os.environ.pop("NATIVE_B_EXTERNAL_CONTAINER_ISOLATION", None)
            else:
                os.environ["NATIVE_B_EXTERNAL_CONTAINER_ISOLATION"] = old

    def test_answer_blind_env_removes_all_api_material(self) -> None:
        source = {key: "secret" for key in EVALUATOR_ENV_KEYS}
        source["KEEP_ME"] = "yes"
        result = answer_blind_env(source)
        self.assertEqual(result["KEEP_ME"], "yes")
        self.assertEqual(result["GENERATION_EVALUATOR_ACCESS"], "forbidden")
        for key in EVALUATOR_ENV_KEYS:
            self.assertNotIn(key, result)

    def test_only_command_events_are_guarded(self) -> None:
        prose = {"type": "item.completed", "item": {"type": "agent_message", "text": "local scoring service"}}
        command = {"type": "item.started", "item": {"type": "command_execution", "command": "curl http://127.0.0.1:54321/"}}
        self.assertIsNone(evaluator_api_event_guard(prose))
        self.assertEqual(evaluator_api_event_guard(command)["reason"], "local_scoring_service")

    def test_private_bundle_is_hidden_with_bubblewrap(self) -> None:
        checks = [f"test ! -e {PRIVATE_EVALUATION_ROOT}/README.md"]
        for path in private_evaluation_roots():
            if path.is_dir():
                checks.append(
                    f'test -z "$(find {path} -mindepth 1 -maxdepth 1 -print -quit)"'
                )
        wrapped = answer_blind_command([
            "sh", "-c",
            " && ".join(checks),
        ])
        return_code, _, _, _ = capture_codex(wrapped, dict(os.environ), timeout=5)
        self.assertEqual(return_code, 0)

    def test_capture_interrupts_live_attempt(self) -> None:
        event = {
            "type": "item.started",
            "item": {"type": "command_execution", "command": "curl http://localhost:54321/"},
        }
        script = "import json,time; print(json.dumps(" + repr(event) + "),flush=True); time.sleep(30)"
        started = time.monotonic()
        return_code, records, _, _ = capture_codex(
            [sys.executable, "-c", script],
            dict(os.environ),
            timeout=35,
            event_guard=evaluator_api_event_guard,
            policy_exit_code=POLICY_EXIT_CODE,
        )
        self.assertEqual(return_code, POLICY_EXIT_CODE)
        self.assertIsNotNone(policy_violation(records))
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()

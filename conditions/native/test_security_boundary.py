"""Native safety wiring: real workspace construction, simulated model events."""
from __future__ import annotations

import os
import contextlib
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conditions.native import runner


class NativeSecurityBoundaryTests(unittest.TestCase):
    def test_real_attempts_strip_secrets_and_retry_with_fresh_tmp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capsules = root / "capsules"
            source = capsules / "CapsuleFolder-test-uuid"
            source.mkdir(parents=True)
            (source / "input.csv").write_text("x\n1\n")
            row = {"question_id": "test-q1", "short_id": "test", "capsule_uuid": "test-uuid", "question": "Compute x."}
            observed = []

            def capture(command, env, timeout, event_guard, policy_exit_code, *, progress_label=None):
                workspace = Path(env["TMPDIR"]).parent
                observed.append(workspace)
                self.assertTrue((workspace / "data/input.csv").is_file())
                self.assertEqual(stat.S_IMODE(Path(env["TMPDIR"]).stat().st_mode), 0o700)
                self.assertEqual(env["TMP"], env["TEMP"])
                self.assertFalse(any(key.startswith("EVALUATOR_API_") for key in env))
                event = {"type": "item.started", "item": {
                    "type": "command_execution", "command": "cat ../other/predictions.json"}}
                violation = event_guard(event)
                self.assertEqual(violation["policy"], "generation_isolation_forbidden")
                records = [{"event": {"type": "controller.policy_violation", **violation}}]
                return policy_exit_code, records, "", 0.01

            @contextlib.contextmanager
            def isolate(command, environment, workspace, **kwargs):
                yield command, environment

            with patch.dict(os.environ, {"EVALUATOR_API_URL": "secret-url", "EVALUATOR_API_SECRET": "secret"}), \
                 patch.object(runner, "CAPSULES", capsules), \
                 patch.object(runner, "isolated_attempt", side_effect=isolate), \
                 patch.object(runner, "capture_opencode", side_effect=capture), \
                 patch.object(runner, "command_metrics", return_value={}):
                result = runner.run_question(row, root / "runs", 10)
            self.assertEqual(len(observed), 2)
            self.assertNotEqual(observed[0], observed[1])
            self.assertFalse(result["accepted"])
            self.assertEqual(result["controller_status"], "failed_closed_repeated_policy_violation")


if __name__ == "__main__":
    unittest.main()

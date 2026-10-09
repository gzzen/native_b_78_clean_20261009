from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conditions.native import runner as native_runner
from conditions.b import runner as b_runner


def attempt(workspace: Path, index: int, violated: bool) -> dict:
    workspace.mkdir(parents=True)
    record = {
        "workspace": str(workspace),
        "attempt_index": index,
        "return_code": 86 if violated else 0,
        "runtime_seconds": 0.1,
        "controller_status": "interrupted_policy_violation" if violated else "passed",
        "accepted": not violated,
    }
    if violated:
        record["policy_violation"] = {"policy": "evaluator_api_forbidden"}
    return record


class ControllerRetryPolicyTests(unittest.TestCase):
    def test_native_repeated_violation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts = [attempt(root / "0000/q", 0, True), attempt(root / "0001/q", 1, True)]
            with patch.object(native_runner, "run_question_attempt", side_effect=attempts) as run:
                result = native_runner.run_question({}, root, 10)
            self.assertEqual(run.call_count, 2)
            self.assertFalse(result["accepted"])
            self.assertEqual(result["controller_status"], "failed_closed_repeated_policy_violation")

    def test_native_ordinary_failure_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = attempt(root / "0000/q", 0, False)
            record.update(accepted=False, controller_status="failed", return_code=1)
            with patch.object(native_runner, "run_question_attempt", return_value=record) as run:
                result = native_runner.run_question({}, root, 10)
            self.assertEqual(run.call_count, 1)
            self.assertFalse(result["accepted"])

    def test_native_policy_violation_gets_one_fresh_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts = [attempt(root / "0000/q", 0, True), attempt(root / "0001/q", 1, False)]
            with patch.object(native_runner, "run_question_attempt", side_effect=attempts):
                result = native_runner.run_question({}, root, 10)
            self.assertTrue(result["accepted"])
            self.assertEqual(result["attempt_count"], 2)
            self.assertNotEqual(result["attempt_history"][0]["workspace"], result["attempt_history"][1]["workspace"])

    def test_b_repeated_violation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts = [attempt(root / "0000/c", 0, True), attempt(root / "0001/c", 1, True)]
            with patch.object(b_runner, "run_capsule_attempt", side_effect=attempts):
                result = b_runner.run_capsule({}, [], root, 10)
            self.assertFalse(result["accepted"])
            self.assertEqual(result["attempt_count"], 2)
            self.assertEqual(result["controller_status"], "failed_closed_repeated_policy_violation")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.integrity import relay_monitor
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


def relay_failed(workspace: Path, index: int, cause: str = "relay_capacity") -> dict:
    record = attempt(workspace, index, False)
    record.update(accepted=False, controller_status="failed", return_code=1, relay_failure=cause,
                  failure_reason="RuntimeError: generation exit=1; Codex event=turn.failed")
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

    def test_native_relay_failure_gets_fresh_reattempts_until_one_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts = [relay_failed(root / "0000/q", 0), relay_failed(root / "0001/q", 1, "relay_stream_drop"),
                        attempt(root / "0002/q", 2, False)]
            with patch.object(native_runner, "run_question_attempt", side_effect=attempts) as run:
                result = native_runner.run_question({}, root, 10)
            self.assertEqual([call.args[3] for call in run.call_args_list], [0, 1, 2])
            self.assertTrue(result["accepted"])
            self.assertEqual((result["attempt_count"], result["relay_retry_count"], result["policy_retry_count"]),
                             (3, 2, 0))
            self.assertEqual(result["retry_reason"], "prior_attempt_relay_stream_drop")
            history = result["attempt_history"]
            self.assertEqual([item["relay_failure"] for item in history], ["relay_capacity", "relay_stream_drop", None])
            self.assertEqual([item["retry_reason"] for item in history],
                             [None, "prior_attempt_relay_capacity", "prior_attempt_relay_stream_drop"])
            self.assertEqual(len({item["workspace"] for item in history}), 3)

    def test_b_relay_retries_are_bounded_then_fail_with_exhausted_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts = [relay_failed(root / f"{index:04d}/c", index) for index in range(4)]
            with patch.object(b_runner, "run_capsule_attempt", side_effect=attempts) as run:
                result = b_runner.run_capsule({}, [], root, 10)
            self.assertEqual(run.call_count, 4)  # One attempt plus three relay re-attempts.
            self.assertFalse(result["accepted"])
            self.assertEqual(result["controller_status"], "failed_relay_retries_exhausted")
            self.assertEqual(result["relay_retry_count"], 3)

    def test_relay_retry_budget_follows_the_runner_setting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(native_runner, "RELAY_RETRIES", 0), \
                    patch.object(native_runner, "run_question_attempt", return_value=relay_failed(root / "0000/q", 0)) as run:
                result = native_runner.run_question({}, root, 10)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(result["controller_status"], "failed_relay_retries_exhausted")

    def test_policy_and_relay_budgets_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempts = [relay_failed(root / "0000/q", 0), attempt(root / "0001/q", 1, True),
                        relay_failed(root / "0002/q", 2), attempt(root / "0003/q", 3, True)]
            with patch.object(native_runner, "run_question_attempt", side_effect=attempts) as run:
                result = native_runner.run_question({}, root, 10)
            self.assertEqual(run.call_count, 4)
            self.assertEqual(result["controller_status"], "failed_closed_repeated_policy_violation")
            self.assertEqual((result["relay_retry_count"], result["policy_retry_count"]), (2, 1))

    def test_attempts_wait_for_a_cooling_down_relay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor = relay_monitor.RelayMonitor(1, base_backoff=0.2, max_backoff=0.2, jitter=0)
            monitor.refused(monitor.admit(float("inf")))
            with patch.object(relay_monitor, "_shared", monitor), \
                    patch.object(native_runner, "run_question_attempt", return_value=attempt(root / "0000/q", 0, False)):
                import time
                started = time.monotonic()
                native_runner.run_question({}, root, 10)
            self.assertGreaterEqual(time.monotonic() - started, 0.15)


if __name__ == "__main__":
    unittest.main()

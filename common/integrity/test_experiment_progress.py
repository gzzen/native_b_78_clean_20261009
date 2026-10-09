# Path: common/integrity/test_experiment_progress.py
# Purpose: Verify read-only progress snapshots, retry counts, process matching, and log redaction.
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.check_experiment_progress import inspect_run, latest_experiment, print_status, runner_processes

EXPERIMENT = "deepseek_full_20260930T090315Z_282056"


def write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


class ExperimentProgressTests(unittest.TestCase):
    def inspect(self, root, condition="native", processes=None, process_error=None):
        return inspect_run(root, EXPERIMENT, condition, "01", processes or {}, process_error, 123456)

    def test_completed_generation_missing_report_is_complete_and_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "analysis" / f"{EXPERIMENT}_native_seed01_generation.json"
            write(path, {"freeze": {"status": "frozen_before_private_scoring"},
                         "records": [{"accepted": i < 38, "controller_status": "passed" if i < 38 else "failed"}
                                     for i in range(78)]})
            original = path.read_bytes()
            run = self.inspect(root)
            self.assertEqual((run["state"], run["completed"], run["passed"], run["failed"]), ("COMPLETE", 78, 38, 40))
            self.assertFalse(run["report_exists"])
            self.assertIn("report missing", run["warnings"][0])
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((root / "reports").exists())

    def test_retries_are_counted_once_and_partial_workspaces_are_not_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "runs" / f"{EXPERIMENT}_native_seed01"
            write(run_root / "0000/q1/run_manifest.json", {"accepted": False, "policy_violation": {}})
            write(run_root / "0001/q1/run_manifest.json", {"accepted": True, "controller_status": "passed"})
            write(run_root / "0000/q2/run_manifest.json", {"accepted": False, "controller_status": "failed"})
            write(run_root / "0000/q3/run_manifest.json", {"accepted": False, "policy_violation": {"reason": "test"}})
            (run_root / "0001/q3").mkdir(parents=True)
            run = self.inspect(root)
            self.assertEqual((run["state"], run["completed"], run["passed"], run["failed"]), ("INCOMPLETE", 2, 1, 1))
            self.assertEqual(run["unfinished_workspaces"], ["q3"])
            self.assertEqual(self.inspect(root, processes={str(run_root.resolve()): [987654]})["state"], "RUNNING")
            self.assertEqual(self.inspect(root, process_error="ps unavailable")["state"], "UNKNOWN")
            write(run_root / "resume_later/0001/q3/run_manifest.json", {"accepted": True, "controller_status": "passed"})
            resumed = self.inspect(root)
            self.assertEqual((resumed["completed"], resumed["passed"], resumed["failed"]), (3, 2, 1))

    def test_first_policy_interruption_awaits_retry_and_terminal_policy_failure_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "runs" / f"{EXPERIMENT}_b_seed01/0000/capsule/run_manifest.json"
            record = {"accepted": False, "policy_violation": {"reason": "test"}}
            write(path, record)
            self.assertEqual(self.inspect(root, "b")["completed"], 0)
            record["attempt_history"] = [{}, {}]
            record["controller_status"] = "failed_closed_repeated_policy_violation"
            write(path, record)
            run = self.inspect(root, "b")
            self.assertEqual((run["unit"], run["expected"], run["completed"], run["failed"]), ("capsules", 19, 1, 1))

    def test_process_check_matches_exact_run_path_and_does_not_expose_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_root = root / "runs" / f"{EXPERIMENT}_native_seed01"
            output = (f"987654 python conditions/native/runner.py --run-root {run_root}\n"
                      f"987655 python conditions/b/runner.py --run-root={root / 'runs/other'}\n"
                      f"987656 bash -c 'python conditions/native/runner.py --run-root {run_root}'\n"
                      f"987658 bash -c python conditions/native/runner.py --run-root {run_root}\n"
                      "987657 opencode run --title secret-prompt\n")
            with patch("scripts.check_experiment_progress.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, "")):
                found, warning = runner_processes(root)
            self.assertIsNone(warning)
            self.assertEqual(found[str(run_root.resolve())], [987654])
            self.assertNotIn("secret", json.dumps(found))
            with patch("scripts.check_experiment_progress.subprocess.run", side_effect=PermissionError):
                found, warning = runner_processes(root)
            self.assertEqual(found, {})
            self.assertIn("unknown", warning)

    def test_corrupt_json_log_redaction_and_unstarted_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(self.inspect(root)["state"], "NOT STARTED")
            self.assertEqual(self.inspect(root, process_error="ps unavailable")["state"], "UNKNOWN")
            manifest = root / "analysis" / f"{EXPERIMENT}_native_seed01_manifest.json"
            manifest.parent.mkdir()
            manifest.write_text('{"secret":')
            log = root / "logs" / EXPERIMENT / "native_seed01.log"
            log.parent.mkdir(parents=True)
            safe_line = "[12:43:51Z] [Native] completed=78/78 passed=38 failed=40"
            log.write_text(safe_line + "\nAPIError: secret-body\n[12:44:00Z] [Native] secret-prompt\n")
            run = self.inspect(root)
            self.assertEqual(run["log"]["last_progress"], safe_line)
            self.assertEqual(run["log"]["exception_type"], "APIError")
            self.assertNotIn("secret", json.dumps(run))
            self.assertTrue(run["warnings"])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                print_status({"checked_at_utc": "now", "experiment_id": EXPERIMENT,
                              "process_warning": None, "runs": [run]}, True)
            self.assertNotIn("secret", output.getvalue())

    def test_rep_labels_and_auto_discovery_exclude_historical_experiments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write(root / "analysis" / f"{EXPERIMENT}_b_rep01_manifest.json", {"capsule_count": 1})
            write(root / "analysis/historical_b_seed01_manifest.json", {})
            self.assertEqual(latest_experiment(root), EXPERIMENT)
            run = self.inspect(root, "b")
            self.assertEqual(run["tag"], f"{EXPERIMENT}_b_rep01")
            self.assertEqual(run["expected"], 1)
            self.assertEqual(run["state"], "PREPARED")


if __name__ == "__main__":
    unittest.main()

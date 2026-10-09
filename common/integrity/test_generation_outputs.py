# Path: common/integrity/test_generation_outputs.py
# Purpose: Verify fresh report directories, early output failures, and read-only report recovery.
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conditions.native import runner as native_runner
from conditions.b import runner as b_runner
from scripts.recover_generation_report import recover_report


class GenerationOutputTests(unittest.TestCase):
    def run_mock_generation(self, root: Path, condition: str, report: Path, *, blocked=False):
        module = native_runner if condition == "native" else b_runner
        row = {"question_id": "test-q1", "short_id": "test", "capsule_uuid": "test-uuid"}
        item = row if condition == "native" else {**row, "question_ids": ["test-q1"]}
        generation = root / "analysis/generation.json"
        record = {"accepted": True, "attempt_history": [], "total_agent_calls": 1}

        def execute(*args):
            self.assertTrue(report.parent.is_dir(), "report directory must exist before agent execution")
            self.assertTrue(generation.parent.is_dir())
            return record

        def preflight(*args):
            module.MANIFEST.parent.mkdir(parents=True, exist_ok=True)
            module.MANIFEST.write_text("{}")
            return {}

        argv = ["runner.py", "--execute", "--workers", "1", "--run-root", str(root / "runs"),
                "--manifest", str(root / "manifest.json"), "--output", str(generation), "--report", str(report)]
        with contextlib.ExitStack() as stack:
            for name in ("MANIFEST", "MODEL", "OUT", "REASONING_EFFORT", "REPORT"):
                stack.enter_context(patch.object(module, name, getattr(module, name)))
            substrate = module.substrate if condition == "native" else module.BASE_CONDITION
            stack.enter_context(patch.object(substrate, "MODEL", substrate.MODEL))
            stack.enter_context(patch.object(substrate, "REASONING_EFFORT", substrate.REASONING_EFFORT))
            if condition == "b":
                stack.enter_context(patch.object(module.BASE_CONDITION.substrate, "MODEL", module.BASE_CONDITION.substrate.MODEL))
                stack.enter_context(patch.object(module.BASE_CONDITION.substrate, "REASONING_EFFORT", module.BASE_CONDITION.substrate.REASONING_EFFORT))
                stack.enter_context(patch.object(module, "load_rows", return_value=[row]))
            stack.enter_context(patch.object(module, "rows" if condition == "native" else "capsules", return_value=[item]))
            stack.enter_context(patch.object(module, "preflight", side_effect=preflight))
            agent = stack.enter_context(patch.object(module, "run_question" if condition == "native" else "run_capsule", side_effect=execute))
            stack.enter_context(patch("sys.argv", argv))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            if blocked:
                with self.assertRaises(FileExistsError):
                    module.main()
                agent.assert_not_called()
                self.assertFalse(generation.exists())
            else:
                module.main()
                agent.assert_called_once()
                self.assertIn("1/1", report.read_text())
                self.assertEqual(json.loads(generation.read_text())["freeze"]["status"], "frozen_before_private_scoring")

    def test_both_runners_create_missing_nested_report_directories_before_calls(self):
        for condition in ("native", "b"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.run_mock_generation(root, condition, root / "missing/reports/report.md")

    def test_unusable_report_parent_fails_before_calls(self):
        for condition in ("native", "b"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                blocker = root / "reports"
                blocker.write_text("existing file")
                self.run_mock_generation(root, condition, blocker / "report.md", blocked=True)

    def test_recovery_preserves_frozen_bytes_and_counts_and_refuses_overwrite(self):
        for condition in ("native", "b"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                generation = root / "generation.json"
                total_key, passed_key, records_key = (("questions", "controller_passed_questions", "records")
                    if condition == "native" else ("capsules", "accepted_capsules", "episodes"))
                payload = {"freeze": {"status": "frozen_before_private_scoring"},
                    "generation_summary": {total_key: 2, passed_key: 1},
                    records_key: [{"accepted": True}, {"accepted": False}]}
                generation.write_text(json.dumps(payload))
                original = generation.read_bytes()
                report = root / "missing/reports/report.md"
                recover_report(generation, condition, report)
                self.assertIn("1/2", report.read_text())
                self.assertEqual(generation.read_bytes(), original)
                report_bytes = report.read_bytes()
                with self.assertRaises(FileExistsError):
                    recover_report(generation, condition, report)
                self.assertEqual(report.read_bytes(), report_bytes)
                with self.assertRaises(FileExistsError):
                    recover_report(generation, condition, generation)
                self.assertEqual(generation.read_bytes(), original)

    def test_unfrozen_or_inconsistent_generation_is_not_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generation, report = root / "generation.json", root / "reports/report.md"
            for payload in ({"freeze": {"status": "not_frozen"}},
                {"freeze": {"status": "frozen_before_private_scoring"},
                 "generation_summary": {"questions": 2, "controller_passed_questions": 2},
                 "records": [{"accepted": True}, {"accepted": False}]}):
                with self.subTest(payload=payload):
                    generation.write_text(json.dumps(payload))
                    with self.assertRaises(ValueError):
                        recover_report(generation, "native", report)
                    self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()

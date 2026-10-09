import json
import tempfile
import unittest
from pathlib import Path

from conditions.b.merge_service_recovery import _eligible, _is_infrastructure_failure, merge


class ServiceRecoveryEligibilityTests(unittest.TestCase):
    def test_timeout_failed_base_accepts_clean_recovery(self):
        base = {"accepted": False, "return_code": None, "failure_reason": "RuntimeError: generation timeout"}
        recovery = {"accepted": True, "return_code": 0, "retry_attempted": False}
        self.assertTrue(_is_infrastructure_failure(base))
        self.assertTrue(_eligible(recovery, base))

    def test_semantic_failure_is_not_replaceable(self):
        base = {"accepted": False, "return_code": 0, "failure_reason": "ValueError: unresolved grounding"}
        recovery = {"accepted": True, "return_code": 0, "retry_attempted": False}
        self.assertFalse(_is_infrastructure_failure(base))
        self.assertFalse(_eligible(recovery, base))

    def test_successful_base_cannot_be_overwritten(self):
        base = {"accepted": True, "return_code": 0, "failure_reason": None}
        recovery = {"accepted": True, "return_code": 0, "retry_attempted": False}
        self.assertFalse(_eligible(recovery, base))

    def test_merge_counts_all_recovery_calls_not_only_replacements(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            failed = {
                "capsule_uuid": "failed", "short_id": "bix-1",
                "accepted": False, "return_code": None,
                "failure_reason": "RuntimeError: generation timeout",
                "workspace": str(root / "failed"),
            }
            succeeded = {
                "capsule_uuid": "ok", "short_id": "bix-2",
                "accepted": True, "return_code": 0,
                "workspace": str(root / "ok"),
            }
            recovered = {
                **failed, "accepted": True, "return_code": 0,
                "retry_attempted": False, "failure_reason": None,
                "workspace": str(root / "recovered"),
            }
            rejected = {
                **succeeded, "retry_attempted": False,
                "workspace": str(root / "rejected"),
            }
            base = {
                "episodes": [failed, succeeded],
                "generation_summary": {"total_codex_calls": 2},
                "freeze": {"manifest_sha256": "manifest"},
            }
            recovery = {
                "episodes": [recovered, rejected],
                "generation_summary": {"total_codex_calls": 2},
            }
            base_path = root / "base.json"; recovery_path = root / "recovery.json"
            base_path.write_text(json.dumps(base)); recovery_path.write_text(json.dumps(recovery))
            output = root / "merged.json"; report = root / "report.md"
            merge(base_path, recovery_path, output, report)
            merged = json.loads(output.read_text())
            self.assertEqual(merged["generation_summary"]["total_codex_calls"], 4)
            self.assertEqual(merged["generation_summary"]["service_recovery_calls"], 2)
            self.assertEqual(
                merged["generation_summary"]["service_recovery_accepted_replacements"], 1
            )


if __name__ == "__main__":
    unittest.main()

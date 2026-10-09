import json
import tempfile
import unittest
from pathlib import Path

from conditions.b.runner import (
    GROUNDING_FIELDS,
    audit_consistency_component,
    audit_grounding_component,
    canonicalize_result_identity,
)


class AdditiveGroundingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / "analysis").mkdir()
        self.rows = [
            {"capsule_uuid": "capsule", "question_id": "q1"},
            {"capsule_uuid": "capsule", "question_id": "q2"},
        ]

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def record(qid, status="resolved"):
        value = "unresolved" if status == "unresolved" else "defined"
        return {
            "question_id": qid,
            **{field: value for field in GROUNDING_FIELDS},
            "grounding_status": status,
            "selection_rationale": "public question and supplied schema",
        }

    def write(self, questions):
        (self.workspace / "analysis/grounding.json").write_text(
            json.dumps({"capsule_uuid": "capsule", "questions": questions}),
            encoding="utf-8",
        )

    def test_valid_grounding_passes(self):
        self.write([self.record("q1"), self.record("q2")])
        audit = audit_grounding_component(self.workspace, self.rows)
        self.assertTrue(audit["passed"])
        self.assertEqual(audit["question_local_failures"], {})

    def test_missing_grounding_is_local_to_all_questions(self):
        audit = audit_grounding_component(self.workspace, self.rows)
        self.assertFalse(audit["passed"])
        self.assertEqual(set(audit["question_local_failures"]), {"q1", "q2"})

    def test_grounding_and_result_status_are_independent(self):
        bad = self.record("q2", status="unresolved")
        bad["observation_unit"] = "entity"
        bad["result_status"] = "answered"
        self.write([self.record("q1"), bad])
        audit = audit_grounding_component(self.workspace, self.rows)
        self.assertTrue(audit["passed"])
        self.assertEqual(audit["question_local_failures"], {})

    def test_invalid_result_status_is_question_local(self):
        bad = self.record("q2")
        bad["result_status"] = "correct"
        self.write([self.record("q1"), bad])
        audit = audit_grounding_component(self.workspace, self.rows)
        self.assertFalse(audit["passed"])
        self.assertEqual(set(audit["question_local_failures"]), {"q2"})

    def test_missing_execution_record_is_nonblocking(self):
        self.write([self.record("q1"), self.record("q2")])
        audit, failures = audit_consistency_component(self.workspace, self.rows)
        self.assertEqual(audit["status"], "unavailable_no_execution_record")
        self.assertTrue(audit["passed"])
        self.assertEqual(failures, {})
        self.assertTrue((self.workspace / "analysis/consistency.json").is_file())

    def test_malformed_execution_record_is_visible_and_local(self):
        self.write([self.record("q1"), self.record("q2")])
        (self.workspace / "analysis/execution.json").write_text("{}", encoding="utf-8")
        audit, failures = audit_consistency_component(self.workspace, self.rows)
        self.assertFalse(audit["passed"])
        self.assertEqual(set(failures), {"q1", "q2"})

    def test_identity_transposition_is_canonicalized_only(self):
        rows = [{"capsule_uuid": "uuid", "short_id": "bix-1", "question_id": "q1"}]
        payload = {"capsule_uuid": "uuid", "short_id": "uuid", "results": []}
        repaired, audit = canonicalize_result_identity(payload, rows)
        self.assertEqual(repaired["short_id"], "bix-1")
        self.assertEqual(audit["field"], "short_id")
        self.assertEqual(payload["short_id"], "uuid")

    def test_arbitrary_identity_mismatch_is_not_repaired(self):
        rows = [{"capsule_uuid": "uuid", "short_id": "bix-1", "question_id": "q1"}]
        payload = {"capsule_uuid": "other", "short_id": "wrong", "results": []}
        repaired, audit = canonicalize_result_identity(payload, rows)
        self.assertIsNone(audit)
        self.assertIs(repaired, payload)


if __name__ == "__main__":
    unittest.main()

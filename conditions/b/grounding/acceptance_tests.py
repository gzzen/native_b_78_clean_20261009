# Path: conditions/b/grounding/acceptance_tests.py
# Purpose: B-v5 acceptance tests for contrastive grounding selection.
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from conditions.b.grounding.consistency import FIELDS, validate_consistency


class ConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / "analysis/scripts").mkdir(parents=True)
        (self.workspace / "analysis/scripts/run.py").write_text("print('computed')\n", encoding="utf-8")
        self.rows = [{"capsule_uuid": "capsule", "question_id": "q1"}]
        self.values = {
            "data_object": "filtered VCF; QUAL >= 30",
            "observation_unit": "unique non-reference SNP allele",
            "comparison": "MDR versus retail isolate",
            "metric_or_outcome": "unique SNP count",
            "unit_or_scale": "variants",
            "denominator_or_population": "unique non-reference SNP alleles",
            "method_or_estimator": "bcftools normalized allele count",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, execution_updates: dict | None = None, status: str = "resolved") -> None:
        alternatives = [
            {"id": "participant", **self.values, "evidence": "Question wording says participant."},
            {"id": "event", **{**self.values, "denominator_or_population": "all event rows"},
             "evidence": "The supplied table contains event rows."},
        ]
        grounded = {"question_id": "q1", "interpretations": alternatives,
                    "selected_interpretation_id": "participant",
                    "selection_rationale": "Question asks for a participant-level comparison.",
                    "grounding_status": "resolved", **self.values}
        executed = {"question_id": "q1", **self.values, "status": status,
                    "evidence_files": ["analysis/scripts/run.py"]}
        executed.update(execution_updates or {})
        for name, record in (("grounding", grounded), ("execution", executed)):
            payload = {"capsule_uuid": "capsule", "questions": [record]}
            (self.workspace / f"analysis/{name}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

    def test_resolved_exact_semantics_pass(self) -> None:
        self.write()
        report = validate_consistency(self.workspace, self.rows)
        self.assertTrue(report["passed"])
        self.assertEqual(set(self.values), set(FIELDS))

    def test_unambiguous_grounding_may_omit_interpretations(self) -> None:
        self.write()
        path = self.workspace / "analysis/grounding.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["questions"][0].pop("interpretations")
        payload["questions"][0].pop("selected_interpretation_id")
        path.write_text(json.dumps(payload), encoding="utf-8")
        report = validate_consistency(self.workspace, self.rows)
        self.assertTrue(report["passed"])

    def test_resolved_metric_mismatch_fails(self) -> None:
        self.write({"metric_or_outcome": "raw VCF record count"})
        with self.assertRaisesRegex(ValueError, "resolved grounding/execution conflicts"):
            validate_consistency(self.workspace, self.rows)

    def test_resolved_observation_unit_mismatch_fails(self) -> None:
        self.write({"observation_unit": "VCF row"})
        with self.assertRaisesRegex(ValueError, "resolved grounding/execution conflicts"):
            validate_consistency(self.workspace, self.rows)

    def test_deseq2_method_must_include_design(self) -> None:
        self.values["method_or_estimator"] = "DESeq2 Wald test with apeglm lfc shrinkage"
        self.write()
        with self.assertRaisesRegex(ValueError, "must state its design formula"):
            validate_consistency(self.workspace, self.rows)

    def test_unresolved_semantic_mismatch_is_recorded(self) -> None:
        alternatives = [
            {"id": "one", **self.values, "evidence": "Question wording supports this."},
            {"id": "two", **{**self.values, "metric_or_outcome": "raw record count"},
             "evidence": "Schema exposes raw records."},
        ]
        grounded = {"question_id": "q1", "interpretations": alternatives,
                    "selected_interpretation_id": None,
                    "selection_rationale": "Allowed evidence does not distinguish the metric.",
                    "grounding_status": "unresolved",
                    **{field: "unresolved" for field in self.values}}
        execution = {"question_id": "q1", **self.values, "status": "unresolved",
                     "evidence_files": ["analysis/scripts/run.py"]}
        for name, record in (("grounding", grounded), ("execution", execution)):
            payload = {"capsule_uuid": "capsule", "questions": [record]}
            (self.workspace / f"analysis/{name}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
        report = validate_consistency(self.workspace, self.rows)
        self.assertTrue(report["passed"])
        self.assertFalse(report["questions"][0]["consistent"])
        self.assertEqual(report["questions"][0]["disposition"], "unresolved_semantics")


if __name__ == "__main__":
    unittest.main()

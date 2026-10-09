import json
import tempfile
import unittest
from pathlib import Path

from conditions.b.operation_contract import audit_operation_contract


class OperationContractTests(unittest.TestCase):
    def test_named_metric_without_operation_evidence_is_local_failure(self):
        with tempfile.TemporaryDirectory() as td:
            w = Path(td); (w / "analysis/scripts").mkdir(parents=True)
            (w / "analysis/scripts/c.py").write_text("values = terminal_lengths(tree)\n")
            rows = [{"question_id": "q1", "question": "What is the long branch score?"}]
            grounding = {"questions": [{"question_id": "q1", "metric_or_outcome": "long branch score", "method_or_estimator": "median score", "unit_or_scale": "ratio"}]}
            results = [{"question_id": "q1", "status": "resolved"}]
            report = audit_operation_contract(w, rows, grounding, results)
            self.assertIn("q1", report["question_local_failures"])

    def test_named_metric_with_formula_evidence_passes(self):
        with tempfile.TemporaryDirectory() as td:
            w = Path(td); (w / "analysis/scripts").mkdir(parents=True)
            (w / "analysis/scripts/c.py").write_text("# formula: mean patristic distance / average pairwise distance\n")
            rows = [{"question_id": "q1", "question": "What is the score?"}]
            grounding = {"questions": [{"question_id": "q1", "metric_or_outcome": "score", "method_or_estimator": "formula", "unit_or_scale": "ratio"}]}
            results = [{"question_id": "q1", "status": "resolved", "candidates": [{"method": "score formula", "evidence": "formula"}]}]
            report = audit_operation_contract(w, rows, grounding, results)
            self.assertFalse(report["question_local_failures"])

    def test_candidate_proxy_is_rejected_even_when_other_script_has_help(self):
        with tempfile.TemporaryDirectory() as td:
            w = Path(td); (w / "analysis/scripts").mkdir(parents=True)
            (w / "analysis/scripts/lookup.py").write_text("phykit long_branch_score --help\n")
            rows = [{"question_id": "q1", "question": "What is the long branch score?"}]
            grounding = {"questions": [{"question_id": "q1", "metric_or_outcome": "long branch score", "method_or_estimator": "PhyKIT", "unit_or_scale": "ratio"}]}
            results = [{"question_id": "q1", "status": "resolved", "candidates": [{"answer": "1", "method": "terminal branch length", "evidence": "computed"}]}]
            report = audit_operation_contract(w, rows, grounding, results)
            self.assertIn("q1", report["question_local_failures"])

    def test_ordinary_odds_ratio_is_not_treated_as_external_operation(self):
        with tempfile.TemporaryDirectory() as td:
            w = Path(td); (w / "analysis/scripts").mkdir(parents=True)
            (w / "analysis/scripts/c.py").write_text("odds_ratio = exposed_odds / control_odds\n")
            rows = [{"question_id": "q1", "question": "What is the odds ratio?"}]
            grounding = {"questions": [{"question_id": "q1", "metric_or_outcome": "odds ratio", "method_or_estimator": "ordinary 2x2 calculation", "unit_or_scale": "ratio"}]}
            results = [{"question_id": "q1", "status": "resolved", "candidates": [{"answer": "2", "method": "2x2 odds calculation", "evidence": "c.py"}]}]
            report = audit_operation_contract(w, rows, grounding, results)
            self.assertFalse(report["question_local_failures"])

    def test_hyphenated_named_metric_binds_to_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            w = Path(td); (w / "analysis/scripts").mkdir(parents=True)
            (w / "analysis/scripts/c.py").write_text("# phykit long_branch_score --help\n")
            rows = [{"question_id": "q1", "question": "What is the long branch score?"}]
            grounding = {"questions": [{"question_id": "q1", "metric_or_outcome": "long branch score", "method_or_estimator": "PhyKIT", "unit_or_scale": "score"}]}
            results = [{"question_id": "q1", "status": "resolved", "candidates": [{"answer": "1", "method": "median long-branch score", "evidence": "c.py"}]}]
            report = audit_operation_contract(w, rows, grounding, results)
            self.assertFalse(report["question_local_failures"])

    def test_cited_script_binding_satisfies_named_metric(self):
        """A concise candidate may cite the exact script that runs the metric."""
        with tempfile.TemporaryDirectory() as td:
            w = Path(td); (w / "analysis/scripts").mkdir(parents=True)
            (w / "analysis/scripts/compute.py").write_text(
                "subprocess.run(['phykit', 'long_branch_score', tree])\n"
            )
            rows = [{"question_id": "q1", "question": "What is the long branch score?"}]
            grounding = {"questions": [{
                "question_id": "q1", "metric_or_outcome": "long branch score",
                "method_or_estimator": "script", "unit_or_scale": "score",
            }]}
            results = [{"question_id": "q1", "status": "resolved", "candidates": [{
                "answer": "1.2", "method": "median score",
                "evidence": "analysis/scripts/compute.py",
            }]}]
            report = audit_operation_contract(w, rows, grounding, results)
            self.assertFalse(report["question_local_failures"])
            self.assertTrue(report["question_checks"][0]["candidate_cited_script_operation"])

    def test_cited_script_binding_strips_sentence_punctuation(self):
        with tempfile.TemporaryDirectory() as td:
            w = Path(td); (w / "analysis/scripts").mkdir(parents=True)
            (w / "analysis/scripts/compute.py").write_text(
                "subprocess.run(['phykit', 'long_branch_score', tree])\n"
            )
            rows = [{"question_id": "q1", "question": "What is the long branch score?"}]
            grounding = {"questions": [{"question_id": "q1"}]}
            results = [{"question_id": "q1", "status": "resolved", "candidates": [{
                "answer": "1.2", "method": "median score",
                "evidence": "Computed by analysis/scripts/compute.py.",
            }]}]
            report = audit_operation_contract(w, rows, grounding, results)
            self.assertFalse(report["question_local_failures"])

    def test_false_discovery_rate_is_not_external_operation(self):
        with tempfile.TemporaryDirectory() as td:
            w = Path(td); (w / "analysis/scripts").mkdir(parents=True)
            (w / "analysis/scripts/c.py").write_text("# DESeq2 Benjamini-Hochberg FDR\n")
            rows = [{"question_id": "q1", "question": "Use a Benjamini-Hochberg False Discovery Rate threshold of 0.05."}]
            grounding = {"questions": [{"question_id": "q1", "metric_or_outcome": "significance", "method_or_estimator": "DESeq2 BH FDR", "unit_or_scale": "genes"}]}
            results = [{"question_id": "q1", "status": "resolved", "candidates": [{"answer": "10", "method": "DESeq2 BH FDR", "evidence": "c.py"}]}]
            report = audit_operation_contract(w, rows, grounding, results)
            self.assertFalse(report["question_local_failures"])

    def test_explicit_library_must_bind_to_scripts_and_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            w = Path(td); (w / "analysis/scripts").mkdir(parents=True)
            (w / "analysis/scripts/c.py").write_text("import gseapy\n# GO_Biological_Process_2021\n")
            rows = [{"question_id": "q1", "question": "Using gseapy and GO_Biological_Process_2021, find the top process."}]
            grounding = {"questions": [{"question_id": "q1", "metric_or_outcome": "top GO term", "method_or_estimator": "gseapy enrichr", "unit_or_scale": "term"}]}
            results = [{"question_id": "q1", "status": "resolved", "candidates": [{"answer": "GO:1", "method": "gseapy GO_Biological_Process_2021", "evidence": "c.py"}]}]
            report = audit_operation_contract(w, rows, grounding, results)
            self.assertFalse(report["question_local_failures"])

    def test_explicit_library_fallback_is_local_failure(self):
        with tempfile.TemporaryDirectory() as td:
            w = Path(td); (w / "analysis/scripts").mkdir(parents=True)
            (w / "analysis/scripts/c.R").write_text("library(clusterProfiler); library(org.Hs.eg.db)\n")
            rows = [{"question_id": "q1", "question": "Using gseapy and GO_Biological_Process_2021, find the top process."}]
            grounding = {"questions": [{"question_id": "q1", "metric_or_outcome": "top GO term", "method_or_estimator": "gseapy enrichr", "unit_or_scale": "term"}]}
            results = [{"question_id": "q1", "status": "resolved", "candidates": [{"answer": "GO:1", "method": "clusterProfiler", "evidence": "c.R"}]}]
            report = audit_operation_contract(w, rows, grounding, results)
            self.assertIn("q1", report["question_local_failures"])

    def test_exact_r_ns_cannot_be_replaced_by_python_cr(self):
        with tempfile.TemporaryDirectory() as td:
            w = Path(td); (w / "analysis/scripts").mkdir(parents=True)
            (w / "analysis/scripts/c.py").write_text("dmatrix('cr(x, df=4)')\n")
            rows = [{"question_id": "q1", "question": "Fit lm(y ~ ns(x, df = 4)) and report the peak."}]
            grounding = {"questions": [{"question_id": "q1", "metric_or_outcome": "peak", "method_or_estimator": "R ns", "unit_or_scale": "value"}]}
            results = [{"question_id": "q1", "status": "resolved", "candidates": [{"answer": "1", "method": "Python cr spline", "evidence": "c.py"}]}]
            report = audit_operation_contract(w, rows, grounding, results)
            self.assertIn("q1", report["question_local_failures"])

    def test_frequency_ratio_field_is_not_external_operation(self):
        with tempfile.TemporaryDirectory() as td:
            w = Path(td); (w / "analysis/scripts").mkdir(parents=True)
            (w / "analysis/scripts/c.py").write_text("ratio = numerator / (numerator + denominator)\n")
            rows = [{"question_id": "q1", "question": "What frequency ratio maximizes the response?"}]
            grounding = {"questions": [{"question_id": "q1", "metric_or_outcome": "peak", "method_or_estimator": "ordinary ratio from input", "unit_or_scale": "frequency ratio"}]}
            results = [{"question_id": "q1", "status": "resolved", "candidates": [{"answer": "0.9", "method": "natural spline", "evidence": "c.py"}]}]
            report = audit_operation_contract(w, rows, grounding, results)
            self.assertFalse(report["question_local_failures"])


if __name__ == "__main__":
    unittest.main()

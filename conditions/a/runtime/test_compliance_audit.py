from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conditions.a.runtime.compliance_audit import compliance_audit


class ComplianceAuditMarkdownCitationTest(unittest.TestCase):
    def test_markdown_link_to_persisted_source_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "analysis/scripts/solve.py"
            source.parent.mkdir(parents=True)
            source.write_text("print(42)\n", encoding="utf-8")
            events = [{"event": {"item": {"command": "python analysis/scripts/solve.py"}}}]
            results = [{
                "question_id": "q1",
                "status": "resolved",
                "candidates": [{
                    "answer": "42",
                    "method": "computed",
                    "evidence": f"[analysis/scripts/solve.py]({source})",
                }],
            }]
            audit = compliance_audit(workspace, events, results)
        self.assertEqual(audit["fatal"], [])
        self.assertEqual(audit["question_local"], {})


if __name__ == "__main__":
    unittest.main()

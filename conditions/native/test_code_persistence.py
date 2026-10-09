from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conditions.native.runner import audit_and_enforce_code, solver_prompt


class NativeCodePersistenceTest(unittest.TestCase):
    def test_solver_prompt_always_contains_code_contract(self) -> None:
        prompt = solver_prompt({"question": "Compute the requested value."})
        self.assertTrue(prompt.startswith("Compute the requested value."))
        self.assertIn("analysis/scripts/", prompt)
        self.assertIn("At least one such source file must be persisted", prompt)

    def test_accepts_persisted_executed_and_cited_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "analysis/scripts/solve.py"
            source.parent.mkdir(parents=True)
            source.write_text("print(42)\n", encoding="utf-8")
            events = [{"event": {"item": {"command": "python analysis/scripts/solve.py"}}}]
            payload = {
                "status": "resolved",
                "candidates": [{
                    "answer": "42",
                    "method": "direct computation",
                    "evidence": "analysis/scripts/solve.py",
                }],
            }
            audit, sources = audit_and_enforce_code(workspace, events, payload, "q1")
            self.assertTrue(audit["executed_persisted_source"])
            self.assertEqual(sources, ["analysis/scripts/solve.py"])

    def test_rejects_unexecuted_source_even_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source = workspace / "analysis/scripts/solve.py"
            source.parent.mkdir(parents=True)
            source.write_text("print(42)\n", encoding="utf-8")
            payload = {"status": "unresolved", "candidates": []}
            with self.assertRaisesRegex(RuntimeError, "actual execution"):
                audit_and_enforce_code(workspace, [], payload, "q1")


if __name__ == "__main__":
    unittest.main()

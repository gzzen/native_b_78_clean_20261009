"""Structural checks that keep B an additive extension of Condition A."""
from __future__ import annotations

import unittest

from conditions.a import runner as condition_a
from conditions.b import components as b_components
from conditions.b import runner as condition_b


class BCompositionTests(unittest.TestCase):
    def test_b_imports_a_as_its_base_condition(self) -> None:
        self.assertIs(condition_b.BASE_CONDITION, condition_a)

    def test_b_additions_are_condition_local_components(self) -> None:
        self.assertIs(condition_b.b_components, b_components)
        self.assertEqual(
            condition_b.GROUNDING_FIELDS,
            (
                "data_object",
                "observation_unit",
                "comparison",
                "metric_or_outcome",
                "unit_or_scale",
                "denominator_or_population",
                "method_or_estimator",
            ),
        )

    def test_b_prompt_is_a_prompt_plus_b_extension(self) -> None:
        self.assertTrue(condition_b.SOLVER_PROMPT.startswith(condition_a.SOLVER_PROMPT.rstrip()))
        suffix = condition_b.SOLVER_PROMPT[len(condition_a.SOLVER_PROMPT.rstrip()):]
        self.assertIn("Condition B extension: selective semantic grounding", suffix)
        self.assertIn("storage row is not automatically an independent observation", suffix)
        self.assertIn("A-compatibility rule", suffix)
        self.assertIn("Preserve A's data object, filters, population", suffix)
        self.assertIn("audit of the executable", suffix)
        self.assertIn("outcome label such as a", suffix)
        self.assertIn("entity-level", suffix)
        self.assertIn("ordinary regression, logistic/ordered-logit model", suffix)
        self.assertIn("Operational priority (final)", suffix)
        self.assertIn("complete Condition-A-compatible analysis", suffix)
        self.assertIn("persist its ordinary reproducible computation as the baseline", suffix)
        self.assertIn("must not prevent a defensible A baseline", suffix)
        self.assertIn("preserve A's executable interpretation", suffix)
        self.assertIn("operation with a definition", suffix)
        self.assertIn("canonical derived", suffix)
        self.assertIn("complete metric identity", suffix)
        self.assertIn("lightweight operation lookup", suffix)
        self.assertIn("operation lookup before the", suffix)
        self.assertIn("command -v", suffix)
        self.assertIn("lookup non-fatal", suffix)
        self.assertIn("run it at most once", suffix)
        self.assertIn("exact existing input path", suffix)
        self.assertIn("use that syntax verbatim", suffix)
        self.assertIn("silently fall back to a primitive", suffix)
        self.assertIn("returned candidate's method", suffix)
        self.assertIn("3–6 significant digits", suffix)
        self.assertIn("human-readable form", suffix)
        self.assertIn("Aggregation-source fidelity", suffix)
        self.assertIn("same level and precision", suffix)
        self.assertIn("numeric path deterministic", suffix)

    def test_b_component_static_validation_runs(self) -> None:
        b_components.validate_component_prompt(condition_b.COMPONENT_PROMPT)

    def test_b_reuses_a_execution_and_compliance_components(self) -> None:
        self.assertIs(condition_b.prepare_workspace, condition_a.prepare_workspace)
        self.assertIs(condition_b.capture_opencode, condition_a.capture_opencode)
        self.assertIs(condition_b.compliance_audit, condition_a.compliance_audit)
        self.assertIs(condition_b.validate_native_result, condition_a.validate_native_result)

    def test_b_reuses_a_capsule_definition(self) -> None:
        self.assertEqual(condition_b.capsules(), condition_a.caps())


if __name__ == "__main__":
    unittest.main()

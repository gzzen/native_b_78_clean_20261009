#!/usr/bin/env python3
# Path: conditions/b/components.py
# Purpose: Compose lightweight Condition-B selective grounding onto Condition A.

from __future__ import annotations

from pathlib import Path


def compose_solver_prompt(a_solver_prompt: str, component_prompt: Path) -> str:
    """Append B-only semantic-grounding instructions to the complete A prompt."""
    return (
        a_solver_prompt.rstrip()
        + "\n\n"
        + component_prompt.read_text(encoding="utf-8").strip()
        + "\n"
    )


def validate_component_prompt(component_prompt: Path) -> None:
    """Static guard against accidentally reintroducing the heavy B-v3 workflow."""
    text = component_prompt.read_text(encoding="utf-8")

    required = (
        "analysis/grounding.json",
        "data_object",
        "observation_unit",
        "comparison",
        "metric_or_outcome",
        "unit_or_scale",
        "denominator_or_population",
        "method_or_estimator",
        "grounding_status",
        "selection_rationale",
        "scientific observation unit",
        "Do not discard files, rows, samples",
        "Preserve\nA's population",
        "mathematical\nminimum-size requirement",
        "A-compatibility rule",
        "audit of the executable",
        "outcome label such as a",
        "entity-level",
        "ordinary regression, logistic/ordered-logit model",
        "Operational priority (final)",
        "complete Condition-A-compatible analysis",
        "preserve A's executable interpretation",
        "operation with a definition",
        "canonical derived",
        "Preserve the complete metric identity",
        "lightweight operation lookup",
        "operation lookup before the",
        "command -v",
        "lookup non-fatal",
        "run it at most once",
        "exact existing input path",
        "use that syntax verbatim",
        "silently fall back to a primitive",
        "returned candidate's method",
        "3–6 significant digits",
        "human-readable form",
        "ordinary statistical-test p-values",
        "machine-readable ASCII `e`",
        "domain-specific named operation",
        "Answer exactly the quantity requested",
        "primary `answer` field",
        "directly judgeable",
        "classified, labeled, tested",
        "ineligible/missing outcome",
        "eligible denominator",
        "superclass",
        "qualified\nlabels",
        "unique labels and schema\ndefinitions",
        "raw string",
        "Likely X",
        "superclass X",
        "exact literal label",
        "component ratio `A:B`",
        "normalized fraction",
        "requested scale",
        "frequency ratio” describes the frequency",
        "normalized share",
        "colon inside a strain",
        "ontology, pathway, gene-set",
        "top-ranked term and identifier",
        "broader parent",
        "mapping is unavailable",
        "explicitly names a software package",
        "named implementation in the substantive computation",
        "lookup-only mention",
        "executable code, a named statistical function",
        "exact function in its named environment",
        "approximate analogue",
        "implementation-specific semantics",
        "Aggregation-source fidelity",
        "lower-level observation values",
        "pre-aggregated summary",
        "same level and precision",
        "rank- or order-sensitive aggregates",
        "numeric path deterministic",
        "fixed aggregation routine",
        "tie handling",
        "shared statistical design",
        "interactions” without enumerating",
        "do not add every pairwise product",
        "exact formula and reference coding",
    )

    for token in required:
        if token not in text:
            raise ValueError(f"missing required B grounding token: {token}")

    forbidden = (
        "exactly 2 or 3 plausible semantic interpretations",
        "grounding_consistency.py",
        "rerun only the affected computation",
        "correct the analysis to follow the selected grounding",
    )

    for token in forbidden:
        if token in text:
            raise ValueError(f"heavy B-v3 behavior unexpectedly present: {token}")

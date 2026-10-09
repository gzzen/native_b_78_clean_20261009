"""Deterministic, post-generation audit for named derived operations.

This is a protocol check, not an evaluator or a second analysis.  It prevents a
resolved answer from claiming a score/index/rate/ratio when the capsule's own
reproducible scripts contain no evidence of the named operation or its formula.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

_EVIDENCE_WORDS = re.compile(
    r"(?:command\s+-v|--help|--version|version|formula|equation|patristic|"
    r"long[_ -]?branch[_ -]?score|phykit|scipy\.stats|deseq2|estimateDispersions|"
    r"adjusted|shrink)",
    re.I,
)
_PRIMITIVE_WORDS = re.compile(r"\b(?:raw|terminal|branch)\s+(?:length|count|mean|median)\b", re.I)
_TOOL_PATTERNS = {
    "gseapy": r"\bgseapy\b",
    "GO_Biological_Process_2021": r"go[_ -]?biological[_ -]?process[_ -]?2021",
    "DESeq2": r"\bdeseq2\b",
    "PhyKIT": r"\bphykit\b",
}


def _named_terms(question: str) -> set[str]:
    """Return derived-operation tokens explicitly requested by the question."""
    normalized = question.casefold().replace("–", "-").replace("—", "-")
    # ``function`` is a grammatical qualifier (for example, “PhyKIT's
    # tree_length function”), not itself an operation name.  The named tool
    # and the executable script provide the binding; treating the generic word
    # as a required candidate token creates false failures for valid methods.
    terms = set(re.findall(r"\b(?:score|index|rate|ratio)\b", normalized))
    # Common statistical ratios are calculated by the A-compatible method,
    # not looked up as domain-specific executable operations.
    if re.search(r"\b(?:odds|risk|rate|mean|percent|fold|relative|frequency|sample|read|allele|variant|mutation|population|proportion|group)\s+ratio\b|\bratio\s+of\b", normalized):
        terms.discard("ratio")
    # FDR/DESeq2 language names a correction criterion, not an external
    # executable called a rate.
    if re.search(r"(?:false\s+discovery\s+rate|\bfdr\b|benjamini[- ]hochberg)", normalized):
        terms.discard("rate")
    if re.search(r"long\s+branch\s+scores?", normalized):
        terms.add("long branch score")
    # A ratio used as an experimental group/condition (rather than a derived
    # numerical operation) should not trigger an operation contract.
    if "ratio" in terms and (
        re.search(r"\bwhich\s+ratio\b", normalized)
        or re.search(r"\b(?:at|among|across)\s+(?:various|different|mixed)?\s*ratios\b", normalized)
    ) and not re.search(r"\bratio\s+(?:of|between)\b", normalized):
        terms.discard("ratio")
    return terms


def _requested_tools(question: str) -> list[str]:
    """Return explicitly named tools/libraries, not generic method words."""
    return [name for name, pattern in _TOOL_PATTERNS.items() if re.search(pattern, question, re.I)]


def audit_operation_contract(
    workspace: Path,
    expected_rows: Sequence[Mapping[str, Any]],
    grounding: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return question-local failures for unresolved named-operation binding."""
    script_dir = workspace / "analysis/scripts"
    scripts = sorted(path for path in script_dir.rglob("*") if path.is_file()) if script_dir.is_dir() else []
    text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in scripts)
    by_id = {str(item.get("question_id")): item for item in results}
    qrows = {str(row["question_id"]): row for row in expected_rows}
    grecords = {str(item.get("question_id")): item for item in grounding.get("questions", [])}
    failures: dict[str, str] = {}
    checks: list[dict[str, Any]] = []
    for qid, row in qrows.items():
        result = by_id.get(qid, {})
        g = grecords.get(qid, {})
        question_text = str(row.get("question", ""))
        named_terms = _named_terms(question_text)
        requested_tools = _requested_tools(question_text)
        exact_impl_conflicts = []
        if (
            re.search(r"\bns\s*\(", question_text, re.I)
            and re.search(r"\bcr\s*\(", text, re.I)
            and not re.search(r"\bns\s*\(", text, re.I)
        ):
            exact_impl_conflicts.append("R ns replaced by cr")
        # Only question wording may introduce an operation requirement;
        # grounding describes the claimed analysis and cannot manufacture one.
        named = bool(named_terms)
        resolved = result.get("status") == "resolved"
        evidence = bool(_EVIDENCE_WORDS.search(text))
        primitive_only = bool(_PRIMITIVE_WORDS.search(text)) and not bool(
            re.search(r"long[_ -]?branch[_ -]?score|patristic|phykit", text, re.I)
        )
        candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
        candidate = candidates[0] if candidates and isinstance(candidates[0], Mapping) else {}
        candidate_text = " ".join(str(candidate.get(field, "")) for field in ("answer", "method", "summary", "evidence"))
        candidate_norm = (
            candidate_text.casefold()
            .replace("–", "-")
            .replace("—", "-")
            .replace("-", " ")
            .replace("_", " ")
        )
        missing_terms = [term for term in sorted(named_terms) if term not in candidate_norm]
        # A candidate may cite the exact persisted implementation without
        # repeating its operation name in prose. Treat that as equivalent
        # evidence only when the cited script contains the named operation;
        # this avoids penalising concise, otherwise faithful answers.
        cited_script_operation = False
        cited_paths = [
            raw.rstrip(".,;:!?)]}\u201d'\"")
            for raw in re.findall(r"analysis/scripts/([A-Za-z0-9_.-]+)", candidate_text)
        ]
        if cited_paths and "long branch score" in named_terms:
            cited_script_operation = any(
                re.search(r"phykit[^\n]*long[_ -]?branch[_ -]?score|long[_ -]?branch[_ -]?score", script_text, re.I)
                for script_text in (
                    next((path.read_text(encoding="utf-8", errors="replace") for path in scripts
                          if path.name == name), "")
                    for name in cited_paths
                )
            )
            if cited_script_operation:
                missing_terms = [term for term in missing_terms if term != "long branch score"]
        missing_tools = [
            name for name in requested_tools
            if not re.search(_TOOL_PATTERNS[name], text, re.I)
            or not re.search(_TOOL_PATTERNS[name], candidate_text, re.I)
        ]
        fallback_conflict = (
            "gseapy" in requested_tools
            and bool(re.search(r"\bclusterprofiler\b|org\.hs\.eg\.db", text, re.I))
        )
        # A long-branch score is specifically a PhyKIT/patristic operation;
        # mentioning only a generic score is insufficient.
        if (
            "long branch score" in named_terms
            and not cited_script_operation
            and not re.search(r"long\s+branch\s+score|phykit|patristic", candidate_norm)
        ):
            missing_terms.append("long branch score operation")
        conflict = resolved and (
            (named and (not evidence or primitive_only or bool(missing_terms)))
            or bool(missing_tools)
            or fallback_conflict
            or bool(exact_impl_conflicts)
        )
        if conflict:
            failures[qid] = "named derived operation is not bound to candidate method/evidence"
        checks.append({
            "question_id": qid,
            "named_derived_operation": named,
            "resolved_candidate": resolved,
            "operation_evidence": evidence,
            "primitive_only_signal": primitive_only,
            "named_terms": sorted(named_terms),
            "candidate_missing_terms": missing_terms,
            "candidate_cited_script_operation": cited_script_operation,
            "requested_tools": requested_tools,
            "candidate_missing_tools": missing_tools,
            "requested_tool_fallback_conflict": fallback_conflict,
            "exact_implementation_conflicts": exact_impl_conflicts,
            "passed": not conflict,
        })
    report = {
        "checker": "b_additive_named_operation_contract_v1",
        "script_count": len(scripts),
        "question_checks": checks,
        "question_local_failures": failures,
        "passed": not failures,
    }
    (workspace / "analysis/operation_contract.json").write_text(
        __import__("json").dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report

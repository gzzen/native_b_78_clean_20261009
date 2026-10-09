<!-- Path: `conditions/b/protocol.md`; Purpose: define B as an additive extension of A. -->
# B-v3-contrastive-grounding-selection protocol

B imports and retains Condition A in full: one coherent capsule-level executable
workflow, A's solver-visible requirements, persisted task code, isolation, and
graded compliance controller. B then adds only the following components:
contrastive semantic selection, a locked grounding record, a compact truthful
execution record, and a deterministic grounding/execution consistency check.

Each capsule attempt receives its own workspace-local `tmp/` tree. The
controller binds `TMPDIR`, `TMP`, `TEMP`, and common scientific-library cache
variables to that tree before starting Codex. Literal `/tmp`, `/var/tmp`, and
temporary paths owned by another capsule remain isolation violations.

B first performs the complete A-compatible computation as the baseline. It then records 2–3 evidence-backed semantic interpretations only where ambiguity is genuine, and locks one interpretation into data object/filter, comparison, metric/outcome, unit/scale, denominator/population, and method/estimator. The compact grounding audits the baseline; an optional B annotation must not erase a defensible A candidate. After analysis, an optional lightweight execution record and deterministic consistency check may be compared with the locked grounding for resolved questions. A clear mismatch is recorded as a B question-local issue; no semantic retry or candidate erasure is performed. This is not a long plan, reflection, retry, multihead, or prescribed workflow. The default formal runner covers all 59 capsules and 205 questions in one fresh call per capsule; an explicit short-ID subset is supported for diagnostics.

For model-based differential analysis, method/estimator is binding at design level: it records the exact formula, covariates, coefficient/contrast, adjustment, and shrinkage. A package or test name without its design/estimator specification is insufficient. Covariates supported by supplied metadata must be retained, while unsupported covariates must not be invented; unresolved design ambiguity remains unresolved rather than being guessed.

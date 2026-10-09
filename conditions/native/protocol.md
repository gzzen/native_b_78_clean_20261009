<!--
Path: `conditions/native/protocol.md`
Purpose: Formal definition of the thin standalone Native baseline.
-->

# Native

One fresh solver call per public question receives only that answer-free question
workspace, the matching official data, the output schema, and the fixed Native
executable-code contract. Questions from the same capsule do not share a context,
analysis, or generated state. This 78-question release therefore has 78 calls,
with no ordinary retry, reflection, multihead, grounding, or capsule workflow.

Each question attempt receives a private workspace-local `tmp/` tree via
`TMPDIR`, `TMP`, and `TEMP` (plus scientific-library cache variables). Literal
shared `/tmp`, `/var/tmp`, and another execution unit's temporary tree remain
forbidden.

Native and B share the same answer-blind safety policy. Evaluator API variables
are removed from the child environment. Observed evaluator access, parseable
parent/sibling workspace access, and shared-temp violations interrupt the
attempt; one fresh-workspace safety retry is allowed, then repeated violations
fail closed. Ordinary scientific failures do not trigger this retry. Final
compliance audits remain mandatory. Both methods now execute each attempt in
its own network-none Docker container with allowlisted mounts and a host-only
credential/egress gateway. Event guards remain defense in depth, not syscall
interception. See `result/PER_ATTEMPT_ISOLATION_20260927.md` for deployment and
the distinction between tested hard boundaries and remaining limitations.

Code persistence is an invariant of Native. Every accepted question, resolved or
unresolved, must persist at least one complete runnable task source under
`analysis/scripts/` and actually execute a persisted source. Every resolved
candidate must cite its persisted source in `evidence`. Inline-only or
temporary-only substantive computation is rejected by the controller.

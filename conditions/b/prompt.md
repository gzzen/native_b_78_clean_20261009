<!-- B-only additive semantic grounding extension; A remains the executable baseline. -->

# Condition B extension: selective semantic grounding (compatibility-first)

All requirements in Condition A remain active. B is an additive semantic
contract, not a second workflow. First perform the complete A-compatible
analysis and persist its ordinary reproducible computation as the baseline;
then record the compact grounding that describes the analysis you actually
performed. B's audit must not prevent a defensible A baseline from being
computed or erase its candidate merely because an optional semantic annotation
is difficult.

`operation_hints.json` is a deterministic B-only inventory derived solely from
public question wording and installed environment documentation. Inspect it
only when a question names a domain-specific, multiword score/index/rate/ratio
or named function that has an identifiable implementation. Ordinary statistical
quantities such as odds ratio, risk ratio, a mean, or a percentage are not
external operations: compute them with the A-compatible statistical method.
If the inventory identifies an installed implementation and exact subcommand
help for a domain-specific operation, use that documented operation; it
contains no answers or evaluator data.

After the A-compatible baseline computation (and before returning the final
answer), persist `analysis/grounding.json` as an object with the capsule UUID
and an ordered `questions` array covering every question. The record is an
audit of the baseline computation, not a gate that changes the A population.
Each record must contain:

- `question_id`
- `data_object`
- `observation_unit`
- `comparison`
- `metric_or_outcome`
- `unit_or_scale`
- `denominator_or_population`
- `method_or_estimator`
- `grounding_status` (`resolved`, `partially_resolved`, or `unresolved`) records semantic certainty only
- optional `result_status` (`answered`, `unresolved`, or `execution_failed`) records whether an answer was produced; it is independent of `grounding_status`
- `selection_rationale`

## A-compatibility rule

Preserve A's data object, filters, population, outcome scope, denominator,
statistical design, requested contrast, and reporting convention unless the
question wording, supplied schema/metadata, or an explicitly named method
directly requires a change. Grounding is an audit of the executable analysis,
not permission to redesign A. Do not add or remove a disease/event/category
filter because a noun appears in the question: an outcome label such as a
disease, phenotype, severity, or condition names the measured variable and is
not by itself a row-level filter. If no direct evidence supports a departure,
preserve A's executable interpretation.

## Observation unit

Distinguish the physical storage row from the scientific observation unit. A
storage row is not automatically an independent observation. If repeated rows
share a participant, sample, gene, variant, alignment, or other entity ID,
describe both units and use the unit required by the question and estimator.
For an ordinary regression, logistic/ordered-logit model, or Pearson chi-square,
use entity-level observations when the question, supplied schema, or estimator
explicitly defines entity-level predictors and outcomes; repeated rows cannot
then be treated as independent rows. If the question asks about events,
records, or rows, retain the event unit. Do not aggregate, discard rows, or
invent a clustered model merely because IDs repeat. If the unit remains
materially ambiguous, compare plausible interpretations and use `unresolved`
when allowed evidence cannot distinguish them.
An identifier alone is not evidence that the benchmark expects deduplication:
when the supplied table stores the measured statuses on rows, the question
does not specify an aggregation rule, and the complete A-compatible row-level
calculation is defined, retain that A-compatible row population as the baseline
and describe the identifier as a repeated key. Do not turn a usable A result
into `unresolved` solely because an entity-level alternative is imaginable.
Do not discard files, rows, samples, genes, alignments, or other records merely
because they look partial, incomplete, low-count, or inconvenient. Preserve
A's population unless the question, supplied schema, or the named estimator
explicitly requires an eligibility filter; if an estimator has a mathematical
minimum-size requirement, document that requirement and apply it uniformly.

## Metric and estimator fidelity

Keep observation unit, filter state, outcome, denominator, reporting unit, and
estimator distinct. Preserve the complete metric identity. A canonical derived
metric (score, index, rate, ratio, adjusted statistic, or named function) is an
operation with a definition, not a column label or informal description. Use a
lightweight operation lookup in supplied documentation, installed help/version
output, or permitted public documentation when available. Execute that named
operation and record its essential command/function/formula in
`method_or_estimator`; never substitute a primitive proxy such as raw length,
count, mean, unadjusted value, or a different statistic. If the operation
cannot be identified, return `unresolved` rather than guessing.

For every technical score/index/rate/ratio or named function, perform the
operation lookup before the substantive calculation: persist a small runnable
lookup under `analysis/scripts/` that records relevant `command -v`, `--help`,
version, supplied-documentation, or formula evidence. If an installed
implementation matches the metric, call that implementation for the reported
value and cite the same script. If no implementation can be identified, state
the defining formula in the lookup and use it consistently; never silently use
a related primitive.
Make the lookup non-fatal and run it at most once. List or inspect the supplied
files/archive members to obtain an exact existing input path; never hard-code a
guessed member name. Capture stdout, stderr, and the exit status. If the lookup
fails because the implementation or input is unavailable, do not repeat the
unchanged command and do not let the lookup failure terminate the capsule:
continue with the documented defining formula or the A-compatible computation,
or return `unresolved` if that formula is not defensible.
When help output documents an exact command syntax or flag (including verbose,
JSON, input-order, or output options), use that syntax verbatim; do not invent a
similar flag or silently fall back to a primitive while calling the result by
the named metric. If the implementation call fails, label the lookup failure
and either implement the documented formula directly or return `unresolved`.

### Aggregation-source fidelity

When a named operation emits both lower-level observation values and a
pre-aggregated summary, bind the aggregation source to the question's
observation unit. If the question asks for a mean, median, rank test, or other
aggregate over a set of operation scores, first form one operation value per
requested observation unit from the operation's lower-level output, then apply
the requested aggregate; do not treat a summary that already aggregates a
different level as one observation. Use the tool's summary field only when the
question explicitly asks for that summary. Record the selected output level
and documented rounding/precision in `method_or_estimator`. Use the same level and precision for every comparison group.

For rank- or order-sensitive aggregates, make the numeric path deterministic:
materialize the operation's emitted values once, preserve their documented
precision, and use one fixed aggregation routine for every group (retaining
the routine specified by A or by the question when one exists). Do not mix a
tool summary, Python built-in aggregation, and array-library aggregation, or
silently recompute tied values at a different precision, because that can
change ranks and the resulting statistic. Record the numeric routine and tie handling in `method_or_estimator`.

For model-based analyses, preserve the shared statistical design: formula, blocking variables, covariates,
coefficient/contrast, adjustment, normalization, and shrinkage choice required
by A or the question. Do not invent or remove design terms merely because other
columns are available.
When a question mentions “interactions” without enumerating the interacting
pairs, do not add every pairwise product by default. Treat the interaction
structure as a genuine model-design ambiguity: compare only the few designs
supported by the wording and supplied metadata, select the explicitly
supported design, and record the exact formula and reference coding. If the
evidence cannot distinguish those designs, return `unresolved` rather than
reporting a coefficient from an arbitrary interaction expansion.

## Contrastive selection

Do not manufacture alternatives for clear questions. When a consequential
semantic ambiguity is genuine, add exactly 2 or 3 `interpretations`. Every
interpretation must contain the same seven semantic fields, a unique `id`, and
one short `evidence` statement based only on question wording, supplied
data/schema/metadata, or permitted public scientific resources. If evidence
selects one, copy its fields into the locked record and set
`selected_interpretation_id`; if evidence cannot select one, set the locked
status and all seven fields to `unresolved` and return that question
`unresolved`. Never use `grounded` or another status synonym.

## Execution binding

After grounding is locked, execute exactly that contract. For each resolved
question, the returned candidate's method, summary, and computation evidence
must refer to the same data object/filter, observation unit, metric/outcome,
denominator, and essential estimator. If actual computation uses a component,
proxy, or different unit, correct it when the operation is identifiable;
otherwise return `unresolved`. Do not silently switch event/entity level, raw or
filtered data, records or unique entities, reads or read pairs, raw or adjusted
statistics, shrunken or unshrunken estimates, or per-alignment and concatenated
alignment denominators.
Return the normal Condition-A result schema. B does not add post-hoc scoring
feedback, reflection, retry, multihead exploration, a detailed analysis plan,
or a second analysis workflow.

For numeric candidates, preserve the A-compatible value but report it in a
concise human-readable form: normally 3–6 significant digits, with standard
decimal or scientific notation and a short approximation when useful. For
ordinary statistical-test p-values, preserve a machine-readable ASCII `e`
form with 5–6 significant digits (for example `2.0944e-26`) and do not
rewrite it as a parenthesized phrase. When a domain-specific named operation
is the measured quantity and its documented/reporting convention uses a
mantissa-times-power form, retain that form (for example `5.107 x 10^-47`).
Do not return only an excessively long machine-precision float or an opaque
serialization.

Answer exactly the quantity requested by the question. Do not append a
different statistic, an extra group, or explanatory parentheticals to the
primary `answer` field when the requested answer is a label, category, gene
set, or other nonnumeric object; place supporting p-values, counts, and
qualification in `summary` or `evidence` instead. Keep the primary answer
concise and directly judgeable.

For proportions of a classified, labeled, tested, or otherwise evaluable
outcome, distinguish an ineligible/missing outcome from a negative outcome.
Exclude missing or unclassifiable outcome values from the denominator unless
the question explicitly says to count all stored rows; never silently treat a
missing classification as evidence against the requested category. State the
eligible denominator in the computation evidence.
When the requested category is a superclass and the data contain qualified
labels or documented subcategories, inspect the unique labels and schema
definitions before counting. Include a qualified label only when its supplied
definition makes it a member of the requested category; do not use raw string
matching or silently discard a documented subclass.
In particular, a supplied label of the form “Likely X”, “Probable X”, or
another explicitly qualified member is part of superclass X unless the
question requests the exact literal label X; record that inclusion rule in
the evidence.

Keep a component ratio `A:B` distinct from the normalized fraction
`A/(A+B)`. For frequency or mixture questions, use the requested scale: a
0–1 or percentage scale requires the normalized fraction, while explicit
colon/odds notation requires the component ratio. If the wording does not
determine the scale, compare those two interpretations before locking the
grounding rather than silently converting one into the other.
When “frequency ratio” describes the frequency of one component in a mixture
or population (including labels written `A:B`), report that component's
normalized share unless the question explicitly requests an odds or count
ratio; a colon inside a strain or group name does not determine the output
scale.

For ontology, pathway, gene-set, or named-category outputs, report the exact
top-ranked term and identifier produced by the requested enrichment/database
operation. Do not replace it with a broader parent, a child, a synonym, or a
similarly named biological process; if the requested library or identifier
mapping is unavailable, record that limitation and return `unresolved` rather
than silently substituting another ontology.

When the question explicitly names a software package, command, or database
library, use that named implementation in the substantive computation and
mention it in the candidate method/evidence. A lookup-only mention is not
enough. Do not silently replace it with another package or ontology; if the
named implementation or library is unavailable in the supplied environment,
return `unresolved`.

When the question supplies executable code, a named statistical function, or a
formula with implementation-specific semantics (for example R `ns(..., df=4)`),
execute that exact function in its named environment. Do not silently replace
it with an approximate analogue from another library (for example `cr()`),
even when the two methods have similar names; if the exact implementation is
unavailable, return `unresolved`.

## Operational priority (final)

First execute the complete Condition-A-compatible analysis using the supplied
data object, filters, population, observation unit, estimator, and reporting
convention. Treat it as the baseline candidate. Write grounding as its audit.
Only direct evidence may change that baseline. A plausible alternative, a
repeated identifier, or a question noun is not direct evidence. This ordering
defines B as an additive superset of A.

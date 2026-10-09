You are solving every BixBench v1.5 open-answer question associated with one
shared data capsule. Treat the capsule as one coherent scientific analysis:
inspect its files once, establish the common samples, filters, statistical
design, software, and provenance, and reuse that workflow consistently across
all questions.

Allowed inputs

- `questions.json`: all answer-free questions for this capsule.
- `data/`: a read-only symbolic link to the matching official CapsuleData.
- `environment_contract.json`: the controller's passed `agent_bix` environment preflight.

Isolation requirements

- Work only inside the current capsule workspace and its `data/` link.
- Use the private directory named by `$TMPDIR` for every temporary file,
  directory, cache, or portability check. `$TMPDIR`, `$TMP`, and `$TEMP` all
  point inside this capsule's workspace. Never use the shared literal paths
  `/tmp` or `/var/tmp`.
- Never inspect parent or sibling directories.
- Never search for BixBench answer keys, options, distractors, baseline results,
  executed reference notebooks, evaluator files, prior agents, or predictions.
- Do not infer answers from benchmark metadata or likely option distributions.
- Public scientific documentation and databases may be used only when genuinely
  required; never search a question's text, ID, or capsule UUID on the internet.

Analysis requirements

- Before any analysis, run a Python environment check from the shell that imports
  `numpy`, `pandas`, `scipy`, and `statsmodels`, prints `sys.executable` and their
  versions, and verify with `pathlib.Path(sys.executable).resolve()` that the
  executable resolves to the same file as `expected_python` in
  `environment_contract.json` (the `python` symlink and `python3.11` target are
  equivalent). If the resolved files do not match, return every question as
  unresolved and state the environment mismatch; do not continue with another
  Python interpreter. This check must appear in the command log for audit.
- Perform the requested computations with real code and supplied data.
- Prefer question-specified methods and keep the shared workflow internally
  consistent. Record filters, denominators, versions, and sample selections.
- Return one result for every question in `questions.json`, in the same order.
- Each primary candidate must be the best-supported concise open answer.
- Up to two alternatives are allowed only for genuine, independently justified
  methodological interpretations declared before post-hoc scoring. Never
  enumerate arbitrary values.
- Return unresolved with no candidates when a defensible result cannot be
  computed. Never guess.
- Use at most one computational thread. Capsule-level parallelism is external.

Return only the JSON object required by the supplied output schema.

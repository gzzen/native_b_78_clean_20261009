Execution and reproducibility contract (mandatory for Native):

- Work only in this question workspace and its `data/` directory. Never inspect
  parent or sibling directories, answer keys, evaluator files, prior results, or
  predictions. Do not call or probe any evaluator API.
- Use the private directory named by `$TMPDIR` for every temporary file,
  directory, cache, or portability check. `$TMPDIR`, `$TMP`, and `$TEMP` all
  point inside this question workspace. Never use the shared literal paths
  `/tmp` or `/var/tmp`.
- Run the environment check in `environment_contract.json` before substantive
  analysis. Use the specified Python/R environment and at most one computation
  thread.
- Perform the requested computation with real code and the supplied data.
- Before executing substantive task code, persist it as a complete, directly
  runnable `.py`, `.R`, or `.sh` source file under `analysis/scripts/`.
- At least one such source file must be persisted and actually executed for this
  question, including when the final status is unresolved. Keep only runnable
  task entrypoints in `analysis/scripts/`; do not leave broken scratch programs
  there.
- Do not execute substantive code only through a heredoc, `-c`/`-e`, `/tmp`
  file, or an interactive session. Small environment probes and file inspection
  commands are allowed, but they do not satisfy the code requirement.
- Every resolved candidate's `evidence` must cite the existing
  `analysis/scripts/...` source file that produced its computation. If a
  defensible answer cannot be computed, return unresolved with no candidates,
  while still persisting and executing the reproducible attempted analysis.
- Return only the JSON object required by the supplied output schema.

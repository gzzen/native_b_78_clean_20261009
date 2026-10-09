<!--
Path: `conditions/native/README.md`
Purpose: Standalone package entry point for the Native baseline.
-->

# Native

Question-level baseline. Each of the 78 public questions receives an independent
workspace and OpenCode v2 context. The solver prompt contains the public question and
the fixed executable-code contract in `prompt.md`, but no capsule workflow. The
runner makes 78 independent agent runs, defaulting to 2 concurrent workers
(maximum 59). The default model is `deepseek/deepseek-flash` with high reasoning
effort; real credentials stay in the host gateway. Every
accepted run must persist and execute task code under `analysis/scripts/`; a
resolved candidate must cite that source. The public questions and capsule
inputs are loaded from sibling `../../data/`.

Run with `python conditions/native/runner.py --execute`.

See [OpenCode deployment](../../OPENCODE_DEEPSEEK.md) for Linux runtime,
credentials, diagnostics and fresh output paths.

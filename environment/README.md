# Environment

- `agent_bix_environment.yml`: portable conda/mamba environment export without build strings.
- `agent_bix_explicit_linux-64.txt`: exact Linux-64 package lock from the source environment.
- `runtime_versions.json`: model, effort, timeout, Python, R, and Codex CLI provenance.

Create the portable environment with `mamba env create -f environment/agent_bix_environment.yml`. The exact lock is platform-specific and can be recreated with `mamba create -n agent_bix --file environment/agent_bix_explicit_linux-64.txt`.

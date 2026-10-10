# Environment

- `agent_bix_environment.yml`: portable conda/mamba environment export without build strings.
- `agent_bix_explicit_linux-64.txt`: exact Linux-64 package lock from the source environment.
- `codex_lock.json`: the pinned Linux Codex CLI and code-mode host release archives, with sha256; install them with `scripts/install_codex.sh`.
- `runtime_versions.json`: provenance of the historical source environment.

Create the portable environment with `mamba env create -f environment/agent_bix_environment.yml`. The exact lock is platform-specific and can be recreated with `mamba create -n agent_bix --file environment/agent_bix_explicit_linux-64.txt`.

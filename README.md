# Native/B 78-question generation code — clean checkout

Answer-blind generation code only. This directory contains no generated
output, reports, frozen predictions, diagnostics, run workspaces, or staging
files. It also contains no answer key or scorer. Score frozen predictions
later in the private evaluation bundle, never from this checkout.

- `conditions/`: Native and B runners, prompts, schemas and protocols
- `common/`: isolation boundary, host gateway, Codex adapter and tests
- `environment/`: `agent_bix` Conda environment specification and the pinned Codex lock
- `scripts/`: run, continue, progress, export and recovery tools
- `protocols/`: experiment substrate notes

Fresh generation uses a pinned Codex CLI (`codex-cli 0.158.0`) through an
OpenAI Responses relay (`openai-relay/gpt-5.6-sol`), on a Linux host with
Docker and the `agent_bix` environment. Each attempt gets a fresh, isolated
Codex home; nothing from the host's Codex setup is used. See
[CODEX.md](CODEX.md) for the server runbook, credentials, diagnostics and run commands.
The earlier OpenCode/DeepSeek pipeline is preserved at the git tag
`opencode-final`.

Restore the public `data/` tree as a sibling of this directory before
generating; the code resolves `data/` relative to this directory's parent.
Runs create `analysis/`, `reports/`, `results/`, `runs/` and `logs/` here,
and `.gitignore` keeps them out of Git.

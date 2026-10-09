# Native/B 78-question generation code — clean checkout

Answer-blind generation code only. This directory contains no generated
output, reports, frozen predictions, diagnostics, run workspaces, or staging
files. It also contains no answer key or scorer. Score frozen predictions
later in the private evaluation bundle, never from this checkout.

- `conditions/`: Native and B runners, prompts, schemas and protocols
- `common/`: isolation boundary, host gateway, OpenCode adapter and tests
- `environment/`: `agent_bix` Conda environment specification
- `scripts/`: run, continue, progress, export and recovery tools
- `protocols/`: experiment substrate notes

Fresh generation uses OpenCode v2 with either the direct DeepSeek API
(`deepseek/deepseek-flash`) or an OpenAI Responses relay
(`openai-relay/gpt-5.6-sol`). See [OPENCODE_DEEPSEEK.md](OPENCODE_DEEPSEEK.md)
for setup, credentials, diagnostics and run commands.

Restore the public `data/` tree as a sibling of this directory before
generating; the code resolves `data/` relative to this directory's parent.
Runs create `analysis/`, `reports/`, `results/`, `runs/` and `logs/` here,
and `.gitignore` keeps them out of Git.

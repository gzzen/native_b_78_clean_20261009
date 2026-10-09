# Path: conditions/b/agent_bix_bash_env.sh
# Purpose: Condition-local public shell environment support for B execution.
if [ -n "${CONDA_PREFIX:-}" ]; then
  export PATH="${CONDA_PREFIX}/bin:${PATH}"
fi

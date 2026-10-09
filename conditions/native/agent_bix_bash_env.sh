# Path: public/agent_bix_bash_env.sh
# Purpose: Public runtime support source.
if [ -n "${CONDA_PREFIX:-}" ]; then
  export PATH="${CONDA_PREFIX}/bin:${PATH}"
fi

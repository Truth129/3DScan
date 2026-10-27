#!/usr/bin/env bash
# Run one verification command with explicitly isolated runtime/model caches.
set -euo pipefail
umask 077
runtime="${1:-}"
if [[ "$runtime" != baseline && "$runtime" != candidate ]] || (( $# < 2 )); then
  echo 'Usage: benchmark-stack.sh baseline|candidate gpu|segment|interactive|postprocess|compare|provenance ...' >&2
  exit 2
fi
shift
source "$(dirname -- "${BASH_SOURCE[0]}")/rocm10-env.sh"
cd "$SCAN3D_ROOT"
interpreter="$SCAN3D_ROOT/.venv-rocm10-py314/bin/python"
if [[ "$runtime" == baseline ]]; then
  interpreter="$SCAN3D_ROOT/.venv/bin/python"
  export PATH="$SCAN3D_ROOT/.venv/bin:$PATH"
  export TOTALSEG_HOME_DIR="$SCAN3D_EXPERIMENT_ROOT/baseline-totalsegmentator"
  export XDG_CACHE_HOME="$SCAN3D_EXPERIMENT_ROOT/baseline-cache"
  export XDG_CONFIG_HOME="$SCAN3D_EXPERIMENT_ROOT/baseline-config"
  export MIOPEN_USER_DB_PATH="$XDG_CACHE_HOME/miopen-db"
  export MIOPEN_CUSTOM_CACHE_DIR="$XDG_CACHE_HOME/miopen-kernels"
  export TORCHINDUCTOR_CACHE_DIR="$XDG_CACHE_HOME/torchinductor"
  export TRITON_CACHE_DIR="$XDG_CACHE_HOME/triton"
fi
mkdir -p "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$MIOPEN_USER_DB_PATH" "$MIOPEN_CUSTOM_CACHE_DIR"
"$interpreter" scripts/configure_totalseg.py
exec "$interpreter" scripts/stack_validation.py "$@"

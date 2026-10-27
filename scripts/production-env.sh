#!/usr/bin/env bash
# Production data and model locations; independent from experimental stores.
# Other shells have no BASH_SOURCE and would resolve the root one level too high,
# pointing SCAN3D_DATA_DIR and the model paths outside this checkout.
if [ -z "${BASH_SOURCE[0]:-}" ]; then
  echo "Source scripts/production-env.sh from bash; other shells resolve the wrong project root." >&2
  return 1 2>/dev/null || exit 1
fi
SCAN3D_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export SCAN3D_ROOT
# A production release may pin source independently from the editable checkout.
# Reading a path is deliberately not shell evaluation; release paths are data.
if [[ -z "${SCAN3D_CODE_DIR:-}" ]]; then
  if [[ -f "$SCAN3D_ROOT/.runtime/rocm10/code-dir" ]]; then
    IFS= read -r SCAN3D_CODE_DIR < "$SCAN3D_ROOT/.runtime/rocm10/code-dir" || true
  else
    SCAN3D_CODE_DIR="$SCAN3D_ROOT"
  fi
fi
export SCAN3D_CODE_DIR
export PATH="$SCAN3D_ROOT/.venv-rocm10-py314/bin:$SCAN3D_ROOT/.tools/bin:$PATH"
export PYTHONPATH="$SCAN3D_CODE_DIR"
export PYTHONNOUSERSITE=1
# Without safe-path mode, `python -m` from the checkout would precede PYTHONPATH.
export PYTHONSAFEPATH=1
export UV_CACHE_DIR="$SCAN3D_ROOT/.cache/stack-upgrade/uv"
export SCAN3D_DATA_DIR="$SCAN3D_ROOT/data"
export SCAN3D_IMPORT_ROOTS="${SCAN3D_IMPORT_ROOTS:-$SCAN3D_ROOT}"
export TOTALSEG_HOME_DIR="$SCAN3D_ROOT/.runtime/totalsegmentator"
export TOTALSEG_WEIGHTS_PATH="$TOTALSEG_HOME_DIR/nnunet/results"
export SCAN3D_NNINTERACTIVE_MODEL="$SCAN3D_ROOT/.runtime/nninteractive/nnInteractive_v1.0"
export SCAN3D_GPU_PROOF="$SCAN3D_ROOT/.runtime/rocm10/gpu-proof.json"
export XDG_CACHE_HOME="$SCAN3D_ROOT/.cache/rocm10"
export XDG_CONFIG_HOME="$SCAN3D_ROOT/.runtime/rocm10/config"
export MIOPEN_USER_DB_PATH="$XDG_CACHE_HOME/miopen-db"
export MIOPEN_CUSTOM_CACHE_DIR="$XDG_CACHE_HOME/miopen-kernels"
export TORCHINDUCTOR_CACHE_DIR="$XDG_CACHE_HOME/torchinductor"
export TRITON_CACHE_DIR="$XDG_CACHE_HOME/triton"
export SCAN3D_DEVICE="${SCAN3D_DEVICE:-gpu}"
# Production stays CT-only until MRI is qualified on this workstation; set CT,MR to offer it.
export SCAN3D_MODALITIES=CT
# The validated AMD wheel family supplies its own coordinated userspace.
unset ROCM_PATH ROCM_HOME HIP_PATH HIP_HOME LD_LIBRARY_PATH LD_PRELOAD HIPBLASLT_TENSILE_LIBPATH HSA_OVERRIDE_GFX_VERSION

#!/usr/bin/env bash
# Source for experimental commands only. The live service uses env.sh.
SCAN3D_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export SCAN3D_ROOT
export SCAN3D_EXPERIMENT_ROOT="$SCAN3D_ROOT/.runtime/stack-upgrade"
export PATH="$SCAN3D_ROOT/.venv-rocm10-py314/bin:$SCAN3D_ROOT/.tools/bin:$PATH"
export PYTHONPATH="$SCAN3D_ROOT"
export PYTHONNOUSERSITE=1
export UV_CACHE_DIR="$SCAN3D_ROOT/.cache/stack-upgrade/uv"
export SCAN3D_DATA_DIR="$SCAN3D_EXPERIMENT_ROOT/app-data"
export TOTALSEG_HOME_DIR="$SCAN3D_EXPERIMENT_ROOT/totalsegmentator"
export TOTALSEG_WEIGHTS_PATH="$SCAN3D_EXPERIMENT_ROOT/weights"
export SCAN3D_NNINTERACTIVE_MODEL="$SCAN3D_EXPERIMENT_ROOT/models/nnInteractive_v1.0"
export XDG_CACHE_HOME="$SCAN3D_EXPERIMENT_ROOT/cache"
export XDG_CONFIG_HOME="$SCAN3D_EXPERIMENT_ROOT/config"
export MIOPEN_USER_DB_PATH="$SCAN3D_EXPERIMENT_ROOT/cache/miopen-db"
export MIOPEN_CUSTOM_CACHE_DIR="$SCAN3D_EXPERIMENT_ROOT/cache/miopen-kernels"
export TORCHINDUCTOR_CACHE_DIR="$SCAN3D_EXPERIMENT_ROOT/cache/torchinductor"
export TRITON_CACHE_DIR="$SCAN3D_EXPERIMENT_ROOT/cache/triton"
export SCAN3D_DEVICE=gpu
# Let AMD's coordinated wheels locate their libraries, without legacy overrides.
unset ROCM_PATH ROCM_HOME HIP_PATH HIP_HOME LD_LIBRARY_PATH LD_PRELOAD

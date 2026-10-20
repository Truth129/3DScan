#!/usr/bin/env bash
# Source from project scripts; all mutable state stays in this checkout.
SCAN3D_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export SCAN3D_ROOT
export PATH="$SCAN3D_ROOT/.tools/bin:$SCAN3D_ROOT/.venv/bin:/opt/rocm/bin:$PATH"
export UV_CACHE_DIR="$SCAN3D_ROOT/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$SCAN3D_ROOT/.tools/python"
export UV_PYTHON_BIN_DIR="$SCAN3D_ROOT/.tools/bin"
export TOTALSEG_HOME_DIR="${TOTALSEG_HOME_DIR:-$SCAN3D_ROOT/.runtime/totalsegmentator}"
export SCAN3D_DATA_DIR="${SCAN3D_DATA_DIR:-$SCAN3D_ROOT/data}"
export SCAN3D_DEVICE="${SCAN3D_DEVICE:-gpu}"
export XDG_CACHE_HOME="$SCAN3D_ROOT/.cache"
export PYTHONNOUSERSITE=1
export PYTHONPATH="$SCAN3D_ROOT${PYTHONPATH:+:$PYTHONPATH}"


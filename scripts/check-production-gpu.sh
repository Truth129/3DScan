#!/usr/bin/env bash
set -euo pipefail
umask 077
source "$(dirname -- "${BASH_SOURCE[0]}")/production-env.sh"
mkdir -p "$MIOPEN_USER_DB_PATH" "$MIOPEN_CUSTOM_CACHE_DIR" "$XDG_CONFIG_HOME"
exec "$SCAN3D_ROOT/.venv-rocm10-py314/bin/python" "$SCAN3D_ROOT/scripts/check_gpu.py" "$@"

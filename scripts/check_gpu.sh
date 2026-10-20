#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
exec "$SCAN3D_ROOT/.venv/bin/python" "$SCAN3D_ROOT/scripts/check_gpu.py" "$@"


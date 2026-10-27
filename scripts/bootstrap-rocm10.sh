#!/usr/bin/env bash
# Install the locked candidate only. Never mutates .venv or system packages.
set -euo pipefail
umask 077
source "$(dirname -- "${BASH_SOURCE[0]}")/rocm10-env.sh"
cd "$SCAN3D_ROOT"
interpreter="${SCAN3D_EXPERIMENT_PYTHON:-/usr/bin/python3.14}"
if [[ ! -x .tools/bin/uv || ! -x "$interpreter" ]]; then
  echo 'Requires the existing project uv and a Python 3.14 interpreter.' >&2
  exit 1
fi
"$interpreter" -c 'import sys; assert sys.version_info[:2] == (3, 14)'
mkdir -p "$UV_CACHE_DIR" "$TOTALSEG_HOME_DIR" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME"
if [[ ! -x .venv-rocm10-py314/bin/python ]]; then
  .tools/bin/uv venv --python "$interpreter" .venv-rocm10-py314
fi
.venv-rocm10-py314/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 14)'
.tools/bin/uv pip sync --python .venv-rocm10-py314/bin/python --require-hashes --index-strategy unsafe-best-match requirements/rocm10-py314.lock
.tools/bin/uv pip check --python .venv-rocm10-py314/bin/python
.venv-rocm10-py314/bin/python scripts/configure_totalseg.py
echo 'Candidate packages installed. Run stack_validation.py before using this runtime.'
echo 'Model copies and native GPU compatibility must be validated separately.'

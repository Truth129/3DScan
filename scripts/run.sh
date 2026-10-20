#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
cd "$SCAN3D_ROOT"
if [[ ! -x .venv/bin/python ]]; then
  echo 'Run scripts/bootstrap.sh first.' >&2
  exit 1
fi
exec .venv/bin/python -m scan3d serve --host "${SCAN3D_HOST:-127.0.0.1}" --port "${SCAN3D_PORT:-9010}" "$@"


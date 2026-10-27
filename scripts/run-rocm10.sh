#!/usr/bin/env bash
set -euo pipefail
umask 077
source "$(dirname -- "${BASH_SOURCE[0]}")/rocm10-env.sh"
cd "$SCAN3D_ROOT"
if [[ ! -x .venv-rocm10-py314/bin/python ]]; then
  echo 'Provision the experimental runtime with scripts/bootstrap-rocm10.sh.' >&2
  exit 1
fi
candidate_port="${SCAN3D_PORT:-9011}"
if (( $# )) || [[ ! "$candidate_port" =~ ^[1-9][0-9]{0,4}$ ]] || (( candidate_port > 65535 || candidate_port == 9010 )); then
  echo 'Use SCAN3D_PORT=9011 (or another unused port); no CLI overrides or production port 9010.' >&2
  exit 2
fi
mkdir -p "$SCAN3D_DATA_DIR" "$TOTALSEG_HOME_DIR" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$MIOPEN_USER_DB_PATH" "$MIOPEN_CUSTOM_CACHE_DIR"
exec .venv-rocm10-py314/bin/python -m scan3d serve --host 127.0.0.1 --port "$candidate_port"

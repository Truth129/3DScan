#!/usr/bin/env bash
set -euo pipefail
umask 077
source "$(dirname -- "${BASH_SOURCE[0]}")/production-env.sh"
interpreter="$SCAN3D_ROOT/.venv-rocm10-py314/bin/python"
if [[ ! -x "$interpreter" ]]; then
  echo 'The validated Python 3.14 production runtime is not installed.' >&2
  exit 1
fi
if [[ "$SCAN3D_CODE_DIR" != /* || "$SCAN3D_CODE_DIR" == *:* || "$SCAN3D_CODE_DIR" == *$'\n'* || "$SCAN3D_CODE_DIR" == *$'\r'* \
      || ! -f "$SCAN3D_CODE_DIR/scan3d/__init__.py" || ! -f "$SCAN3D_CODE_DIR/web/dist/index.html" ]]; then
  echo 'Production code selection must be an absolute release directory containing scan3d and the built viewer.' >&2
  exit 1
fi
cd "$SCAN3D_CODE_DIR"
"$interpreter" - <<'PY'
from importlib import metadata
import sys

if sys.version_info[:2] != (3, 14):
    raise SystemExit("Production requires the validated Python 3.14 interpreter")
expected = {"torch": "2.14.0+rocm10.1.0", "rocm": "10.1.0",
            "torchvision": "0.29.0a0+rocm10.1.0", "triton": "3.8.0+git669b31ac.rocm10.1.0",
            "rocm-sdk-core": "10.1.0", "rocm-sdk-libraries": "10.1.0",
            "rocm-sdk-device-gfx1201": "10.1.0", "nnunetv2": "2.8.1",
            "TotalSegmentator": "2.18.0", "nnInteractive": "2.6.0"}
for package, version in expected.items():
    try:
        installed = metadata.version(package)
    except metadata.PackageNotFoundError:
        raise SystemExit(f"Missing production runtime package: {package}") from None
    if installed != version:
        raise SystemExit(f"Unvalidated {package} version {installed}; expected {version}")
PY
mkdir -p "$SCAN3D_DATA_DIR" "$TOTALSEG_HOME_DIR" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$MIOPEN_USER_DB_PATH" "$MIOPEN_CUSTOM_CACHE_DIR"
exec "$interpreter" -m scan3d serve --host "${SCAN3D_HOST:-127.0.0.1}" --port "${SCAN3D_PORT:-9010}" "$@"

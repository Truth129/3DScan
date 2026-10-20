#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
cd "$SCAN3D_ROOT"
# An extracted native Xvfb package is sufficient; no global Qt or Slicer edits.
if ! command -v Xvfb >/dev/null 2>&1; then
  export PATH="$SCAN3D_ROOT/.tools/xvfb/usr/bin:$PATH"
fi
for required in Xvfb xvfb-run xauth; do
  if ! command -v "$required" >/dev/null 2>&1; then
    echo "Missing $required. Install xorg-server-xvfb/xorg-xauth, or extract their packages under .tools/xvfb/." >&2
    exit 1
  fi
done
if [[ ! -x /opt/3dslicer/Slicer ]]; then
  echo 'Installed /opt/3dslicer/Slicer was not found.' >&2
  exit 1
fi
export SCAN3D_XVFB=1
export QT_QPA_PLATFORM=xcb
exec xvfb-run -a -s '-screen 0 1280x1024x24 -nolisten tcp' \
  "$SCAN3D_ROOT/.venv/bin/python" "$SCAN3D_ROOT/tests/slicer_roundtrip.py" "$@"

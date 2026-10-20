#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
cd "$SCAN3D_ROOT"
mode="${1:-full}"
if [[ "$mode" != core && "$mode" != full ]]; then
  echo 'Usage: scripts/bootstrap.sh [core|full]' >&2
  exit 2
fi
mkdir -p .tools/bin .tools/python .cache/uv .runtime "$TOTALSEG_HOME_DIR"
if [[ ! -x .tools/bin/uv ]]; then
  uv_version=0.9.18
  archive="$(mktemp -d "$SCAN3D_ROOT/.cache/uv-download.XXXXXX")"
  curl --fail --location --retry 3 \
    "https://github.com/astral-sh/uv/releases/download/$uv_version/uv-x86_64-unknown-linux-gnu.tar.gz" \
    --output "$archive/uv.tar.gz"
  tar -xzf "$archive/uv.tar.gz" -C "$archive"
  install -m 0755 "$archive/uv-x86_64-unknown-linux-gnu/uv" .tools/bin/uv
fi
.tools/bin/uv python install 3.12
if [[ ! -x .venv/bin/python ]]; then
  .tools/bin/uv venv --python 3.12 .venv
fi
.venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 12), "Recreate .venv with Python 3.12"'
lock="requirements/$mode.lock"
input=requirements/core.in
[[ "$mode" == full ]] && input=requirements/segmentation.in
if [[ ! -f "$lock" ]]; then
  .tools/bin/uv pip compile --quiet --python .venv/bin/python "$input" --output-file "$lock"
fi
if [[ "$mode" == core ]]; then
  # Add core tools without removing an already provisioned inference stack.
  .tools/bin/uv pip install --python .venv/bin/python --requirement "$lock"
else
  .tools/bin/uv pip sync --python .venv/bin/python "$lock"
fi
.tools/bin/uv pip check --python .venv/bin/python
.venv/bin/python scripts/configure_totalseg.py
if [[ "$mode" == full ]]; then
  if ! .venv/bin/python -c 'import torch; assert torch.version.hip, "Expected the pinned AMD ROCm torch build"'; then
    echo 'Python packages are installed, but native ROCm libraries are incomplete.' >&2
    echo 'Run: sudo pacman -S --needed rocm-ml-libraries hipsparselt rocm-smi-lib' >&2
    exit 1
  fi
fi
echo "Environment ready ($mode): $SCAN3D_ROOT/.venv/bin/python"
echo 'Native GPU validation: scripts/check_gpu.sh'


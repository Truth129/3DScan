#!/usr/bin/env bash
# Set up this checkout: Python environment, AI packages, DICOM converter, viewer and config.
set -euo pipefail
umask 077
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

usage() {
  cat <<'USAGE'
Usage: scripts/bootstrap.sh [options]

  --torch cpu     PyTorch for CPU-only inference (default)
  --torch cuda    PyTorch for NVIDIA GPUs (needs a driver that supports CUDA 13)
  --torch rocm    the hash-locked AMD stack for gfx1201 GPUs (Radeon AI PRO R9700)
  --torch none    viewer and editing only: no PyTorch and no AI packages
  --skip-dcm2niix use the dcm2niix already on PATH instead of building the pinned release
  --skip-web      do not build the viewer
USAGE
}

torch_kind=cpu
build_converter=1
build_web=1
while (( $# )); do
  case "$1" in
    --torch)
      torch_kind="${2:-}"
      shift 2 || { usage >&2; exit 2; }
      ;;
    --skip-dcm2niix) build_converter=0; shift ;;
    --skip-web) build_web=0; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
case "$torch_kind" in
  cpu | cuda | rocm | none) ;;
  *) usage >&2; exit 2 ;;
esac

mkdir -p .tools/bin .cache .runtime
export UV_CACHE_DIR="$root/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$root/.tools/python"
export UV_PYTHON_BIN_DIR="$root/.tools/bin"

# uv supplies Python 3.14 too, so the distribution's Python version does not matter.
uv=.tools/bin/uv
if [[ ! -x "$uv" ]]; then
  uv_version=0.9.18
  arch="$(uname -m)"
  case "$arch" in
    x86_64 | aarch64) ;;
    *) echo "No uv release is configured for $arch." >&2; exit 1 ;;
  esac
  download="$(mktemp -d "$root/.cache/uv-download.XXXXXX")"
  trap 'rm -rf "$download"' EXIT
  curl --fail --location --retry 3 --silent --show-error \
    "https://github.com/astral-sh/uv/releases/download/$uv_version/uv-$arch-unknown-linux-gnu.tar.gz" \
    --output "$download/uv.tar.gz"
  tar -xzf "$download/uv.tar.gz" -C "$download"
  install -m 0755 "$download/uv-$arch-unknown-linux-gnu/uv" "$uv"
fi

"$uv" python install 3.14
[[ -x .venv/bin/python ]] || "$uv" venv --python 3.14 .venv
python=.venv/bin/python

case "$torch_kind" in
  rocm)
    "$uv" pip sync --python "$python" --require-hashes --index-strategy unsafe-best-match requirements/rocm10-py314.lock
    "$uv" pip install --python "$python" --no-deps --editable .
    ;;
  cuda)
    # PyPI's Linux wheels are the CUDA builds and bundle their own CUDA runtime.
    "$uv" pip install --python "$python" --constraint requirements/constraints.txt --editable ".[ai]"
    ;;
  cpu)
    "$uv" pip install --python "$python" --torch-backend cpu --constraint requirements/constraints.txt --editable ".[ai]"
    ;;
  none)
    "$uv" pip install --python "$python" --constraint requirements/constraints.txt --editable .
    ;;
esac
if [[ "$torch_kind" != none ]]; then
  "$python" scripts/configure_totalseg.py
fi

if (( build_converter )); then
  scripts/build_dcm2niix.sh
elif ! command -v dcm2niix >/dev/null; then
  echo 'dcm2niix is not on PATH; DICOM import will fail until it is installed.' >&2
fi

if (( build_web )); then
  if ! command -v npm >/dev/null; then
    echo 'npm was not found. Install Node.js 22 or newer and run this script again, or use --skip-web.' >&2
    exit 1
  fi
  (
    cd web
    npm ci
    npx vite build
    npx vite build --config vite.offline.config.ts
  )
fi

if [[ ! -e scan3d.env ]]; then
  cp scan3d.env.example scan3d.env
  chmod 600 scan3d.env
fi

echo
echo "3DScan is installed in $root."
echo 'Start it with:  .venv/bin/scan3d serve'
echo 'Check it with:  .venv/bin/scan3d doctor'

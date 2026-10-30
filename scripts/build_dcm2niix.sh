#!/usr/bin/env bash
# Build the pinned native converter with compressed-DICOM decoders enabled.
set -euo pipefail
SCAN3D_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export SCAN3D_ROOT
cd "$SCAN3D_ROOT"

version=1.0.20250506
expected_sha512=1954cc4229478bee69bb0382c90a5c212da4b632fd8f6a5aeb8342d1fa8ac704c83313ecae8cf7b00f6cd8614cc3826a7aefa0209c2633f8ef3c0f1c4b367e3e
for dependency in cmake c++ pkg-config sha512sum; do
  if ! command -v "$dependency" >/dev/null; then
    echo "Missing $dependency. Install a C++ compiler, cmake, pkg-config, and the OpenJPEG and zlib development packages." >&2
    exit 1
  fi
done
if ! pkg-config --exists libopenjp2 zlib; then
  echo 'Converter decoding libraries are missing. Install the OpenJPEG and zlib development packages.' >&2
  exit 1
fi

cache="$SCAN3D_ROOT/.cache/dcm2niix"
mkdir -p "$cache" "$SCAN3D_ROOT/.tools/bin"
archive="${1:-$cache/dcm2niix-$version.tar.gz}"
if [[ ! -f "$archive" ]]; then
  if [[ $# -gt 0 ]]; then
    echo 'The specified dcm2niix source archive does not exist.' >&2
    exit 1
  fi
  curl --fail --location --retry 3 \
    "https://github.com/rordenlab/dcm2niix/archive/refs/tags/v$version.tar.gz" \
    --output "$archive.download"
  mv "$archive.download" "$archive"
fi
actual_sha512="$(sha512sum "$archive")"
if [[ "${actual_sha512%% *}" != "$expected_sha512" ]]; then
  echo 'dcm2niix source checksum does not match the pinned release.' >&2
  exit 1
fi
if [[ ! "$archive" -ef "$cache/dcm2niix-$version.tar.gz" ]]; then
  cp "$archive" "$cache/dcm2niix-$version.tar.gz"
fi
tar -xzf "$archive" -C "$cache"
build="$cache/build-$version-openjpeg"
cmake -S "$cache/dcm2niix-$version/console" -B "$build" \
  -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_BUILD_TYPE=Release \
  -DUSE_OPENJPEG=ON -DUSE_JPEGLS=ON -DUSE_STATIC_RUNTIME=OFF \
  -DZLIB_IMPLEMENTATION=System -DBATCH_VERSION=OFF
cmake --build "$build" --parallel "$(nproc)"
install -m 0755 "$build/dcm2niix" "$SCAN3D_ROOT/.tools/bin/dcm2niix.writing"
mv "$SCAN3D_ROOT/.tools/bin/dcm2niix.writing" "$SCAN3D_ROOT/.tools/bin/dcm2niix"

python="$SCAN3D_ROOT/.venv/bin/python"
[[ -x "$python" ]] || python=python3
SOURCE_SHA512="$expected_sha512" CONVERTER_VERSION="$version" \
  "$python" - <<'PY'
from pathlib import Path
import hashlib, json, os, subprocess
root = Path(os.environ['SCAN3D_ROOT'])
binary = root / '.tools/bin/dcm2niix'
record = {'release': os.environ['CONVERTER_VERSION'], 'source_sha512': os.environ['SOURCE_SHA512'],
          'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
          'openjpeg': subprocess.check_output(['pkg-config', '--modversion', 'libopenjp2'], text=True).strip(),
          'features': ['OpenJPEG', 'JPEG-LS', 'system-zlib'],
          'build_script_sha256': hashlib.sha256((root / 'scripts/build_dcm2niix.sh').read_bytes()).hexdigest()}
(root / '.tools/dcm2niix-build.json').write_text(json.dumps(record, indent=2) + '\n')
PY
echo 'Native DICOM converter ready with JPEG 2000 and JPEG-LS decoding.'

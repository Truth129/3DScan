#!/usr/bin/env bash
set -euo pipefail

# This intentionally does not source the production environment or case library.
exec python3 "$(dirname -- "${BASH_SOURCE[0]}")/orthanc_dev.py" "$@"

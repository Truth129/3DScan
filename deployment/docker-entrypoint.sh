#!/bin/sh
# Container entrypoint: check the volumes, then run the command (by default `scan3d serve`).
set -eu
# Like the app itself, keep everything this writes private to the app's uid.
umask 077

for dir in "$SCAN3D_DATA_DIR" /app/.runtime; do
  if [ ! -w "$dir" ]; then
    echo "$dir is not writable by uid $(id -u). Give that uid the folder, or use a named volume." >&2
    exit 1
  fi
done

for dir in "${MIOPEN_USER_DB_PATH:-}" "${MIOPEN_CUSTOM_CACHE_DIR:-}"; do
  if [ -n "$dir" ]; then mkdir -p "$dir"; fi
done

# A job killed mid-run, such as a cancelled TotalSegmentator run, can leave a copy of the scan in
# /tmp, which a restarted container keeps. A host clears /tmp at boot, so clear it at each start.
find /tmp -mindepth 1 -user "$(id -u)" -delete 2>/dev/null || true

# Every start, because a new or bind-mounted runtime folder has no TotalSegmentator settings yet,
# and TotalSegmentator would then send usage statistics.
python /app/scripts/configure_totalseg.py >/dev/null

exec "$@"

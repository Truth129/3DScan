#!/usr/bin/env bash
# Add the NVIDIA inference path: a separate hash-locked Python environment, then a GPU check
# whose proof registers it. Nothing the default path runs on is touched.
set -euo pipefail
umask 077
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'USAGE'
Usage: scripts/add_inference_path.sh nvidia [options]

  --venv DIR  where the NVIDIA environment goes (default: .venv-nvidia-py314 in this checkout).
              A directory that exists is used only if this script made it.
  --dry-run   check this workstation and print the plan, creating, installing and testing nothing

Run it again after a driver change or when the app shows this GPU as unavailable. An environment
that equals the lock stays, anything else is rebuilt from empty, and the GPU check runs again. If
the reason is a TotalSegmentator or nnInteractive version, update the existing install as well,
because this script cannot fix that.
USAGE
}

fail() {
  echo "$1" >&2
  exit 1
}

# For a run stopped past the finished mark: discovery now refuses the path, and with no proof yet
# there is no row to refuse.
stopped() {
  fail "$1 Until a run finishes, the app shows this GPU as unavailable, or not at all if it was never checked. Fix what was reported above, then run this again."
}

# Free bytes where a directory is, or where it would be created.
free_bytes() {
  local place="$1"
  until [[ -e "$place" ]]; do
    place="$(dirname -- "$place")"
  done
  df --output=avail --block-size=1 -- "$place" | tail -n 1
}

# Changes whenever the check writes, because it replaces the file instead of rewriting it.
proof_state() {
  stat --format='%i %y' -- "$proof" 2>/dev/null || true
}

# The link alone is not an environment. uv writes pyvenv.cfg after it, and without that file the
# link is the Python it points at, so what uv says about it would be about that Python.
has_environment() {
  [[ -x "$python" && -f "$venv/pyvenv.cfg" ]]
}

# Whether the last install finished and left exactly what the lock pins. It reads no index, and
# what uv prints for an installed package is what it writes for a pin, so the two compare as text.
matches_lock() {
  [[ -f "$finished" ]] && has_environment || return 1
  local pins packages
  pins="$(grep -oE '^[a-z0-9][^ ]*==[^ ]+' "$lock" | sort)"
  packages="$("$uv" pip freeze --python "$python" 2>/dev/null | sort)"
  [[ "$packages" == "$pins" ]]
}

chosen=
venv="$root/.venv-nvidia-py314"
dry_run=0
while (( $# )); do
  case "$1" in
    nvidia)
      [[ -z "$chosen" ]] || { usage >&2; exit 2; }
      chosen="$1"
      shift
      ;;
    --venv)
      venv="${2:-}"
      # The next option is not a directory, and an empty value would name the current one.
      [[ -n "$venv" && "$venv" != -* ]] || { usage >&2; exit 2; }
      shift 2
      ;;
    --dry-run) dry_run=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n "$chosen" ]] || { usage >&2; exit 2; }

# One absolute path from here on, taken before moving to the checkout so that a relative --venv
# means the caller's directory. It follows links, so the marker test below reads the real
# directory however it was spelled.
named="$(realpath --canonicalize-missing --no-symlinks -- "$venv")"
venv="$(realpath --canonicalize-missing -- "$venv")"
python="$venv/bin/python"
marker="$venv/.scan3d-inference-path"
finished="$venv/.scan3d-inference-path-finished"
lock="$root/requirements/cuda-py314.lock"
cd "$root"

# The marker alone lets this script use a directory that exists, whatever its name or however
# empty it is: `uv pip sync` removes what the lock does not list, so another environment would
# be destroyed. A link that points nowhere still holds the name.
if [[ -e "$venv" || -L "$named" ]] && [[ ! -f "$marker" ]]; then
  fail "$venv already exists and this script did not make it, so nothing was changed. Name a new directory with --venv."
fi

uv="$root/.tools/bin/uv"
if [[ ! -x "$uv" ]]; then
  uv="$(command -v uv)" || fail 'uv was not found in .tools/bin or on PATH. Install uv, then run this again.'
fi
export UV_CACHE_DIR="${UV_CACHE_DIR:-$root/.cache/uv}"
# uv fetches a Python it cannot find unless told not to, and a runtime must not rest on an
# interpreter nobody chose.
export UV_PYTHON_DOWNLOADS=never
# A checkout's own Python, when it has one.
if [[ -d "$root/.tools/python" ]]; then
  export UV_PYTHON_INSTALL_DIR="$root/.tools/python"
fi

# --system, or an active environment or a .venv here could answer instead of a Python to build
# on. --no-cache, because uv would otherwise record the answer, and these checks write nothing.
interpreter="$("$uv" python find --no-cache --system 3.14)" ||
  fail 'Python 3.14 was not found, and this script does not install one. Install it, then run this again.'

command -v nvidia-smi >/dev/null ||
  fail 'nvidia-smi was not found. Install the NVIDIA driver, 580 or newer, then run this again.'
driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader)" ||
  fail "nvidia-smi could not read the driver version: $driver"
# It prints one line per card, and every card runs the same driver.
driver="${driver%%$'\n'*}"
[[ "$driver" =~ ^([0-9]+)\. ]] || fail "nvidia-smi gave no driver version: $driver"
(( 10#${BASH_REMATCH[1]} >= 580 )) ||
  fail "NVIDIA driver $driver is too old: the CUDA 13 build of PyTorch needs 580 or newer."

# Only the run that starts an install needs the room. What it leaves in the environment and the
# cache counts against the free space from then on, so asking again would refuse the re-run that
# rebuilds it. The cache holds a second copy when it is on another disk.
if [[ ! -f "$marker" ]]; then
  for place in "$venv" "$UV_CACHE_DIR"; do
    free="$(free_bytes "$place")"
    (( free >= 12 * 1024 ** 3 )) ||
      fail "Only $(( free / 1024 ** 3 )) GiB is free for $place. This needs at least 12 GiB."
  done
fi

# Ask the code the check runs, because scan3d.env and SCAN3D_CONFIG can name this folder too.
# -B, so that a dry run writes nothing into the checkout.
proofs="$("$interpreter" -B -P -c '
import sys

sys.path.insert(0, sys.argv[1])
from scan3d.config import load_environment
from scan3d.inference_paths import directory

load_environment()
print(directory())
' "$root")" || fail 'The settings could not be read, so nothing was changed.'
proof="$proofs/$chosen.json"

earlier=
if has_environment; then
  earlier=' (from an earlier run)'
fi
cat <<PLAN

Adding the NVIDIA inference path.

  Environment  $venv$earlier
  Python       $interpreter
  Driver       $driver
  Lock         $lock
  Proof        $proof

A first install downloads about 3.3 GiB and takes about 9 GiB on disk. A re-run leaves an
environment that equals the lock alone, and needs no network to do so. Anything else, such as
an install that did not finish, is rebuilt from empty, from what uv has already fetched where
it can. Every package is checked against a hash, with one gap: acvl-utils is published only as
source, and the tools that build it are fetched without hashes.

It will not touch any other Python environment, the viewer, the DICOM converter,
TotalSegmentator's settings, scan3d.env or the running app, and it does not install a Python.

Last, it runs a short GPU check on the NVIDIA card. The proof that check writes is the
registration: when it passes, a running app that reads the proof's folder offers the card the
next time a panel opens, with no restart, provided the existing install's TotalSegmentator and
nnInteractive versions equal the lock's. If they differ, the app shows the card as unavailable
and says why. This script cannot tell which Python serves the app, so it cannot compare them.

PLAN
if (( dry_run )); then
  echo 'Dry run: nothing was created, installed or tested.'
  exit 0
fi

# Only a finished environment that equals the lock stays, and `uv pip sync` cannot judge that. It
# reads a pin by PEP 440, so ==2.14.0 accepts an installed 2.14.0+cu126, the wheel the lock exists
# to keep out. And it asks the index even when nothing differs, so a re-run could not reach the
# check offline.
#
# Nor can uv repair what a stop leaves. It trusts a dist-info, with no sign that the package's
# files all arrived, and a stop inside a package's RECORD leaves a state it refuses forever
# ('RECORD file is invalid'), so running this again could never work. An empty environment gives
# it nothing to trust or refuse. --reinstall would not help, and it implies --refresh, so it would
# fetch again what uv already holds. The cache is outside the environment and stays.
if matches_lock; then
  echo 'Every package already equals the lock, so nothing was installed.'
else
  # A job can run from a finished environment and never from an unfinished one, which discovery
  # refuses, so a first or a stopped install does not wait for the queue.
  if [[ -f "$finished" ]]; then
    "$interpreter" -B -P "$root/scripts/check_gpu.py" --queue ||
      fail 'This environment needs a rebuild and a job may be using it, so nothing was changed. Run this again when the queue is empty.'
  fi
  # The marker stays through the wipe, so that a stop at any point leaves a directory this script
  # still owns. The finished mark goes first, so that discovery withdraws the path before anything
  # changes.
  mkdir -p "$venv"
  echo "Made by scripts/add_inference_path.sh $chosen." >"$marker"
  rm -f -- "$finished"
  find "$venv" -mindepth 1 -maxdepth 1 ! -name "${marker##*/}" -exec rm -rf -- {} + ||
    stopped 'The old environment could not be cleared.'
  "$uv" venv --python "$interpreter" --allow-existing "$venv" ||
    stopped 'The environment could not be created.'
  # --require-hashes covers what is installed, and acvl-utils's build requirements are outside
  # it: it has no wheel, and they come from the index unhashed.
  "$uv" pip sync --python "$python" --require-hashes "$lock" ||
    stopped 'The install did not finish, so no GPU check ran.'
  echo 'The last install finished.' >"$finished"
fi

before="$(proof_state)"
status=0
(
  # This runtime's wheels carry their own libraries, and an AMD launcher's would shadow them.
  unset LD_LIBRARY_PATH LD_PRELOAD
  exec "$python" "$root/scripts/check_gpu.py" --path "$chosen"
) || status=$?
after="$(proof_state)"

echo
if [[ -z "$after" || "$after" == "$before" ]]; then
  # A refused check exits 1 as a failed one does, but leaves no proof. What the app shows can still
  # have changed, if this run rebuilt the environment, so say only what it goes by.
  fail 'The GPU check recorded nothing, so the app still goes by the last proof, if there is one. Its message is above. If it was waiting on the job queue, run this again when the queue is empty.'
elif (( status == 0 )); then
  echo "The GPU check passed and wrote $proof."
  echo 'That file is the whole registration: a running app that reads its folder offers this GPU'
  echo "the next time a panel opens, with no restart, provided the existing install's"
  echo "TotalSegmentator and nnInteractive versions equal this environment's. If they differ, the"
  echo 'app shows this GPU as unavailable and says why. To remove the path, move the file out of'
  echo 'that folder. The environment stays until you delete it.'
elif (( status == 1 )); then
  echo "The GPU check failed and wrote $proof." >&2
  fail 'A running app that reads its folder now shows this GPU as unavailable, with its reason. Fix what the check reported above, then run this again.'
else
  fail "The GPU check stopped with exit status $status after writing $proof. Run this again."
fi

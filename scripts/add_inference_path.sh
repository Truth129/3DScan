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
the reason is a TotalSegmentator, nnU-Net or nnInteractive version, update the existing install as
well, because this script cannot fix that.
USAGE
}

fail() {
  echo "$1" >&2
  exit 1
}

# For a run stopped past the finished mark: discovery now refuses the path, and with no proof yet
# there is no row to refuse.
stopped() {
  fail "$1 Until an install finishes, the app shows this GPU as unavailable if it was last checked on this environment, or not at all if it was never checked. Fix what was reported above, then run this again."
}

# A link is not the marker, wherever it points: a copy of an environment can be made of links.
marked() {
  [[ -f "$marker" && ! -L "$marker" ]]
}

# The marker alone lets this script use a directory that exists, whatever its name or however
# empty it is: `uv pip sync` removes what the lock does not list, so another environment would
# be destroyed. A link that points nowhere still holds the name, and it is all there is to name.
refuse_unmarked() {
  local taken="$venv"
  [[ -e "$venv" ]] || taken="$named"
  if [[ -e "$taken" || -L "$taken" ]] && ! marked; then
    fail "$taken already exists and this script did not make it, so nothing was changed. Name a new directory with --venv."
  fi
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
  local packages
  # A failed read says nothing about the lock, and treating it as a difference would wipe an
  # environment that may equal it. --quiet drops only the line naming the environment. A forced
  # colour reaches through a pipe and bolds every name, and then no name equals its pin.
  packages="$("$uv" pip freeze --python "$python" --quiet --color never | sort)" ||
    fail 'uv could not read this environment, so nothing was changed. Fix what it reported above, then run this again. If the fault is in the environment itself, move the directory aside first, and the run after that installs it from empty.'
  [[ "$packages" == "$pins" ]]
}

# Whether uv can build this whole lock, found out by doing it in a folder nobody will use. So by the
# time this passes, uv has fetched and built everything a rebuild needs, and no download can stop
# one after its wipe, though a full disk or a kill still can. A dry run would not show that: it reads
# index pages alone, and passes with every wheel gone. The folder is in uv's cache because a hard
# link costs no data within a disk, and --target lets uv take a base Python. A signal takes the
# folder with it, and only a kill leaves it behind, as links that `uv cache clean` removes.
can_rebuild() {
  local cache status=0
  cache="$("$uv" cache dir --color never)" && mkdir -p -- "$cache" || return 1
  trial="$(mktemp -d -p "$cache" scan3d-trial.XXXXXXXXXX)" || return 1
  trap 'rm -rf -- "$trial"' EXIT
  echo 'Trying the rebuild in a folder of its own first, so that this environment stays unless uv can'
  echo 'finish. That can take many minutes if uv has to fetch packages.'
  "$uv" pip sync --link-mode hardlink --python "$interpreter" --target "$trial" --require-hashes "$lock" || status=$?
  rm -rf -- "$trial"
  trap - EXIT
  return "$status"
}

# The check says what it found. This sentence has to fit a busy queue and one that cannot be read,
# which no amount of waiting will clear.
queue_is_empty() {
  "$interpreter" -B -P "$root/scripts/check_gpu.py" --queue ||
    fail 'This environment needs a rebuild, which can start only when the job queue can be read and is empty, so it was left as it is. Deal with what is reported above, then run this again.'
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

refuse_unmarked

# Before anything else, because a lock with no pins would read as a difference from any environment,
# and a difference starts a rebuild.
pins="$(grep -oE '^[a-z0-9][^ ]*==[^ ]+' "$lock" | sort)" ||
  fail "$lock is missing or pins nothing, so nothing was changed."

uv="$root/.tools/bin/uv"
if [[ ! -x "$uv" ]]; then
  uv="$(command -v uv)" || fail 'uv was not found in .tools/bin or on PATH. Install uv, then run this again.'
fi
export UV_CACHE_DIR="${UV_CACHE_DIR:-$root/.cache/uv}"
# uv fetches a Python it cannot find unless told not to, and a runtime must not rest on an
# interpreter nobody chose.
export UV_PYTHON_DOWNLOADS=never
# By default uv hard-links, on every disk, so a file edited in place here would change in its cache
# and in any environment built from the same wheel, and a rebuild would put the edit back. A clone
# is a file of its own for no more data. Where a disk cannot clone, uv warns and copies instead.
export UV_LINK_MODE=clone
# Each of these reaches `uv venv` alone, and so would stop a rebuild only after its wipe: one refuses
# --allow-existing, and the other fetches pip.
unset UV_VENV_CLEAR UV_VENV_SEED
# With no cache a trial keeps nothing, so a rebuild would have to fetch it all again after its wipe.
unset UV_NO_CACHE
# A checkout's own Python, when it has one.
if [[ -d "$root/.tools/python" ]]; then
  export UV_PYTHON_INSTALL_DIR="$root/.tools/python"
fi

# --system, or an active environment or a .venv here could answer instead of a Python to build
# on. --no-cache, because uv would otherwise record the answer, and these checks write nothing.
interpreter="$("$uv" python find --no-cache --system --color never 3.14)" ||
  fail 'uv could not find Python 3.14, so nothing was changed. See its error above. This script does not install a Python.'

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
# rebuilds it. A disk that cannot clone, or a cache on another disk, holds two full copies.
if ! marked; then
  for place in "$venv" "$UV_CACHE_DIR"; do
    free="$(free_bytes "$place")"
    (( free >= 20 * 1024 ** 3 )) ||
      fail "Only $(( free / 1024 ** 3 )) GiB is free for $place. This needs at least 20 GiB, because where a disk cannot clone files, the environment and uv's cache each hold a full copy."
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

A first install downloads about 3.3 GiB and takes about 9 GiB on disk. It asks for 20 GiB free,
because where a disk cannot clone files, the environment and uv's cache each hold a full copy.
A re-run leaves an environment that equals the lock alone, and needs no network to do so.
Anything else, such as an install that did not finish, is rebuilt from empty, from what uv has
already fetched where it can. A finished environment waits for an empty queue, and stays until
a trial has fetched and built all that the rebuild needs. uv checks a package's hash when it
fetches it, not when it reuses its cache, and there is one gap: acvl-utils is published only as
source, and the tools that build it are fetched without hashes.

It will not touch any other Python environment, the viewer, the DICOM converter,
TotalSegmentator's settings, scan3d.env or the running app, and it does not install a Python.

Last, it runs a short GPU check on the NVIDIA card. The proof that check writes is the
registration: when it passes, a running app that reads the proof's folder offers the card the
next time a panel opens, with no restart, provided the existing install's TotalSegmentator,
nnU-Net and nnInteractive versions equal the lock's. If they differ, the app shows the card as
unavailable and says why. This script cannot tell which Python serves the app, so it cannot
compare them.

PLAN
if (( dry_run )); then
  echo 'Dry run: nothing was created, installed or tested.'
  exit 0
fi

# A new directory comes from plain mkdir, which cannot succeed on anything that arrived after the
# guard above, so a directory this script did not make is never adopted. If mkdir loses to another
# run of this script, the marker says so and the lock below settles it. Otherwise mkdir said why.
if ! marked; then
  mkdir -p -- "$(dirname -- "$venv")"
  if mkdir -- "$venv"; then
    echo "Made by scripts/add_inference_path.sh $chosen." >"$marker"
  else
    refuse_unmarked
    marked || exit 1
  fi
fi

# One run at a time, or a second wipe would empty the environment under the first run's uv, which
# would then mark it finished. The lock is on the marker, which the wipe keeps and which is the same
# file however the directory is spelled. Every child inherits the descriptor, so a killed run's uv
# keeps it until that has gone too. It is opened only to read because that cannot create a marker.
exec 9<"$marker" || fail "$marker cannot be read, so nothing was changed."
flock --nonblock --conflict-exit-code 75 9 || case $? in
  75) fail 'Another run is using this environment, so nothing was changed. Run this again when it has finished.' ;;
  *) fail 'The lock on this environment failed, so nothing was changed. See the error above.' ;;
esac

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
  # Only a finished environment has anything to lose: it may be working, and a job can run from it
  # and never from an unfinished one, which discovery refuses. So it alone waits, and it goes only
  # after a trial has built its replacement. The queue comes first so that a busy one never sets off
  # a download, and again after, because a cold trial can take many minutes.
  if [[ -f "$finished" ]]; then
    queue_is_empty
    can_rebuild ||
      fail 'uv cannot rebuild this environment from what it can reach, so it was left as it is. Fix what uv reported above, then run this again.'
    queue_is_empty
  fi
  # This happens from inside the directory, after a last look that its marker is the one this run
  # locked, by the test `marked` uses, so that the wipe stays in the directory it checked. A hard
  # link to that marker is the same file, and no test can tell. The uv steps after it go by name, so
  # another directory that takes the name once the wipe has begun is installed into. The marker
  # stays, so that a stop at any point leaves a directory this script still owns. The finished mark
  # goes first, so that discovery withdraws the path before anything changes.
  cd -- "$venv" && [[ ! -L "${marker##*/}" && "${marker##*/}" -ef /dev/fd/9 ]] ||
    fail "$venv changed during this run, so nothing was changed. Run this again."
  rm -f -- "${finished##*/}"
  find . -mindepth 1 -maxdepth 1 ! -name "${marker##*/}" -exec rm -rf -- {} + ||
    stopped 'The old environment could not be cleared.'
  cd "$root"
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
  echo "TotalSegmentator, nnU-Net and nnInteractive versions equal this environment's. If they"
  echo 'differ, the app shows this GPU as unavailable and says why. To remove the path, move the'
  echo 'file out of that folder. The environment stays until you delete it.'
elif (( status == 1 )); then
  echo "The GPU check failed and wrote $proof." >&2
  fail 'A running app that reads its folder now shows this GPU as unavailable, with its reason. Fix what the check reported above, then run this again.'
else
  fail "The GPU check stopped with exit status $status after writing $proof. Run this again."
fi

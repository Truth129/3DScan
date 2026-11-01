# Installing 3DScan

3DScan runs on Linux. `scripts/bootstrap.sh` installs everything except the system
packages into the checkout, so it does not depend on your distribution's Python and
needs no root access itself.

**3DScan is for research use only.** It is not a medical device and is not cleared or
approved for diagnosis or treatment. It has no sign-in, so keep it on a machine and
network you control. See [What is not supported](#what-is-not-supported).

## Requirements

| Item          | Requirement                                                                                  |
| ------------- | -------------------------------------------------------------------------------------------- |
| Linux         | x86_64 with glibc 2.28 or newer. arm64 may work but is untested.                             |
| System tools  | git, curl, a C++ compiler, cmake, pkg-config, and OpenJPEG and zlib development packages     |
| Node.js       | 22.12 or newer, to build the viewer                                                          |
| Python        | Nothing to install: the bootstrap fetches Python 3.14 with [uv](https://docs.astral.sh/uv/)  |
| Browser       | Chrome, Chromium or Edge with WebGL 2. Other browsers are untested.                          |
| Memory        | At least 16 GB of RAM. Large CTs and AI tasks can use much more.                             |
| Disk          | About 3.5 GB for a CPU install and 9 GB for AMD, plus AI weights and your cases.             |
| GPU           | Optional. See [Choosing how AI runs](#choosing-how-ai-runs).                                 |

## Worked example: Ubuntu

These commands take a fresh Ubuntu 24.04 or 26.04 server or desktop to a running app.
Both releases were tested from their stock cloud images, with `--torch cpu`.

### 1. Install the system packages

```bash
sudo apt update
sudo apt install -y git curl build-essential cmake pkg-config libopenjp2-7-dev zlib1g-dev
```

### 2. Install Node.js

Ubuntu 24.04's `nodejs` package is too old, so use [nvm](https://github.com/nvm-sh/nvm).
On 26.04, `sudo apt install -y nodejs npm` also works.

```bash
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
source ~/.nvm/nvm.sh
nvm install 26
```

### 3. Get 3DScan and install it

```bash
git clone https://github.com/OWNER/3DScan.git
cd 3DScan
scripts/bootstrap.sh --torch cpu
```

Use `--torch cuda` instead for an NVIDIA GPU. The first run takes a few minutes on a
good connection. It creates `.venv/`, builds a pinned `dcm2niix`, builds the viewer and
copies `scan3d.env.example` to `scan3d.env`.

### 4. Check it and start it

```bash
.venv/bin/scan3d doctor
.venv/bin/scan3d serve
```

Open <http://127.0.0.1:9010>. On a remote server, reach it with an SSH tunnel rather
than opening the port:

```bash
ssh -L 9010:127.0.0.1:9010 user@server
```

To try it without any scans, choose **Explore synthetic demo** in the app, or run
`.venv/bin/scan3d demo`. The demo does not run AI models.

### 5. Add the AI models (optional)

See [AI models](#ai-models). TotalSegmentator downloads its weights on first use.
nnInteractive needs one command:

```bash
.venv/bin/python scripts/download_interactive_model.py
```

## Other distributions

Only the system packages differ. Then follow Ubuntu steps 2 to 5.

```bash
# Debian 12 or newer: use the Ubuntu command.

# Fedora, RHEL and derivatives (untested)
sudo dnf install git curl gcc-c++ cmake pkgconf-pkg-config openjpeg2-devel zlib-devel

# Arch and derivatives (tested on CachyOS)
sudo pacman -S --needed git curl base-devel cmake pkgconf openjpeg2 zlib
```

## Bootstrap options

| Option           | Effect                                                                         |
| ---------------- | ------------------------------------------------------------------------------ |
| `--torch cpu`    | CPU-only PyTorch and AI packages. This is the default.                         |
| `--torch cuda`   | PyTorch for NVIDIA GPUs and AI packages.                                       |
| `--torch rocm`   | The hash-locked AMD stack in `requirements/rocm10-py314.lock`, for gfx1201.    |
| `--torch none`   | Viewer and editing only, with no PyTorch or AI packages.                       |
| `--skip-dcm2niix`| Use the `dcm2niix` on `PATH`. It must handle your compression formats.         |
| `--skip-web`     | Do not build the viewer.                                                       |

Run the script again at any time to update an install. It keeps `scan3d.env`.

## Choosing how AI runs

`SCAN3D_DEVICE` in `scan3d.env` selects the device. The default, `auto`, uses the
discrete GPU with the most memory if PyTorch sees one, otherwise the CPU. A PyTorch
build sees only its own vendor's GPUs, so `auto` never chooses between AMD and NVIDIA.
`gpu` and `gpu:N` fail rather than falling back to the CPU, and `cpu` always uses the
CPU.

| Setup                           | Install flag   | Status                                                                          |
| ------------------------------- | -------------- | ------------------------------------------------------------------------------- |
| CPU                             | `--torch cpu`  | Tested with TotalSegmentator and nnInteractive. Works, but is slow on real CTs. |
| AMD Radeon AI PRO (gfx1201)     | `--torch rocm` | The one GPU stack exercised in depth.                                           |
| NVIDIA with CUDA 13             | `--torch cuda` | **Untested.** The code path is shared, but no real NVIDIA card has run it.      |
| NVIDIA as a second GPU          | See below      | **Untested**, for the same reason. See [Add a second GPU](#add-a-second-gpu).   |
| Other AMD GPUs                  | Manual         | Untested. Edit the device extras in `requirements/rocm10-py314-gpu.in`.         |

For a GPU, install the vendor's driver first. On AMD, your user needs access to the
render nodes, usually by being in the `render` and `video` groups. After installing,
exercise the GPU once:

```bash
.venv/bin/python scripts/check_gpu.py
```

This runs a small 3D convolution and records the result in `.runtime/gpu-proof.json`,
which the app reads to show the GPU's name. It does not prove that a model produces
good results. The check is repeated automatically only when you run it, so run it again
after changing the PyTorch or driver version, or that vendor's cards. It will not run
while a job is queued or running.

For scale, the `total` task on a very small synthetic phantom took just under two
minutes on a 32-thread CPU, and one nnInteractive point took about 13 seconds. Full-size
CTs take far longer on a CPU, and TotalSegmentator's authors recommend a GPU.

## Add a second GPU

A PyTorch build drives one vendor's GPUs, so a workstation with an AMD and an NVIDIA card
needs a second Python environment to use both. `scripts/add_inference_path.sh` builds
one. The result is an **inference path**, an interpreter plus a GPU that a person can
choose in the app. Your existing install stays the default path, and nothing about it
changes. To use the new GPU, see the [user guide](USER_GUIDE.md#choose-a-gpu).

**No real NVIDIA card or CUDA build has run this.** The project's tests use a fake
PyTorch, so they cover how the app behaves, not whether this stack works on a card. The
GPU check at the end of the script is the first real evidence on your machine.

```bash
scripts/add_inference_path.sh nvidia --dry-run
scripts/add_inference_path.sh nvidia
```

| Option       | Effect                                                                     |
| ------------ | -------------------------------------------------------------------------- |
| `--venv DIR` | Where the environment goes. The default is `.venv-nvidia-py314`.           |
| `--dry-run`  | Check this workstation and print the plan, changing nothing.               |

**Before it changes anything**, the script needs:

- `uv`, from `.tools/bin` or `PATH`
- Python 3.14, which it never installs. It also searches `.tools/python`, where the
  bootstrap puts one
- `nvidia-smi` reporting driver 580 or newer
- For a first install, at least 12 GiB free where the environment goes and where uv
  keeps its cache

A first install downloads about 3.3 GiB and takes an estimated 9 GiB on disk.

**Your existing install needs the AI packages too.** Every path must use the same
TotalSegmentator, nnU-Net and nnInteractive versions, and the script does not check
yours. An install made with `--torch none` has none of them, so the GPU stays unavailable
even after its check passes. Add them first with `scripts/bootstrap.sh --torch cpu`.

**What it never touches.** It leaves every other Python environment, the viewer, the
DICOM converter, TotalSegmentator's settings, `scan3d.env` and the running app alone.
`scripts/bootstrap.sh` plays no part, and `--torch cuda` is not the way to add a GPU:
that changes `.venv`, and rebuilds the viewer and the converter. All paths share the
AI weights, the license and your cases, so nothing downloads twice.

The script installs only into a directory it made, which it marks with a
`.scan3d-inference-path` file. It refuses any other directory that already exists,
however empty, and a link with that name does not count, because a rebuild removes
everything in the directory except that file. Keep nothing of your own there. Packages
are checked against the hashes in `requirements/cuda-py314.lock`, with one gap: acvl-utils
is published only as source, and the tools that build it are fetched without hashes.

**The GPU check.** Last, the script runs `scripts/check_gpu.py --path nvidia`, which
writes a proof to `.runtime/inference-paths/nvidia.json`, or under
`SCAN3D_INFERENCE_PATH_DIR` if you set it. That file is the whole registration. When the
check passes, a running app that reads that folder offers the GPU the next time a panel
opens, with no restart and no setting to change, provided your existing install's
TotalSegmentator, nnU-Net and nnInteractive versions equal this environment's. If they
differ, the app shows the GPU as unavailable and says why. The script cannot compare them,
because it cannot tell which Python serves the app.

A passing check proves that this PyTorch build sees the card and ran a small 3D
convolution on it, and that this environment can import what the app's AI processes
import. It runs neither model. It does not prove that a model fits in the card's memory,
that results are good, or that they match another GPU's.

- **A failed check** still writes the proof, so the app lists the GPU as unavailable
  with its reason. Fix what the check reported and run the script again.
- **A refused check** writes nothing, so the app still goes by the last proof, if there
  is one. The check refuses while a job is queued or running, and when it cannot read the
  queue. A data directory that does not exist counts as a fresh install, with no jobs, but
  a link to one whose target has gone, as with an unmounted disk, counts as a queue it
  cannot read.

**Run the script again** after changing the NVIDIA driver, after anything changes that
environment, and whenever the app shows the GPU as unavailable. If its last install
finished and its package names and versions equal the lock, the environment stays: the
script installs nothing and needs no network. Anything else is rebuilt from empty, from
what uv has already fetched where it can, because uv cannot repair what a stopped install
leaves. Either way the script then repeats the GPU check.

A finished environment may be in use, so the script rebuilds it only when no job is
queued or running, by the same rule as the GPU check, and uv can plan the rebuild.
Otherwise it stops with nothing changed. `scripts/check_gpu.py --queue` runs that queue
rule alone. No job can start from an unfinished environment, so a first install and a
stopped install do not wait for the queue. An idle nnInteractive process is not a job, so
it does not hold a rebuild back. It keeps what it had loaded until a segmentation, AI
assist on another GPU or a restart stops it, and if the GPU check records no change it
goes on serving that GPU.

If an install stops, no check runs. Until an install finishes, the app shows the GPU as
unavailable if it was last checked on that environment, or not at all if it was never
checked, and running the script again starts from empty. Only one run can use an
environment at a time, so a second stops with nothing changed.

Two faults in a finished environment survive a re-run. If uv cannot read it, the script
stops with nothing changed. If files are missing or damaged but its package names and
versions still equal the lock, the script keeps it, and the GPU check can then fail on
every run. When the fault is in the environment itself, wait until no job is queued or
running and move the directory aside. The next run is then a first install: it starts
from empty, needs the free space above, and does not wait for the queue.

The app notices changed PyTorch and engine packages by itself. It cannot see other
packages or a driver change, so until you re-run the script, only the device check a job
makes as PyTorch loads can catch a broken driver. A job that stops there because PyTorch
sees no GPU says so, and that no other GPU was tried. If PyTorch hangs or crashes there,
the error names the device check or the nnInteractive worker instead. Check the driver,
then run the script again to repeat the GPU check. If the reason shown is a
TotalSegmentator, nnU-Net or nnInteractive version, a re-run fixes only this environment,
so update your existing install too: every path must use the same versions.

**To remove a path**, move its proof out of the folder. The GPU leaves the app the next
time a panel opens, with no restart, and a job already queued on it fails, naming the
GPU. The environment stays until you delete it.

```bash
mkdir -p .runtime/archive
mv .runtime/inference-paths/nvidia.json .runtime/archive/
```

An idle nnInteractive process on that GPU stays until the next AI job runs, a segmentation
or AI assist on another GPU, or until the app restarts. No job can reach it, but it keeps
the card's memory, which is what you want back if you are removing a card that misbehaves.

**Two cards of one vendor** can share an environment, so the script is not needed.
Running `scripts/check_gpu.py --path second --device gpu:1` with that environment's
Python records the second card as its own path. `N` counts within one PyTorch build. The
script repeats only its own path's check, so repeat this command after a re-run. This has
only run against a fake PyTorch. A task with a crop model, currently `teeth`, cannot run
on an index above 0, because TotalSegmentator starts that model on the first GPU, so the
job fails and says so.

## AI models

Models are downloaded separately and are not part of this repository. Your scans are
never sent to a model host.

**TotalSegmentator** downloads its weights into `TOTALSEG_HOME_DIR`, by default
`.runtime/totalsegmentator`, the first time a task runs. The bootstrap turns off its
anonymous usage statistics. Some subtasks need a license from the provider. Enter a key
under **Segment CT → TotalSegmentator license**. See the
[provider's license page](https://backend.totalsegmentator.com/license-academic/).

**nnInteractive** powers assisted corrections. `scripts/download_interactive_model.py`
fetches the pinned `nnInteractive_v1.0` model into `.runtime/nninteractive/` and checks
its SHA-256. The worker checks it again before loading it.

**Check the licenses before you use them.** The nnInteractive engine is Apache-2.0, but
its [pretrained weights](https://huggingface.co/MIC-DKFZ/nnInteractive) are
**CC BY-NC-SA 4.0, which is noncommercial**. Some TotalSegmentator subtasks are also
noncommercial. You are responsible for accepting and following these terms. 3DScan's own
Apache-2.0 license does not change them.

## Run it as a service

`deployment/scan3d.service` is a systemd user unit. This installs it for your checkout
and starts it:

```bash
mkdir -p ~/.config/systemd/user
sed "s|%h/3DScan|$PWD|g" deployment/scan3d.service > ~/.config/systemd/user/scan3d.service
systemctl --user daemon-reload
systemctl --user enable --now scan3d.service
loginctl enable-linger "$USER"
```

`enable-linger` keeps it running after you log out. Follow its log with
`journalctl --user -u scan3d.service -f`. Only one worker may use a data directory, so
stop the service before running `scan3d serve` by hand on the same library.

## Back up your cases

```bash
.venv/bin/scan3d backup /path/on/another/disk/scan3d-backups
```

See the [user guide](USER_GUIDE.md) for what a backup contains and how to restore it.

## What is not supported

- **Clinical use.** Nothing here has regulatory clearance or clinical validation.
- **macOS and Windows.** Only Linux is supported.
- **Several users.** There is no sign-in, audit trail or per-user access control, and a
  single worker set owns each data directory.
- **Exposing it to a network.** Anyone who can reach the port can read every case. Keep
  the default loopback address, or put an authenticating proxy or VPN in front of it and
  list its host name in `SCAN3D_ALLOWED_HOSTS`.
- **MRI by default.** It is implemented but experimental, and is off unless you set
  `SCAN3D_MODALITIES=CT,MR`.

## Troubleshooting

| Symptom                                 | What to do                                                                                  |
| --------------------------------------- | ------------------------------------------------------------------------------------------- |
| `dcm2niix is required`                  | Run `scripts/build_dcm2niix.sh`, or set `SCAN3D_DCM2NIIX`.                                  |
| Compressed CT will not import           | Your `dcm2niix` may lack JPEG 2000 or JPEG-LS support. Use the pinned build.                |
| Blank page at the app's address         | The viewer is not built. Run `scripts/bootstrap.sh` again without `--skip-web`.             |
| `Invalid host header`                   | Add the name you browse to in `SCAN3D_ALLOWED_HOSTS`.                                       |
| The app shows **GPU setup required**    | Run `scripts/check_gpu.py`, or set `SCAN3D_DEVICE=auto` or `cpu`.                           |
| Import by path is refused               | Add the folder to `SCAN3D_IMPORT_ROOTS`. Browser upload needs no entry.                     |
| AI task fails with an out-of-memory error | Close other GPU work, choose a larger GPU, or set `SCAN3D_DEVICE=cpu`, which moves only the default path. |
| A GPU shows **· unavailable**           | Read its reason, then run `scripts/add_inference_path.sh nvidia` again. A version reason also needs your existing install updated. |
| Port 9010 is taken                      | Set `SCAN3D_PORT` in `scan3d.env`.                                                          |

`.venv/bin/scan3d doctor` reports the device, PyTorch build, `dcm2niix` status and
inference paths.

Next: [configuration](CONFIGURATION.md), then the [user guide](USER_GUIDE.md).

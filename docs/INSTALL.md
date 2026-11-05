# Installing 3DScan

3DScan runs on Linux. `scripts/bootstrap.sh` installs everything except the system packages into
the checkout, and needs no root access itself.

**3DScan is for research use only.** It is not a medical device and is not cleared or approved for
diagnosis or treatment. Keep it on a machine and network you control. Sign-in is off by default on
a loopback address, and any other address [needs an account](CONFIGURATION.md#sign-in). See
[What is not supported](#what-is-not-supported).

## Requirements

| Item         | Requirement                                                                                 |
| ------------ | ------------------------------------------------------------------------------------------- |
| Linux        | x86_64 with glibc 2.28 or newer. arm64 may work but is untested.                            |
| System tools | git, curl, a C++ compiler, cmake, pkg-config, and OpenJPEG and zlib development packages    |
| Node.js      | 22.12 or newer, to build the viewer                                                         |
| Python       | Nothing to install: the bootstrap fetches Python 3.14 with [uv](https://docs.astral.sh/uv/) |
| Browser      | Chrome, Chromium or Edge with WebGL 2. Other browsers are untested.                         |
| Memory       | At least 16 GB of RAM. Large CTs and AI tasks can use much more.                            |
| Disk         | About 3.5 GB for a CPU install and 9 GB for AMD, plus AI weights and your cases.            |
| GPU          | Optional. See [Choosing how AI runs](#choosing-how-ai-runs).                                |

## Worked example: Ubuntu

These commands take a fresh Ubuntu 24.04 or 26.04 server or desktop to a running app.

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
git clone https://github.com/Truth129/3DScan.git
cd 3DScan
scripts/bootstrap.sh --torch cpu
```

For a GPU, take the flag from [Choosing how AI runs](#choosing-how-ai-runs). The first run takes a
few minutes on a good connection. It creates `.venv/`, builds a pinned `dcm2niix`, builds the viewer
and copies `scan3d.env.example` to `scan3d.env`.

### 4. Check it and start it

```bash
.venv/bin/scan3d doctor
.venv/bin/scan3d serve
```

Open <http://127.0.0.1:9010>. On a remote server, use an SSH tunnel rather than opening the port:

```bash
ssh -L 9010:127.0.0.1:9010 user@server
```

To try it without any scans, choose **Explore synthetic demo** in the app, which shows while the
Library is empty, or run `.venv/bin/scan3d demo`. The demo does not run AI models.

### 5. Add the AI models (optional)

See [AI models](#ai-models). Only nnInteractive needs a command:

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

| Option            | Effect                                                                      |
| ----------------- | --------------------------------------------------------------------------- |
| `--torch cpu`     | CPU-only PyTorch and AI packages. This is the default.                      |
| `--torch cuda`    | PyTorch for NVIDIA GPUs and AI packages. Untested.                          |
| `--torch rocm`    | The hash-locked AMD stack in `requirements/rocm10-py314.lock`, for gfx1201. |
| `--torch none`    | Viewer and editing only, with no PyTorch or AI packages.                    |
| `--skip-dcm2niix` | Use the `dcm2niix` on `PATH`. It must handle your compression formats.      |
| `--skip-web`      | Do not build the viewer.                                                    |

Run the script again at any time to update an install. It keeps `scan3d.env`.

## Choosing how AI runs

`SCAN3D_DEVICE` in `scan3d.env` selects the device:

- `auto`, the default: a GPU if PyTorch sees one, preferring a discrete GPU with the most memory,
  otherwise the CPU.
- `gpu` or `gpu:N`: a GPU. These fail rather than falling back to the CPU.
- `cpu`: always the CPU.

| Setup                       | Install flag   | Status                                                                                |
| --------------------------- | -------------- | ------------------------------------------------------------------------------------- |
| CPU                         | `--torch cpu`  | Works, but is slow on real CTs. TotalSegmentator's authors recommend a GPU.           |
| AMD Radeon AI PRO (gfx1201) | `--torch rocm` | Supported. The most exercised GPU stack.                                              |
| NVIDIA as a second GPU      | See below      | Supported: an RTX 5070 Ti beside an R9700. See [Add a second GPU](#add-a-second-gpu). |
| NVIDIA as the only GPU      | `--torch cuda` | **Untested.** Needs CUDA 13.                                                          |
| Other AMD GPUs              | Manual         | Untested. Edit the device extras in `requirements/rocm10-py314-gpu.in`.               |

For a GPU, install the vendor's driver first. On AMD, your user needs access to the render nodes,
usually through the `render` and `video` groups. Then exercise the GPU once:

```bash
.venv/bin/python scripts/check_gpu.py
```

This runs a small 3D convolution and records the result in `.runtime/gpu-proof.json`, which the app
reads to show the GPU's name. It does not prove that a model produces good results. Run it again
after changing the PyTorch or driver version or that vendor's cards. It refuses to run while a job
is queued or running, or when it cannot read the job queue.

## Add a second GPU

A PyTorch build drives one vendor's GPUs, so AMD and NVIDIA cards together need a second Python
environment, an **inference path** whose GPU you can [choose in the app](USER_GUIDE.md#choose-a-gpu).
Your existing install stays the default path, and all paths share the AI weights, the license and
your cases.

The tested pairing is an RTX 5070 Ti beside an AMD Radeon AI PRO R9700, for TotalSegmentator and
AI assist. Results from the two cards are not identical.

```bash
scripts/add_inference_path.sh nvidia --dry-run   # checks and prints the plan, changing nothing
scripts/add_inference_path.sh nvidia
```

`--venv DIR` sets where the environment goes, by default `.venv-nvidia-py314`. The script refuses
a directory that exists unless it made it.

**Requirements**, checked before anything changes: the `uv` and Python 3.14 that the bootstrap put
in `.tools/`, or on `PATH` (the script never installs a Python), `nvidia-smi` reporting driver 580
or newer, and for a first install 20 GiB free where the environment goes and where uv keeps its
cache.

A first install downloads about 3.3 GiB, and the environment and uv's cache can each take about
7 GiB.

**Your existing install needs the same TotalSegmentator, nnU-Net and nnInteractive versions.** The
script cannot check this, but the app shows the GPU as unavailable and says why. An install made
with `--torch none` has none of them: add them first with `scripts/bootstrap.sh --torch cpu`.

**Cautions:**

- Do not add a GPU with `--torch cuda`, which changes `.venv` and rebuilds the viewer and the
  converter.
- A rebuild removes everything in the environment's directory except the `.scan3d-inference-path`
  marker, so keep nothing of your own there, and never put another environment at that name during
  a run.
- `target` or `prefix` under `[pip]` or `no-cache` in a `uv.toml`, and an index that forbids
  storing what it serves, are not supported.
- The lock is `requirements/cuda-py314.lock`. uv checks its hashes only when it fetches a package,
  and the tools that build the source-only acvl-utils are fetched without hashes.

The script ends with a GPU check. Its proof, `nvidia.json` in `.runtime/inference-paths/` or
`SCAN3D_INFERENCE_PATH_DIR`, is the whole registration: once the check passes, a running app that
reads that folder offers the GPU the next time a panel opens, with no restart. Like the default
path's check, it does not prove that a model fits in memory or gives good results.

**Run the script again** after a driver change, after anything changes that environment, and
whenever the app shows the GPU as unavailable. It keeps a finished environment that equals the
lock, with no install and no network, and rebuilds anything else from empty.

A finished environment is rebuilt only when the job queue can be read and is empty, and once uv has
built its replacement elsewhere. If not, it stays as it was.
`.venv/bin/python scripts/check_gpu.py --queue` runs the queue rule alone.

**If something goes wrong:**

- **The GPU is unavailable or missing in the app.** Fix the reason it gives, if any, and run the
  script again.
- **The check was refused**, because a job is queued or running or the queue cannot be read. Nothing
  is written, so the app goes by the last proof, if there is one. Run the script again once the
  queue can be read and is empty.
- **A job says PyTorch cannot access a GPU, or names its device check or nnInteractive worker.** The
  app cannot see a driver change, so check the driver and run the script again.
- **A re-run does not help**, because uv cannot read the environment, or files are damaged while its
  package versions equal the lock. With no job queued or running, move the directory aside and run
  the script, which then needs the free space above. If the same fault returns, clear that package
  from uv's cache and do this again.

With uv in `.tools/bin` and the script's default cache:

```bash
UV_CACHE_DIR=.cache/uv .tools/bin/uv cache clean <package>  # with no package, clears everything
UV_CACHE_DIR=.cache/uv .tools/bin/uv cache prune            # removes a killed trial's folder
```

**To remove a path**, move its proof out of the folder. The GPU leaves the app the next time a panel
opens, and a job already queued on it fails. The environment stays until you delete it.

```bash
mkdir -p .runtime/archive
mv .runtime/inference-paths/nvidia.json .runtime/archive/
```

An idle nnInteractive process keeps the card's memory until a segmentation, AI assist on another
GPU or an app restart stops it.

**Two cards of one vendor** can share one environment, untested on real cards. With that
environment's Python, `scripts/check_gpu.py --path second --device gpu:1` records the second card
as its own path. Repeat it after each re-run of the script. A task with a crop model, currently
`teeth`, fails on an index above 0.

## AI models

Models are downloaded separately and are not part of this repository. Your scans are never sent to
a model host.

**TotalSegmentator** downloads its weights into `TOTALSEG_HOME_DIR`, by default
`.runtime/totalsegmentator`, the first time a task runs. The bootstrap turns off its anonymous usage
statistics. Some subtasks need a [license from the
provider](https://backend.totalsegmentator.com/license-academic/): enter a key under **Segment CT →
TotalSegmentator license**.

**nnInteractive** powers assisted corrections. `scripts/download_interactive_model.py` fetches the
pinned `nnInteractive_v1.0` model into `.runtime/nninteractive/` and checks its SHA-256.

**MRSegmentator** is an optional second model for comparison. It needs about 1.2 GB:

```bash
.venv/bin/python scripts/download_segmentation_model.py mrsegmentator
```

The weights go to `.runtime/segmentation-models/`. Every file is checked against a pinned SHA-256
when it is downloaded and before each run. Requests never download them.

**Check the licenses before you use them.** nnInteractive's
[pretrained weights](https://huggingface.co/MIC-DKFZ/nnInteractive) are **CC BY-NC-SA 4.0, which is
noncommercial**, as are some TotalSegmentator subtasks. [Licenses](../README.md#licenses) lists each
model's terms, which you must accept and follow yourself.

## Run it as a service

`deployment/scan3d.service` is a systemd user unit. This installs it for your checkout and starts
it:

```bash
mkdir -p ~/.config/systemd/user
sed "s|%h/3DScan|$PWD|g" deployment/scan3d.service > ~/.config/systemd/user/scan3d.service
systemctl --user daemon-reload
systemctl --user enable --now scan3d.service
loginctl enable-linger "$USER"
```

`enable-linger` keeps it running after you log out. Follow its log with
`journalctl --user -u scan3d.service -f`. Only one worker may use a data directory, so stop the
service before running `scan3d serve` by hand on the same library.

## Back up your cases

```bash
.venv/bin/scan3d backup /path/on/another/disk/scan3d-backups
```

See the [user guide](USER_GUIDE.md) for what a backup contains and how to restore it.

## What is not supported

- **Clinical use.** Nothing here has regulatory clearance or clinical validation.
- **macOS and Windows.** Only Linux is supported.
- **Private cases.** Every account sees every case.
- **Plain HTTP beyond loopback.** A sign-in needs HTTPS there. See
  [Sign-in](CONFIGURATION.md#sign-in).
- **MRI by default.** It is implemented but experimental, and is off unless you set
  `SCAN3D_MODALITIES=CT,MR`.

## Troubleshooting

| Symptom                                   | What to do                                                                                                                         |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `dcm2niix is required`                    | Run `scripts/build_dcm2niix.sh`, or set `SCAN3D_DCM2NIIX`.                                                                         |
| Compressed CT will not import             | Your `dcm2niix` may lack JPEG 2000 or JPEG-LS support. Use the pinned build.                                                       |
| Blank page at the app's address           | The viewer is not built. Run `scripts/bootstrap.sh` again without `--skip-web`.                                                    |
| `Invalid host header`                     | Add the name you browse to in `SCAN3D_ALLOWED_HOSTS`.                                                                              |
| `Sign in through the HTTPS address`       | Open the app at its HTTPS address. Beyond loopback, plain HTTP cannot sign in.                                                     |
| `SCAN3D_AUTH=off is refused…`             | Remove `SCAN3D_AUTH=off`, or keep every host on loopback. See [Sign-in](CONFIGURATION.md#sign-in).                                 |
| The app shows **GPU setup required**      | Run `scripts/check_gpu.py`, or set `SCAN3D_DEVICE=auto` or `cpu`.                                                                  |
| Import by path is refused                 | Add the folder to `SCAN3D_IMPORT_ROOTS`. Browser upload needs no entry.                                                            |
| AI task fails with an out-of-memory error | Close other GPU work, choose a larger GPU, or set `SCAN3D_DEVICE=cpu`, which moves only the default path.                          |
| A GPU shows **· unavailable**             | Read its reason, then run `scripts/add_inference_path.sh nvidia` again. A version reason also needs your existing install updated. |
| Port 9010 is taken                        | Set `SCAN3D_PORT` in `scan3d.env`.                                                                                                 |

`.venv/bin/scan3d doctor` reports the device, PyTorch build, `dcm2niix` status and inference paths.

Next: [configuration](CONFIGURATION.md), then the [user guide](USER_GUIDE.md).

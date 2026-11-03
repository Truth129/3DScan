# Installing 3DScan

3DScan runs on Linux. `scripts/bootstrap.sh` installs everything except the system packages into
the checkout, and needs no root access itself.

**3DScan is for research use only.** It is not a medical device and is not cleared or approved for
diagnosis or treatment. Keep it on a machine and network you control. Sign-in is off by default on
a loopback address, and any other address [needs an account](CONFIGURATION.md#sign-in). See
[What is not supported](#what-is-not-supported).

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
after changing the PyTorch or driver version or that vendor's cards, but not while a job is queued
or running.

## Add a second GPU

A PyTorch build drives one vendor's GPUs, so using AMD and NVIDIA cards together needs a second
Python environment. `scripts/add_inference_path.sh` builds this **inference path**: an interpreter
plus a GPU you can [choose in the app](USER_GUIDE.md#choose-a-gpu). Your existing install stays the
default path, unchanged, and all paths share the AI weights, the license and your cases.

An RTX 5070 Ti is supported as a second GPU beside an AMD Radeon AI PRO R9700, for TotalSegmentator
and AI assist. Results from the two cards are not identical.

On a 512 x 512 x 833 CT, `total` took about 89 s on an RTX 5070 Ti and 96 s on an R9700, and the
5070 Ti peaked at about 11 GiB including the desktop.

```bash
scripts/add_inference_path.sh nvidia --dry-run
scripts/add_inference_path.sh nvidia
```

| Option       | Effect                                                                                    |
| ------------ | ----------------------------------------------------------------------------------------- |
| `--venv DIR` | Where the environment goes, by default `.venv-nvidia-py314`. It refuses a directory that exists unless it made it. |
| `--dry-run`  | Check this workstation and print the plan, changing nothing.                              |

**Before it changes anything**, the script needs:

- `uv`, from `.tools/bin` or `PATH`
- Python 3.14, which it never installs. It also searches `.tools/python`
- `nvidia-smi` reporting driver 580 or newer
- For a first install, at least 20 GiB free where the environment goes and where uv keeps its
  cache. It downloads about 3.3 GiB, and the environment and cache can each take about 7 GiB

**Your existing install needs the same TotalSegmentator, nnU-Net and nnInteractive versions.** The
script cannot check this, but the app shows the GPU as unavailable and says why. An install made
with `--torch none` has none of them: add them first with `scripts/bootstrap.sh --torch cpu`.

**Cautions:**

- Do not add a GPU with `--torch cuda`, which changes `.venv` and rebuilds the viewer and the
  converter.
- Keep nothing of your own in the environment's directory. A rebuild removes everything there
  except the `.scan3d-inference-path` marker. Never put another environment at that name during a
  run.
- `target` or `prefix` under `[pip]` or `no-cache` in a `uv.toml`, and an index that forbids
  storing what it serves, are not supported.
- uv checks the hashes in `requirements/cuda-py314.lock` only when it fetches a package. The tools
  that build acvl-utils, which is source-only, are fetched without hashes.

**The GPU check** comes last. `scripts/check_gpu.py --path nvidia` writes a proof to
`.runtime/inference-paths/nvidia.json`, or under `SCAN3D_INFERENCE_PATH_DIR` if you set it. That
file is the whole registration: when the check passes, a running app that reads that folder offers
the GPU the next time a panel opens, with no restart.

The check runs a small 3D convolution, not either model, so it does not prove that a model fits in
the card's memory or that results are good.

**Run the script again** after changing the NVIDIA driver, after anything changes that environment,
and whenever the app shows the GPU as unavailable. It then repeats the GPU check.

- An environment that finished and equals the lock stays, with no install and no network.
- Any other finished environment is rebuilt from empty, but only if the job queue is empty and a
  trial in uv's cache first builds the replacement. If not, it stays as it was.
  `scripts/check_gpu.py --queue` runs the queue rule alone.
- A first or stopped install starts from empty, with no trial and no queue rule.

**If something goes wrong:**

- **The check failed.** The app lists the GPU as unavailable with its reason. Fix that and run the
  script again.
- **The check was refused**, because a job is queued or running or the queue cannot be read.
  Nothing is written, so the app goes by the last proof, if there is one.
- **An install stopped.** No check ran, and the app shows the GPU as unavailable, or not at all if
  it was never checked. Run the script again.
- **A job says PyTorch cannot access a GPU, or names its device check or nnInteractive worker.** The
  app cannot see a driver change, so check the driver and run the script again.
- **A re-run does not help.** uv cannot read the environment, or files are damaged while its
  package versions equal the lock. With no job queued or running, move the directory aside and run
  the script, which then needs the free space above.
- **The same fault returns.** The damage may be in uv's cache, which nothing checks. Clear that
  package, then move the directory aside and run the script again.

With uv in `.tools/bin` and the script's default cache:

```bash
UV_CACHE_DIR=.cache/uv .tools/bin/uv cache clean <package>  # with no package, clears everything
UV_CACHE_DIR=.cache/uv .tools/bin/uv cache prune            # removes a killed trial's folder
```

**To remove a path**, move its proof out of the folder. The GPU leaves the app the next time a panel
opens, with no restart, and a job already queued on it fails, naming the GPU. The environment stays
until you delete it.

```bash
mkdir -p .runtime/archive
mv .runtime/inference-paths/nvidia.json .runtime/archive/
```

An idle nnInteractive process keeps the card's memory until a segmentation, AI assist on another
GPU or an app restart stops it.

**Two cards of one vendor** can share one environment, though this is untested on real cards. With
that environment's Python, `scripts/check_gpu.py --path second --device gpu:1` records the second
card as its own path, and must be repeated after a re-run of the script. A task with a crop model,
currently `teeth`, fails on an index above 0.

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

**Check the licenses before you use them.** The nnInteractive engine is Apache-2.0, but its
[pretrained weights](https://huggingface.co/MIC-DKFZ/nnInteractive) are **CC BY-NC-SA 4.0, which is
noncommercial**, as are some TotalSegmentator subtasks. MRSegmentator's code is Apache-2.0, and its
repository states no separate license for the weights. You are responsible for accepting and
following these terms. 3DScan's own Apache-2.0 license does not change them.

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

## Upgrade to a version with sign-in

An install on loopback alone keeps working with sign-in off. A host published beyond loopback now
needs an account before anyone can use the app:

```bash
.venv/bin/scan3d users add NAME --admin
```

- Existing cases go to the first admin.
- The command line adds admins only. Members come from People, with a link.
- Sign out once on each device to clear older cached scan data.

The [user guide](USER_GUIDE.md#sign-in) covers everyday use.

## What is not supported

- **Clinical use.** Nothing here has regulatory clearance or clinical validation.
- **macOS and Windows.** Only Linux is supported.
- **Private cases.** Every account sees every case. A single worker set owns each data directory.
- **Plain HTTP beyond loopback.** A sign-in is refused there. Keep the default loopback address, or
  put an HTTPS reverse proxy in front of it and list its host name in `SCAN3D_ALLOWED_HOSTS`.
- **MRI by default.** It is implemented but experimental, and is off unless you set
  `SCAN3D_MODALITIES=CT,MR`.

## Troubleshooting

| Symptom                                   | What to do                                                                                                                          |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `dcm2niix is required`                    | Run `scripts/build_dcm2niix.sh`, or set `SCAN3D_DCM2NIIX`.                                                                          |
| Compressed CT will not import             | Your `dcm2niix` may lack JPEG 2000 or JPEG-LS support. Use the pinned build.                                                        |
| Blank page at the app's address           | The viewer is not built. Run `scripts/bootstrap.sh` again without `--skip-web`.                                                     |
| `Invalid host header`                     | Add the name you browse to in `SCAN3D_ALLOWED_HOSTS`.                                                                               |
| `Sign in through the HTTPS address`       | Open the app at its HTTPS address. Beyond loopback, plain HTTP cannot sign in.                                                      |
| `SCAN3D_AUTH=off is refused…`             | Remove `SCAN3D_AUTH=off`, or keep every host on loopback. See [Sign-in](CONFIGURATION.md#sign-in).                                  |
| The app shows **GPU setup required**      | Run `scripts/check_gpu.py`, or set `SCAN3D_DEVICE=auto` or `cpu`.                                                                   |
| Import by path is refused                 | Add the folder to `SCAN3D_IMPORT_ROOTS`. Browser upload needs no entry.                                                             |
| AI task fails with an out-of-memory error | Close other GPU work, choose a larger GPU, or set `SCAN3D_DEVICE=cpu`, which moves only the default path.                           |
| A GPU shows **· unavailable**             | Read its reason, then run `scripts/add_inference_path.sh nvidia` again. A version reason also needs your existing install updated.  |
| Port 9010 is taken                        | Set `SCAN3D_PORT` in `scan3d.env`.                                                                                                  |

`.venv/bin/scan3d doctor` reports the device, PyTorch build, `dcm2niix` status and inference paths.

Next: [configuration](CONFIGURATION.md), then the [user guide](USER_GUIDE.md).

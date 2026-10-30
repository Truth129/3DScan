# 3DScan

A local, AI-assisted case-analysis workbench for radiology research. It imports CT
studies, segments them with [TotalSegmentator](https://github.com/wasserth/TotalSegmentator),
lets you correct the results with [nnInteractive](https://github.com/MIC-DKFZ/nnInteractive)
or direct edits, and keeps an exact history of every change. It is designed to sit
alongside established DICOM viewers, not to replace them.

> **Research use only. Not a medical device.** 3DScan is not cleared or approved for
> diagnosis, treatment or any other clinical decision. Its AI models are research tools,
> and their output can be wrong. It comes with no warranty. See [LICENSE](LICENSE).

Everything runs on your own machine. Scans are never uploaded to a model host or any
other service.

## Features

- **DICOM import.** Browser upload, the `scan3d import` command, or an optional
  [Orthanc](docs/ORTHANC.md) archive. CT is on by default; static MRI is experimental and
  opt-in.
- **Segmentation.** Selected TotalSegmentator tasks, on a GPU or the CPU.
- **Corrections.** nnInteractive prompts, direct mask edits, scissors and recipes. Every
  save is a new revision with an exact parent, so undo is exact too.
- **Viewer.** Linked multiplanar slices and 3D volume rendering built on Cornerstone3D and
  vtk.js, with surfaces, masking and slabs. Three.js covers sheared stacks and offline
  exports.
- **Measurements.** Volumes from masks in patient space, and AI-agreement metrics that
  compare corrected masks with the original model output.
- **Exchange.** DICOM SEG import and export, 3D Slicer exchange, PNG, and portable offline
  HTML.

## Quick start

On Ubuntu, with `git`, Node.js 22.12 or newer, and the build tools listed in
[INSTALL.md](docs/INSTALL.md):

```bash
git clone https://github.com/OWNER/3DScan.git
cd 3DScan
scripts/bootstrap.sh --torch cpu
.venv/bin/scan3d serve
```

Open <http://127.0.0.1:9010> and choose **Explore synthetic demo**, or import a scan.

Use `--torch cuda` for an NVIDIA GPU. `scripts/bootstrap.sh --help` lists the options.
Settings such as the port, the data directory and the Orthanc connection live in a
git-ignored `scan3d.env`. See [configuration](docs/CONFIGURATION.md).

## Documentation

| Read this                                | For                                                       |
| ---------------------------------------- | --------------------------------------------------------- |
| [Install](docs/INSTALL.md)               | Ubuntu walkthrough, other distributions, GPUs, AI models  |
| [Configuration](docs/CONFIGURATION.md)   | Every setting in `scan3d.env`                             |
| [User guide](docs/USER_GUIDE.md)         | Importing, reviewing, correcting and exporting            |
| [Orthanc](docs/ORTHANC.md)               | Connecting to a DICOM archive                             |
| [Architecture](docs/ARCHITECTURE.md)     | How the system works                                      |
| [API](docs/API.md)                       | HTTP API and data model                                   |
| [Development](docs/DEVELOPMENT.md)       | Tests, checks and project rules                           |

## Platform support

3DScan runs on Linux. These combinations have been checked:

| Setup                               | Status                                                  |
| ----------------------------------- | ------------------------------------------------------- |
| Ubuntu 24.04 and 26.04, CPU         | Install, app and test suite verified on clean machines  |
| AMD GPU (gfx1201) with ROCm         | Used extensively                                        |
| NVIDIA GPU with CUDA                | **Untested**: no hardware was available                 |
| Other Linux distributions, arm64    | Expected to work, untested unless listed in the install guide |
| macOS, Windows                      | Not supported                                           |

CPU inference works but is slow on full-size CTs.

## Safety and privacy

- **There is no sign-in.** Anyone who can reach the port can read every case. By default
  3DScan listens on `127.0.0.1` only. Do not expose it to a network without an
  authenticating proxy or VPN in front of it.
- **Your data directory holds patient images.** Protect and back it up like any other
  clinical data. Never put patient data in a bug report, screenshot or pull request.
- **Check that your use of patient data is allowed** under your own institution's rules.

## Licenses

3DScan is released under the [Apache License 2.0](LICENSE).

The AI models are separate downloads with their own terms, which you must accept yourself:

- **nnInteractive weights:** CC BY-NC-SA 4.0, noncommercial. The engine code is Apache-2.0.
- **TotalSegmentator:** the code is Apache-2.0, but some subtasks need a separate license
  and are noncommercial. See the
  [provider's license page](https://backend.totalsegmentator.com/license-academic/).

3DScan does not change those terms, and using these models commercially may require
permission from their authors. Bundled third-party notices are in
[`web/public/licenses/`](web/public/licenses/).

# 3DScan

A local, AI-assisted case-analysis workbench for radiology research. It imports CT
studies, segments them with [TotalSegmentator](https://github.com/wasserth/TotalSegmentator),
lets you correct the results with [nnInteractive](https://github.com/MIC-DKFZ/nnInteractive)
or direct edits, and keeps an exact history of every change. It sits alongside established
DICOM viewers and does not replace them.

> **Research use only. Not a medical device.** 3DScan is not cleared or approved for
> diagnosis, treatment or any other clinical decision. Its AI models are research tools,
> and their output can be wrong. It comes with no warranty. See [LICENSE](LICENSE).

Everything runs on your own machine. Scans are never uploaded to a model host or any
other service.

## Features

- **Import:** browser upload, `scan3d import`, an optional [Orthanc](docs/ORTHANC.md), or any
  [DICOMweb archive](docs/ARCHIVES.md). Static MRI is experimental and opt-in.
- **Segmentation:** selected TotalSegmentator tasks on a GPU or the CPU, plus optional
  MRSegmentator to compare beside them. With
  [a second GPU](docs/INSTALL.md#add-a-second-gpu), choose which one each AI job runs on.
- **Corrections:** nnInteractive prompts, direct mask edits, scissors and recipes, with
  exact undo.
- **Library:** one library for everyone signed in, with search, rename, Trash with undo, and
  Delete forever. An administrator adds people with a link.
- **Viewer:** linked slices and 3D volume rendering.
- **Measurements:** mask volumes and AI-agreement metrics.
- **Exchange:** DICOM SEG import and export, native OsiriX ROIs, 3D views as new DICOM series,
  archiving by DICOMweb, direct DICOM sends, 3D Slicer, PNG and portable offline HTML.

## Quick start

On Ubuntu, with `git`, Node.js 22.12 or newer, and the build tools listed in
[INSTALL.md](docs/INSTALL.md):

```bash
git clone https://github.com/Truth129/3DScan.git
cd 3DScan
scripts/bootstrap.sh --torch cpu
.venv/bin/scan3d serve
```

Open <http://127.0.0.1:9010> and choose **Explore synthetic demo**, or import a scan.

`scripts/bootstrap.sh --help` lists the install options. Settings live in a git-ignored
`scan3d.env`. See [configuration](docs/CONFIGURATION.md).

## Documentation

| Read this                              | For                                            |
| -------------------------------------- | ---------------------------------------------- |
| [Install](docs/INSTALL.md)             | Linux setup, GPUs, AI models                   |
| [Configuration](docs/CONFIGURATION.md) | Every setting in `scan3d.env`                  |
| [User guide](docs/USER_GUIDE.md)       | Importing, reviewing, correcting and exporting |
| [Orthanc](docs/ORTHANC.md)             | Connecting to an Orthanc archive               |
| [Archives](docs/ARCHIVES.md)           | DICOMweb archives and direct DICOM sends       |
| [Architecture](docs/ARCHITECTURE.md)   | How the system works                           |
| [API](docs/API.md)                     | HTTP API and data model                        |
| [Development](docs/DEVELOPMENT.md)     | Tests, checks and project rules                |

## Platform support

3DScan runs on Linux, not macOS or Windows. [INSTALL.md](docs/INSTALL.md) lists the tested
distributions and GPUs.

## Safety and privacy

- **Sign-in follows the address.** By default 3DScan listens on `127.0.0.1` only, with sign-in
  off, so anything that can reach the port can read every case. Any other address requires an
  account, and a sign-in needs HTTPS there. See [Sign-in](docs/CONFIGURATION.md#sign-in).
- **Your data directory holds patient images.** Protect and back it up like any other
  clinical data. Never put patient data in a bug report, screenshot or pull request.
- **Check that your use of patient data is allowed** under your own institution's rules.

## Licenses

3DScan is released under the [Apache License 2.0](LICENSE). The AI models are separate
downloads with their own terms, which you must accept yourself:

- **nnInteractive weights:** CC BY-NC-SA 4.0, noncommercial. The engine code is Apache-2.0.
- **TotalSegmentator:** the code is Apache-2.0, but some subtasks need a separate license
  and are noncommercial. See the
  [provider's license page](https://backend.totalsegmentator.com/license-academic/).
- **MRSegmentator:** the code is Apache-2.0. Its repository states no separate license for the
  weights.

Using these models commercially may require permission from their authors. Third-party
notices are in [`web/public/licenses/`](web/public/licenses/). A viewer build adds
`licenses/THIRD-PARTY-NOTICES.txt` for the packages it bundles, and portable exports carry theirs.

# Configuration

3DScan reads its settings in this order, and the first value found wins:

1. A real environment variable.
2. The local config file, `scan3d.env` in the checkout.
3. The built-in default.

`scripts/bootstrap.sh` creates `scan3d.env` from `scan3d.env.example` with mode 600. Git
ignores it, so ports, paths and passwords stay out of version control. Set
`SCAN3D_CONFIG=/path/to/file` to read a different file, which must then exist.

Restart 3DScan after changing a setting.

## File format

```bash
# A comment
SCAN3D_PORT=9100
SCAN3D_ORTHANC_PASSWORD="a value with spaces"
```

- Each line is `KEY=VALUE`. The value is the rest of the line, and quotes are optional.
- There are no inline comments, and `$VARIABLES` are not expanded.
- Use absolute paths.
- The file is parsed, not run. systemd's `EnvironmentFile=` accepts the same format.

## Settings

### Server

| Variable               | Default               | Purpose                                                       |
| ---------------------- | --------------------- | ------------------------------------------------------------- |
| `SCAN3D_HOST`          | `127.0.0.1`           | Listen address. `scan3d serve --host` overrides it.           |
| `SCAN3D_PORT`          | `9010`                | Listen port. `scan3d serve --port` overrides it.              |
| `SCAN3D_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Host names browsers may use, separated by commas.             |
| `SCAN3D_AUTH`          | by address            | Sign-in mode: `off` or `password`. See [Sign-in](#sign-in).   |

### Sign-in

The mode depends on `SCAN3D_AUTH` and the hosts in use: `SCAN3D_HOST` or `scan3d serve --host`, and
every name in `SCAN3D_ALLOWED_HOSTS`. Loopback hosts are `127.0.0.1`, `::1`, `[::1]` and `localhost`.
`0.0.0.0` and a wildcard are not loopback.

| `SCAN3D_AUTH`  | All hosts loopback | Any other host   |
| -------------- | ------------------ | ---------------- |
| unset or empty | `off`              | `password`       |
| `off`          | `off`              | Refuses to start |
| `password`     | `password`         | `password`       |

Any other value refuses to start.

- `off`: one built-in user, and anything that can reach the port can read every case.
- `password`: everyone needs an account. Create the first with `scan3d users add NAME --admin`, and
  add others from People. See the [user guide](USER_GUIDE.md#sign-in).
- Beyond loopback a sign-in needs HTTPS, so publish 3DScan through an HTTPS reverse proxy.
- A proxy that rewrites `Host` to loopback hides the published name from this table, so it must set
  `SCAN3D_AUTH=password` itself.

### Storage

| Variable              | Default          | Purpose                                                                 |
| --------------------- | ---------------- | ----------------------------------------------------------------------- |
| `SCAN3D_DATA_DIR`     | `data/`          | The case library. It holds patient images, so protect it accordingly.   |
| `SCAN3D_IMPORT_ROOTS` | the checkout     | Folders the server may import from by path, separated by `:`.           |
| `SCAN3D_WEB_DIR`      | `web/dist/`      | The built viewer.                                                       |

Browser upload needs no import root.

### Imaging

| Variable            | Default | Purpose                                                         |
| ------------------- | ------- | --------------------------------------------------------------- |
| `SCAN3D_MODALITIES` | `CT`    | Modalities offered. `CT,MR` enables experimental MRI support.   |

Enabling a modality later makes series already in the library available.

### AI compute

| Variable                             | Default                     | Purpose                                                      |
| ------------------------------------ | --------------------------- | ------------------------------------------------------------ |
| `SCAN3D_DEVICE`                      | `auto`                      | `auto`, `gpu`, `gpu:N` or `cpu`.                             |
| `SCAN3D_GPU_PROOF`                   | `.runtime/gpu-proof.json`   | Result file written by `scripts/check_gpu.py`.               |
| `SCAN3D_INFERENCE_PATH_DIR`          | `.runtime/inference-paths`  | Proof files of added inference paths, one for each path.     |
| `TOTALSEG_HOME_DIR`                  | `.runtime/totalsegmentator` | TotalSegmentator's weights, license and settings.            |
| `TOTALSEG_WEIGHTS_PATH`              | under `TOTALSEG_HOME_DIR`   | Use weights stored elsewhere.                                |
| `SCAN3D_NNINTERACTIVE_MODEL`         | `.runtime/nninteractive/…`  | The pinned nnInteractive model folder.                       |
| `SCAN3D_NNINTERACTIVE_STALL_SECONDS` | built in                    | How long a correction may run without progress.              |
| `SCAN3D_SEGMENTATION_MODELS`         | `.runtime/segmentation-…`   | Folder that holds the pinned nnU-Net models.                 |
| `SCAN3D_MESH_THREADS`                | `16`                        | Threads for mesh generation.                                 |
| `SCAN3D_POSTPROCESS_WORKERS`         | `4`                         | Structures processed at once after segmentation.             |
| `SCAN3D_DCM2NIIX`                    | `.tools/bin`, then `PATH`   | The DICOM converter.                                         |

`SCAN3D_DEVICE` and `SCAN3D_GPU_PROOF` describe the default
[inference path](INSTALL.md#add-a-second-gpu). A PyTorch build sees only its own vendor's
GPUs, so `auto` never chooses between AMD and NVIDIA, and the `N` in `gpu:N` counts within
one build.

`SCAN3D_INFERENCE_PATH_DIR`:

- A blank value means the default.
- Changing it in `scan3d.env` needs a restart. Adding or moving a proof file does not: the
  app reads the folder each time a panel opens and each time a job starts.
- `scripts/check_gpu.py --path` and `scripts/add_inference_path.sh` read it too, and
  `SCAN3D_DATA_DIR` for the job queue that guards a rebuild. Run them with the values the app
  sees.
- A proof names an interpreter the app will run, so protect a folder outside the checkout
  like the checkout itself.

Variables the runtime reads, such as `XDG_CACHE_HOME` or the `MIOPEN_*` caches on AMD systems,
can go in `scan3d.env` too. Every variable in the file reaches the default path's workers. An
added path's workers inherit the same environment, except for these:

| Variable                                        | Withheld because                         |
| ----------------------------------------------- | ---------------------------------------- |
| `CUDA_VISIBLE_DEVICES`, `HIP_VISIBLE_DEVICES`, `ROCR_VISIBLE_DEVICES`, `GPU_DEVICE_ORDINAL` | They select another stack's cards. Keep them out of `scan3d.env` and any launcher: no choice of card needs them, and HIP reads `CUDA_VISIBLE_DEVICES` too. |
| `LD_LIBRARY_PATH`, `LD_PRELOAD`                 | They could load another stack's libraries over the runtime's own. |
| `nnUNet_compile`                                | It would turn on compiled models there, which no check covers and no recorded result would show. It still reaches the default path, so leave it unset or the two paths will differ. |
| `TORCHINDUCTOR_CACHE_DIR`, `TRITON_CACHE_DIR`   | They point at another stack's compiled kernels. |

`scripts/check_gpu.py --path` removes the same variables before it loads PyTorch. A process
cannot take `LD_LIBRARY_PATH` or `LD_PRELOAD` back from itself, so
`scripts/add_inference_path.sh` clears those two first. Clear them yourself if you run the
check by hand.

An added path's workers also get their own `XDG_CACHE_HOME`, under
`.cache/inference-paths/<id>`, that path's `SCAN3D_DEVICE`, and that environment's `bin`
folder first on `PATH`. `SCAN3D_INFERENCE_PATH`, set to the path's ID, is not a setting: it
only changes the advice a failed device check gives. Every path shares the weights, the
license and the nnInteractive model.

### Orthanc

Orthanc is optional, and the integration is off until `SCAN3D_ORTHANC_URL` is set.
[Configure 3DScan](ORTHANC.md#configure-3dscan) has an example.

| Variable                             | Default | Purpose                                                       |
| ------------------------------------ | ------- | ------------------------------------------------------------- |
| `SCAN3D_ORTHANC_URL`                 | unset   | The address the 3DScan server uses for Orthanc's REST API.    |
| `SCAN3D_ORTHANC_USERNAME`            | unset   | REST API user.                                                |
| `SCAN3D_ORTHANC_PASSWORD`            | unset   | REST API password.                                            |
| `SCAN3D_ORTHANC_EXPORT_DESTINATIONS` | unset   | Orthanc modality identifiers offered for SEG and view export. |
| `SCAN3D_ORTHANC_CA_BUNDLE`           | unset   | Absolute path to a PEM file, for an Orthanc with a private CA. |

### DICOMweb archive

A DICOMweb archive is optional, and off until `SCAN3D_DICOMWEB_URL` is set.
[ARCHIVES.md](ARCHIVES.md) explains importing from one.

| Variable                         | Default         | Purpose                                                                                     |
| -------------------------------- | --------------- | ------------------------------------------------------------------------------------------- |
| `SCAN3D_DICOMWEB_URL`            | unset           | The archive's DICOMweb root. Not on the same host and port as `SCAN3D_ORTHANC_URL`.          |
| `SCAN3D_DICOMWEB_NAME`           | `DICOM archive` | The name shown in 3DScan.                                                                   |
| `SCAN3D_DICOMWEB_ID`             | unset           | The archive's name in case records: letters, digits, `.`, `_` or `-`. Set it before the first import; without it, the URL is used. |
| `SCAN3D_DICOMWEB_AUTH`           | `none`          | `none`, `basic` or `bearer`. Credentials need https unless the archive is on this machine.  |
| `SCAN3D_DICOMWEB_USERNAME`       | unset           | User, for `basic`.                                                                          |
| `SCAN3D_DICOMWEB_PASSWORD`       | unset           | Password, for `basic`.                                                                      |
| `SCAN3D_DICOMWEB_TOKEN_FILE`     | unset           | Absolute path to the token, for `bearer`. Read again whenever the file changes.             |
| `SCAN3D_DICOMWEB_CA_BUNDLE`      | unset           | Absolute path to a PEM file, for an archive with a private CA.                              |
| `SCAN3D_DICOMWEB_TIMEOUT`        | `30`            | Request timeout in seconds, 1 to 300.                                                       |
| `SCAN3D_DICOMWEB_QUIET_SECONDS`  | `60`            | Seconds, 0 to 3600, that a series' image list must stay unchanged before it can be imported. |
| `SCAN3D_DICOMWEB_WILDCARDS`      | `false`         | `true` lets searches use `*` and `?`.                                                       |
| `SCAN3D_DICOMWEB_STORE`          | `true`          | `false` keeps the archive read-only: SEG and views are not archived there.                  |

The server refuses to start if the token or CA file cannot be read.

## Examples

Keep the case library on another disk and allow imports from a scans folder:

```bash
SCAN3D_DATA_DIR=/mnt/cases/3dscan
SCAN3D_IMPORT_ROOTS=/mnt/scans
```

Serve behind an HTTPS reverse proxy on the same machine. The published name turns sign-in on:

```bash
SCAN3D_ALLOWED_HOSTS=127.0.0.1,localhost,scan.example.test
```

For a one-off run, set a variable in the shell. It overrides the file:

```bash
SCAN3D_PORT=9200 SCAN3D_DATA_DIR=/tmp/scan3d-trial .venv/bin/scan3d serve
```

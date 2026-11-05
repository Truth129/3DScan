# Configuration

3DScan reads its settings in this order, and the first value found wins:

1. A real environment variable.
2. The local config file, `scan3d.env` in the checkout.
3. The built-in default.

`scripts/bootstrap.sh` creates `scan3d.env` from `scan3d.env.example` with mode 600, and Git
ignores it. Set `SCAN3D_CONFIG=/path/to/file` to read a different file, which must then exist.

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

| Variable               | Default               | Purpose                                                     |
| ---------------------- | --------------------- | ----------------------------------------------------------- |
| `SCAN3D_HOST`          | `127.0.0.1`           | Listen address. `scan3d serve --host` overrides it.         |
| `SCAN3D_PORT`          | `9010`                | Listen port. `scan3d serve --port` overrides it.            |
| `SCAN3D_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Host names browsers may use, separated by commas.           |
| `SCAN3D_AUTH`          | by address            | Sign-in mode: `off` or `password`. See [Sign-in](#sign-in). |

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
- `password`: everyone needs an account. Create the first with `scan3d users add NAME --admin`,
  which also takes any case with no owner, and add others from People. See the
  [user guide](USER_GUIDE.md#sign-in).
- Beyond loopback a sign-in needs HTTPS, so publish 3DScan through an HTTPS reverse proxy.
- A proxy that rewrites `Host` to loopback hides the published name from this table, so set
  `SCAN3D_AUTH=password` for it.

### Storage

| Variable              | Default      | Purpose                                                               |
| --------------------- | ------------ | --------------------------------------------------------------------- |
| `SCAN3D_DATA_DIR`     | `data/`      | The case library. It holds patient images, so protect it accordingly. |
| `SCAN3D_IMPORT_ROOTS` | the checkout | Folders the server may import from by path, separated by `:`.         |
| `SCAN3D_WEB_DIR`      | `web/dist/`  | The built viewer.                                                     |

Browser upload needs no import root.

### Imaging

| Variable            | Default | Purpose                                                       |
| ------------------- | ------- | ------------------------------------------------------------- |
| `SCAN3D_MODALITIES` | `CT`    | Modalities offered. `CT,MR` enables experimental MRI support. |

Enabling a modality later makes series already in the library available.

### AI compute

| Variable                             | Default                     | Purpose                                                  |
| ------------------------------------ | --------------------------- | -------------------------------------------------------- |
| `SCAN3D_DEVICE`                      | `auto`                      | `auto`, `gpu`, `gpu:N` or `cpu`.                         |
| `SCAN3D_GPU_PROOF`                   | `.runtime/gpu-proof.json`   | Result file written by `scripts/check_gpu.py`.           |
| `SCAN3D_INFERENCE_PATH_DIR`          | `.runtime/inference-paths`  | Proof files of added inference paths, one for each path. |
| `TOTALSEG_HOME_DIR`                  | `.runtime/totalsegmentator` | TotalSegmentator's weights, license and settings.        |
| `TOTALSEG_WEIGHTS_PATH`              | under `TOTALSEG_HOME_DIR`   | Use weights stored elsewhere.                            |
| `SCAN3D_NNINTERACTIVE_MODEL`         | `.runtime/nninteractive/…`  | The pinned nnInteractive model folder.                   |
| `SCAN3D_NNINTERACTIVE_STALL_SECONDS` | built in                    | How long a correction may run without progress.          |
| `SCAN3D_SEGMENTATION_MODELS`         | `.runtime/segmentation-…`   | Folder that holds the pinned nnU-Net models.             |
| `SCAN3D_MESH_THREADS`                | `16`                        | Threads for mesh generation.                             |
| `SCAN3D_POSTPROCESS_WORKERS`         | `4`                         | Structures processed at once after segmentation.         |
| `SCAN3D_DCM2NIIX`                    | `.tools/bin`, then `PATH`   | The DICOM converter.                                     |

`SCAN3D_DEVICE` and `SCAN3D_GPU_PROOF` describe the default
[inference path](INSTALL.md#add-a-second-gpu). A PyTorch build sees only its own vendor's GPUs, so
`auto` never chooses between AMD and NVIDIA, and the `N` in `gpu:N` counts within one build.

`SCAN3D_INFERENCE_PATH_DIR`, when blank, means the default. Adding or moving a proof file needs no
restart, because the app reads the folder whenever a panel opens or a job starts.

- `scripts/check_gpu.py --path` and `scripts/add_inference_path.sh` read it, and `SCAN3D_DATA_DIR`
  for the job queue, so run them with the values the app sees.
- A proof names an interpreter the app will run, so protect a folder outside the checkout like the
  checkout itself.

Every variable in `scan3d.env`, such as `XDG_CACHE_HOME` or AMD's `MIOPEN_*` caches, reaches the
default path's workers. An added path's workers get the same environment, except:

- `CUDA_VISIBLE_DEVICES`, `HIP_VISIBLE_DEVICES`, `ROCR_VISIBLE_DEVICES` and `GPU_DEVICE_ORDINAL`
  are withheld. Keep them out of `scan3d.env` and any launcher: no card choice needs them, and HIP
  reads `CUDA_VISIBLE_DEVICES` too.
- `LD_LIBRARY_PATH`, `LD_PRELOAD`, `TORCHINDUCTOR_CACHE_DIR` and `TRITON_CACHE_DIR` are withheld.
- `nnUNet_compile` is withheld but still reaches the default path, so leave it unset or the paths
  will differ.
- `XDG_CACHE_HOME` becomes `.cache/inference-paths/<id>`, `SCAN3D_DEVICE` becomes that path's
  device, and that environment's `bin` folder goes first on `PATH`.

`scripts/check_gpu.py --path` drops the same variables, but a process cannot take `LD_LIBRARY_PATH`
or `LD_PRELOAD` back from itself. `scripts/add_inference_path.sh` clears those two first, so clear
them yourself if you run the check by hand.

### Orthanc

Orthanc is optional, and the integration is off until `SCAN3D_ORTHANC_URL` is set.
[Configure 3DScan](ORTHANC.md#configure-3dscan) has an example.

| Variable                             | Default | Purpose                                                        |
| ------------------------------------ | ------- | -------------------------------------------------------------- |
| `SCAN3D_ORTHANC_URL`                 | unset   | The address the 3DScan server uses for Orthanc's REST API.     |
| `SCAN3D_ORTHANC_USERNAME`            | unset   | REST API user.                                                 |
| `SCAN3D_ORTHANC_PASSWORD`            | unset   | REST API password.                                             |
| `SCAN3D_ORTHANC_EXPORT_DESTINATIONS` | unset   | Orthanc modality identifiers offered for SEG and view export.  |
| `SCAN3D_ORTHANC_CA_BUNDLE`           | unset   | Absolute path to a PEM file, for an Orthanc with a private CA. |

### DICOMweb archive

A DICOMweb archive is optional, and off until `SCAN3D_DICOMWEB_URL` is set. See
[ARCHIVES.md](ARCHIVES.md).

| Variable                        | Default         | Purpose                                                                                                               |
| ------------------------------- | --------------- | --------------------------------------------------------------------------------------------------------------------- |
| `SCAN3D_DICOMWEB_URL`           | unset           | The archive's DICOMweb root. Not on the same host and port as `SCAN3D_ORTHANC_URL`.                                   |
| `SCAN3D_DICOMWEB_NAME`          | `DICOM archive` | The name shown in 3DScan.                                                                                             |
| `SCAN3D_DICOMWEB_ID`            | unset           | The archive's name in case records: 1 to 64 letters, digits, `.`, `_` or `-`. See [Configure](ARCHIVES.md#configure). |
| `SCAN3D_DICOMWEB_AUTH`          | `none`          | `none`, `basic` or `bearer`. Credentials need https unless the archive is on this machine.                            |
| `SCAN3D_DICOMWEB_USERNAME`      | unset           | User, for `basic`.                                                                                                    |
| `SCAN3D_DICOMWEB_PASSWORD`      | unset           | Password, for `basic`.                                                                                                |
| `SCAN3D_DICOMWEB_TOKEN_FILE`    | unset           | Absolute path to the token. Required with `bearer`, and refused otherwise.                                            |
| `SCAN3D_DICOMWEB_CA_BUNDLE`     | unset           | Absolute path to a PEM file, for an archive with a private CA.                                                        |
| `SCAN3D_DICOMWEB_TIMEOUT`       | `30`            | Request timeout in seconds, 1 to 300.                                                                                 |
| `SCAN3D_DICOMWEB_QUIET_SECONDS` | `60`            | Seconds, 0 to 3600, that a series' image list must stay unchanged before import.                                      |
| `SCAN3D_DICOMWEB_WILDCARDS`     | `false`         | `true` lets searches use `*` and `?`.                                                                                 |
| `SCAN3D_DICOMWEB_STORE`         | `true`          | `false` keeps the archive read-only.                                                                                  |

The server refuses to start if the token or CA file cannot be read.

### Direct DICOM sends

3DScan can send SEG and 3D views of cases not imported from Orthanc straight to DICOM nodes with
C-STORE. It is off until `SCAN3D_DICOM_DESTINATIONS_FILE` is set. See
[ARCHIVES.md](ARCHIVES.md#direct-dicom-sends).

| Variable                         | Default  | Purpose                                                        |
| -------------------------------- | -------- | -------------------------------------------------------------- |
| `SCAN3D_DICOM_DESTINATIONS_FILE` | unset    | Absolute path to a JSON list of destinations.                  |
| `SCAN3D_DICOM_AE_TITLE`          | `SCAN3D` | The AE title 3DScan sends as. Each destination must accept it. |

Each destination in the file:

```json
[
  {
    "id": "pacs",
    "name": "Hospital PACS",
    "ae_title": "PACS",
    "host": "pacs.example.test",
    "port": 104
  }
]
```

| Key          | Required | Meaning                                                                   |
| ------------ | -------- | ------------------------------------------------------------------------- |
| `id`         | yes      | 1 to 64 letters, digits, `_` or `-`, unique in the file.                  |
| `ae_title`   | yes      | The node's AE title.                                                      |
| `host`       | yes      | Host name or address.                                                     |
| `port`       | yes      | 1 to 65535.                                                               |
| `name`       | no       | The name shown in 3DScan; `id` by default.                                |
| `calling_ae` | no       | The AE title to send as to this node, instead of `SCAN3D_DICOM_AE_TITLE`. |
| `timeout`    | no       | Seconds, 1 to 300; `30` by default.                                       |
| `tls`        | no       | `true` for DICOM over TLS.                                                |
| `ca_bundle`  | no       | Absolute path to a PEM file, with `tls`, for a node with a private CA.    |

The file is read when the server starts, which refuses an invalid one or an unreadable CA file,
and again before each send.

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

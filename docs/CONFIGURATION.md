# Configuration

3DScan reads its settings in this order, and the first value found wins:

1. A real environment variable.
2. The local config file, `scan3d.env` in the checkout.
3. The built-in default.

`scripts/bootstrap.sh` creates `scan3d.env` from `scan3d.env.example` with mode 600. Git
ignores `scan3d.env`, so ports, paths and passwords stay out of version control. Set
`SCAN3D_CONFIG=/path/to/file` to read a different file, which must then exist.

Restart 3DScan after changing a setting.

## File format

```bash
# A comment
SCAN3D_PORT=9100
SCAN3D_ORTHANC_PASSWORD="a value with spaces"
```

Each line is `KEY=VALUE`, and the value is the rest of the line. There are no inline
comments, and `$VARIABLES` are not expanded. Quotes are optional. The file is parsed, not
run, and systemd's `EnvironmentFile=` accepts the same format. Use absolute paths.

## Settings

### Server

| Variable               | Default               | Purpose                                                       |
| ---------------------- | --------------------- | ------------------------------------------------------------- |
| `SCAN3D_HOST`          | `127.0.0.1`           | Listen address. `scan3d serve --host` overrides it.           |
| `SCAN3D_PORT`          | `9010`                | Listen port. `scan3d serve --port` overrides it.              |
| `SCAN3D_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Host names browsers may use, separated by commas.             |

3DScan has no sign-in. The app warns when it listens beyond loopback, because anyone who
can reach the port can read every case.

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

Stored series keep their source details, so enabling a modality later makes series
already in the library available.

### AI compute

| Variable                             | Default                     | Purpose                                                      |
| ------------------------------------ | --------------------------- | ------------------------------------------------------------ |
| `SCAN3D_DEVICE`                      | `auto`                      | `auto`, `gpu`, `gpu:N` or `cpu`.                             |
| `SCAN3D_GPU_PROOF`                   | `.runtime/gpu-proof.json`   | Result file written by `scripts/check_gpu.py`.               |
| `TOTALSEG_HOME_DIR`                  | `.runtime/totalsegmentator` | TotalSegmentator's weights, license and settings.            |
| `TOTALSEG_WEIGHTS_PATH`              | under `TOTALSEG_HOME_DIR`   | Use weights stored elsewhere.                                |
| `SCAN3D_NNINTERACTIVE_MODEL`         | `.runtime/nninteractive/…`  | The pinned nnInteractive model folder.                       |
| `SCAN3D_NNINTERACTIVE_STALL_SECONDS` | built in                    | How long a correction may run without progress.              |
| `SCAN3D_MESH_THREADS`                | `16`                        | Threads for mesh generation.                                 |
| `SCAN3D_POSTPROCESS_WORKERS`         | `4`                         | Structures processed at once after segmentation.             |
| `SCAN3D_DCM2NIIX`                    | `.tools/bin`, then `PATH`   | The DICOM converter.                                         |

Variables the runtime itself reads, such as `XDG_CACHE_HOME` or the `MIOPEN_*` caches on
AMD systems, can go in `scan3d.env` too. Every variable in the file reaches the worker
processes.

### Orthanc

Orthanc is optional, and the integration is off until `SCAN3D_ORTHANC_URL` is set. See
[Orthanc](ORTHANC.md).

| Variable                             | Default | Purpose                                                       |
| ------------------------------------ | ------- | ------------------------------------------------------------- |
| `SCAN3D_ORTHANC_URL`                 | unset   | The address the 3DScan server uses for Orthanc's REST API.    |
| `SCAN3D_ORTHANC_USERNAME`            | unset   | REST API user.                                                |
| `SCAN3D_ORTHANC_PASSWORD`            | unset   | REST API password.                                            |
| `SCAN3D_ORTHANC_EXPORT_DESTINATIONS` | unset   | Orthanc modality identifiers offered for DICOM SEG export.    |

## Examples

Change the port:

```bash
SCAN3D_PORT=9100
```

Keep the case library on another disk and allow imports from a scans folder:

```bash
SCAN3D_DATA_DIR=/mnt/cases/3dscan
SCAN3D_IMPORT_ROOTS=/mnt/scans
```

Connect to a local Orthanc and offer one export destination:

```bash
SCAN3D_ORTHANC_URL=http://127.0.0.1:8042
SCAN3D_ORTHANC_USERNAME=scan3d
SCAN3D_ORTHANC_PASSWORD=choose-a-long-password
SCAN3D_ORTHANC_EXPORT_DESTINATIONS=workstation
```

Serve behind an authenticating reverse proxy on the same machine:

```bash
SCAN3D_ALLOWED_HOSTS=127.0.0.1,localhost,scan.example.test
```

For a one-off run, set a variable in the shell. It overrides the file:

```bash
SCAN3D_PORT=9200 SCAN3D_DATA_DIR=/tmp/scan3d-trial .venv/bin/scan3d serve
```

# Running 3DScan in Docker

The image runs 3DScan's AI on the CPU, which is slow on real CTs, or on an
[AMD GPU](#amd-gpu). For NVIDIA, use the [native install](INSTALL.md). You build the image from
this repository; none is published.

**3DScan is for research use only.** It is not a medical device. See
[What is not supported](INSTALL.md#what-is-not-supported).

You need Docker with Compose 2.24 or newer on Linux x86_64, and at least 16 GB of RAM.

## Build and start

```bash
git clone https://github.com/Truth129/3DScan.git
cd 3DScan
docker compose up --build -d
```

The first build takes a few minutes on a good connection, and the image is about 4.3 GB.

## Create the first account

Sign-in is always on in the container.

```bash
docker compose exec scan3d scan3d users add NAME --admin
```

Then open <http://127.0.0.1:9010> and sign in. Add other people from **People**. The port is
published on this machine's loopback address only.

## Where things are kept

| In the container | Volume           | Holds                                                                |
| ---------------- | ---------------- | -------------------------------------------------------------------- |
| `/data`          | `scan3d_data`    | The case library, with patient images. Protect it.                   |
| `/app/.runtime`  | `scan3d_runtime` | AI weights, GPU kernels and TotalSegmentator's license and settings. |
| `/import`        | none             | A folder to import from by path. Mount it read-only to use it.       |

`docker compose down` keeps the volumes, and `docker compose down --volumes` deletes them with
every case.

The app runs as uid 10001. A host folder mounted in place of a volume must be writable by that uid,
and one mounted at `/import` readable by it. In the app, use paths inside the container, such as
`/import/study`.

## AI models

TotalSegmentator downloads its weights the first time a task runs. The others need a command:

```bash
docker compose exec scan3d python scripts/download_interactive_model.py
docker compose exec scan3d python scripts/download_segmentation_model.py mrsegmentator
```

Check the licenses first. See [AI models](INSTALL.md#ai-models).

## AMD GPU

For an AMD Radeon AI PRO (gfx1201), such as the R9700, put this in
[`compose.override.yaml`](#your-settings), with each group ID that
`stat -c %g /dev/kfd /dev/dri/renderD*` prints:

```yaml
services:
  scan3d:
    build: {args: {TORCH: rocm}}
    devices: [/dev/kfd, /dev/dri]
    group_add: ["987"]
```

Then build it, start it and check the GPU once:

```bash
docker compose up --build -d
docker compose exec scan3d python scripts/check_gpu.py
```

The image has GPU kernels for gfx1201 only. On another GPU, leave this override out and use the CPU
image. ROCm comes with the image, which is about 11.8 GB, so the host needs only the amdgpu kernel
driver.

The first run of each AI model on a new runtime volume compiles GPU kernels and can take a few
minutes longer. Run the check again after an update, a driver or card change, or on a new runtime
volume.

## Your settings

Put your changes in `compose.override.yaml` beside `compose.yaml`. Compose merges it, and git
ignores it. It can hold passwords, so run `chmod 600 compose.override.yaml`. For example:

```yaml
services:
  scan3d:
    environment:
      SCAN3D_MODALITIES: CT,MR
    volumes:
      - /path/to/dicom:/import:ro
    ports: !override
      - "127.0.0.1:9020:9010"
```

Then run `docker compose up -d`. `ports: !override` changes the port on this machine.

- **Settings:** any from [CONFIGURATION.md](CONFIGURATION.md) under `environment:`, except
  `SCAN3D_HOST`, `SCAN3D_PORT`, `SCAN3D_DATA_DIR` and `SCAN3D_IMPORT_ROOTS`, which the image sets.
  A setting that names a file, such as a CA bundle, needs that file mounted read-only, readable by
  uid 10001, and its path inside the container.
- **Other machines:** put an HTTPS reverse proxy in front of `127.0.0.1:9010` that passes the
  browser's `Host` header unchanged, and add that name to `SCAN3D_ALLOWED_HOSTS`. Keep `127.0.0.1`
  there for the health check.
- **No AI:** `build: {args: {TORCH: none}}` builds an image without AI, of about 1.8 GB. Run
  `docker compose up --build -d` after changing it.

### Orthanc

The container cannot reach an Orthanc that listens only on this machine's `127.0.0.1`, as
[ORTHANC.md](ORTHANC.md) sets it up. Run Orthanc in Docker too, with remote access allowed and
authentication on, and join its network in `compose.override.yaml`:

```yaml
services:
  scan3d:
    environment:
      SCAN3D_ORTHANC_URL: http://orthanc:8042
      SCAN3D_ORTHANC_USERNAME: scan3d
      SCAN3D_ORTHANC_PASSWORD: change-me
    networks: [default, orthanc]
networks:
  orthanc:
    name: orthanc_default
    external: true
```

Here `orthanc` is Orthanc's service name, and `orthanc_default` its Compose project's network.

## Back up the case library

```bash
mkdir -p /path/to/scan3d-backups
sudo chown 10001:10001 /path/to/scan3d-backups
docker compose run --rm -v /path/to/scan3d-backups:/backups scan3d scan3d backup /backups
```

The server can keep running. [Back up the case library](USER_GUIDE.md#back-up-the-case-library)
says what a backup holds. To restore one, stop the app, copy the snapshot over `/data`, and start
it again:

```bash
docker compose stop
docker compose run --rm -v /path/to/scan3d-backups:/backups scan3d sh -c 'cp -a /backups/scan3d-<UTC time>/. /data/'
docker compose start
```

## Update

```bash
git pull
docker compose up --build -d
```

The volumes and their cases stay.

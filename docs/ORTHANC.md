# Orthanc and OsiriX incoming studies

The incoming connector is implemented and qualified with synthetic data. Main-app
activation is staged; the LAN receiver is not yet running. Segmentation return
is deferred. The target is **OsiriX MD 14.3.0** on the local network, with
Tailscale receiving deferred until a later configuration change.

## Connection settings

| Purpose | Setting |
| --- | --- |
| OsiriX destination name | 3DScan |
| Destination address | `10.20.0.3` |
| Destination DICOM port | `4243` |
| Destination AE title | `SCAN3D` |
| Allowed Mac address | `10.20.0.118` |
| Mac calling AE title | `OsiriX-mac` (case-sensitive) |
| Private Orthanc API | `http://127.0.0.1:8044` |

In OsiriX add that destination under its DICOM locations and verify it, then
select a series and use **DICOM Send**. Open the usual 3DScan app on this
workstation, choose **Import → Orthanc archive**, search, select the source
series and choose **Import selected series**. Then choose acquisition labels
and **Open CT**. Import and opening run on the CPU without starting segmentation.

The API stays on loopback; `8044` is not the DICOM sending port. The main app's
current browser addresses are loopback `http://127.0.0.1:9010` and its existing
private HTTPS tailnet address. A new LAN browser endpoint is not enabled here.

## Receiver activation

The separate incoming archive is `.runtime/orthanc-incoming/`. Its storage and
index may contain patient studies after the user sends them; never seed it with
test data, inspect its clinical contents for experiments or remove it as a cache.
Its database is independent of the 3DScan case library. Imports retain local
source copies and exact archive provenance; backing up 3DScan alone does not
back up Orthanc's originals/index.

Prepare the private configuration without starting a listener:

```bash
.venv-rocm10-py314/bin/python scripts/orthanc_receiver.py prepare --remote-host 10.20.0.118 --remote-aet OsiriX-mac
```

The helper reuses the checksum-pinned binary, creates private config/credentials,
and refuses to overwrite differing existing settings. Sender AE titles preserve
case. Only that Mac may echo/store to called AE `SCAN3D`; incoming query/retrieve
is disabled and automatic result forwarding is not configured. A remote port is
unused for incoming associations and does not configure sending results back to
OsiriX.

Orthanc 1.13 has no DICOM interface-binding option. Its wildcard DICOM listener
therefore requires the existing host firewall and strict sender AE/IP checks.
Before activation, apply this single rule in the workstation's terminal with
administrator privileges (no other firewall rules are reset):

```bash
sudo ufw allow in on <interface> from 10.20.0.118 to 10.20.0.3 port 4243 proto tcp comment '3DScan OsiriX'
```

The host's UFW input default is DROP. Review the active policy when applying the
rule. If either machine's IP changes, review the destination, sender allowlist
and firewall rule. Tailscale access likewise needs an explicit update rather than
a wildcard rule.

`deployment/scan3d-orthanc.service` is the receiver user-unit template. The main
app needs its private `connection.env` as a systemd EnvironmentFile, the qualified
frontend assets and an idle-service restart. Credentials are never sent to the
browser or embedded in case/job responses. Service activation/restarts require
the user's approval under `AGENTS.md`.

## Import integrity and recovery

Known incomplete or still-receiving series cannot be imported. Unknown expected
counts are explicitly labelled; an inactivity interval alone does not prove
completeness. Import checks the selected snapshot before and after transfer,
verifies actual source hashes and DICOM identities, then publishes one source
atomically. A changed upstream series requires refreshing the selection.

Repeated imports of the same snapshot reuse the existing case/job. Interrupted
status polling resumes the same job; failed/cancelled attempts can retry without
overwriting previous source bytes. Restart recovery marks interrupted transfers
retryable. Opening the CT preserves archive/content/manifest digests in its image
source identity. DICOM SEG/RTSTRUCT return is not implemented in this milestone.

## Synthetic development archive

The development archive is a separate local Orthanc 1.13.0 process at
`http://127.0.0.1:8043`, using only the synthetic CT fixtures described below.
It does not use the live `data/` library, production environment or existing
`scan3d.service`. The native portable binary is used because this workstation
has no Docker or Podman runtime installed.

```bash
scripts/orthanc-dev.sh install
scripts/orthanc-dev.sh start
scripts/orthanc-dev.sh status
scripts/orthanc-dev.sh stop
```

`install` downloads the official versioned Linux Standard Base binary, verifies
the pinned SHA-256 before execution and checks its reported version. The pin is
`c30cbcb8e6af3d209ebc5458b6db751e073822433612c1108927c40184e8d4a8`.
The binary and its source URL/hash record are under `.runtime/orthanc-dev/`.
No system packages are installed.

`start` creates only the new transient user unit
`scan3d-orthanc-dev.service`, with `Restart=no`. It is not enabled for boot.
An existing unit must be transient, use the owned binary/configuration and have
the expected working directory before the helper will interact with it.
`stop` verifies the process executable, configuration, start time and unit PID;
it retains the archive, configuration and credentials. `run` is an alternative
foreground mode for debugging and must not be used while the unit is active.

HTTP is bound to `127.0.0.1` and requires authentication, including from
localhost. A random password is generated once in the ignored, mode-0600 files
`.runtime/orthanc-dev/orthanc.json` and `connection.env`; the runtime directory
is mode 0700. Commands print the credential-file path, never its contents.
This development process has no DICOM listener. Its fixture imports use
Orthanc's REST API and need no DICOMweb plugin.

For an isolated development API, the protected `connection.env` defines
`SCAN3D_ORTHANC_URL`, `SCAN3D_ORTHANC_USERNAME` and `SCAN3D_ORTHANC_PASSWORD`.
Use it only with a separate case directory and test port. Do not source
`scripts/production-env.sh`, which explicitly selects the live library.

## Synthetic fixtures

```bash
.venv-rocm10-py314/bin/python scripts/orthanc_seed_demo.py
```

The seed writes real classic CT DICOM with signed 64×64 pixels, exact study,
series, frame-of-reference and SOP instance UIDs, 1 mm in-plane spacing and 2 mm
slice spacing. The patient label is `SYNTHETIC^Orthanc` and the patient ID is
`SCAN3D-ORTHANC-DEMO`. The v3 fixtures have constant study/series timestamps so
dcm2niix stacks them into one volume. They use fresh series/SOP UIDs, preserving
earlier fixtures. The complete series has 12 of 12 declared instances; the
incomplete series has 3 of 12. Orthanc reports `Complete` and `Missing`,
respectively. Both use one temporal position; there is no time-series sampling.

The seed is deterministic and performs only uploads and reads. Repeating it
reuses the same UIDs; it never deletes archive resources or overwrites differing
seed files. Source files and per-instance SHA-256 values remain under
`.runtime/orthanc-dev/synthetic/native-ct-v3/`. The manifest there records
Orthanc resource IDs for integration/browser tests. Two earlier fixtures with
unknown declared counts are retained on this workstation; no cleanup request
was sent to Orthanc.

The sandbox is a development fixture, not the planned shared clinic archive.
It uses its own local Orthanc SQLite index and storage, independently of 3DScan's
records. Clinic identity, access, audit, backup and production database design
remain separate work.

Sources: [official portable Linux quickstart](https://orthanc.uclouvain.be/book/users/cookbook.html),
[versioned Orthanc 1.13.0 binaries](https://orthanc.uclouvain.be/downloads/linux-standard-base/orthanc/1.13.0/index.html),
and [Orthanc configuration](https://orthanc.uclouvain.be/book/users/configuration.html).

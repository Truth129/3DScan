# Orthanc and OsiriX incoming studies

The incoming connector is implemented, qualified with synthetic data and active
in the main app. The incoming receiver is running. Synthetic DICOM SEG return
passes transport and user-confirmed basic ROI display. Saved-revision export is
implemented and active, but the user reports that OsiriX cannot discover masks
in the first per-structure encoding. A per-frame compatibility update is staged
and still needs actual viewer display confirmation. The target is **OsiriX MD 14.3.0** on the local network, with
Tailscale receiving deferred until a later configuration change.

## Connection settings

| Purpose | Setting |
| --- | --- |
| OsiriX destination name | 3DScan |
| Destination address | `10.20.0.3` |
| Destination DICOM port | `4243` |
| Destination AE title | `SCAN3D` |
| Allowed Mac address | `10.20.0.107` |
| Mac calling AE title | `OsiriX-Mac` (case-sensitive) |
| Orthanc API / Explorer on workstation | `http://127.0.0.1:8044` |
| Orthanc Explorer from the Mac | `http://10.20.0.3:8044/app/explorer.html` |

In OsiriX add that destination under its DICOM locations and verify it, then
select a series and use **DICOM Send**. Open the usual 3DScan app on this
workstation, choose **Import → Orthanc archive**, search, select the source
series and choose **Import selected series**. Then choose acquisition labels
and **Open CT**. Import and opening run on the CPU without starting segmentation.

Port `8044` serves Orthanc's API and Explorer; `4243` receives DICOM. The user
enabled authenticated remote HTTP access and confirmed Explorer works from the
Mac. Its current HTTP bind is only `["0.0.0.0"]`, with `RemoteAccessAllowed` and
`AuthenticationEnabled` both true. Do not also bind `127.0.0.1`: it overlaps
the wildcard and causes startup failure. The main 3DScan app's browser addresses
remain loopback `http://127.0.0.1:9010` and its existing private HTTPS tailnet URL.

## Receiver activation

The separate incoming archive is `.runtime/orthanc-incoming/`. Its storage and
index may contain patient studies after the user sends them; never seed it with
test data, inspect its clinical contents for experiments or remove it as a cache.
Its database is independent of the 3DScan case library. Imports retain local
source copies and exact archive provenance; backing up 3DScan alone does not
back up Orthanc's originals/index.

Prepare the private configuration without starting a listener:

```bash
.venv-rocm10-py314/bin/python scripts/orthanc_receiver.py prepare --remote-host 10.20.0.107 --remote-aet OsiriX-Mac
```

The helper reuses the checksum-pinned binary, creates private config/credentials,
and refuses to overwrite differing existing settings. Sender AE titles preserve
case. Only that Mac may echo/store to called AE `SCAN3D`; incoming query/retrieve
is disabled and automatic result forwarding is not configured. A remote port is
unused for incoming associations and does not configure sending results back to
OsiriX.

Orthanc 1.13 has no DICOM interface-binding option. The user explicitly approved
the existing any-address UFW TCP 4243 rule (IPv4/IPv6). Orthanc's sender AE/IP
allowlist remains restricted to the Mac above. The user later enabled an
any-address UFW 8044 rule and remote HTTP access for Explorer. The preparation
helper still defaults to loopback-only HTTP for a new receiver; it refuses to
overwrite the user's changed configuration.

If either machine's IP changes, review the destination and sender allowlist.
Accepting the Mac through Tailscale also needs an explicit sender update.

On 28 October, rejected-association metadata identified the actual Mac sender
as `10.20.0.107` / `OsiriX-Mac`, correcting the initially supplied
`10.20.0.118` / `OsiriX-mac`. A reachable host can still fail C-ECHO when
the calling AE or source IP differs from this allowlist. The correction was
saved atomically in the private configuration and applied to the running
receiver with authenticated `PUT /modalities/osirix`, without a service restart.
This API replaces the whole sender definition; preserve all operation flags.
REST updates alone are lost on restart with the default file-based modality
configuration. Audit: `.runtime/orthanc-sender-correction-20261028T125441Z/`.
The user subsequently confirmed that OsiriX → SCAN3D sending works.

`osirix-mac.local` resolves on this workstation, but Orthanc requires a
literal IP for incoming validation while `DicomCheckModalityHost` is enabled.
Use a router DHCP reservation to retain the LAN address; a later Tailscale
configuration can use the Mac's stable tailnet IP. Simply substituting the
hostname in this incoming allowlist would reject associations. See the
[Orthanc DICOM guide](https://orthanc.uclouvain.be/book/dicom-guide.html#configuring-orthanc).

`deployment/scan3d-orthanc.service` is the receiver user-unit template. The main
app loads the private `connection.env` through its `orthanc.conf` systemd drop-in.
The qualified frontend was installed with prior lazy chunks retained, and the
idle app was restarted with approval. Credentials are never sent to the browser
or embedded in case/job responses. Later service changes still require approval
under `AGENTS.md`. Audit: `.runtime/orthanc-integration-20261028T075156Z/`.

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
source identity.

## Export saved masks through Orthanc

In a saved segmentation's toolbar choose **DICOM SEG…**, select the nonempty
masks, then **Archive in Orthanc** or choose a viewer and **Send to** it. This
uses the current saved revision's masks, with independent objects preserving
overlaps; source CT pixels are not sent again. The original patient/study/frame
and source-image references are retained. CT must have been imported through
the configured Orthanc; local-folder sources and MR are not supported by this
first export adapter. Unsaved correction and comparison states block export.

Export checks every source slice against the saved mask grid, including all
four corners. Converter/metadata rounding is allowed only within both 0.05
voxel per axis and 0.05 mm in patient space. Masks are gathered by integer
indices without interpolation, and the original DICOM positions remain in the
SEG. Larger discrepancies stop export before anything is uploaded or sent.

An archive-only action stores SEG beside the original study in Orthanc. Sending
asks that same Orthanc to perform C-STORE. The app does not make a direct DICOM
connection to OsiriX. Existing modality `osirix` already has the confirmed Mac
AE/port. Enable it in the main app environment with:

```bash
SCAN3D_ORTHANC_EXPORT_DESTINATIONS=osirix
```

Only configured IDs are accepted; users cannot supply a host/URL from the
browser. An empty allowlist keeps archive-only export available. Reuse the
existing incoming modality profile, whose host must remain a literal IP for
strict incoming checks; introducing a second same-AE hostname profile can make
incoming lookup ambiguous. DHCP reservation remains recommended for this LAN
configuration. The synthetic test uses a separate outgoing-only profile and
successfully sends via `osirix-mac.local:11112`.

Closing/reopening the dialog never sends automatically. A lost status check
resumes the same job. **Send again** is deliberately different: after known
success it creates another delivery attempt of the same SOP instances, useful
if the receiver deleted the results. Unknown or incomplete delivery is shown
honestly; explicit retry checks a known Orthanc job and never regenerates SEG
identities. Nothing resumes or sends automatically at application startup.

The compatibility encoder writes `SegmentIdentificationSequence` on each frame,
matching the accepted two-mask prototype. highdicom's shared-only placement for
single-segment files is valid DICOM but differs from that prototype; OsiriX's
exact rejection mechanism is not confirmed. The macro is moved, never duplicated
between shared and per-frame groups, following
[DICOM functional-group rules](https://dicom.nema.org/medical/dicom/current/output/chtml/part03/sect_C.7.6.16.html).
Encoding version 2 generates new SOPs only on an explicit export, leaving prior
files and delivery receipts intact. After activation, refresh the app and use
**Send to osirix** to create the updated files. **Send again** reuses files
already sent with the current encoding. Successful C-STORE proves transport;
it does not establish OsiriX mask discovery or clinical alignment.

The user approved activation, which installed the qualified packages/frontend
and `osirix` allowlist and restarted idle 3DScan. Both lanes are healthy; all 19
new assets and the private HTTPS index match the build, with aggregate library
counts unchanged. Orthanc stayed running with its configuration unchanged.
Audit and guarded rollback: `.runtime/dicom-seg-integration-20261028T150235Z/`.
Later restart/rollback still requires approval under AGENTS.md. RTSTRUCT and
editing/resave fidelity remain separate work.

## Synthetic segmentation return qualification

The user confirmed OsiriX's listener AE `OsiriX-Mac`, port `11112`.
Outbound C-ECHO works with `osirix-mac.local`, which resolves to
`10.20.0.107`. Hostnames work for outgoing connections even though incoming
source-host checks require a literal IP.

`tests/osirix_seg_smoke.py` generates fresh synthetic CT and two independent
binary masks, then creates a source-referenced binary SEG with test-only
highdicom 0.28.2. It uses an oblique, anisotropic, non-square CT and a permuted,
reversed NIfTI grid. Export gathers exact mask voxels through the full affine;
it neither resamples nor flattens overlapping segments into one labelmap.

To regenerate locally, use an isolated environment inheriting the app's imaging
packages, with highdicom 0.28.2 installed only there:

```bash
PYTHONPATH=$PWD /path/to/test-venv/bin/python tests/osirix_seg_smoke.py --output /tmp/scan3d-osirix-seg-fixture-NEW
```

The runner never sends anything. It verifies packed bits, each frame's source
UID/geometry, exact native CT/mask projection, DS lengths and a highdicom source
round trip, and writes a 13-file hash manifest. Standard IOD validation is a
separate gate; see the validation record for the reviewed validator limitation.

The prepared test was uploaded only to the separate synthetic archive on 8043;
every downloaded source hash matched. Orthanc sent 12 CT images followed by one
SEG to OsiriX with calling AE `SCAN3D`. Both C-STORE jobs succeeded with zero
failed instances; the temporary outbound profile was removed. Patient:
`SYNTHETIC^SEGRETURN`, ID `SCAN3D-SEG-6610cfe7`. Expected display: a red
**Synthetic ring** with a hole and a cyan **Synthetic block** overlapping its
right side. After an accidental deletion, all 13 instances were resent
successfully. The user confirms the SEG appears as usable ROIs, while its
database presentation differs from native OsiriX ROIs. This establishes basic
receive/display compatibility; exact alignment, editing/resave and RTSTRUCT
fallback remain separate qualification. Audit and synthetic fixtures:
`.runtime/validation/osirix-seg-return-20261028T133800Z/`.

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

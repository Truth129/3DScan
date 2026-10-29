# Orthanc and OsiriX incoming studies

The incoming connector is implemented, qualified with synthetic data and active
in the main app. The incoming receiver is running. Synthetic DICOM SEG return
passes transport and user-confirmed basic ROI display. Saved-revision export is
implemented and active. The combined multi-segment object update is active after
an approved idle app restart, and all eight selected segments display. On
29 October 2026 the user confirms they have verified that SEG works in OsiriX;
the active combined SEG return workflow is user-verified. The target is
**OsiriX MD 14.3.0** on the local network, with Tailscale receiving deferred until
a later configuration change.

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

## Explorer 2 trial

Orthanc Explorer 2 **1.15.0** is active after the user's approved restart on
29 October 2026. Only `scan3d-orthanc.service` was restarted; its new PID is
**842420**. The additive configuration retains the current default UI with
`IsDefaultOrthancUI:false` and offers the new interface at `/ui/app/`, using the
existing Orthanc login. Open
[Explorer 2 over private HTTPS](https://scan3d-host.example.ts.net:8444/ui/app/).
The workstation/LAN paths use the same suffix on port `8044`.

The configuration preserves OHIF/DICOMweb, authentication, network
listeners, archive paths and every DICOM peer permission. Explorer 2 detects
the existing OHIF plugin and supplies study links to `/ohif/`. No new network
route or modality-persistence change is needed. The existing legacy interface
remains at `/app/explorer.html`.

Seven API checks and eight browser gates pass on a separate loopback archive
with twelve synthetic CT images. The browser uses SwiftShader and checks the
study list, expanded metadata, OHIF launch link, plugin versions and absence of
severe JavaScript/network errors. Live checks use only static/system/aggregate
endpoints; opening the actual Explorer 2 study list is left to the user.
Expanding its **DICOM Modalities** menu automatically sends C-ECHO requests to
configured destinations, so it is not part of live installation verification.

Private plugin, hashes, before/after configurations, synthetic evidence and
approval-only activation/rollback:
`.runtime/orthanc-explorer2-20261029T080042Z/`. Retain this directory;
the running configuration loads the plugin directly from it. Config snapshots
contain credentials and must remain private. Running the helper without `--approved`
prints the plan without changing the live service:

```bash
.venv-rocm10-py314/bin/python .runtime/orthanc-explorer2-20261029T080042Z/activate.py
```

Activation guarded idle jobs, incoming associations, the baseline PID and
configuration/plugin/evidence hashes. Loopback and certificate-validated HTTPS
static/authentication checks pass, including all eleven referenced JS/CSS assets
through both addresses. Archive counts remain unchanged. 3DScan remains PID735851 with both lanes healthy and CT-only
policy; all existing Tailscale routes are unchanged. Journal priority metadata
shows no warnings or errors. No patient records were read or DICOM sent.

The helper restores the previous configuration on activation failure. A later
rollback requires fresh restart approval, verifies the active configuration/PID
and retains the existing OHIF/DICOMweb configuration and HTTPS route:

```bash
.venv-rocm10-py314/bin/python .runtime/orthanc-explorer2-20261029T080042Z/activate.py --rollback --approved
```

## OHIF browser viewer with DICOMweb

OHIF is active on the existing Orthanc after the user's approved restart on
29 October 2026. Open
[OHIF over private HTTPS](https://scan3d-host.example.ts.net:8444/ohif/)
and sign in with the existing Orthanc HTTP Basic credentials. The workstation
address is `http://127.0.0.1:8044/ohif/`; LAN access remains
`http://10.20.0.3:8044/ohif/`. Prefer HTTPS from the Mac: browser secure-context
requirements affect the decoder workers and MPR over ordinary remote HTTP.

Orthanc remains version **1.13.0**. Its OHIF plugin **1.8** bundles OHIF
**3.12.0**, alongside DICOMweb plugin **1.24**. The configuration explicitly sets
`OHIF.DataSource` to `dicom-web` and `OHIF.RouterBasename` to `/ohif/`, enabling
the study list. DICOMweb is enabled at `/dicom-web/`; WADO-URI is at `/wado`.
The bundled viewer uses relative DICOMweb URLs, so the same configuration works
through loopback, LAN and private HTTPS. The OHIF plugin supplies the COOP/COEP
headers used for browser isolation.

The private HTTPS port `8444` proxies the existing authenticated Orthanc HTTP
server. Existing Tailscale Serve routes on `443` and `8443` are preserved; no
Funnel or new firewall rule was added. Authentication, HTTP bind, archive storage
and index, DICOM port `4243` and all Mac AE/IP/operation permissions are unchanged.
Only `scan3d-orthanc.service` was restarted; 3DScan was not restarted.

OHIF is a separate archive viewer. Opening a study there does not import it into
3DScan, prepare its reference grid or run segmentation. 3DScan's saved revisions,
correction history and explicit SEG archive/send workflow retain their existing
boundaries. The synthetic qualification covers CT stack and MPR display; it does
not establish an OHIF SEG editing/write-back round trip or clinical accuracy.

Seven API checks and seven software-browser gates pass on a fresh scratch
archive with twelve synthetic CT images. They cover authentication, relative
configuration, QIDO/full metadata, exact WADO frame bytes, forwarded HTTPS bulk
URLs, signed CT decoding, visible stack/MPR images and no severe browser errors.
The browser harness uses normal HTTP Basic authentication for decoder workers
(`browser_smoke.py --prime-auth`). It uses SwiftShader, without the R9700.
No live patient queries, archive image reads or DICOM sends were used.

Audit, versioned plugin files and hashes, private before/after configurations,
qualification evidence and activation record:
`.runtime/ohif-dicomweb-20261029T072802Z/`. Retain this directory: the running
configuration loads its plugin files. The before/after configurations contain
credentials and must stay private. Guarded rollback restores the prior Orthanc
configuration and removes only this unchanged `8444` Serve route, retaining the
archive and other listeners:

```bash
.venv-rocm10-py314/bin/python .runtime/ohif-dicomweb-20261029T072802Z/activate.py --rollback --approved
```

Obtain fresh approval before a later rollback/restart unless the current approval
already covers failure recovery. Running the helper without `--approved` only
prints its plan. See the newest [validation record](VALIDATION.md).

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
case. By default only that Mac may echo/store to called AE `SCAN3D`; incoming
query/retrieve is disabled until explicitly enabled, as recorded below for the
current receiver. Automatic result forwarding is not configured. A remote port is
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

### Two addresses with the same AE title

An isolated synthetic experiment on the exact pinned Orthanc **1.13.0**
confirms that duplicate symbolic entries do **not** enable incoming access from
both addresses with this receiver's strict configuration. With
`DicomCheckModalityHost:true` and all global `DicomAlwaysAllow*` flags false,
the lexically first same-AE entry accepts C-ECHO, C-STORE and C-FIND; the other
address is rejected at association. Swapping the hosts between the symbolic
names reverses the accepted address, even with reverse JSON insertion order.

Explicit REST requests by symbolic modality ID can C-ECHO and C-STORE to either
address. C-MOVE destination lookup by AE chooses the first entry and does not
fall back when that listener is unavailable. Duplicate entries are therefore
useful for explicitly selected outgoing routes, but are not a bidirectional
LAN/Tailscale configuration under the current policy.

The test used two loopback addresses and synthetic CT in a private network
namespace; it did not contact the Mac or qualify the actual Tailscale route.
Unknown source/AE refusal remained enabled. No live configuration, peer entry,
service, firewall or runtime dependency was changed. Evidence and harness:
`.runtime/orthanc-dual-address-20261029T085936Z/`. See the dated
[validation record](VALIDATION.md).

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

## Querying Orthanc from OsiriX

The receiver was initially configured for C-ECHO/C-STORE only. When the issue
was reported, configuration and live REST inspection confirmed that `osirix`
had `AllowFind:false` and `AllowMove:false`. A reachable node could send images
while its C-FIND search was refused and appeared empty in OsiriX. Sending from
Orthanc or 3DScan is a separate operation.

With explicit approval, `AllowFind` and `AllowMove` are now enabled only for
the existing Mac AE/IP. Global always-allow flags,
worklist, C-GET and storage commitment remain disabled. Save the change in the
private configuration and hot-apply the full modality definition with local
authenticated `PUT /modalities/osirix`; file-based REST updates alone disappear
on restart. Both live and saved definitions match; Orthanc PID436143 and app
PID584950 remain unchanged and healthy. No patient records were searched or
retrieved to apply this change. Audit/guarded rollback:
`.runtime/orthanc-query-retrieve-20261028T193732Z/`.

In OsiriX Query/Retrieve use the same node as DICOM Send: host `10.20.0.3`,
port `4243`, called AE `SCAN3D`, and calling/listener AE `OsiriX-Mac`.
Choose C-MOVE retrieval; Orthanc already knows the Mac's listener port `11112`.
Port `8044` is the HTTP Explorer, not the DICOM query port. Clear restrictive
date/name filters when checking a newly enabled query. See the official
[Orthanc permissions guide](https://orthanc.uclouvain.be/book/faq/security.html)
and [OsiriX PACS guide](https://osirixviewer.com/resources/pacs/).

The current permissions are in `.runtime/orthanc-incoming/orthanc.json` under
`DicomModalities.osirix`: `AllowStore:true`, `AllowFind:true`, `AllowMove:true`,
`AllowGet:false`. The approved change enabled Find/Move; Store was already true.
Top-level `DicomAlwaysAllow*` flags remain false, so permissions come from the
configured peer. C-MOVE sends results to the Mac's separate listener; C-GET
retrieves over the request association and would require `AllowGet:true` for
this peer if that mode is selected in OsiriX. The Mac's actual selected mode
has not been independently inspected. File changes and live REST updates must
both be applied when changing these permissions without restarting Orthanc.

## Export saved masks through Orthanc

In a saved segmentation's toolbar choose **DICOM SEG…**, select the nonempty
masks, then **Archive in Orthanc** or choose a viewer and **Send to** it. This
uses the current saved revision's masks, with independent segments preserving
overlaps; source CT pixels are not sent again. The original patient/study/frame
and source-image references are retained. CT must have been imported through
the configured Orthanc; local-folder sources and MR are not supported by this
first export adapter. Unsaved correction and comparison states block export.

Export checks every source slice against the saved mask grid, including all
four corners. Converter/metadata rounding is allowed only within both 0.05
voxel per axis and 0.05 mm in patient space. Masks are gathered by integer
indices without interpolation, and the original DICOM positions remain in the
SEG. Larger discrepancies stop export before anything is uploaded or sent.

Study Description `(0008,1030)` is inherited from the original CT study metadata
by highdicom and retained in the combined object, including decoded non-Latin
text in the UTF-8 export. A read-only check of the user's reported "Unknown"
column confirms the affected export already contains the exact nonempty source
description. The user also confirms the correct raw tag in the received SEG's
OsiriX Meta-Data view despite "Unknown" in the database column. Inheritance is
working; the remaining issue is database label population. Its exact MD 14.3
cause remains unconfirmed. OsiriX documents
[editing fields in its database window](https://pixmeo.zendesk.com/hc/en-us/articles/6049508617244-How-to-edit-DICOM-fields-directly-in-the-database-window)
for local label correction. No encoder/profile change or resend was made for
this report.

### OsiriX DICOM verification warnings

`99SCAN3D` is a valid private coding scheme: DICOM private designators start
with `99` and contain alphanumeric characters. The current SEG defines:

| Code | Meaning | Use |
| --- | --- | --- |
| `REGION` | Image region | Segmented property category and type |
| `SEGMENTATION` | Image segmentation | Algorithm family |

Each segment uses these codes independently, so an unknown-scheme warning can
repeat three times per segment. These are local generic definitions, not claims
of standard anatomical terminology. SEG's baseline context-group bindings permit
private codes. `CodingSchemeIdentificationSequence` is optional (Type 3), although
recommended for local schemes; adding it would not itself make a validator know
the private vocabulary. The
[validator documentation](https://www.dclunie.com/dicom3tools/dciodvfy.html)
explicitly acknowledges legitimate private schemes can produce "Unrecognized
defined term" warnings. See [DICOM coding scheme rules](https://dicom.nema.org/medical/dicom/current/output/chtml/part16/chapter_8.html).

The reported `99CT_VIA` underscore is a separate private-designator syntax
problem in inherited coded study metadata. Changing a namespace changes code
identity, so do not silently rename it or invent a standard replacement.
The reported retired person-name form is also inherited: the validator permits
informal names while recommending caret-separated components. Preserve the
source physician-name value rather than guessing component boundaries. No
patient text should be copied into synthetic fixtures, reports or validator logs.

These messages concern metadata; they do not themselves establish a pixel or
geometry problem, and they are not a complete conformance verdict. No export
profile or runtime change was made solely to suppress the reported warnings.

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
Encoding version 3 places all selected masks in one SEG instance and generates
new SOPs only on an explicit export, leaving prior
files and delivery receipts intact. Refresh the app and use
**Send to osirix** to create the updated files. **Send again** reuses files
already sent with the current encoding. Successful C-STORE proves transport;
it does not establish OsiriX mask discovery or clinical alignment.
The compatibility update was activated with explicit approval; both worker
lanes, the served frontend and aggregate library counts pass. Orthanc stayed
running and no DICOM was sent during activation. Audit/guarded rollback:
`.runtime/dicom-seg-per-frame-20261028T191145Z/`.

The user subsequently confirms mask discovery but only one mask visible from
an eight-mask export. With explicit read-only permission for that affected
case, inspection confirms eight nonempty, distinct masks in eight valid objects,
matching frozen hashes/counts and successful upload/delivery receipts. Each
object has segment number 1 within one series. No clinical identifiers, images
or masks were copied or retained as fixtures. OsiriX's internal handling is
unconfirmed; one object containing all eight independently numbered segments
matches the earlier accepted prototype. The combined encoder streams packed
pixels and keeps the same per-mask memory bound. Display was awaiting user
confirmation at activation, as subsequently recorded below. Encoding version 3
is now active after an approved idle app restart; both lanes and aggregate counts pass.
Orthanc and frontend assets are unchanged. Audit and prior backend snapshot:
`.runtime/dicom-seg-combined-20261028T192927Z/`.
The user subsequently confirms all eight segments now show in OsiriX. On
29 October 2026 they explicitly confirm they have already verified that SEG
works in OsiriX MD 14.3.0. The active combined SEG return workflow is
user-verified; earlier pending display statements are historical.

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

## Native OsiriX ROI export investigation

The user requested a second export option for native OsiriX brush ROIs. The
existing user-verified DICOM SEG path remains active. A standalone experimental
native writer now emits Basic Text SR reports directly from exact saved masks.
The user confirms its first synthetic native ROI import/display works in
MD 14.3. API/UI integration is not enabled; editing and save/reload still need
qualification.

With explicit approval on 29 October, sent a fresh 12-image synthetic CT and
one two-mask SEG from the separate 8043 archive. Both transfers succeeded with
zero failed instances; no experimental native archives were sent. Look for
`SYNTHETIC^SEGRETURN`, patient ID `SCAN3D-SEG-7363b7af`. The expected ROIs are a
red **Synthetic ring** with a hole and cyan **Synthetic block**, overlapping on
the ring's right side.

The user saved and returned the native ROIs. Ten verified synthetic SR files
contain 16 brush objects / 15 unique per-image ROIs: one source slice has a
byte-identical duplicate archive under another report SOP. All reconstructed
masks match the known source masks exactly, including the ring's hole and 529
overlapping voxels. Source references, spacing, crop offsets and origins pass.
The next gate is the native writer's synthetic OsiriX import, display and save
round trip. Inspect only the returned synthetic study;
receiving it does not authorize broader clinical archive inspection.

The verified MD 14.3 reference uses `NSKeyedArchiver` in a binary plist, with
type-20 brush ROIs. `textureBuffer` holds a one-byte zero placeholder and
`textureWidth`/`textureHeight` are 1; the real crop dimensions are in
`textureWidthCompressed`/`textureHeightCompressed`. The
`textureBufferCompressedSize` field is the decoded byte count, equal to their
product. `textureBufferCompressed` uses documented
[Apple LZ4 framing](https://developer.apple.com/documentation/compression/compression_lz4)
and decodes to row-major 0/255 mask bytes. Crop corners are inclusive. Source
SOP references, rather than the ROI's unset `zLocation`, identify the image
plane. The reference's `imageOrigin` matches source LPS x/y for this fixture;
that observation alone is not a general geometry qualification.

The new writer uses fresh NSKeyedArchiver objects matching the reference's
72 ROI keys and scalar/UID types. It emits Apple's documented `bv4-` stored
blocks plus `bv4$` end marker, preserving cropped row-major 0/255 bytes without
requiring a platform-specific compressor. Placeholder texture fields, inclusive
crop bounds, exact spacing and populated `savedStudyInstanceUID` match the
returned brush profile. Each occupied image gets one report with all its
independent brushes; each new report has fresh report/series/brush identities.
`SeriesNumber=5002`, `SeriesDescription=OsiriX ROI SR` and `InstanceNumber=0`
preserve the observed recognition/source-frame behavior. Manufacturer remains
3DScan. The observed private namespace `99_OSIRIX` is a deliberate compatibility
limitation: its underscore violates current DICOM coding-scheme syntax.

Run the prepare-only synthetic gate from an isolated source copy:

```bash
PYTHONPATH=$PWD:$PWD/tests PYTHONSAFEPATH=1 python tests/osirix_roi_smoke.py \
  --output /tmp/scan3d-osirix-roi-<unique>
```

It writes a new private fixture, twelve CT files and nine native ROI reports
with fifteen brushes, exact readback evidence and a SHA-256 manifest. There is
no SEG in this package. Outgoing transfer still requires explicit user approval,
the separate synthetic 8043 archive, exact-file hash verification and CT-before-SR
ordering. Never send a patient/study archive resource that could include other
instances. Retain write-ahead transfer intents and receipts; an unknown outcome
must be inspected before a retry. This experiment does not enable native ROI
exports for clinical cases or qualify untested source orientations.

The user approved this writer's synthetic test on 29 October. Both transfers
succeeded: 12/12 CT and 9/9 native ROI SR files, zero failures, no SEG sent.
Open `SYNTHETIC^NATIVEROI`, patient ID `SCAN3D-NROI-ebc4b232`, then its CT.
Expected ROIs are red **Synthetic ring** with a hole and cyan **Synthetic block**
overlapping its right side. The user confirms "Yes, it works", passing basic
native ROI import/display for this fixture. Editing/save/reload and an exact
receiver round trip are not yet separately qualified. To verify them, save the
ROIs and send this synthetic study back to SCAN3D with native ROI SR objects.
Audit and terminal receipts:
`.runtime/validation/osirix-native-roi-writer-20261029T065107Z/`.

Audit and retained synthetic sources/truth:
`.runtime/validation/osirix-native-roi-reference-20261029T062846Z/`.

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

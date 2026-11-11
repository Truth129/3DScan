# Orthanc integration

3DScan is a REST client of an [Orthanc](https://www.orthanc-server.com/) DICOM archive. It imports a
selected series, and can store a saved revision there as DICOM SEG, OsiriX ROIs or a 3D view, or ask
Orthanc to send it to a DICOM modality. For other archives, see [ARCHIVES.md](ARCHIVES.md).

3DScan does not install or run Orthanc. For cases from Orthanc, all DICOM networking happens in
Orthanc. Nothing is imported, prepared, segmented or sent automatically.

> **Research use only · not for diagnostic use.** A successful transfer does not establish that a
> segmentation is correct or correctly displayed.

## Configure 3DScan

Add these to `scan3d.env` (see [configuration](CONFIGURATION.md)), then restart 3DScan:

```ini
SCAN3D_ORTHANC_URL=http://127.0.0.1:8042
SCAN3D_ORTHANC_USERNAME=scan3d
SCAN3D_ORTHANC_PASSWORD=replace-with-a-long-random-password
SCAN3D_ORTHANC_EXPORT_DESTINATIONS=workstation
```

- **`SCAN3D_ORTHANC_URL`** is the address the 3DScan server uses, not the browser: `http` or `https`,
  optionally with a path prefix, and no credentials, query, fragment, or `.` or `..` path segment.
  Leave it unset to turn the integration off.
- Its SHA-256 identifies the archive. If it changes, earlier cases can no longer be archived or
  sent, and Open in 3DScan links must be regenerated.
- **`SCAN3D_ORTHANC_USERNAME`** and **`SCAN3D_ORTHANC_PASSWORD`** are sent with HTTP Basic
  authentication. They never reach the browser or case and job records.
- **`SCAN3D_ORTHANC_EXPORT_DESTINATIONS`** is a comma-separated allowlist of Orthanc modality
  identifiers, each up to 64 letters, digits, `_` or `-`. A malformed value stops 3DScan at startup.
  An identifier missing from Orthanc's `/modalities` is left out of the menu, and an empty list
  still allows **Archive in Orthanc**.

The client verifies TLS certificates, follows no redirects, and ignores proxy variables, `.netrc`
and `REQUESTS_CA_BUNDLE`. It trusts the public CAs bundled with Python's `requests`, so for a
private CA set `SCAN3D_ORTHANC_CA_BUNDLE` to the absolute path of its PEM file.

With sign-in on, an account also needs archive access. Without it, the page shows no **Orthanc
archive** tab or **Add series from Orthanc**, and **Export SEG…** and **DICOM** offer a local file
only.

## Run Orthanc

See the [Orthanc Book](https://orthanc.uclouvain.be/book/) to install and operate Orthanc. This
`orthanc.json` fragment keeps the REST API on loopback with authentication and defines one peer:
its identifier for `SCAN3D_ORTHANC_EXPORT_DESTINATIONS`, then AE title, host and port.

```json
{
  "HttpPort": 8042,
  "HttpBindAddresses": ["127.0.0.1"],
  "RemoteAccessAllowed": false,
  "AuthenticationEnabled": true,
  "RegisteredUsers": { "scan3d": "replace-with-a-long-random-password" },
  "DicomAet": "ORTHANC",
  "DicomPort": 4242,
  "DicomModalities": {
    "workstation": ["WORKSTATION", "viewer.example.test", 11112]
  }
}
```

To let only known peers connect, set `DicomAlwaysAllowEcho` and `DicomAlwaysAllowStore` to `false`,
and `DicomCheckModalityHost` to `true`. Then:

- The calling AE title, which is case-sensitive, and the source address must match an entry. A
  reachable peer that fails C-ECHO usually differs in one of these.
- The entry's host must be a literal IP address.
- Keep one entry per AE title. A second at another address is not accepted, and C-MOVE uses only
  the first.

The object form chooses operations:

```json
"DicomModalities": {
  "workstation": {
    "AET": "WORKSTATION", "Host": "192.0.2.10", "Port": 11112,
    "AllowEcho": true, "AllowStore": true, "AllowFind": true, "AllowMove": true, "AllowGet": false
  }
}
```

`PUT /modalities/<id>` replaces the whole definition. By default the change is lost on restart
unless you also save it in the configuration file.

## Send studies to Orthanc

Point the sender at Orthanc's DICOM port and AE title, not HTTP port `8042`. In OsiriX, add a
destination in **Preferences → Locations** with Orthanc's address, port `4242` and called AE
`ORTHANC`, then use **DICOM Send**.

For strict checks and for sending results back, list the workstation in `DicomModalities` with its
AE title and listener port, often `11112`, not the `4242` it sends to.

Query/Retrieve from the workstation needs `AllowFind` and `AllowMove` in its entry, or its searches
look empty. C-MOVE delivers to the entry's listener port. C-GET needs `AllowGet`.

## Import a series

Choose **+ Import scan**, then the **Orthanc archive** tab, and follow
[Import a study](USER_GUIDE.md#import-a-study) and [Open a series](USER_GUIDE.md#open-a-series).
Search terms pass to Orthanc's `/tools/find`, so its wildcard rules apply.

Import runs on the CPU and is bound to the snapshot you selected:

- A series still receiving, or that Orthanc reports as incomplete, is blocked. If the expected image
  count is unknown, 3DScan imports the listed images and says so: confirm the acquisition is
  complete.
- The modality must be enabled in `SCAN3D_MODALITIES`, and the 16 GiB import limit applies.
- The snapshot is checked before and after transfer, with file hashes, DICOM identities and a single
  frame of reference. If the series changed, refresh and select it again.
- Importing the same snapshot again reuses the library's case or job, whoever imported it. A case in
  Trash, or deleted forever, counts as absent, so importing makes a new one.
- Failed, cancelled and interrupted imports can be retried without touching earlier source files.
  They do not resume on their own after a restart.
- A series the validator refuses fails its import with the validator's reason.
- A case imported from Orthanc can add other series of the same study. A series of another study is
  refused, and a refused addition leaves the case as it was.

3DScan keeps its own copy of imported files. Its backups do not cover Orthanc's archive.

## Send a saved segmentation back

In a saved revision, choose **Export SEG…**, select nonempty masks, then choose **Archive in Orthanc**
or pick a **Send destination** and choose **Send to** it. Both upload the SEG and read it back to
compare hashes. Sending then asks Orthanc to perform C-STORE to that modality.

- Both need classic CT or classic single-frame MR imported from this same Orthanc. Other classic CT
  or MR prepared from DICOM can be [saved as a SEG file](USER_GUIDE.md#dicom-seg), and scans from a
  folder, a ZIP or a DICOMweb archive can also be delivered as in [ARCHIVES.md](ARCHIVES.md).
- To bring masks in, see [Import DICOM SEG](USER_GUIDE.md#import-existing-dicom-seg-masks).
- All selected masks are numbered binary segments of one instance, so overlaps survive.
- The SEG references the original patient, study, frame of reference and source images. Source
  pixels are not sent again.
- Source slices must match the saved mask grid within 0.05 voxel and 0.05 mm, or the export stops
  before upload. Masks are not interpolated.
- After a confirmed success, **Send again** delivers the same SOP instances anew, for example if the
  receiver deleted them.
- If 3DScan cannot confirm an outcome it says so. A retry reuses the same instances and first checks
  any known Orthanc job. Orthanc keeps only `JobsHistorySize` finished jobs, 10 by default, so set
  it to `100` or more. Once Orthanc drops a job, check the destination before retrying: the next
  retry sends again.

To send native OsiriX ROIs instead, choose **OsiriX ROIs** under **Format** first. Each slice that a
selected mask touches becomes one `OsiriX ROI SR` report in its own series, holding one brush ROI
per mask. Archiving, sending, retries and **Send again** work as for a SEG.

Only OsiriX reads these reports, so send them to an OsiriX destination. See
[OsiriX ROIs](USER_GUIDE.md#osirix-rois) for the limits.

## Send a 3D view back

**DICOM** in the 3D view exports what **PNG** saves, as a new one-image series in the original
patient and study. It needs a classic CT or MR imported from this same Orthanc, but no masks.
Archiving, sending, retries and **Send again** work as for a segmentation.

The image is a Secondary Capture with modality `OT`, so the study then lists `CT\OT`. **It is a
picture, not a measurement:** see [DICOM view](USER_GUIDE.md#dicom-view) and its
[encoding](API.md#dicom-view-export).

- If Orthanc holds the study under a different patient ID, which source bytes that disagree with
  their declared character set can cause, nothing is stored.
- If Orthanc files the upload outside the original study, nothing is sent, and the stored instance
  stays in Orthanc for you to check.
- A retry needs the same image, which only the dialog holds, and an unchanged view reuses its
  instance. A different image is a new series, so if an unconfirmed delivery did arrive, the
  receiver has both.

## Open in 3DScan from Orthanc Explorer 2

An optional button on each series opens it in 3DScan. Custom buttons are available in Orthanc
Explorer 2 1.15.0. Generate the configuration, which contains no credentials:

```bash
.venv/bin/python scripts/orthanc_launch_config.py \
  --orthanc-url http://127.0.0.1:8042 \
  --scan3d-url https://scan3d.example.test:8443
```

- `--orthanc-url` (required): the same value as `SCAN3D_ORTHANC_URL`, without credentials.
- `--scan3d-url` (required): the 3DScan root URL as the browser reaches it, with no path or query.
- `--launcher-path /3dscan-launch/index.html` (optional, and the only value accepted) points the
  button at your own same-origin page that chooses among 3DScan addresses and keeps the fragment.
  3DScan does not include one: serve it yourself, for example with Orthanc's ServeFolders plugin.

The script prints JSON and contacts nothing. Merge its button, by `Id`, into
`OrthancExplorer2.UiOptions.CustomButtons.series`, keeping other buttons, then restart Orthanc. Do
not load it as a second file with duplicate keys.

The button opens `<3DScan root>#orthanc-series=<id>&orthanc-source=<key>` in a new tab.

- `<key>` is the archive identity from [above](#configure-3dscan), not a secret or a login. Both
  values stay in the URL fragment, so the browser does not send them in the page request.
- A link for a different archive is refused, so regenerate the button if `SCAN3D_ORTHANC_URL` changes.
- If the browser reaches 3DScan under a new name, add it to `SCAN3D_ALLOWED_HOSTS`.
- With sign-in on, the page asks for it first and keeps the link.

In Explorer 2, click a study, click a series, then click the cube icon at the right of its toolbar.
3DScan reopens an earlier import of the same snapshot, as in [Import a series](#import-a-series), and
a pending one resumes its status check.

Otherwise confirm the patient shown and choose **Import selected series**. The link never prepares a
CT, runs segmentation or imports SEG.

## Other archives through Orthanc

Orthanc can fetch studies from other archives and send results to other nodes for 3DScan.

- **A DICOM archive (PACS).** Add it to `DicomModalities`, then find and retrieve studies in
  Explorer 2 or through `/modalities/<id>/query`. With `"RetrieveMethod": "C-GET"` on its entry
  (Orthanc 1.12.6 and later), the archive needs no entry for Orthanc. With C-MOVE, the default,
  the archive must list Orthanc's AE title, address and port as a move destination.
- **A DICOMweb server.** List it under `DicomWeb.Servers`, then retrieve through
  `POST /dicom-web/servers/<name>/retrieve`.
- **Results.** Add any DICOM node to `DicomModalities` and its identifier to
  `SCAN3D_ORTHANC_EXPORT_DESTINATIONS`.

Retrieved series then import as above. Without any server, import a folder or ZIP and
[save the SEG as a file](USER_GUIDE.md#dicom-seg).

## Compatibility notes

Display in another viewer shows interoperability, not clinical alignment. Viewers not named here are
untested.

- **OsiriX MD 14.3** displays SEG as ROIs, listed differently from ROIs drawn in OsiriX. Its Study
  Description column can show "Unknown" although `(0008,1030)` is correct. Editing the ROIs and
  saving them back is unchecked.
- **OHIF**, checked only for this: it stacks overlapping segments for its 3D view, so overlap
  doubles the volume. In OHIF 3.12.0, 3.13.13 and 3.14.0-beta.68, 512×512×910 stacked slices failed
  with `Failed to convert labelmap to surface`, and 800 worked. Use
  [Resolve overlaps in this export](USER_GUIDE.md#dicom-seg).
- **Validator warnings.** `99SCAN3D` is a legal private coding scheme that a validator may report as
  unrecognized. Coded values and person names copied from the source are preserved. These warnings
  concern metadata, not pixels or geometry.
- **3D views.** OsiriX display of a view is unverified.
- **OsiriX ROIs.** OsiriX MD 14.3 displayed synthetic brush ROIs correctly, including holes and
  overlap. Real scans, other source orientations, and editing and saving them in OsiriX are
  unchecked.

## Security

- Do not move patient data over unauthenticated or unencrypted links. Plain HTTP and standard DICOM
  are unencrypted, and DICOM has no user authentication.
- Keep Orthanc's REST API on loopback, or at least off untrusted networks, with authentication on.
  Restrict the DICOM port to known peers with a firewall as well as `DicomModalities`.
- Keep credentials in `scan3d.env` with mode 600 (`chmod 600 scan3d.env`). Never commit them.
- With sign-in off, anyone who can reach 3DScan can read every case, so keep it on loopback. See
  [Sign-in](CONFIGURATION.md#sign-in).
- A VPN or authenticating reverse proxy between machines is yours to choose, configure and secure.

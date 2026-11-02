# Orthanc integration

3DScan is a REST client of an [Orthanc](https://www.orthanc-server.com/) DICOM archive. It imports a
selected series, and can store a saved revision there as DICOM SEG or ask Orthanc to send it to a
DICOM modality.

3DScan does not install or run Orthanc, and all DICOM networking happens in Orthanc. Nothing is
imported, prepared, segmented or sent automatically.

> **Research use only · not for diagnostic use.** This integration moves research results between
> systems. It carries no clinical validation, and a successful transfer does not establish that
> a segmentation is correct or correctly displayed.

## Configure 3DScan

Add these to `scan3d.env` (see [configuration](CONFIGURATION.md)), then restart 3DScan:

```ini
SCAN3D_ORTHANC_URL=http://127.0.0.1:8042
SCAN3D_ORTHANC_USERNAME=scan3d
SCAN3D_ORTHANC_PASSWORD=replace-with-a-long-random-password
SCAN3D_ORTHANC_EXPORT_DESTINATIONS=workstation
```

**`SCAN3D_ORTHANC_URL`** is the address the 3DScan server uses, not the browser.

- Leave it unset to turn the integration off. The import dialog then says so, and **Export SEG…**
  offers only a [local file](USER_GUIDE.md#dicom-seg).
- Use `http` or `https` with a host name and an optional path prefix.
- Credentials, a query string, a fragment and `.` or `..` path segments are refused.
- Its SHA-256 identifies the archive. If it changes, earlier cases can no longer be archived or
  sent, and Open in 3DScan links must be regenerated.

**`SCAN3D_ORTHANC_USERNAME`** and **`SCAN3D_ORTHANC_PASSWORD`** are sent with HTTP Basic
authentication. They never reach the browser or case and job records.

**`SCAN3D_ORTHANC_EXPORT_DESTINATIONS`** is a comma-separated allowlist of Orthanc modality
identifiers, each up to 64 letters, digits, `_` or `-`.

- A malformed value stops 3DScan at startup.
- An identifier missing from Orthanc's `/modalities` is left out of the menu. The rest appear with
  their AE titles.
- An empty list still allows **Archive in Orthanc**.

The client verifies TLS certificates, follows no redirects, and ignores proxy variables, `.netrc`
and `REQUESTS_CA_BUNDLE`. Use a certificate that Python's default CA bundle trusts.

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
AE title and listener port. That port, often `11112`, is for delivery to it, not the destination
port.

Query/Retrieve needs `AllowFind` and `AllowMove` in its entry, or its searches look empty. C-MOVE
delivers to the entry's listener port. C-GET needs `AllowGet`.

## Import a series

Choose **+ Import scan**, then the **Orthanc archive** tab, and follow
[Import a study](USER_GUIDE.md#import-a-study) and [Open a series](USER_GUIDE.md#open-a-series).
Search terms pass to Orthanc's `/tools/find`, so its wildcard rules apply.

Import runs on the CPU and is bound to the snapshot you selected:

- A series still receiving, or that Orthanc reports as incomplete, is blocked.
- If the expected image count is unknown, 3DScan imports the listed images and says so. Confirm the
  acquisition is complete.
- The modality must be enabled in `SCAN3D_MODALITIES`, and the 16 GiB import limit applies.
- The snapshot is checked before and after transfer, with file hashes, DICOM identities and a single
  frame of reference. If the series changed, refresh and select it again.
- Importing the same snapshot again reuses your case or job.
- Failed, cancelled and interrupted imports can be retried without touching earlier source files.
  They do not resume on their own after a restart.

3DScan keeps its own copy of imported files. Its backups do not cover Orthanc's archive.

## Send a saved segmentation back

In a saved revision, choose **Export SEG…**, select nonempty masks, then choose **Archive in Orthanc**
or pick a **Send destination** and choose **Send to** it. Both upload the SEG and read it back to
compare hashes. Sending then asks Orthanc to perform C-STORE to that modality.

- Both need classic CT imported from this same Orthanc. Local-folder sources and MR are not
  supported, but a local-folder CT can be [saved as a SEG file](USER_GUIDE.md#dicom-seg).
- To bring masks in, see [Import DICOM SEG](USER_GUIDE.md#import-existing-dicom-seg-masks).
- All selected masks are numbered binary segments of one instance, so overlaps survive.
- The SEG references the original patient, study, frame of reference and source images. Source
  pixels are not sent again.
- Source slices must match the saved mask grid within 0.05 voxel and 0.05 mm, or the export stops
  before upload. Masks are not interpolated.
- After a confirmed success, **Send again** delivers the same SOP instances anew, for example if the
  receiver deleted them.
- If 3DScan cannot confirm an outcome it says so. A retry reuses the same instances and first checks
  any known Orthanc job.

## Send a 3D view back

**DICOM ↗** in the 3D view exports what **PNG ↗** saves, as a new one-image series in the original
patient and study. It needs a classic CT imported from this same Orthanc, but no masks. Archiving,
sending, retries and **Send again** work as for a segmentation.

The image is a Secondary Capture with modality `OT`, so the study then lists `CT\OT`. Its series
description is `3DScan view` and the start of the revision ID. Its series number is always 501.

- **It is a picture, not a measurement.** It has no pixel spacing, position, orientation or frame of
  reference, so viewers cannot calibrate it. It carries no orientation labels or left and right
  markers, so a symmetric view can be read from either side.
- If Orthanc holds the study under a different patient ID, nothing is stored. Source bytes that
  disagree with their declared character set cause this.
- If Orthanc files the upload outside the original study, nothing is sent, and the stored instance
  stays in Orthanc for you to check.
- A retry needs the same image, which only the dialog holds. An unchanged view reuses its instance.
  A different image is a new series, so if an unconfirmed delivery did arrive, the receiver has both.

## Open in 3DScan from Orthanc Explorer 2

An optional button on each series opens it in 3DScan. Custom buttons are available in Orthanc
Explorer 2 1.15.0. Generate the configuration, which contains no credentials:

```bash
.venv/bin/python scripts/orthanc_launch_config.py \
  --orthanc-url http://127.0.0.1:8042 \
  --scan3d-url https://scan3d.example.test:8443
```

- `--orthanc-url` is required: the same value as `SCAN3D_ORTHANC_URL`, without credentials.
- `--scan3d-url` is required: the 3DScan root URL as the browser reaches it, with no path or query.
- `--launcher-path` is optional, and only `/3dscan-launch/index.html` is accepted. It is for your
  own same-origin page that chooses among 3DScan addresses and keeps the fragment. 3DScan does not
  include one, so serve it yourself, for example with Orthanc's ServeFolders plugin, or omit this.

The script prints JSON and contacts nothing. Merge its button, by `Id`, into
`OrthancExplorer2.UiOptions.CustomButtons.series`, keeping other buttons, then restart Orthanc. Do
not load it as a second file with duplicate keys.

The button opens `<3DScan root>#orthanc-series=<id>&orthanc-source=<key>` in a new tab.

- `<key>` is the archive identity from [above](#configure-3dscan), not a secret or a login.
- `<id>` and `<key>` stay in the URL fragment, so the browser does not send them in the page request.
- A link for a different archive is refused, so regenerate the button if `SCAN3D_ORTHANC_URL` changes.
- If the browser reaches 3DScan under a new name, add it to `SCAN3D_ALLOWED_HOSTS`.
- With sign-in on, the page asks for it first and keeps the link.

In Explorer 2, click a study, click a series, then click the cube icon at the right of its toolbar.

- If you already imported the same snapshot, 3DScan reopens it, and a pending import resumes its
  status check.
- Otherwise confirm the patient shown and choose **Import selected series**.
- The link never prepares a CT, runs segmentation or imports SEG.

## Compatibility notes

SEG output displays as ROIs in OsiriX MD 14.3. That shows basic interoperability, not clinical
alignment. Editing ROIs and saving them back has not been checked. OHIF has been checked only for the 3D limit below, and other viewers are untested.

- **OsiriX database.** SEG appears differently from ROIs drawn in OsiriX. The Study Description
  column can show "Unknown" even though `(0008,1030)` is correct, as the metadata view confirms.
- **OHIF 3D limit.** OHIF stacks overlapping segments for its 3D view, so overlap doubles the volume.
  In OHIF 3.12.0, 3.13.13 and 3.14.0-beta.68, 512×512×910 stacked slices failed with
  `Failed to convert labelmap to surface`, and 800 worked. Use **Resolve overlaps in this export**,
  as in the [user guide](USER_GUIDE.md#dicom-seg).
- **Validator warnings.** `99SCAN3D` is a legal private coding scheme, and a validator may report it
  as unrecognized. Coded values and person names copied from the source are preserved. These
  warnings concern metadata, not pixels or geometry.
- **3D views.** OsiriX display of a view is not verified, and other viewers are untested.

## Security

- Do not move patient data over unauthenticated or unencrypted links. Plain HTTP and standard DICOM
  are unencrypted, and DICOM has no user authentication.
- Keep Orthanc's REST API on loopback, or at least off untrusted networks, and keep authentication
  on. Restrict the DICOM port to known peers with a firewall as well as `DicomModalities`.
- Keep credentials in `scan3d.env` with mode 600 (`chmod 600 scan3d.env`). Never commit them.
- With sign-in off, anyone who can reach 3DScan can read every case, so keep it on loopback. See
  [Sign-in](CONFIGURATION.md#sign-in).
- A VPN or authenticating reverse proxy between machines is yours to choose, configure and secure.

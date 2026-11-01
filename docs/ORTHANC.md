# Orthanc integration

3DScan can work with an [Orthanc](https://www.orthanc-server.com/) DICOM archive. It is an Orthanc
REST client: it searches the archive, imports a selected series into the local case library, and
can store a saved revision in the archive as DICOM SEG or ask Orthanc to send that SEG to a
configured DICOM modality.

3DScan does not install or run Orthanc, and it makes no DICOM connections itself. Studies reach
Orthanc from your scanners or workstations, and all DICOM networking happens in Orthanc. Nothing is
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

Leave `SCAN3D_ORTHANC_URL` unset to turn the integration off. The import dialog then says so, and
DICOM SEG export is unavailable.

**`SCAN3D_ORTHANC_URL`** is the address the 3DScan server uses, not the browser.

- It must be `http` or `https` with a host name. A path prefix is allowed.
- Credentials, a query string, a fragment and `.` or `..` path segments are refused. Put credentials
  in the two variables below.
- It is normalized: the host is lowercased, a default port and a trailing slash are removed.
- Its SHA-256 identifies the archive. Each import records it, so changing the URL makes earlier
  cases look like they came from a different archive: they stop being exportable, and Open in
  3DScan links must be regenerated.

**`SCAN3D_ORTHANC_USERNAME`** and **`SCAN3D_ORTHANC_PASSWORD`** are sent with HTTP Basic
authentication. The browser never sees them, and they do not appear in case or job records.

**`SCAN3D_ORTHANC_EXPORT_DESTINATIONS`** is a comma-separated allowlist of Orthanc modality
identifiers, each up to 64 letters, digits, `_` or `-`. A malformed value stops 3DScan at startup.
Each identifier is looked up in Orthanc's `/modalities` list: one that Orthanc does not define is
left out of the menu, and the rest appear with their AE titles. Users pick from this list and cannot
supply a host from the browser. An empty list still allows **Archive in Orthanc**.

The client verifies TLS certificates and follows no redirects. It ignores proxy variables, `.netrc`
and `REQUESTS_CA_BUNDLE`, so use a certificate that Python's default CA bundle trusts.

## Run Orthanc

Follow the [Orthanc Book](https://orthanc.uclouvain.be/book/) to install and operate Orthanc. This
minimal `orthanc.json` fragment keeps the REST API on loopback with authentication, and defines one
DICOM peer:

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

`workstation` is the identifier for `SCAN3D_ORTHANC_EXPORT_DESTINATIONS`, then AE title, host, port.

To let only known peers connect, set `DicomAlwaysAllowEcho` and `DicomAlwaysAllowStore` to `false`,
and set `DicomCheckModalityHost` to `true`. Orthanc then accepts an association only when the
calling AE title and source address match an entry, so the entry's host must be a literal IP
address, and AE titles are case-sensitive. A reachable peer that fails C-ECHO usually differs in one
of these. Use the object form to choose operations:

```json
"DicomModalities": {
  "workstation": {
    "AET": "WORKSTATION", "Host": "192.0.2.10", "Port": 11112,
    "AllowEcho": true, "AllowStore": true, "AllowFind": true, "AllowMove": true, "AllowGet": false
  }
}
```

Keep one entry per AE title. With strict checks, a second entry for the same AE at another address
is not accepted, and C-MOVE uses only the first. Changing a modality with `PUT /modalities/<id>`
replaces the whole definition, and by default that change is lost on restart unless you also save
it in the configuration file.

## Send studies to Orthanc

Point the sending device at Orthanc's DICOM port and AE title. As an example, in OsiriX open
**Preferences → Locations** and add a destination with Orthanc's address, port `4242` and called AE
`ORTHANC`, then use **DICOM Send** on a series. For strict checks and for sending results back,
list OsiriX under `DicomModalities` with its AE title and listener port. That port, often `11112`,
is for delivery to OsiriX and is not the destination port. Port `8042` is HTTP and takes no DICOM.

To search Orthanc from the workstation with Query/Retrieve, its modality entry needs `AllowFind`
and `AllowMove` enabled. A peer that may store but not find can send images, while its searches
look empty. C-MOVE delivers to the listener port in the entry. C-GET retrieves over the same
association and needs `AllowGet`.

## Import a series

In 3DScan choose **+ Import scan**, then the **Orthanc archive** tab, search, select one series and
choose **Import selected series →**. Search terms pass to Orthanc's `/tools/find`, so its wildcard
rules apply. Then set the acquisition labels and choose **Open CT →**, as in the
[user guide](USER_GUIDE.md#open-a-series).

Import runs on the CPU and is bound to the snapshot you selected:

- A series that is still receiving, or that Orthanc reports as incomplete, is blocked.
- Stability is only an inactivity interval. If the expected image count is unknown, the series is
  labelled so and can be imported, but you must confirm that the acquisition is complete.
- The series modality must be enabled in `SCAN3D_MODALITIES`, and its total size must be within the
  16 GiB import limit.
- The snapshot is checked before and after transfer, and 3DScan verifies file hashes, DICOM
  identities and a single frame of reference. If the series changed, refresh and select it again.
- Importing the same snapshot again reuses its case or job. Failed and cancelled attempts can be
  retried without touching earlier source files. After a restart, interrupted imports become
  retryable; they do not resume on their own.

3DScan keeps its own copy of imported files, and its backups do not cover Orthanc's archive.

## Send a saved segmentation back

In a saved revision, choose **DICOM SEG…**, select nonempty masks, then choose **Archive in Orthanc**
or pick a **Send destination** and choose **Send to** it. Archiving uploads the SEG and reads it back
to compare hashes. Sending does the same, then asks Orthanc to perform C-STORE to that modality.

- Export needs classic CT imported from this same Orthanc. Local-folder sources and MR are not
  supported. To reuse masks in the other direction, see
  [Import DICOM SEG](USER_GUIDE.md#import-existing-dicom-seg-masks), which works from local files.
- The SEG references the original patient, study, frame of reference and source images, and source
  pixels are not sent again.
- Each source slice is checked against the saved mask grid, tolerating rounding of up to 0.05 voxel
  and 0.05 mm. Masks are gathered by integer index without interpolation. A larger discrepancy
  stops the export before anything is uploaded.
- Prepared files are immutable. **Send again** starts a new delivery of the same SOP instances after
  a confirmed success, which helps if the receiver deleted them.
- Delivery is only ever started by a request. If 3DScan cannot confirm an outcome it says so. A
  retry reuses the same instances and first checks any known Orthanc job.

## Open in 3DScan from Orthanc Explorer 2

An optional button on each series in Orthanc Explorer 2 opens that series in 3DScan. Generate the
configuration, which contains no credentials:

```bash
.venv/bin/python scripts/orthanc_launch_config.py \
  --orthanc-url http://127.0.0.1:8042 \
  --scan3d-url https://scan3d.example.test:8443
```

| Argument          | Meaning                                                                     |
| ----------------- | --------------------------------------------------------------------------- |
| `--orthanc-url`   | Required. The same value as `SCAN3D_ORTHANC_URL`, without credentials.      |
| `--scan3d-url`    | Required. The 3DScan root URL as the browser reaches it. No path or query.  |
| `--launcher-path` | Optional. Only `/3dscan-launch/index.html` is accepted; see below.          |

The script prints JSON and contacts nothing. Merge its single button, by `Id`, into
`OrthancExplorer2.UiOptions.CustomButtons.series` in your Orthanc configuration, keeping any other
buttons, then restart Orthanc. Merge rather than loading it as a second file with duplicate keys.
Custom buttons are available in Orthanc Explorer 2 1.15.0.

The button opens a new tab at:

```text
<3DScan root>#orthanc-series=<id>&orthanc-source=<key>
```

Explorer 2 replaces `{UUID}` in the generated URL with the series `<id>`. The `<key>` is the archive
identity from [above](#configure-3dscan), and it is not a secret or a login. Both travel in the URL
fragment, so the browser does not send them in the page request. A link for a different archive is
refused, so regenerate the button whenever `SCAN3D_ORTHANC_URL` changes. If the browser reaches
3DScan under a new name, add that name to `SCAN3D_ALLOWED_HOSTS`.

`--launcher-path` writes a relative button URL, `../../3dscan-launch/index.html#...`, for a site
that serves its own same-origin page which chooses among several 3DScan addresses and keeps the
fragment. 3DScan does not include that page: you must provide it, for example through Orthanc's
ServeFolders plugin. Without one, omit this argument.

In Explorer 2, click a study, click a series, then click the cube icon at the right of the series
toolbar. If the same snapshot was already imported, 3DScan reopens it, and a pending import resumes
its status check. Otherwise it shows the patient, study and series, and waits for you to confirm the
patient and choose **Import selected series**. The link never prepares a CT, runs segmentation or
imports SEG.

## Compatibility notes

3DScan's SEG output has been checked for display as ROIs in OsiriX MD 14.3 on synthetic data only.
That shows basic interoperability, not clinical alignment, and editing ROIs and saving them back has
not been checked. OHIF has been checked only for the 3D limit below, and other viewers are untested.
SEG also appears differently in the OsiriX database from ROIs drawn in OsiriX.

- **Encoding.** All selected masks are separately numbered binary segments in one instance, so
  overlaps survive. Segment identification sits in the per-frame functional groups.
- **Overlaps in viewers.** OHIF stacks overlapping segments into one volume for its 3D view, so any
  overlap doubles that volume. Its surface converter aborted at 512×512×910 stacked slices and
  worked at 800, shown as `Failed to convert labelmap to surface`. These tests used synthetic data
  in OHIF 3.12.0, 3.13.13 and 3.14.0-beta.68. **Resolve overlaps in this export** keeps the stack to
  one layer without changing saved masks. See the [user guide](USER_GUIDE.md#dicom-seg).
- **`99SCAN3D` warnings.** This is a legal private coding scheme, used for the generic `REGION` and
  `SEGMENTATION` codes. A validator may report an unrecognized scheme or term several times per
  segment. These warnings concern metadata, not pixels or geometry.
- **Inherited metadata.** Warnings about coded values or person-name forms copied from the source
  study are preserved rather than rewritten.
- **"Unknown" study description.** OsiriX's database column can show "Unknown" for a SEG series even
  though Study Description `(0008,1030)` is present and correct, as its metadata view confirms.

## Security

- Do not move patient data over unauthenticated or unencrypted links. Plain HTTP and standard DICOM
  are unencrypted, and DICOM has no user authentication.
- Keep Orthanc's REST API on loopback, or at least off untrusted networks, and keep authentication
  on. Restrict the DICOM port to known peers with a firewall as well as `DicomModalities`.
- Keep credentials in `scan3d.env` with mode 600 (`chmod 600 scan3d.env`). Never commit them.
- 3DScan has no sign-in of its own, so anyone who can reach it can read every case.
- If you need access between machines, a VPN or an authenticating reverse proxy is your own
  responsibility to choose, configure and secure.

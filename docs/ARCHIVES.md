# DICOMweb archives

3DScan can search, import from and archive results to any archive that offers DICOMweb (QIDO-RS,
WADO-RS and STOW-RS), beside or instead of [Orthanc](ORTHANC.md). It is off until
`SCAN3D_DICOMWEB_URL` is set, and [CONFIGURATION.md](CONFIGURATION.md#dicomweb-archive) lists the
settings.

Sending results to another DICOM viewer is not built yet for these cases. Send through
[Orthanc as a gateway](ORTHANC.md#other-archives-through-orthanc), or save SEG and view files.

## Configure

```bash
SCAN3D_DICOMWEB_URL=https://pacs.example.test/dicom-web
SCAN3D_DICOMWEB_NAME="Hospital PACS"
SCAN3D_DICOMWEB_ID=hospital-pacs
SCAN3D_DICOMWEB_AUTH=bearer
SCAN3D_DICOMWEB_TOKEN_FILE=/etc/scan3d/pacs.token
```

- Set `SCAN3D_DICOMWEB_ID` before the first import. Case records name the archive by it, so a
  server that moves keeps its cases. Without it, the URL names the archive.

## Tested archives

Import, Add series, launch links and archiving were tested on these, with synthetic data only:

| Archive                         | `SCAN3D_DICOMWEB_URL`                               | Notes                                                                                     |
| ------------------------------- | --------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Orthanc 1.13.0, DICOMweb plugin | `http://<host>:8042/dicom-web`                      | Keeps the first copy of a SOP stored twice. Use the [Orthanc integration](ORTHANC.md) where you can. |
| dcm4chee-arc 5.35.2             | `http://<host>:8080/dcm4chee-arc/aets/DCM4CHEE/rs` | Gives an issuer to a patient ID stored without one, so archived copies verify as re-encoded. Image listings leave out the Frame of Reference, which import reads from the files. |

Other archives are untested. Run `scan3d archive-check` on them first.

## Check an archive

```bash
.venv/bin/scan3d archive-check
```

This checks the connection and the archive's identity, whether searches honour `limit` and
`offset`, and the listing of series and images. It also retrieves one image and deletes it. It
changes nothing in the archive, and prints only counts, the archive key and transfer syntaxes, so
the output can go into a support ticket. It exits 1 if any check fails.

`--write-synthetic` also stores one synthetic image, in a new study for patient
`SCAN3D-ARCHIVE-CHECK`, and reads it back. Use it only on a scratch archive: DICOMweb cannot delete
the image.
- A token file can be refreshed by the provider's own tool. 3DScan reads it again whenever it
  changes, and once more after a 401.

## Search

- Searches match exactly, with fuzzy matching off. `*` and `?` are refused unless
  `SCAN3D_DICOMWEB_WILDCARDS=true`, because some cloud archives have no wildcards.
- Search terms, such as patient names, reach the archive's own request logs.

## Import

Import runs on the CPU and is bound to the selected snapshot, like an
[Orthanc import](ORTHANC.md#import-a-series), with these differences:

- DICOMweb cannot say that a series is still arriving. A series can be imported once its image
  list has stayed the same for `SCAN3D_DICOMWEB_QUIET_SECONDS`, counted from when this server
  first listed it. Before that, import answers 409 with the seconds left. A restart counts again.
- The image list is read again before and after the transfer. A change fails the import.
- Every file must carry the study, series, SOP identity and patient seen when the series was
  listed. The patient is kept only as a key salted with the archive, never shown.
- HTJ2K and JPEG XL files are fetched again as Explicit VR Little Endian, which 3DScan can read.
- DICOMweb has no stored-file identity, so 3DScan cannot see an archive replace a file under the
  same UID. **Import again** (`reimport: true`) makes a fresh copy beside the earlier case.
- Add series takes only series of the case's own study, and only while the archive names the same
  patient for it. Import a series of a merged or changed study as a new case.

[USER_GUIDE.md](USER_GUIDE.md#import-a-study) shows the screens, and the routes are in
[API.md](API.md#dicomweb-archive).

## Archive results

For a case imported from the archive, **Export SEG…** and **DICOM ↗** offer **Archive in** the
archive, which stores the SEG or view in the original study with STOW-RS. SEG and view files work
as for any CT prepared from DICOM.

- An archive's STOW answer does not show which bytes it keeps, so 3DScan reads every instance back.
  It must return the same bytes, or the same identity, patient and decoded pixels after a
  re-encode. Anything else fails the export, and the job says so.
- Before and after storing, the archive must still list the original study under the same patient.
  A merged or changed study stores nothing.
- A retry stores the same instance again, which an archive keeps once.
- `SCAN3D_DICOMWEB_STORE=false` keeps the archive read-only, and archiving is then refused.

## Launch links

A viewer or worklist can open a series in 3DScan with:

```text
<3DScan root>#dicom-study=<StudyInstanceUID>&dicom-series=<SeriesInstanceUID>&dicom-source=<key>
```

- `<key>` names the archive and is not a secret. `scan3d archive-check` prints it. A link for
  another archive is refused.
- The identities stay in the URL fragment, so the browser does not send them in the page request.
- 3DScan shows the patient and series, then reopens an earlier import of it or offers
  **Import selected series**. A link never prepares a CT or runs segmentation.
- A link with `#orthanc-*` keys is read as an Orthanc link, and its `#dicom-*` keys are ignored.

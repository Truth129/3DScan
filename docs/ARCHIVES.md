# DICOMweb archives

3DScan can search and import from any archive that offers DICOMweb (QIDO-RS and WADO-RS), beside
or instead of [Orthanc](ORTHANC.md). It is off until `SCAN3D_DICOMWEB_URL` is set, and
[CONFIGURATION.md](CONFIGURATION.md#dicomweb-archive) lists the settings.

Sending results to a DICOMweb archive is not built yet. Until then, send through
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

The routes are in [API.md](API.md#dicomweb-archive).

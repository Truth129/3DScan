"""Add deterministic synthetic CT series to the separate local Orthanc archive."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

import numpy as np
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "orthanc-dev"
UID_ROOT = "1.2.826.0.1.3680043.10.543.20261028"
STUDY_UID = UID_ROOT + ".1"
FRAME_UID = UID_ROOT + ".9"
FIXTURE_GENERATION = 3
COMPLETE_SERIES_UID = f"{UID_ROOT}.{FIXTURE_GENERATION}.1.3"
SEED_DIRECTORY = "native-ct-v3"
URL = "http://127.0.0.1:8043"


def dicom_bytes(series_number: int, index: int, *, complete: bool = True) -> bytes:
    """One classic CT image with real signed pixels and physical source geometry."""
    from io import BytesIO

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = f"{UID_ROOT}.{FIXTURE_GENERATION}.{series_number + 1}.{index + 1}"
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = "1.2.826.0.1.3680043.10.543.1"
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SpecificCharacterSet = "ISO_IR 100"
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = STUDY_UID
    ds.SeriesInstanceUID = f"{UID_ROOT}.{FIXTURE_GENERATION}.1.{series_number}"
    ds.FrameOfReferenceUID = FRAME_UID
    ds.PatientName = "SYNTHETIC^Orthanc"
    ds.PatientID = "SCAN3D-ORTHANC-DEMO"
    ds.StudyID = "ORTHANC-DEMO"
    ds.AccessionNumber = "SYNTHETIC"
    ds.StudyDescription = "Synthetic Orthanc integration"
    ds.SeriesDescription = "Synthetic CT complete v3 (12 of12)" if complete else "Synthetic CT incomplete v3 (3 of12)"
    ds.Modality = "CT"
    ds.SeriesNumber = series_number
    ds.AcquisitionNumber = 1
    ds.InstanceNumber = index + 1
    ds.ImagesInAcquisition = 12
    ds.NumberOfTemporalPositions = 1
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.PatientPosition = "HFS"
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.ImagePositionPatient = [-32, -32, index * 2]
    ds.PixelSpacing = [1, 1]
    ds.SliceThickness = 2
    ds.SpacingBetweenSlices = 2
    ds.Rows = ds.Columns = 64
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 1
    ds.RescaleSlope = 1
    ds.RescaleIntercept = 0
    ds.RescaleType = "HU"
    ds.WindowCenter = 40
    ds.WindowWidth = 400
    for date in ["StudyDate", "SeriesDate", "AcquisitionDate", "ContentDate"]:
        setattr(ds, date, "20261028")
    # Study/series times describe the shared acquisition, not each slice. Varying
    # them makes dcm2niix correctly split the source into separate volumes.
    ds.StudyTime = ds.SeriesTime = "090000"
    ds.AcquisitionTime = ds.ContentTime = f"0900{index:02d}"
    y, x = np.mgrid[:64, :64]
    pixels = np.full((64, 64), -1000, dtype="<i2")
    body = ((x - 32) / 26) ** 2 + ((y - 32) / 25) ** 2 < 1
    pixels[body] = 35 + index
    for center in [22, 42]:
        lung = ((x - center) / 8) ** 2 + ((y - 29) / 15) ** 2 < 1
        pixels[lung & body] = -720
    pixels[(x - 32) ** 2 + (y - 46) ** 2 < 16] = 650
    pixels[(x - 31) ** 2 + (y - 35) ** 2 < 9] = 350
    ds.PixelData = pixels.tobytes()
    stream = BytesIO()
    ds.save_as(stream, enforce_file_format=True)
    return stream.getvalue()


def main() -> None:
    config = json.loads((RUNTIME / "orthanc.json").read_text())
    username, password = next(iter(config["RegisteredUsers"].items()))
    token = base64.b64encode(f"{username}:{password}".encode()).decode()

    def request(path: str, body: bytes | None = None) -> dict:
        headers = {"Authorization": f"Basic {token}"}
        if body is not None:
            headers["Content-Type"] = "application/dicom"
        with urlopen(Request(URL + path, data=body, headers=headers), timeout=15) as response:
            return json.load(response)

    system = request("/system")
    if system["Name"] != "3DScan synthetic development archive" or system["Version"] != "1.13.0":
        raise RuntimeError("Refusing to seed a server other than the configured synthetic development archive.")
    output = RUNTIME / "synthetic" / SEED_DIRECTORY
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest = {"synthetic_only": True, "study_uid": STUDY_UID, "frame_of_reference_uid": FRAME_UID, "series": []}
    for number, count in [(3, 12), (4, 3)]:
        directory = output / ("complete" if number == 3 else "incomplete")
        directory.mkdir(mode=0o700, exist_ok=True)
        instances = []
        resource = None
        for index in range(count):
            data = dicom_bytes(number, index, complete=number == 3)
            target = directory / f"image-{index + 1:03d}.dcm"
            if target.exists():
                if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(data).digest():
                    raise RuntimeError("An existing seed file differs; it was left untouched.")
            else:
                target.write_bytes(data)
                target.chmod(0o600)
            resource = request("/instances", data)
            if resource.get("Status") not in {"Success", "AlreadyStored"}:
                raise RuntimeError("Orthanc refused a synthetic instance.")
            instances.append({"orthanc_id": resource["ID"], "file": str(target.relative_to(output)), "sha256": hashlib.sha256(data).hexdigest()})
        summary = request(f"/series/{resource['ParentSeries']}")
        manifest["series"].append(
            {
                "id": resource["ParentSeries"],
                "study_id": resource["ParentStudy"],
                "description": summary["MainDicomTags"]["SeriesDescription"],
                "instance_count": len(summary["Instances"]),
                "expected_instances": summary["ExpectedNumberOfInstances"],
                "status": summary["Status"],
                "instances": instances,
            }
        )
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "manifest.json").chmod(0o600)
    print(
        json.dumps(
            {
                "synthetic_only": True,
                "study_id": manifest["series"][0]["study_id"],
                "series": [{key: item[key] for key in ["id", "description", "instance_count", "expected_instances", "status"]} for item in manifest["series"]],
                "manifest": str(output / "manifest.json"),
            }
        )
    )


if __name__ == "__main__":
    main()

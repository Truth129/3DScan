"""Installed Slicer smoke test; run with scripts/slicer_smoke.sh.

Uses only a synthetic CT and overlapping masks. Launches the installed Slicer
without a window or persisted settings. Artifacts stay under .runtime/validation.
"""

from pathlib import Path
import json
import os
import subprocess
import sys
import uuid
import zipfile

import nibabel as nib
import nrrd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scan3d.slicer_io import export_bundle, import_segmentation  # noqa: E402


def main():
    directory = ROOT / ".runtime" / "validation" / ("slicer-" + uuid.uuid4().hex[:12])
    directory.mkdir(parents=True)
    shape = (16, 18, 14)
    angle = np.deg2rad(31)
    rotation = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
    affine = np.eye(4)
    affine[:3, :3] = rotation @ np.diag([-0.8, 1.4, 2.2])
    affine[:3, 3] = [31, -52, 87]
    values = np.indices(shape).sum(axis=0).astype(np.int16) * 10 - 900
    image = nib.Nifti1Image(values, affine)
    image.header.set_xyzt_units("mm")
    ct_path = directory / "reference-original.nii.gz"
    nib.save(image, ct_path)
    masks = []
    first = np.zeros(shape, np.uint8)
    first[2:9, 3:11, 4:12] = 1
    second = np.zeros(shape, np.uint8)
    second[6:12, 7:14, 6:11] = 1
    for index, mask in enumerate((first, second)):
        path = directory / f"mask{index}.nii.gz"
        nib.save(nib.Nifti1Image(mask, affine), path)
        masks.append(
            {
                "id": f"synthetic:structure_{index}",
                "name": f"Synthetic structure {index}",
                "task": "synthetic",
                "group": "Test",
                "color": "#88aabb",
                "mask_file": str(path),
            }
        )
    bundle = export_bundle(ct_path, masks, directory / "bundle.zip")
    with zipfile.ZipFile(bundle) as archive:
        archive.extractall(directory / "bundle")
    output = directory / "slicer-resaved.seg.nrrd"
    script = directory / "run-in-slicer.py"
    slicer_report = directory / "slicer-runtime.json"
    script.write_text(
        "import json, slicer\n"
        f"volume = slicer.util.loadVolume({str(directory / 'bundle/reference.nii.gz')!r})\n"
        f"node = slicer.util.loadSegmentation({str(directory / 'bundle/segmentation.seg.nrrd')!r})\n"
        "assert volume is not None and node is not None, 'Slicer could not read exported bundle'\n"
        f"assert slicer.util.saveNode(node, {str(output)!r}), 'Slicer could not save segmentation'\n"
        "report = {'version': slicer.app.applicationVersion, 'revision': slicer.app.repositoryRevision,\n"
        "          'ct_dimensions': list(volume.GetImageData().GetDimensions()),\n"
        "          'segment_ids': [node.GetSegmentation().GetNthSegmentID(i)\n"
        "                          for i in range(node.GetSegmentation().GetNumberOfSegments())]}\n"
        f"with open({str(slicer_report)!r}, 'w') as handle: json.dump(report, handle, indent=2)\n"
        "slicer.util.exit(0)\n"
    )
    command = ["/opt/3dslicer/Slicer", "--no-main-window", "--no-splash", "--disable-settings", "--ignore-slicerrc", "--python-script", str(script), "--exit"]
    environment = dict(os.environ, QT_QPA_PLATFORM="xcb" if os.environ.get("SCAN3D_XVFB") == "1" else "offscreen")
    with (directory / "slicer.log").open("wb") as log:
        completed = subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=180)
    if completed.returncode or not output.is_file() or not slicer_report.is_file():
        raise RuntimeError(f"Installed Slicer roundtrip failed (exit {completed.returncode}); inspect {directory / 'slicer.log'}")
    imported = import_segmentation(ct_path, output, masks, directory / "corrected", require_source_hash=True)
    for structure, expected in zip(imported, (first, second)):
        actual = nib.load(structure["mask_file"])
        np.testing.assert_array_equal(actual.get_fdata(), expected)
        np.testing.assert_allclose(actual.affine, nib.load(ct_path).affine)
    _, header = nrrd.read(str(output), index_order="F")
    report = json.loads(slicer_report.read_text())
    report.update(
        status="passed",
        strict_source_identity=True,
        overlapping_masks=True,
        oblique_reflected_affine=True,
        voxel_equality=True,
        source_hash_tags_preserved=all("Scan3D.ReferenceSHA256:" in header.get(f"Segment{i}_Tags", "") for i in range(2)),
        voxel_counts=[int(first.sum()), int(second.sum())],
        artifacts=str(directory),
    )
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

"""Clearly labelled synthetic CT phantom for testing the entire review workflow."""


def create_demo(service):
    import nibabel as nib
    import numpy as np

    case = service.new_case("Synthetic anatomy demo")
    folder = service.store.case_dir(case["id"]) / "synthetic"
    folder.mkdir(parents=True)
    shape = (80, 80, 112)
    affine = np.array([[3., 0, 0, -120], [0, 3., 0, -120], [0, 0, 3., -168], [0, 0, 0, 1]])
    i, j, k = np.indices(shape)
    r, a, s = 3 * i - 120, 3 * j - 120, 3 * k - 168
    body = (r / 100)**2 + (a / 74)**2 < 1
    ct = np.full(shape, -1000, dtype=np.int16)
    ct[body] = 35
    definitions = [
        ("lung_left", "Left lung", "lungs", "#66bdc9", ((r + 44)/29)**2 + (a/50)**2 + ((s-55)/77)**2 < 1, -750),
        ("lung_right", "Right lung", "lungs", "#8bb5e4", ((r - 44)/29)**2 + (a/50)**2 + ((s-55)/77)**2 < 1, -750),
        ("liver", "Liver", "organs", "#d48a71", ((r-37)/58)**2 + ((a-5)/46)**2 + ((s+55)/34)**2 < 1, 105),
        ("kidney_left", "Left kidney", "organs", "#bf8fc3", ((r+49)/17)**2 + ((a+31)/17)**2 + ((s+97)/27)**2 < 1, 140),
        ("kidney_right", "Right kidney", "organs", "#bf8fc3", ((r-49)/17)**2 + ((a+31)/17)**2 + ((s+97)/27)**2 < 1, 140),
        ("aorta", "Aorta", "vessels", "#e77676", (r+7)**2 + (a+28)**2 < 9**2, 280),
        ("spine", "Spine", "bones", "#dacdaf", r**2 + (a+53)**2 < 13**2, 650),
    ]
    structures = []
    for name, label, group, color, mask, hu in definitions:
        mask &= body
        ct[mask] = hu
        path = folder / f"{name}.nii.gz"
        nib.save(nib.Nifti1Image(mask.astype(np.uint8), affine), path)
        structures.append(dict(id=f"synthetic:{name}", name=label, task="synthetic", group=group, color=color, mask_file=str(path)))
    ct_file = folder / "reference.nii.gz"
    nib.save(nib.Nifti1Image(ct, affine), ct_file)
    case.update(status="imported", synthetic=True)
    service.store.save("cases", case)
    revision = service.build_revision(case["id"], ct_file, structures, "synthetic_demo",
        extra_qc=["Synthetic geometric phantom — not a patient scan or model inference result"])
    service.store.commit_revision(revision)
    return service.store.get("cases", case["id"])

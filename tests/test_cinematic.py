"""Exact, bounded, immutable HU reference sampling for cinematic presentation."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
from pydantic import ValidationError
import pytest

from scan3d import cinematic
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.portable_export import ExportRequest
from scan3d.store import identifier


def save(path, values, affine, *, slope=1, intercept=0, units="mm"):
    image = nib.Nifti1Image(values, affine)
    image.header.set_slope_inter(slope, intercept)
    image.header.set_xyzt_units(units)
    nib.save(image, path)
    return str(path)


@pytest.fixture
def example(tmp_path):
    affine = np.array([[-.8, .1, 0, 21], [.2, 1.4, .1, -13], [0, 0, 2.2, 44], [0, 0, 0, 1]])
    shape = (5, 6, 7)
    values = np.arange(np.prod(shape), dtype=np.int16).reshape(shape) + 50
    values[0, 0, :4] = [-1000, -900, -800, 90]
    myocardial = np.zeros(shape, np.uint8)
    myocardial[0] = 1
    aorta = np.zeros(shape, np.uint8)
    aorta[3:5] = 1
    left = np.zeros(shape, np.uint8)
    left[2] = 1
    values[aorta == 1] += 300
    values[left == 1] += 400
    structures = []
    for name, mask in (("heart_myocardium", myocardial), ("aorta", aorta), ("heart_ventricle_left", left)):
        structures.append({"id": "cardiac:" + name, "name": name, "mask_file": save(tmp_path / (name + '.nii.gz'), mask, affine)})
    revision = {"id": identifier(), "case_id": identifier(), "ct_file": save(tmp_path / 'ct.nii.gz', values, affine), "structures": structures}
    return revision, values, (myocardial, aorta, left), affine


def test_exact_native_medians_immutable_and_aorta_priority(example, tmp_path):
    revision, values, masks, _ = example
    before = deepcopy(revision)
    files = [revision["ct_file"], *[s["mask_file"] for s in revision["structures"]]]
    hashes = [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in files]
    result = cinematic.reference_values(revision, tmp_path / "temps")
    expected_myocardium = values[(masks[0] == 1) & (values > -900)]
    expected_blood = values[masks[1] == 1]
    assert result["myocardium_hu"] == float(np.median(expected_myocardium))
    assert result["blood_hu"] == float(np.median(expected_blood))
    assert result["myocardium_voxels"] == expected_myocardium.size
    assert result["blood_voxels"] == expected_blood.size
    assert result["blood_source"] == "aorta"
    assert result["blood_structure_ids"] == ["cardiac:aorta"]
    assert str(tmp_path) not in str(result)
    assert revision == before
    assert [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in files] == hashes
    assert list((tmp_path / 'temps').iterdir()) == []


def test_float_hu_scaling_even_median_and_nonfinite_filtering(example, tmp_path):
    revision, values, masks, affine = example
    values = values.astype(np.float64) + .237
    values[0, 0, 0] = np.nan
    values[0, 0, 1] = np.inf
    values[0, 0, 2] = -np.inf
    save(Path(revision['ct_file']), values, affine, slope=.75, intercept=17.1)
    hu = np.asarray(nib.load(revision['ct_file']).dataobj, dtype=np.float64)
    result = cinematic.reference_values(revision, tmp_path)
    for key, mask in zip(('myocardium_hu', 'blood_hu'), masks):
        expected = hu[(mask == 1) & np.isfinite(hu) & (hu > -900)]
        assert result[key] == float(np.median(expected))


@pytest.mark.parametrize('name', ['heart_ventricle_left', 'left ventricle', 'left_atrium', 'atrium_left'])
def test_left_heart_fallback_when_aorta_absent_or_empty(example, tmp_path, name):
    revision, _, _, affine = example
    revision['structures'][2]['name'] = name
    save(Path(revision['structures'][1]['mask_file']), np.zeros((5, 6, 7), np.uint8), affine)
    result = cinematic.reference_values(revision, tmp_path)
    assert result['blood_source'] == 'left_heart'
    assert result['blood_structure_ids'] == ['cardiac:heart_ventricle_left']


def test_missing_and_empty_reference_explicit_errors(example, tmp_path):
    revision, _, masks, affine = example
    missing = deepcopy(revision)
    missing['structures'] = revision['structures'][1:]
    with pytest.raises(ValueError, match='No myocardium.*manually'):
        cinematic.reference_values(missing, tmp_path)
    missing['structures'] = revision['structures'][:1]
    with pytest.raises(ValueError, match='No aorta.*manually'):
        cinematic.reference_values(missing, tmp_path)
    save(Path(revision['structures'][0]['mask_file']), np.zeros_like(masks[0]), affine)
    with pytest.raises(ValueError, match='myocardium.*no finite tissue.*manually'):
        cinematic.reference_values(revision, tmp_path)


@pytest.mark.parametrize('kind', ['geometry', 'binary'])
def test_reference_geometry_and_binary_validation(example, tmp_path, kind):
    revision, _, masks, affine = example
    if kind == 'geometry':
        affine = affine.copy()
        affine[0, 3] += 2
    save(Path(revision['structures'][0]['mask_file']), masks[0] * (2 if kind == 'binary' else 1), affine)
    with pytest.raises(ValueError, match=kind):
        cinematic.reference_values(revision, tmp_path)
    assert not list(tmp_path.glob('cinematic-hu-*'))


def test_low_contrast_rejected_and_exact_100_accepted(example, tmp_path):
    revision, values, masks, affine = example
    values[masks[0] == 1] = 80
    values[masks[1] == 1] = 179
    save(Path(revision['ct_file']), values, affine)
    with pytest.raises(ValueError, match='at least 100 HU.*manually'):
        cinematic.reference_values(revision, tmp_path)
    values[masks[1] == 1] = 180
    save(Path(revision['ct_file']), values, affine)
    result = cinematic.reference_values(revision, tmp_path)
    assert result['myocardium_hu'] == 80 and result['blood_hu'] == 180


def test_small_cache_reuses_without_decode_and_checks_same_mtime_replacement(example, tmp_path, monkeypatch):
    revision, values, _, affine = example
    result = cinematic.reference_values(revision, tmp_path)
    original = cinematic._median
    monkeypatch.setattr(cinematic, '_median', lambda *args: pytest.fail('warm cache decoded HU'))
    assert cinematic.reference_values(revision, tmp_path) == result
    # Returned dictionaries are owned; callers cannot poison the retained entry.
    result['blood_hu'] = -500
    assert cinematic.reference_values(revision, tmp_path)['blood_hu'] > 300
    monkeypatch.setattr(cinematic, '_median', original)
    target = Path(revision['ct_file'])
    stat = target.stat()
    replacement = tmp_path / 'replacement.nii.gz'
    save(replacement, values + 20, affine)
    replacement.replace(target)
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    changed = cinematic.reference_values(revision, tmp_path)
    assert changed['myocardium_hu'] == cinematic._CACHE[next(reversed(cinematic._CACHE))]['myocardium_hu']
    assert changed['blood_hu'] > 300
    assert changed['myocardium_hu'] != result['myocardium_hu']


def test_bounded_slabs_and_sample_budget_cleanup(example, tmp_path, monkeypatch):
    monkeypatch.setattr(cinematic, '_SLAB_VOXELS', 17)
    for shape in ((5, 6, 7), (1000, 2, 3), (2, 1000, 3), (2, 3, 1000)):
        regions = list(cinematic._regions(shape))
        assert all(np.prod([part.stop - part.start for part in region]) <= 17 for region in regions)
        assert sum(np.prod([part.stop - part.start for part in region]) for region in regions) == np.prod(shape)
    monkeypatch.setattr(cinematic, '_MAX_SAMPLES', 10)
    revision = deepcopy(example[0])
    revision['id'] = identifier()
    with pytest.raises(ValueError, match='too large.*manually'):
        cinematic.reference_values(revision, tmp_path)
    assert not list(tmp_path.glob('cinematic-hu-*'))


def test_http_ownership_readonly_and_missing_reference(example, tmp_path):
    revision, _, _, _ = example
    app = create_app(Settings(tmp_path / 'data', (tmp_path,), web_dir=tmp_path / 'dist'))
    store = app.state.service.store
    store.save('revisions', revision)
    client = TestClient(app, base_url='http://127.0.0.1')
    url = f"/api/cases/{revision['case_id']}/revisions/{revision['id']}/cinematic-reference"
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.json()['myocardium_hu'] > -900
    assert client.get(url.replace(revision['case_id'], identifier())).status_code == 404
    assert client.get(url.replace(revision['id'], 'review-any-1')).status_code == 404
    assert store.revision(revision['case_id'], revision['id']) == revision
    assert not store.list('jobs')
    revision['structures'] = []
    store.save('revisions', revision)
    missing = client.get(url)
    assert missing.status_code == 400
    assert 'manually' in missing.json()['detail']


def test_portable_cinematic_defaults_validation_and_persistence():
    default = ExportRequest().state.volume.cinematic
    assert default.model_dump() == {'myocardiumHU': 80, 'bloodHU': 400, 'shadows': 1.5, 'shadowReach': .3}
    data = {'state': {'volume': {'preset': 'CT-Cinematic-Heart', 'cinematic': {'myocardiumHU': 90, 'bloodHU': 520, 'shadows': 1.8, 'shadowReach': .4}}}}
    assert ExportRequest.model_validate(data).model_dump()['state']['volume'] == {
        'preset': 'CT-Cinematic-Heart', 'shift': 0, 'density': 1, 'quality': 'auto', 'crop': ((0, 0, 0), (1, 1, 1)),
        'cinematic': {'myocardiumHU': 90, 'bloodHU': 520, 'shadows': 1.8, 'shadowReach': .4}}
    for field, value in [('bloodHU', 150), ('myocardiumHU', float('nan')), ('shadows', 3), ('shadowReach', -1), ('bloodHU', 10000)]:
        invalid = deepcopy(data)
        invalid['state']['volume']['cinematic'][field] = value
        with pytest.raises(ValidationError):
            ExportRequest.model_validate(invalid)

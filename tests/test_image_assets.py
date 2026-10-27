"""Image identity is independent of revisions and owns every derived mask.

All images and segmentation outputs are synthetic. The real store, revision,
mesh and HTTP paths run; the inference double never initializes a model/GPU.
"""
from copy import deepcopy
import json
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from scan3d import imaging
from scan3d.image_assets import (
    assert_reference_image, bind_structures, create_image_asset,
    public_image_asset, reference_image,
)
from scan3d.segmentation_merge import merge_task_outputs
from scan3d.store import identifier
from test_on_demand import execute, prepared, requested, rig


@pytest.fixture
def scalar_image(tmp_path):
    values = np.arange(7 * 9 * 11, dtype=np.uint16).reshape(7, 9, 11)
    affine = np.array([[-.8, .2, 0, 31], [0, 1.4, .1, -52], [0, 0, 2.2, 87], [0, 0, 0, 1.]])
    image = nib.Nifti1Image(values, affine)
    image.header.set_xyzt_units('mm')
    image.header.set_slope_inter(.5, -1024)
    path = tmp_path / 'reference.nii'
    nib.save(image, path)
    return path, values


def test_asset_identity_follows_content_case_series_and_frame_not_revision_or_path(scalar_image, tmp_path):
    path, _ = scalar_image
    case_id = identifier()
    acquisition = {'series_uid': 'synthetic-series', 'profile': 'corcta', 'phase': 'arterial'}
    first = create_image_asset(case_id, path, acquisition=acquisition)
    copied = tmp_path / 'copied-reference.nii'
    copied.write_bytes(path.read_bytes())
    repeated = create_image_asset(case_id, copied, acquisition={**acquisition, 'phase': 'unknown'})
    assert repeated['id'] == first['id']
    assert_reference_image(first, copied, first['geometry'])
    assert first['schema_version'] == 1 and len(first['id']) == 32
    assert first['kind'] == 'volume3d' and first['modality'] == 'CT'
    assert first['scalar']['units'] == 'HU'
    assert first['scalar']['dtype'] == 'uint16'
    assert first['scalar']['slope'] == .5 and first['scalar']['intercept'] == -1024
    assert first['geometry'] == imaging.geometry(path)
    assert first['content_sha256'] == repeated['content_sha256']
    other_case = create_image_asset(identifier(), path, acquisition=acquisition)
    other_series = create_image_asset(case_id, path, acquisition={**acquisition, 'series_uid': 'other-series'})
    other_frame = create_image_asset(case_id, path, acquisition=acquisition, frame_selector={'time_index': 1})
    assert len({first['id'], other_case['id'], other_series['id'], other_frame['id']}) == 4


def test_public_asset_excludes_internal_path_and_does_not_mutate_record(scalar_image):
    path, _ = scalar_image
    asset = create_image_asset(identifier(), path, acquisition={'series_uid': 'synthetic-series'})
    original = deepcopy(asset)
    public = public_image_asset(asset)
    assert 'file_path' not in public and str(path) not in json.dumps(public)
    assert public['id'] == asset['id'] and public['geometry'] == asset['geometry']
    public['geometry']['shape'][0] = 999
    assert asset == original


def test_dicom_coordinate_frame_and_study_identity_are_not_collapsed_by_equal_pixels(scalar_image):
    path, _ = scalar_image
    case_id = identifier()
    source = {'series_uid': 'synthetic-series', 'study_uid': 'synthetic-study',
              'frame_of_reference_uid': 'synthetic-frame-a', 'sop_class_uids': ['sop-z', 'sop-a']}
    first = create_image_asset(case_id, path, source=source)
    reordered = create_image_asset(case_id, path, source={**source, 'sop_class_uids': ['sop-a', 'sop-z']})
    other_frame = create_image_asset(case_id, path, source={**source, 'frame_of_reference_uid': 'synthetic-frame-b'})
    other_study = create_image_asset(case_id, path, source={**source, 'study_uid': 'other-study'})
    assert first['id'] == reordered['id']
    assert len({first['id'], other_frame['id'], other_study['id']}) == 3


def test_unknown_optional_dicom_source_identifiers_remain_explicitly_unknown(scalar_image):
    path, _ = scalar_image
    source = {'series_uid': 'synthetic-series', 'study_uid': None, 'frame_of_reference_uid': None,
              'sop_class_uids': ['1.2.840.10008.5.1.4.1.1.2']}
    asset = create_image_asset(identifier(), path, source=source)
    assert asset['source']['study_uid'] is None and asset['source']['frame_of_reference_uid'] is None
    assert public_image_asset(asset)['source'] == source


@pytest.mark.parametrize('field', ['shape', 'affine_ras', 'dtype', 'slope', 'intercept'])
def test_first_registration_validates_geometry_and_scalar_claims_against_source_header(rig, field):
    _, existing = prepared(rig)
    case = rig.service.new_case('Synthetic unregistered source')
    path = Path(existing['ct_file'])
    source_before = path.read_bytes()
    original_before = rig.original.read_bytes()
    prior_assets = deepcopy(rig.service.store.list('image_assets'))
    asset = create_image_asset(case['id'], path, acquisition=existing['acquisition'])
    if field == 'shape':
        asset['geometry']['shape'][0] += 1
    elif field == 'affine_ras':
        asset['geometry']['affine_ras'][0][3] += 10
    elif field == 'dtype':
        asset['scalar']['dtype'] = 'float32'
    elif field == 'slope':
        asset['scalar']['slope'] = 2.0
    else:
        asset['scalar']['intercept'] = 17.0
    # These are structurally plausible claims under the genuine content ID.
    # First insertion must inspect the actual header, not merely trust them.
    with pytest.raises(ValueError, match='(?i)(reference|image|geometry|scalar|header|metadata)'):
        rig.service.store.register_image_asset(asset)
    assert rig.service.store.list('image_assets') == prior_assets
    assert rig.service.store.image_assets(case['id']) == []
    assert path.read_bytes() == source_before and rig.original.read_bytes() == original_before


def test_replacing_reference_bytes_invalidates_identity_even_at_same_path(scalar_image):
    path, _ = scalar_image
    case_id = identifier()
    original = create_image_asset(case_id, path)
    image = nib.load(path)
    values = np.asarray(image.dataobj, dtype=np.float32).copy()
    values[0, 0, 0] += 7
    nib.save(nib.Nifti1Image(values, image.affine), path)
    changed = create_image_asset(case_id, path)
    assert changed['id'] != original['id']
    assert changed['content_sha256'] != original['content_sha256']
    with pytest.raises(ValueError, match='(?i)(reference|identity|content|image)'):
        assert_reference_image(original, path)


def test_reference_validation_rejects_other_content_even_when_geometry_matches(scalar_image, tmp_path):
    path, _ = scalar_image
    asset = create_image_asset(identifier(), path)
    altered = tmp_path / 'different-reference.nii'
    image = nib.load(path)
    nib.save(nib.Nifti1Image(np.asarray(image.dataobj, dtype=np.float32) + 1, image.affine), altered)
    assert imaging.geometry(altered) == asset['geometry']
    with pytest.raises(ValueError, match='(?i)(reference|identity|content|image)'):
        assert_reference_image(asset, altered)
    assert_reference_image(asset, path, asset['geometry'])


def test_structure_bindings_are_copied_and_reject_cross_image_aliases():
    first, second = identifier(), identifier()
    entries = [{'id': 'total:liver', 'name': 'Liver', 'task': 'total', 'qc': []}]
    snapshot = deepcopy(entries)
    bound = bind_structures(entries, first)
    assert bound[0]['reference_image_id'] == first and entries == snapshot
    bound[0]['qc'].append('changed copy')
    assert entries == snapshot
    with pytest.raises(ValueError, match='(?i)(reference|image)'):
        bind_structures(bound, second)
    incoming = [{'id': 'total_v3:liver', 'name': 'Liver', 'task': 'total_v3', 'reference_image_id': second}]
    with pytest.raises(ValueError, match='(?i)(reference|image)'):
        merge_task_outputs(bound, incoming, ['total_v3'], reference_image_id=first)


def test_new_ct_segmentation_and_cached_run_share_one_registered_reference(rig):
    case, ct = prepared(rig)
    reference_id = ct['reference_image_id']
    asset = rig.service.store.image_asset(case['id'], reference_id)
    assert ct['image_asset']['id'] == reference_id
    assert asset['case_id'] == case['id'] and asset['source']['series_uid'] == 'fixture-series'
    first = execute(rig, requested(rig, case, ct))
    repeated = execute(rig, requested(rig, case, first))
    assert len(rig.calls.segments) == 1
    for revision in (first, repeated):
        assert revision['reference_image_id'] == reference_id
        assert revision['image_asset'] == ct['image_asset']
        assert all(entry['reference_image_id'] == reference_id for entry in revision['structures'])
    assets = rig.service.store.image_assets(case['id'])
    assert [entry['id'] for entry in assets] == [reference_id]
    assert rig.service.store.image_asset(case['id'], reference_id) == asset


def test_registered_image_assets_cannot_be_rewritten_under_an_existing_identity(rig):
    case, revision = prepared(rig)
    asset = rig.service.store.image_asset(case['id'], revision['reference_image_id'])
    snapshot = deepcopy(asset)
    changed = deepcopy(asset)
    changed['scalar']['units'] = 'invented units'
    with pytest.raises(ValueError, match='(?i)(immutable|identity|different|conflict|asset)'):
        rig.service.store.register_image_asset(changed)
    assert rig.service.store.image_asset(case['id'], asset['id']) == snapshot


def test_reopening_reference_retains_saved_presentation_when_adapter_defaults_change(rig, monkeypatch):
    from dataclasses import replace
    from scan3d import modalities
    from scan3d.domain import Modality

    case, first = prepared(rig)
    saved_asset = rig.service.store.image_asset(case['id'], first['reference_image_id'])
    adapter = modalities.get_modality_adapter(Modality.CT)
    next_adapter = replace(adapter, display_policy=replace(adapter.display_policy, default_window=600))
    monkeypatch.setitem(modalities._ADAPTERS, Modality.CT, next_adapter)
    assert modalities.normalize_presentation()['default_window'] == 600
    reopened = rig.service.build_ct_revision(case['id'], Path(first['ct_file']), acquisition=first['acquisition'])
    assert reopened['reference_image_id'] == first['reference_image_id']
    assert reopened['image_asset']['presentation'] == first['image_asset']['presentation']
    assert reopened['image_asset']['presentation']['default_window'] == 400
    assert rig.service.store.image_asset(case['id'], saved_asset['id']) == saved_asset


def test_mask_correction_reuses_reference_without_changing_original_image_or_masks(rig):
    case, ct = prepared(rig)
    first = execute(rig, requested(rig, case, ct))
    before = deepcopy(first)
    original_paths = [ct['ct_file'], *(entry['mask_file'] for entry in first['structures'])]
    original_bytes = {name: Path(name).read_bytes() for name in original_paths}
    changed = deepcopy(first['structures'])
    target = changed[0]
    image = nib.load(target['mask_file'])
    values = np.asarray(image.dataobj).copy()
    values[tuple(np.argwhere(values)[0])] = 0
    corrected_path = Path(first['directory']) / 'corrected-mask.nii.gz'
    nib.save(nib.Nifti1Image(values, image.affine), corrected_path)
    target['mask_file'] = str(corrected_path)
    for key in ('volume_ml', 'voxel_count', 'centroid_ras', 'qc'):
        target.pop(key, None)
    corrected = rig.service.build_revision(case['id'], Path(ct['ct_file']), changed,
        source='direct_edit_correction', parent=first['id'], acquisition=first['acquisition'])
    rig.service.store.commit_revision(corrected)
    assert corrected['reference_image_id'] == first['reference_image_id']
    assert corrected['image_asset'] == first['image_asset']
    assert corrected['structures'][0]['voxel_count'] == first['structures'][0]['voxel_count'] - 1
    assert rig.service.store.revision(case['id'], first['id']) == before
    assert {name: Path(name).read_bytes() for name in original_paths} == original_bytes


def test_child_revision_rejects_other_reference_and_cross_image_masks(rig):
    case, ct = prepared(rig)
    first = execute(rig, requested(rig, case, ct))
    previous = deepcopy(rig.service.store.list('revisions', case['id']))
    foreign_case = rig.service.new_case('Synthetic foreign image')
    foreign = rig.service.build_ct_revision(foreign_case['id'], Path(ct['ct_file']), acquisition=ct['acquisition'])
    wrong_masks = deepcopy(first['structures'])
    wrong_masks[0]['reference_image_id'] = foreign['reference_image_id']
    with pytest.raises(ValueError, match='(?i)(reference|image)'):
        rig.service.build_revision(case['id'], Path(ct['ct_file']), wrong_masks,
            source='bad_correction', parent=first['id'], acquisition=first['acquisition'])
    with pytest.raises(ValueError, match='(?i)(reference|image|source)'):
        rig.service.build_revision(case['id'], Path(ct['ct_file']), first['structures'],
            source='bad_correction', parent=first['id'], acquisition=first['acquisition'],
            image_source={'series_uid': 'fixture-series', 'frame_of_reference_uid': 'other-coordinate-frame'})
    changed_path = Path(ct['ct_file']).with_name('other-reference.nii.gz')
    source = nib.load(ct['ct_file'])
    nib.save(nib.Nifti1Image(np.asarray(source.dataobj) + 1, source.affine), changed_path)
    with pytest.raises(ValueError, match='(?i)(reference|image|content)'):
        rig.service.build_revision(case['id'], changed_path, first['structures'],
            source='bad_correction', parent=first['id'], acquisition=first['acquisition'])
    assert rig.service.store.list('revisions', case['id']) == previous
    assert rig.service.store.get('cases', case['id'])['active_revision_id'] == first['id']


def test_view_only_image_capability_blocks_api_and_worker_before_model_access(rig):
    _, source = prepared(rig)
    case = rig.service.new_case('Synthetic view-only reference')
    asset = create_image_asset(case['id'], Path(source['ct_file']), acquisition=source['acquisition'])
    asset['capabilities'].remove('segmentation')
    rig.service.store.register_image_asset(asset)
    revision = rig.service.build_ct_revision(case['id'], Path(source['ct_file']), acquisition=source['acquisition'])
    rig.service.store.commit_revision(revision)
    before_jobs = deepcopy(rig.service.store.list('jobs', case['id']))
    response = requested(rig, case, revision)
    assert response.status_code == 400 and 'segmentation' in response.json()['detail'].lower()
    assert rig.service.store.list('jobs', case['id']) == before_jobs
    options = {'tasks': ['total'], 'robust_crop': False, 'body_seg': False, 'remove_small_blobs': False}
    queued = rig.service.store.enqueue(case['id'], 'segment', {'revision_id': revision['id'], 'options': options})
    claimed = rig.service.store.claim()
    assert claimed['id'] == queued['id']
    rig.service.execute(claimed)
    failed = rig.service.store.get('jobs', queued['id'])
    assert failed['status'] == 'failed' and 'segmentation' in failed['error'].lower()
    assert not rig.calls.fingerprints and not rig.calls.segments
    assert rig.service.store.get('cases', case['id'])['active_revision_id'] == revision['id']


def test_direct_revision_storage_rejects_foreign_or_rewritten_bound_image_metadata(rig):
    case, revision = prepared(rig)
    other = rig.service.new_case('Synthetic foreign reference')
    foreign = rig.service.build_ct_revision(other['id'], Path(revision['ct_file']), acquisition=revision['acquisition'])
    before = deepcopy(rig.service.store.list('revisions', case['id']))
    bad_owner = deepcopy(revision)
    bad_owner['id'] = identifier()
    bad_owner['reference_image_id'] = foreign['reference_image_id']
    bad_owner['image_asset'] = foreign['image_asset']
    changed_snapshot = deepcopy(revision)
    changed_snapshot['id'] = identifier()
    changed_snapshot['image_asset']['presentation']['default_window'] = 600
    for invalid in (bad_owner, changed_snapshot):
        for write in (lambda: rig.service.store.save('revisions', invalid),
                      lambda: rig.service.store.commit_revision(invalid)):
            with pytest.raises(ValueError, match='(?i)(reference|image|metadata|asset)'):
                write()
    assert rig.service.store.list('revisions', case['id']) == before
    assert rig.service.store.get('cases', case['id'])['active_revision_id'] == revision['id']


def test_legacy_revision_resolves_without_rewriting_stored_metadata_or_source(rig):
    case, current = prepared(rig)
    legacy = deepcopy(current)
    legacy.pop('reference_image_id', None)
    legacy.pop('image_asset', None)
    rig.service.store.save('revisions', legacy)
    record_path = Path(legacy['directory']) / 'revision.json'
    record_path.write_text(json.dumps(legacy, sort_keys=True) + '\n')
    disk_before = record_path.read_bytes()
    image_before = Path(legacy['ct_file']).read_bytes()
    with rig.service.store.connection() as connection:
        db_before = connection.execute('SELECT data FROM revisions WHERE id=?', (legacy['id'],)).fetchone()[0]
    resolved = rig.service.store.resolve_reference(legacy)
    assert resolved['id'] == current['reference_image_id']
    assert reference_image(legacy)['id'] == resolved['id']
    public = rig.http.get(f"/api/cases/{case['id']}/revisions/{legacy['id']}")
    assert public.status_code == 200, public.text
    assert public.json()['reference_image_id'] == resolved['id']
    assert public.json()['image_asset']['id'] == resolved['id']
    with rig.service.store.connection() as connection:
        assert connection.execute('SELECT data FROM revisions WHERE id=?', (legacy['id'],)).fetchone()[0] == db_before
    assert record_path.read_bytes() == disk_before and Path(legacy['ct_file']).read_bytes() == image_before
    assert 'reference_image_id' not in legacy and 'image_asset' not in legacy


def test_asset_api_is_case_owned_and_never_exposes_internal_paths(rig):
    case, revision = prepared(rig)
    prefix = f"/api/cases/{case['id']}/images"
    listed = rig.http.get(prefix)
    assert listed.status_code == 200, listed.text
    assert [item['id'] for item in listed.json()] == [revision['reference_image_id']]
    fetched = rig.http.get(prefix + '/' + revision['reference_image_id'])
    assert fetched.status_code == 200 and fetched.json() == listed.json()[0]
    assert 'file_path' not in fetched.json() and str(rig.service.settings.data_dir) not in fetched.text
    other = rig.service.new_case('Synthetic other case')
    assert rig.http.get(f"/api/cases/{other['id']}/images/{revision['reference_image_id']}").status_code == 404
    assert rig.http.get(prefix + '/' + '0' * 32).status_code == 404
    assert rig.http.get('/api/cases/' + '0' * 32 + '/images').status_code == 404


def test_bound_reference_api_rejects_changed_source_pixels_instead_of_reusing_cached_views(rig):
    case, revision = prepared(rig)
    prefix = f"/api/cases/{case['id']}/revisions/{revision['id']}"
    assert rig.http.get(prefix + '/image-volume').status_code == 200
    before = deepcopy(rig.service.store.revision(case['id'], revision['id']))
    path = Path(revision['ct_file'])
    image = nib.load(path)
    values = np.asarray(image.dataobj, dtype=np.int16).copy() + 1
    nib.save(nib.Nifti1Image(values, image.affine, image.header.copy()), path)
    for endpoint in ('image-volume', 'ct-volume', 'review-frame', 'slice'):
        response = rig.http.get(prefix + '/' + endpoint)
        assert response.status_code == 400, (endpoint, response.text)
        assert 'reference' in response.json()['detail'].lower()
    assert rig.service.store.revision(case['id'], revision['id']) == before
    assert rig.service.store.get('cases', case['id'])['active_revision_id'] == revision['id']


@pytest.mark.parametrize('quality', ['full', 'preview'])
def test_generic_and_legacy_volume_routes_deliver_identical_ct_samples(rig, quality):
    case, revision = prepared(rig)
    prefix = f"/api/cases/{case['id']}/revisions/{revision['id']}"
    legacy = rig.http.get(prefix + '/ct-volume', params={'quality': quality})
    generic = rig.http.get(prefix + '/image-volume', params={'quality': quality})
    assert legacy.status_code == generic.status_code == 200
    old, new = legacy.json(), generic.json()
    for key in ('key', 'shape', 'affine_ras', 'dtype', 'slope', 'intercept', 'byte_length', 'quality'):
        assert old[key] == new[key]
    assert new['reference_image_id'] == revision['reference_image_id']
    assert new['modality'] == 'CT' and new['presentation']['units'] == 'HU'
    before = b''.join(rig.http.get(chunk['url']).content for chunk in old['chunks'])
    after = b''.join(rig.http.get(chunk['url']).content for chunk in new['chunks'])
    assert before == after
    other = rig.service.new_case('Synthetic unrelated owner')
    unauthorized = new['chunks'][0]['url'].replace('/cases/' + case['id'] + '/', '/cases/' + other['id'] + '/')
    assert rig.http.get(unauthorized).status_code == 404
    if quality == 'full':
        values = np.frombuffer(after, dtype=new['dtype']).reshape(new['shape'], order='F')
        np.testing.assert_array_equal(values * new['slope'] + new['intercept'], rig.values)
    else:
        assert new['approximate'] and new['preview_quantization_hu'] == .5
    assert not rig.calls.segments and not rig.calls.fingerprints

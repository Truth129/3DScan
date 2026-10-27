"""Custom PNG palettes stay presentation-only, including fallback and comparison."""
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from scan3d import imaging
from test_ct_multi import source, api  # shared reflected/oblique source and owned API


def request_params(*colors, ids=('total:liver', 'total:spleen'), compare=False):
    return [('native_axis', '2'), ('native_index', '4'), ('window', '400'), ('level', '-500'),
            ('compare', str(compare).lower()), *(('structure_ids', value) for value in ids),
            *(('structure_colors', value) for value in colors)]


def test_png_color_change_preserves_native_hu_geometry_records_and_source_files(api, source):
    client, service, case_id, revision_id = api
    ct, masks, values = source
    prefix = f'/api/cases/{case_id}/revisions/{revision_id}'
    paths = [ct, *masks]
    before_files = [(sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in paths]
    before_revision = service.store.revision(case_id, revision_id)
    base = request_params(ids=('total:liver',))
    before_frame = client.get(prefix + '/review-frame', params=base)
    baseline = client.get(prefix + '/slice', params=base)
    changed = client.get(prefix + '/slice', params=request_params('#12AB34', ids=('total:liver',)))
    changed_meta = client.get(prefix + '/slice-meta', params=request_params('#12AB34', ids=('total:liver',)))
    assert baseline.status_code == changed.status_code == changed_meta.status_code == 200
    assert json.loads(changed.headers['X-Slice-Meta']) == changed_meta.json()
    assert changed_meta.json()['overlays'] == [{'id': 'total:liver', 'color': '#12ab34'}]
    original_meta = json.loads(baseline.headers['X-Slice-Meta'])
    assert {k: v for k, v in changed_meta.json().items() if k != 'overlays'} == {
        k: v for k, v in original_meta.items() if k != 'overlays'}
    rgb = np.asarray(Image.open(BytesIO(changed.content)))
    assert (rgb == [18, 171, 52]).all(axis=-1).any(), 'Native mask contour did not get its custom RGB'
    assert changed.content != baseline.content
    # A later request must not accidentally reuse the custom-palette PNG.
    assert client.get(prefix + '/slice', params=base).content == baseline.content
    after_frame = client.get(prefix + '/review-frame', params=base)
    assert after_frame.content == before_frame.content
    assert after_frame.headers['X-CT-Frame'] == before_frame.headers['X-CT-Frame']
    assert before_revision == service.store.revision(case_id, revision_id)
    assert before_files == [(sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) for path in paths]


def test_multiple_colors_follow_selected_order_and_keep_comparison_meaning(api):
    client, _, case_id, revision_id = api
    prefix = f'/api/cases/{case_id}/revisions/{revision_id}'
    changed = client.get(prefix + '/slice', params=request_params('#123456', '#abcdef', compare=True))
    assert changed.status_code == 200
    assert json.loads(changed.headers['X-Slice-Meta'])['overlays'] == [
        {'id': 'total:liver', 'color': '#123456'}, {'id': 'total:spleen', 'color': '#abcdef'}]
    # Exclude the second overlay so it cannot occlude the compared structure.
    compared = client.get(prefix + '/slice', params=request_params('#123456', ids=('total:liver',), compare=True))
    pixels = np.asarray(Image.open(BytesIO(compared.content)))
    assert (pixels == [34, 197, 94]).all(axis=-1).any(), 'Added contour meaning changed'
    assert (pixels == [244, 63, 94]).all(axis=-1).any(), 'Removed contour meaning changed'


def test_legacy_single_selection_accepts_explicit_presentation_color(api):
    client, _, case_id, revision_id = api
    response = client.get(f'/api/cases/{case_id}/revisions/{revision_id}/slice', params=[
        ('native_axis', '2'), ('native_index', '4'), ('structure_id', 'total:liver'),
        ('structure_colors', '#aabbcc')])
    assert response.status_code == 200
    assert json.loads(response.headers['X-Slice-Meta'])['overlays'] == [
        {'id': 'total:liver', 'color': '#aabbcc'}]


@pytest.mark.parametrize('query', [
    request_params('#123456'),  # incomplete palette for two selected structures
    request_params('#123456', '#abcdef', '#123456'),
    request_params('#abc', '#abcdef'),
    request_params('red', '#abcdef'),
    request_params('#12345g', '#abcdef'),
    request_params('#123456;opacity:0', '#abcdef'),
    request_params('#123456', ids=()),
    request_params('#123456', ids=('another-case:unknown',)),
    request_params(*(['#123456'] * 129), ids=('total:liver',)),
])
def test_invalid_palette_rejected_before_voxel_decode(api, monkeypatch, query):
    client, _, case_id, revision_id = api
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid display palette must be rejected before voxel decoding')
    monkeypatch.setattr(imaging, '_ct_array', forbidden)
    monkeypatch.setattr(imaging, '_slice_mask', forbidden)
    for endpoint in ('slice', 'slice-meta'):
        assert client.get(f'/api/cases/{case_id}/revisions/{revision_id}/{endpoint}', params=query).status_code == 400


def test_opacity_zero_hides_fill_and_contours_without_changing_scalar_frame(api):
    client, _, case_id, revision_id = api
    prefix = f'/api/cases/{case_id}/revisions/{revision_id}'
    query = request_params('#123456', ids=('total:liver',), compare=True)
    baseline_frame = client.get(prefix + '/review-frame', params=query).content
    invisible = client.get(prefix + '/slice', params=[*query, ('overlay_opacity', '0')])
    grayscale = client.get(prefix + '/slice', params=request_params(ids=()))
    assert invisible.status_code == grayscale.status_code == 200
    np.testing.assert_array_equal(np.asarray(Image.open(BytesIO(invisible.content))),
                                  np.asarray(Image.open(BytesIO(grayscale.content))))
    assert client.get(prefix + '/review-frame', params=query).content == baseline_frame


def test_opacity_blends_native_interior_and_contour_independently(api, source):
    client, _, case_id, revision_id = api
    prefix = f'/api/cases/{case_id}/revisions/{revision_id}'
    query = request_params('#ff0000', ids=('total:liver',))
    translucent = client.get(prefix + '/slice', params=[*query, ('overlay_opacity', '.1')])
    opaque = client.get(prefix + '/slice', params=[*query, ('overlay_opacity', '1')])
    image = np.asarray(Image.open(BytesIO(translucent.content)))
    full = np.asarray(Image.open(BytesIO(opaque.content)))
    gray = np.asarray(Image.open(BytesIO(client.get(prefix + '/slice', params=request_params(ids=())).content)))
    # Native axis2 image is values[:,:,4].T: (i=4,j=5) is interior,
    # (i=2,j=5) is the left foreground contour in the independent fixture.
    for ij, coverage in [((4, 5), .1), ((2, 5), .3)]:
        i, j = ij
        expected = np.rint(gray[j, i] * (1 - coverage) + np.array([255, 0, 0]) * coverage).astype(np.uint8)
        np.testing.assert_array_equal(image[j, i], expected)
        np.testing.assert_array_equal(full[j, i], [255, 0, 0])
    assert translucent.content != opaque.content
    assert client.get(prefix + '/slice', params=[*query, ('overlay_opacity', '.1')]).content == translucent.content


@pytest.mark.parametrize('value', ['-0.01', '1.01', 'NaN', 'Infinity'])
def test_api_rejects_invalid_opacity_before_voxel_decode(api, monkeypatch, value):
    client, _, case_id, revision_id = api
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid opacity must fail before voxel decoding')
    monkeypatch.setattr(imaging, '_ct_array', forbidden)
    for endpoint in ('slice', 'slice-meta'):
        response = client.get(f'/api/cases/{case_id}/revisions/{revision_id}/{endpoint}',
                              params=[*request_params(ids=('total:liver',)), ('overlay_opacity', value)])
        assert response.status_code == 422


def test_direct_slice_api_keeps_legacy_appearance_when_opacity_is_unspecified(source):
    ct, (first, _, previous), _ = source
    original = imaging.slice_image(ct, first, 'axial', 80, 400, -500, previous,
                                  native_axis=2, native_index=4)
    explicit = imaging.slice_image(ct, first, 'axial', 80, 400, -500, previous,
                                  native_axis=2, native_index=4, overlay_opacity=None)
    assert original == explicit
    for opacity in [-.1, 1.1, float('nan'), float('inf')]:
        with pytest.raises(ValueError, match='opacity'):
            imaging.slice_image(ct, first, 'axial', 80, 400, -500, overlay_opacity=opacity)

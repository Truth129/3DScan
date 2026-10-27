"""Display isolation margins preserve native masks, physical scale and overlays."""
import gzip
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
import pytest
from scipy import ndimage as ndi
from scipy.spatial import cKDTree

from scan3d import imaging, review_volume
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.store import identifier


def save(path, values, affine):
    image = nib.Nifti1Image(values, affine)
    image.header.set_xyzt_units('mm')
    nib.save(image, path)
    return path


def decode(cache, descriptor, full_shape):
    raw = b''.join(gzip.decompress(review_volume.chunk_path(cache, descriptor, descriptor['key'], c['index']).read_bytes()) for c in descriptor['chunks'])
    cropped = np.frombuffer(raw, np.uint8).reshape(descriptor['shape'], order='F')
    output = np.zeros(full_shape, np.uint8)
    output[tuple(slice(a, a+n) for a, n in zip(descriptor['origin_ijk'], descriptor['shape']))] = cropped
    return output


@pytest.mark.parametrize('shear', [False, True])
@pytest.mark.parametrize('margin', [.5, 2.5, 5])
def test_native_physical_margin_matches_world_space_nearest_voxel_oracle(tmp_path, shear, margin):
    angle = np.deg2rad(29)
    matrix = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0,0,1]]) @ np.diag([-.8,1.4,2.2])
    if shear:
        matrix[:, 1] += matrix[:, 0] * .23
    affine = np.eye(4); affine[:3,:3] = matrix; affine[:3,3] = [43,-17,91]
    shape = (13,14,15)
    binary = np.zeros(shape, np.uint8); binary[4:7,5:8,4:9]=1; binary[0,0,0]=1
    ct = save(tmp_path/'ct.nii.gz', np.zeros(shape,np.int16),affine)
    mask = save(tmp_path/'mask.nii.gz',binary,affine)
    actual_affine = imaging._image(ct)[1]
    points = np.indices(shape).reshape(3,-1).T
    world = points @ actual_affine[:3,:3].T
    nearest = cKDTree(world[binary.reshape(-1)==1]).query(world)[0].reshape(shape)
    expected = nearest <= margin + 1e-7
    cache = tmp_path/'cache'
    original_hash = hashlib.sha256(mask.read_bytes()).hexdigest()
    original_ct_hash = hashlib.sha256(ct.read_bytes()).hexdigest()
    exact = review_volume.mask_volume(ct,mask,cache)
    padded = review_volume.mask_volume(ct,mask,cache,margin_mm=margin)
    np.testing.assert_array_equal(decode(cache,padded,shape),expected)
    np.testing.assert_array_equal(decode(cache,exact,shape),binary)
    assert padded['display_margin_mm']==margin
    assert exact['key'] != padded['key']
    assert review_volume.mask_volume(ct,mask,cache,margin_mm=0)==exact
    assert hashlib.sha256(mask.read_bytes()).hexdigest()==original_hash
    assert hashlib.sha256(ct.read_bytes()).hexdigest()==original_ct_hash


def test_margin_cache_reuse_and_union_distributivity(tmp_path, monkeypatch):
    affine=np.diag([.7,1.2,2.3,1]); shape=(17,19,21)
    ct=save(tmp_path/'ct.nii',np.zeros(shape,np.int16),affine)
    a=np.zeros(shape,np.uint8);a[3:5,4:6,5:8]=1
    b=np.zeros(shape,np.uint8);b[8:10,4:8,7:10]=1
    cache=tmp_path/'cache';result=[]
    for name, values in [('a',a),('b',b),('union',a|b)]:
        path=save(tmp_path/(name+'.nii'),values,affine)
        d=review_volume.mask_volume(ct,path,cache,margin_mm=2.5)
        result.append(decode(cache,d,shape))
    np.testing.assert_array_equal(result[0]|result[1],result[2])
    monkeypatch.setattr(review_volume,'_margin_crop',lambda *args: pytest.fail('Warm margin cache regenerated'))
    assert review_volume.mask_volume(ct,path,cache,margin_mm=2.5)==d
    assert review_volume.volume_mask_alias('total:heart',2.5)=='volume-margin:2.5:total:heart'
    assert review_volume.volume_mask_alias('total:heart',0)=='total:heart'
    assert review_volume.volume_mask_alias('total:heart',.0625)=='volume-margin:0.063:total:heart'
    assert review_volume.volume_mask_alias('total:heart',.0000001)=='total:heart'


def test_tiled_distance_transform_keeps_native_boundaries_exact(tmp_path):
    shape=(270,19,17);affine=np.diag([.6,1.2,2.2,1]);values=np.zeros(shape,np.uint8)
    values[124:132,8:12,7:10]=1; values[254:259,9,8]=1
    ct=save(tmp_path/'ct.nii',np.zeros(shape,np.int16),affine);mask=save(tmp_path/'mask.nii',values,affine)
    cache=tmp_path/'cache';d=review_volume.mask_volume(ct,mask,cache,margin_mm=2.5)
    expected=ndi.distance_transform_edt(~values.astype(bool),sampling=[.6,1.2,2.2])<=2.5+1e-7
    np.testing.assert_array_equal(decode(cache,d,shape),expected)


def test_preview_samples_native_margin_and_empty_remains_empty(tmp_path):
    shape=(9,11,225);affine=np.diag([.8,1.4,2.2,1]);mask=np.zeros(shape,np.uint8);mask[4,5,109:116]=1
    ct=save(tmp_path/'ct.nii',np.zeros(shape,np.int16),affine);path=save(tmp_path/'mask.nii',mask,affine);cache=tmp_path/'cache'
    full=review_volume.mask_volume(ct,path,cache,margin_mm=2.5)
    preview=review_volume.mask_volume(ct,path,cache,'preview',margin_mm=2.5)
    step=np.asarray(shape)/preview['geometry']['shape'];origin=(step-1)/2
    expected=ndi.affine_transform(decode(cache,full,shape),np.diag(step),origin,output_shape=preview['geometry']['shape'],order=0,mode='constant',cval=0,prefilter=False)
    np.testing.assert_array_equal(decode(cache,preview,preview['geometry']['shape']),expected)
    save(path,np.zeros(shape,np.uint8),affine)
    empty=review_volume.mask_volume(ct,path,cache,margin_mm=2.5)
    assert empty['empty'] and empty['shape']==[1,1,1]


@pytest.mark.parametrize('margin',[-.1,5.1,float('nan'),float('inf')])
def test_margin_is_finite_and_bounded(tmp_path,margin):
    with pytest.raises(ValueError,match='between 0 and 5'):
        review_volume.mask_volume(tmp_path/'missing.nii',tmp_path/'missing.nii',tmp_path/'cache',margin_mm=margin)


def test_margin_http_chunks_keep_case_and_margin_ownership(tmp_path):
    affine=np.eye(4);shape=(15,17,19);mask=np.zeros(shape,np.uint8);mask[7,8,9]=1
    ct=save(tmp_path/'ct.nii',np.zeros(shape,np.int16),affine);path=save(tmp_path/'mask.nii',mask,affine)
    app=create_app(Settings(tmp_path/'data',(tmp_path,),web_dir=tmp_path/'missing'))
    service=app.state.service;case=service.new_case('Display margin fixture');rid=identifier()
    service.store.save('revisions',dict(id=rid,case_id=case['id'],ct_file=str(ct),geometry=imaging.geometry(ct),structures=[dict(id='total:heart',mask_file=str(path))]))
    base=f"/api/cases/{case['id']}/revisions/{rid}/mask-volume"
    with TestClient(app,base_url='http://127.0.0.1') as client:
        exact=client.get(base,params={'structure_id':'total:heart'}).json()['current']
        response=client.get(base,params={'structure_id':'total:heart','margin_mm':2.5})
        assert response.status_code==200,response.text
        padded=response.json()['current'];url=padded['chunks'][0]['url']
        assert 'margin_mm=2.5' in url and padded['key']!=exact['key']
        assert client.get(url).status_code==200
        assert client.get(url.replace('&margin_mm=2.5','')).status_code==404
        assert client.get(url.replace(case['id'],identifier())).status_code==404
        assert client.get(base,params={'structure_id':'total:heart','margin_mm':6}).status_code==422
        assert client.get(base,params={'structure_id':'bad','margin_mm':2.5}).status_code==400

import { afterEach, describe, expect, it, vi } from 'vitest';
import { gzipSync } from 'node:zlib';
import * as THREE from 'three';
import { AnatomyScene } from '../src/lib/scene';
import { EmbeddedCTSession, normalizePortableLayers, normalizePortableState, portableDisplay, portablePoint, validatePortablePayload, type EmbeddedVolume, type PortablePayload } from '../src/lib/portable-viewer';
import type { Geometry, OfflineManifest, Structure } from '../src/types';
import { GPU_VOLUME_SLAB_BYTES } from '../src/lib/gpu-volume-data';
import { transformPoint } from '../src/lib/ct-reslice';
import { CINEMATIC_HEART_PRESET } from '../src/lib/volume-presets';

const affine = [[-.8,.2,0,31],[.1,1.3,.1,-52],[0,.2,2.1,87],[0,0,0,1]];
const geometry: Geometry = { shape:[5,7,3],affine_ras:affine,spacing:[.8,1.3,2.1],bounds_ras:[[0,0,0],[50,50,100]],center_ras:transformPoint(affine,[2,3,1]) };
const structure = (id: string): Structure => ({id,name:id,task:'custom',group:'Custom',color:'#c18dff',volume_ml:1,voxel_count:1,centroid_ras:null,bounds_ras:null,qc:[]});
const structures = [structure('liver'),structure('vessel')];
const manifest: OfflineManifest = {label:'Synthetic',review_status:'unreviewed',structures,geometry};
const base = '/api/cases/offline/revisions/offline';
function embedded(values: Int16Array | Uint8Array, shape:[number,number,number], kind:'ct'|'mask', chunkDepth=16): EmbeddedVolume {
  const sliceBytes=values.byteLength/shape[2],chunks:EmbeddedVolume['descriptor']['chunks']=[],encoded:EmbeddedVolume['chunks']=[];
  for(let start=0,index=0;start<shape[2];start+=chunkDepth,index++){
    const depth=Math.min(chunkDepth,shape[2]-start),offset=start*sliceBytes,length=depth*sliceBytes;
    chunks.push({index,start_k:start,depth,byte_offset:offset,byte_length:length});
    encoded.push({index,gzip_base64:gzipSync(new Uint8Array(values.buffer,values.byteOffset+offset,length)).toString('base64')});
  }
  return {descriptor:{schema_version:1,key:kind==='ct'?'ct-key':'mask-key',kind,shape,affine_ras:affine,dtype:kind==='ct'?'int16':'uint8',slope:1,intercept:0,byte_length:values.byteLength,chunk_depth:chunkDepth,chunks,quality:'full'},chunks:encoded};
}
function payload(): PortablePayload {
  const voxels=new Int16Array(5*7*3); for(let i=0;i<voxels.length;i++)voxels[i]=i*17-1024;
  return {schema_version:1,layers:{segmentation:true,ct_volume:true,ct_plane:true},state:normalizePortableState({plane:{plane:'axial',axis:2,index:1,window:700,level:120,overlays:['liver']},volume_mask:{enabled:true,mode:'outside',ids:['vessel'],margin_mm:0}},structures,geometry),
    ct:embedded(voxels,[5,7,3],'ct',2),masks:[{id:'liver',...embedded(new Uint8Array(5*7*3).fill(1),[5,7,3],'mask',2)},{id:'vessel',...embedded(new Uint8Array(5*7*3),[5,7,3],'mask',2)}]};
}
const sessions:EmbeddedCTSession[]=[];
function session(data=payload(),shown=manifest){const value=new EmbeddedCTSession(data,shown,base);sessions.push(value);return value;}
afterEach(()=>{sessions.splice(0).forEach(value=>value.dispose());vi.unstubAllGlobals();});

describe('portable rendering state and data selection',()=>{
  it('copies visible layer defaults and rejects nonboolean flags',()=>{
    const layers={segmentation:false,ct_volume:true,ct_plane:false},result=normalizePortableLayers(layers);layers.ct_volume=false;
    expect(result).toEqual({segmentation:false,ct_volume:true,ct_plane:false});
    expect(()=>normalizePortableLayers({...layers,ct_plane:1} as never)).toThrow('Invalid portable rendering layers');
  });
  it('owns display, crop, mask and camera values and removes unavailable structure IDs',()=>{
    const input=payload().state;input.display.liver={visible:false,opacity:.32,color:'#123abc'};input.selected='vessel';input.volume.crop=[[.1,.2,.3],[.8,.9,1]];
    input.volume_mask.ids=['vessel','missing','vessel'];input.camera={position_ras:[80,-900,400],target_ras:[2,3,4],up_ras:[0,0,1],zoom:1.7,height_mm:240,name:'Saved'};
    const result=normalizePortableState(input,structures,geometry);input.volume.crop[0][0]=.9;input.camera.position_ras[0]=0;input.volume_mask.ids.push('liver');
    expect(result.display.liver).toEqual({visible:false,opacity:.32,color:'#123abc'});expect(result.volume.crop[0][0]).toBe(.1);expect(result.camera?.position_ras[0]).toBe(80);expect(result.volume_mask.ids).toEqual(['vessel']);
    const display=portableDisplay(result);display.liver.opacity=1;expect(result.display.liver.opacity).toBe(.32);
  });
  it('retains cinematic calibration and shadows in an owned portable state without source IDs',()=>{
    const input=payload();input.state.volume.preset=CINEMATIC_HEART_PRESET;
    input.state.volume.cinematic={myocardiumHU:105,bloodHU:720,shadows:1.2,shadowReach:.6};
    const result=validatePortablePayload(input,manifest);
    input.state.volume.cinematic.bloodHU=500;
    expect(result.state.volume.preset).toBe(CINEMATIC_HEART_PRESET);
    expect(result.state.volume.cinematic).toEqual({myocardiumHU:105,bloodHU:720,shadows:1.2,shadowReach:.6});
    expect(Object.keys(result.state.volume.cinematic).sort()).toEqual(['bloodHU','myocardiumHU','shadowReach','shadows']);
  });
  it('restores an oblique reflected native plane without changing patient coordinates',()=>{
    const state=normalizePortableState({plane:{plane:'sagittal',axis:0,index:4,window:900,level:200,overlays:[]}},structures,geometry);
    const point=portablePoint(state,geometry),expected=transformPoint(affine,[4,3,1]);point.forEach((value,axis)=>expect(value).toBeCloseTo(expected[axis],11));
  });
  it('does not permit excluded CT assets or an empty rendering stack',()=>{
    const data=payload();data.layers={segmentation:true,ct_volume:false,ct_plane:false};expect(()=>validatePortablePayload(data,manifest)).toThrow('Excluded CT');
    delete data.ct;delete data.masks;expect(validatePortablePayload(data,manifest).layers.segmentation).toBe(true);
    data.layers.segmentation=false;expect(()=>validatePortablePayload(data,manifest)).toThrow('at least one');
  });
  it('captures and restores camera position, target, up and scale in patient RAS millimetres',()=>{
    const camera=new THREE.OrthographicCamera();camera.position.set(-.08,.4,-.9);camera.up.set(0,1,0);camera.zoom=1.7;
    const target=new THREE.Vector3(-.002,.004,.003),fake={camera,controls:{target,update:vi.fn()},height:.24,currentViewName:'oblique',refreshControlsUp:vi.fn(),resize:vi.fn(),nameView:vi.fn()};
    const captured=AnatomyScene.prototype.captureCamera.call(fake as unknown as AnatomyScene);
    expect(captured).toMatchObject({position_ras:[80,-900,400],target_ras:[2,3,4],zoom:1.7,height_mm:240});captured.up_ras.forEach((value,axis)=>expect(value).toBeCloseTo([0,0,1][axis],12));
    camera.position.set(0,0,0);target.set(0,0,0);camera.zoom=1;
    AnatomyScene.prototype.restoreCamera.call(fake as unknown as AnatomyScene,captured);
    expect(camera.position.toArray()).toEqual([-.08,.4,-.9]);expect(target.toArray()).toEqual([-.002,.004,.003]);expect(camera.zoom).toBe(1.7);expect(fake.resize).toHaveBeenCalledOnce();
  });
});

describe('embedded CT transport without a network or external worker',()=>{
  it('keeps padded volume masks separate from exact slice overlays without losing original IDs',async()=>{
    const data=payload();data.state.volume_mask={enabled:true,mode:'inside',ids:['liver'],margin_mm:2.5};
    data.masks=[{id:'liver',...embedded(new Uint8Array(5*7*3),[5,7,3],'mask',2)},
      {id:'volume-margin:2.5:liver',...embedded(new Uint8Array(5*7*3).fill(1),[5,7,3],'mask',2)}];
    data.masks[1].descriptor.display_margin_mm=2.5;
    const forbidden=vi.fn(()=>{throw new Error('Network forbidden');});vi.stubGlobal('fetch',forbidden);
    const value=session(data),plane=(await value.getGPUOverlays(['liver'])).overlays[0];
    const padded=(await value.get(base,['liver'],new AbortController().signal,2.5)).overlays[0];
    expect(value.maskIds).toEqual(['liver']);expect(value.overlayIds).toEqual(['liver']);
    expect(plane.id).toBe('liver');expect(padded.id).toBe('volume-margin:2.5:liver');
    expect(new Uint8Array(await plane.current.readSlab(0,1)).every(n=>n===0)).toBe(true);
    expect(new Uint8Array(await padded.current.readSlab(0,1)).every(n=>n===1)).toBe(true);
    await expect(value.get(base,['liver'],new AbortController().signal,3)).rejects.toThrow('display margin');
    expect(forbidden).not.toHaveBeenCalled();expect(data.state.volume_mask.ids).toEqual(['liver']);
  });
  it('offers padded-only structures for volume masking without offering them as CT overlays',async()=>{
    const data=payload();data.state.plane.overlays=[];data.state.volume_mask={enabled:true,mode:'inside',ids:['vessel'],margin_mm:2.5};
    data.masks=[{id:'volume-margin:2.5:vessel',...embedded(new Uint8Array(5*7*3).fill(1),[5,7,3],'mask',2)}];
    data.masks[0].descriptor.display_margin_mm=2.5;
    const value=session(data);expect(value.maskIds).toEqual(['vessel']);expect(value.overlayIds).toEqual([]);
    await expect(value.getGPUOverlays(['vessel'])).rejects.toThrow('not included');
    expect((await value.get(base,['vessel'],new AbortController().signal,2.5)).overlays[0].id).toBe('volume-margin:2.5:vessel');
    const forged=structuredClone(data);forged.masks![0].id='volume-margin:3:vessel';
    expect(()=>session(forged)).toThrow('Invalid embedded mask selection');
  });

  it('shares one CT source between layers and reads lossless native i-fastest slabs',async()=>{
    const forbidden=vi.fn(()=>{throw new Error('External transport forbidden');});vi.stubGlobal('fetch',forbidden);vi.stubGlobal('Worker',forbidden);
    const value=session(),first=await value.getGPUVolume(),second=await value.getGPUVolume();expect(first).toBe(second);
    const bytes=await first.readSlab(1,2),actual=new Int16Array(bytes);expect([...actual]).toEqual(Array.from({length:70},(_,i)=>(i+35)*17-1024));
    expect(value.diagnostics.fullCPUVolumes).toBe(0);expect(value.diagnostics.decodedCacheBytes).toBeLessThanOrEqual(GPU_VOLUME_SLAB_BYTES);expect(forbidden).not.toHaveBeenCalled();
  });
  it('keeps cropped categorical masks separate and never changes the shared CT selection',async()=>{
    const data=payload();data.masks![1]={id:'vessel',...embedded(new Uint8Array([0,1,1,0]),[2,2,1],'mask',1)};
    data.masks![1].descriptor.origin_ijk=[1,2,1];data.masks![1].descriptor.reference_geometry=geometry;
    const translation=transformPoint(affine,[1,2,1]);data.masks![1].descriptor.affine_ras=affine.map((row,a)=>a<3?[...row.slice(0,3),translation[a]]:[...row]);
    const value=session(data),ct=await value.getGPUVolume(),selected=await value.get(base,['vessel'],new AbortController().signal);
    expect([...new Uint8Array(await selected.overlays[0].current.readSlab(0,1))]).toEqual([0,1,1,0]);
    expect(selected.overlays[0].current.descriptor.origin_ijk).toEqual([1,2,1]);expect(await value.getGPUVolume()).toBe(ct);
    const plane=await value.getGPUOverlays(['liver']);expect(plane.overlays[0].id).toBe('liver');expect(selected.overlays[0].id).toBe('vessel');
    await expect(value.get(base,['missing'],new AbortController().signal)).rejects.toThrow('not included');
  });
  it('rejects missing selected masks and duplicate identities before any voxel decode',()=>{
    const data=payload();data.masks=data.masks!.slice(0,1);expect(()=>session(data)).toThrow('missing from this export');
    const duplicated=payload();duplicated.masks!.push(duplicated.masks![0]);expect(()=>session(duplicated)).toThrow('Invalid embedded mask');
  });
  it('rejects mismatched geometry, invalid descriptors and malformed chunk encodings',()=>{
    const data=payload();data.ct!.descriptor.affine_ras=affine.map(row=>[...row]);data.ct!.descriptor.affine_ras[0][3]+=1;expect(()=>session(data)).toThrow('geometry differs');
    const invalid=payload();invalid.ct!.descriptor.byte_length++;expect(()=>session(invalid)).toThrow('Invalid local CT allocation');
    const encoding=payload();encoding.ct!.chunks[0].gzip_base64='bad!';expect(()=>session(encoding)).toThrow('encoding');
  });
  it('rejects truncated or inflated gzip output instead of uploading partial voxels',async()=>{
    const truncated=payload();truncated.ct!.chunks[0].gzip_base64=gzipSync(new Uint8Array(3)).toString('base64');
    await expect((await session(truncated).getGPUVolume()).readSlab(0,1)).rejects.toThrow('missing voxel bytes');
    const inflated=payload();inflated.ct!.chunks[0].gzip_base64=gzipSync(new Uint8Array(1000)).toString('base64');
    await expect((await session(inflated).getGPUVolume()).readSlab(0,1)).rejects.toThrow('declared byte count');
  });
  it('honors cancellation, immutable revision binding and disposal without fetching',async()=>{
    const value=session(),source=await value.getGPUVolume(),controller=new AbortController();controller.abort();
    await expect(source.readSlab(0,1,controller.signal)).rejects.toMatchObject({name:'AbortError'});
    await expect(value.get('/different',[],new AbortController().signal)).rejects.toThrow('different revision');
    await expect(value.requestSlice()).rejects.toThrow('shared GPU volume');value.dispose();
    expect(value.diagnostics.decodedCacheBytes).toBe(0);await expect(source.readSlab(0,1)).rejects.toMatchObject({name:'AbortError'});
  });
  it('streams a large compressed chunk into a bounded requested slab without caching a full volume',async()=>{
    const shape:[number,number,number]=[512,512,17],values=new Int16Array(shape.reduce((n,x)=>n*x,1));values[512*512]=321;
    const data=payload();data.ct=embedded(values,shape,'ct',17);data.masks=[];data.state.plane.overlays=[];data.state.volume_mask.enabled=false;
    const value=session(data,{...manifest,geometry:{...geometry,shape}}),source=await value.getGPUVolume();
    const result=await source.readSlab(1,1);expect(result.byteLength).toBe(512*512*2);expect(new Int16Array(result)[0]).toBe(321);
    expect(value.diagnostics.decodedCacheBytes).toBe(0);expect(value.diagnostics.fullCPUVolumes).toBe(0);
  });
  it('bounds the decoded chunk cache across uploads and releases it on disposal',async()=>{
    const shape:[number,number,number]=[512,512,32],data=payload();data.ct=embedded(new Int16Array(512*512*32),shape,'ct',16);data.masks=[];data.state.plane.overlays=[];data.state.volume_mask.enabled=false;
    const value=session(data,{...manifest,geometry:{...geometry,shape}}),source=await value.getGPUVolume();
    await source.readSlab(0,16);expect(value.diagnostics.decodedCacheBytes).toBe(GPU_VOLUME_SLAB_BYTES);
    await source.readSlab(16,16);expect(value.diagnostics.decodedCacheBytes).toBe(GPU_VOLUME_SLAB_BYTES);expect(value.diagnostics.decodedCacheEntries).toBe(1);
    value.dispose();expect(value.diagnostics.decodedCacheBytes).toBe(0);
  });
});

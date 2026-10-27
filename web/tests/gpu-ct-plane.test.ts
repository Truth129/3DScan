import { describe, expect, it, vi } from 'vitest';
import * as THREE from 'three';
import { GPUCTVolume, GPU_CT_BUDGET, gpuMaskLayout } from '../src/lib/gpu-ct-plane';
import type { GPUVolumeSource } from '../src/lib/gpu-volume-data';
import type { VolumeDescriptor } from '../src/lib/ct-reslice';
import { AnatomyScene } from '../src/lib/scene';
import { CTVolumeRenderer } from '../src/lib/ct-volume-renderer';
import type { VolumeDisplayUnion } from '../src/lib/gpu-volume-data';
const affine = [[-1,.2,0,10],[0,2,.1,-4],[0,0,3,2],[0,0,0,1]];
function descriptor(key = 'ct', shape: [number,number,number] = [4,3,2], kind: 'ct'|'mask' = 'ct'): VolumeDescriptor {
  const bytes = shape.reduce((a,b) => a*b,kind === 'ct' ? 2 : 1);
  return {schema_version:1,key,kind,shape,affine_ras:affine,dtype:kind === 'ct' ? 'int16' : 'uint8',slope:1,intercept:-100,
    byte_length:bytes,quality:'full',chunk_depth:shape[2],chunks:[{index:0,start_k:0,depth:shape[2],byte_offset:0,byte_length:bytes,url:'/unused'}]};
}
function source(value = descriptor()): GPUVolumeSource {
  return {descriptor:value,readSlab:vi.fn(async (_z,depth) => new ArrayBuffer(value.shape[0]*value.shape[1]*depth*(value.kind === 'ct' ? 2 : 1)))};
}
function renderer(max3D = 2048) {
  const gl = {MAX_3D_TEXTURE_SIZE:1,MAX_TEXTURE_SIZE:2,NO_ERROR:0,getParameter:(parameter:number) => parameter===1 ? max3D : 16384,
    getError:vi.fn(() => 0),isContextLost:() => false};
  return {getContext:() => gl,initTexture:vi.fn(),copyTextureToTexture:vi.fn()} as unknown as THREE.WebGLRenderer;
}
describe('GPU CT volume ownership', () => {
  it('uploads once in bounded slabs, reuses the same CT key, and releases all resources', async () => {
    const api=renderer(),volume=new GPUCTVolume(api),input=source(descriptor('large',[512,512,19]));
    await volume.prepare(input); expect(volume.ready).toBe(true); expect(volume.key).toBe('large');
    expect(input.readSlab).toHaveBeenCalledTimes(2);
    expect(input.readSlab).toHaveBeenNthCalledWith(1,0,16,undefined); expect(input.readSlab).toHaveBeenNthCalledWith(2,16,3,undefined);
    expect(api.copyTextureToTexture).toHaveBeenCalledTimes(2); expect(volume.material.defines).toEqual({CT_SIGNED:1});
    await volume.prepare(input); expect(input.readSlab).toHaveBeenCalledTimes(2);
    volume.clear(); expect(volume.ready).toBe(false); expect(volume.textureCount).toBe(0); expect(volume.bytes).toBe(0); volume.dispose();
  });
  it('rejects unsupported native dimensions, preview volumes, and GPU memory budgets before reads', async () => {
    const tiny=renderer(4),volume=new GPUCTVolume(tiny),input=source(descriptor('too-wide',[5,3,2]));
    await expect(volume.prepare(input)).rejects.toThrow('limits'); expect(input.readSlab).not.toHaveBeenCalled();
    const huge=source(descriptor('too-large',[512,512,1025])); await expect(new GPUCTVolume(renderer()).prepare(huge)).rejects.toThrow('limits');
    expect(huge.descriptor.byte_length).toBeGreaterThan(GPU_CT_BUDGET); expect(huge.readSlab).not.toHaveBeenCalled();
    const preview=source({...descriptor(),quality:'preview'}); await expect(volume.prepare(preview)).rejects.toThrow('limits'); volume.dispose();
  });
  it('uses supplied image background/inversion and rejects same-grid masks from another image before reads', async () => {
    const volume = new GPUCTVolume(renderer()), input = source({ ...descriptor(), reference_image_id: 'source-image', modality: 'MR',
      scalar: { units: 'signal' }, presentation: { units: 'signal', background_value: 0, default_window: 500, default_level: 250,
        photometric_interpretation: 'MONOCHROME1', preset_group: 'MR', preview_quantization_step: 0 } });
    await volume.prepare(input);
    expect(volume.material.uniforms.uBackground.value).toBe(0);
    expect(volume.material.uniforms.uInvert.value).toBe(true);
    expect(volume.material.fragmentShader).toContain('return uBackground');
    const other = source({ ...descriptor('other-mask', [3, 2, 1], 'mask'), reference_image_id: 'different-image' });
    await expect(volume.prepareOverlays({ overlays: [{ id: 'organ', current: other, parent: null }] })).rejects.toThrow('different reference image');
    expect(other.readSlab).not.toHaveBeenCalled(); expect(volume.ready).toBe(true); volume.dispose();
  });
  it('preserves CT storage when mask selections change and recolors without mask uploads', async () => {
    const api=renderer(),volume=new GPUCTVolume(api),input=source(); await volume.prepare(input);
    const mask=source(descriptor('mask',[3,2,1],'mask')),sources={overlays:[{id:'a',current:mask,parent:mask}]};
    await volume.prepareOverlays(sources,[{id:'a',color:[255,0,0]}]); const calls=vi.mocked(api.copyTextureToTexture).mock.calls.length;
    expect(mask.readSlab).toHaveBeenCalledTimes(1); expect(volume.textureCount).toBe(4);
    await volume.prepareOverlays(sources,[{id:'a',color:[0,255,0]}]); expect(api.copyTextureToTexture).toHaveBeenCalledTimes(calls);
    expect(mask.readSlab).toHaveBeenCalledTimes(1); expect(input.readSlab).toHaveBeenCalledTimes(1);
    const palette=volume.material.uniforms.uPalette.value.image.data as Float32Array; expect([...palette.slice(0,4)]).toEqual([0,1,0,0]);
    await volume.prepareOverlays({overlays:[]}); expect(volume.ready).toBe(true); expect(input.readSlab).toHaveBeenCalledTimes(1); volume.dispose();
  });
  it('binds an unsigned R8UI placeholder for empty plane labels and releases it with hidden CT', async () => {
    const volume=new GPUCTVolume(renderer()),ct=source();await volume.prepare(ct);
    const placeholder=volume.material.uniforms.uMasks.value as THREE.DataTexture;
    expect(placeholder.internalFormat).toBe('R8UI');expect(placeholder.format).toBe(THREE.RedIntegerFormat);
    expect(placeholder.type).toBe(THREE.UnsignedByteType);expect(placeholder.image.data).toBeInstanceOf(Uint8Array);
    await volume.prepareOverlays({overlays:[]});expect(volume.material.uniforms.uMasks.value).toBe(placeholder);
    const mask=source(descriptor('mask',[2,2,1],'mask'));
    await volume.prepareOverlays({overlays:[{id:'a',current:mask,parent:null}]});
    expect(volume.material.uniforms.uMasks.value).not.toBe(placeholder);
    volume.clearOverlays();expect(volume.material.uniforms.uMasks.value).toBe(placeholder);
    expect(volume.material.uniforms.uLayers.value).toBe(0);
    const disposal=vi.spyOn(placeholder,'dispose');volume.clear();expect(disposal).toHaveBeenCalledTimes(1);
    expect(volume.material.uniforms.uMasks.value).toBeNull();expect(volume.bytes).toBe(0);volume.dispose();
  });
  it('cancels superseded uploads without disposing the replacement volume', async () => {
    const volume=new GPUCTVolume(renderer()); let resolve!:(bytes:ArrayBuffer)=>void;
    const old={descriptor:descriptor('old'),readSlab:vi.fn(() => new Promise<ArrayBuffer>(r=>{resolve=r;}))};
    const first=volume.prepare(old); await volume.prepare(source(descriptor('new'))); resolve(new ArrayBuffer(48));
    await expect(first).rejects.toThrow('Superseded'); expect(volume.key).toBe('new'); volume.dispose();
  });
  it('reports actual GPU allocation failure and releases partial storage', async () => {
    const api=renderer(); vi.mocked(api.getContext().getError).mockReturnValue(1285);
    const volume=new GPUCTVolume(api),input=source(); await expect(volume.prepare(input)).rejects.toThrow('could not allocate');
    expect(volume.ready).toBe(false); expect(volume.bytes).toBe(0); expect(input.readSlab).not.toHaveBeenCalled(); volume.dispose();
  });
  it('shares a current-mask union atlas without adding volume masks to plane overlays', async () => {
    const api=renderer(),volume=new GPUCTVolume(api),ct=source();await volume.prepare(ct);
    const a=source(descriptor('a',[3,2,1],'mask')),b=source(descriptor('b',[2,2,1],'mask'));
    const previous=source(descriptor('previous',[3,2,1],'mask')),ignoredParent=source(descriptor('unused-parent',[2,2,1],'mask'));
    const planes={overlays:[{id:'a',current:a,parent:previous}]};
    await volume.prepareOverlays(planes,[{id:'a',color:[255,0,0]}],undefined,
      {overlays:[{id:'b',current:b,parent:ignoredParent},{id:'a',current:a,parent:null}]});
    const binding=volume.maskBinding!;expect(binding.isCurrent()).toBe(true);expect(binding.ctTexture).toBe(volume.binding!.texture);
    expect([...binding.currentIndices.keys()].sort()).toEqual(['a','b']);
    expect(volume.material.uniforms.uLayers.value).toBe(1);
    expect(a.readSlab).toHaveBeenCalledTimes(1);expect(b.readSlab).toHaveBeenCalledTimes(1);
    expect(previous.readSlab).toHaveBeenCalledTimes(1);expect(ignoredParent.readSlab).not.toHaveBeenCalled();
    const uploads=vi.mocked(api.copyTextureToTexture).mock.calls.length;
    await volume.prepareOverlays(planes,[{id:'a',color:[0,255,0]}],undefined,
      {overlays:[{id:'a',current:a,parent:null},{id:'b',current:b,parent:null}]});
    expect(api.copyTextureToTexture).toHaveBeenCalledTimes(uploads);expect(ct.readSlab).toHaveBeenCalledTimes(1);
    expect(binding.isCurrent()).toBe(false);expect(volume.maskBinding!.isCurrent()).toBe(true);
    volume.clear();expect(volume.maskBinding).toBeUndefined();volume.dispose();
  });
  it('fails closed before oversized mask reads and rejects conflicting current-revision aliases', async () => {
    const volume=new GPUCTVolume(renderer()),ct=source();await volume.prepare(ct);
    const a=source(descriptor('a',[3,2,1],'mask'));await volume.prepareOverlays({overlays:[{id:'a',current:a,parent:null}]});
    const binding=volume.maskBinding!,huge=source(descriptor('huge',[512,512,2048],'mask'));
    await expect(volume.prepareOverlays({overlays:[]},[],undefined,{overlays:[{id:'huge',current:huge,parent:null}]})).rejects.toThrow('budget');
    expect(huge.readSlab).not.toHaveBeenCalled();expect(volume.maskBinding).toBeUndefined();expect(binding.isCurrent()).toBe(false);
    expect(volume.ready).toBe(true);expect(ct.readSlab).toHaveBeenCalledTimes(1);
    const replacement=source(descriptor('changed',[3,2,1],'mask'));
    await expect(volume.prepareOverlays({overlays:[{id:'a',current:a,parent:null}]},[],undefined,
      {overlays:[{id:'a',current:replacement,parent:null}]})).rejects.toThrow('different revisions');
    expect(replacement.readSlab).not.toHaveBeenCalled();volume.dispose();
  });
  it('does not publish a superseded atlas after its final upload yield', async () => {
    vi.useFakeTimers();
    try {
      const volume=new GPUCTVolume(renderer()),ct=source();const preparation=volume.prepare(ct);
      await vi.runAllTimersAsync();await preparation;
      const old=source(descriptor('old-mask',[2,2,1],'mask'));
      const first=volume.prepareOverlays({overlays:[]},[],undefined,{overlays:[{id:'old',current:old,parent:null}]});
      const firstOutcome=first.catch(error=>error);
      // The old upload has yielded after writing its last slab. Replacing the
      // atlas here must not let that late continuation publish its metadata.
      await Promise.resolve();await Promise.resolve();
      const next=source(descriptor('new-mask',[2,2,1],'mask'));
      const second=volume.prepareOverlays({overlays:[]},[],undefined,{overlays:[{id:'new',current:next,parent:null}]});
      await vi.runAllTimersAsync();await second;
      expect(await firstOutcome).toBeInstanceOf(DOMException);
      expect([...volume.maskBinding!.currentIndices.keys()]).toEqual(['new']);expect(volume.maskBinding!.isCurrent()).toBe(true);
      expect(ct.readSlab).toHaveBeenCalledTimes(1);volume.dispose();
    } finally { vi.useRealTimers(); }
  });
  it('keeps a483.5MiB CT and8MiB union when native colored plane overlays do not fit', async () => {
    const api=renderer(), volume=new GPUCTVolume(api), ct=descriptor('large-ct',[512,512,967]);
    ct.reference_image_id='source-image';
    // Mock an already-resident GPU allocation, without constructing a500MiB CPU array.
    Object.assign(volume,{descriptor:ct,ct:new THREE.Data3DTexture(null,...ct.shape),loaded:true});
    const exact=source(descriptor('native-label',[512,512,128],'mask'));
    const unionDescriptor:VolumeDisplayUnion={...descriptor('union',[256,256,128],'mask'),quality:'preview',
      presentation_only:true,volume_mask_union:true,reference_image_id:'source-image',
      reference_geometry:{shape:ct.shape,affine_ras:ct.affine_ras,spacing:[1,2,3],center_ras:[0,0,0],bounds_ras:[[0,0,0],[1,1,1]]}};
    const union=source(unionDescriptor), unionLayers={overlays:[{id:'volume-mask-union:union',current:union,parent:null}]};
    const scene=Object.create(AnatomyScene.prototype) as AnatomyScene;
    Object.assign(scene,{reviewPlane:{prepareGPUOverlays:volume.prepareOverlays.bind(volume)},updateReviewData:vi.fn()});
    const planeReady=await scene.prepareGPUOverlays({overlays:[{id:'native-label',current:exact,parent:null}]},[],undefined,unionLayers);
    expect(planeReady).toBe(false);expect(exact.readSlab).not.toHaveBeenCalled();expect(union.readSlab).toHaveBeenCalledTimes(1);
    expect(volume.bytes).toBeLessThan(GPU_CT_BUDGET);expect(volume.ready).toBe(true);
    expect([...volume.maskBinding!.currentIndices.keys()]).toEqual(['volume-mask-union:union']);
    const raymarch=new CTVolumeRenderer(api);raymarch.setVolume(volume.binding);
    raymarch.setMask(volume.maskBinding,['volume-mask-union:union'],'inside');
    expect(raymarch.diagnostics.maskReady).toBe(true);expect(raymarch.diagnostics.ready).toBe(true);
    const oldBinding=volume.maskBinding!;volume.clearOverlays();expect(oldBinding.isCurrent()).toBe(false);
    expect(volume.bytes).toBe(ct.byte_length);expect(volume.textureCount).toBe(1);
    await volume.prepareOverlays({overlays:[]},[],undefined,unionLayers);
    expect(volume.maskBinding!.isCurrent()).toBe(true);expect(union.readSlab).toHaveBeenCalledTimes(2);
    raymarch.dispose();volume.dispose();
  });
  it('permits presentation unions only in3D mask inputs and rejects foreign grids', async () => {
    const volume=new GPUCTVolume(renderer()), ct=descriptor();await volume.prepare(source(ct));
    const union:VolumeDisplayUnion={...descriptor('union',[2,2,1],'mask'),quality:'preview',presentation_only:true,volume_mask_union:true,
      reference_image_id:'image',reference_geometry:{shape:ct.shape,affine_ras:ct.affine_ras,spacing:[1,2,3],center_ras:[0,0,0],bounds_ras:[[0,0,0],[1,1,1]]}};
    const input=source(union),layers={overlays:[{id:'union',current:input,parent:null}]};
    await expect(volume.prepareOverlays(layers)).rejects.toThrow('exact mask');expect(input.readSlab).not.toHaveBeenCalled();
    union.reference_geometry={...union.reference_geometry!,shape:[99,2,1]};
    await expect(volume.prepareOverlays({overlays:[]},[],undefined,layers)).rejects.toThrow('reference CT geometry');
    expect(input.readSlab).not.toHaveBeenCalled();volume.dispose();
  });
  it('clears hidden GPU storage and re-enables from the retained source without stale mask bindings', async () => {
    const api=renderer(),volume=new GPUCTVolume(api),ct=source();await volume.prepare(ct);
    const first=volume.binding!.texture,dispose=vi.spyOn(first,'dispose');
    const mask=source(descriptor('mask',[2,2,1],'mask'));
    await volume.prepareOverlays({overlays:[{id:'a',current:mask,parent:null}]});const previous=volume.maskBinding!;
    volume.clear();expect(volume.bytes).toBe(0);expect(volume.textureCount).toBe(0);expect(dispose).toHaveBeenCalledTimes(1);
    expect(previous.isCurrent()).toBe(false);await volume.prepare(ct);
    expect(volume.binding!.texture).not.toBe(first);expect(ct.readSlab).toHaveBeenCalledTimes(2);
    expect(volume.maskBinding).toBeUndefined();expect(volume.bytes).toBe(ct.descriptor.byte_length);volume.dispose();
  });
});
describe('GPU cropped-mask layout', () => {
  it('retains exact atlas offsets beyond float32 integer precision and native oblique transforms', () => {
    const ct=descriptor(),first=descriptor('first',[513,512,65],'mask');
    const second=descriptor('second',[2,2,2],'mask');
    second.affine_ras=affine.map(row=>[...row]);
    for (let row=0;row<3;row++) second.affine_ras[row][3]+=affine[row][0]*2+affine[row][1]*3;
    const layout=gpuMaskLayout([first,second],ct,4096),offset=first.byte_length;
    expect(offset).toBeGreaterThan(2**24); expect(layout.info[23]+layout.info[36]*4096).toBe(offset);
    expect([...layout.info.slice(24,28)]).toEqual([1,0,0,-2]);
    expect([...layout.info.slice(28,32)]).toEqual([0,1,0,-3]);
    expect(layout.height).toBe(Math.ceil((first.byte_length+8)/4096));
  });
});

describe('resident overlay presentation colors', () => {
  it('recolors only the palette, preserving CT and mask textures, epochs and voxel uploads', async () => {
    const api=renderer(),volume=new GPUCTVolume(api),ct=source(); await volume.prepare(ct);
    const mask=source(descriptor('color-mask',[2,2,1],'mask'));
    await volume.prepareOverlays({overlays:[{id:'a',current:mask,parent:null}]},[{id:'a',color:[255,0,0]}]);
    const binding=volume.maskBinding!, ctTexture=volume.binding!.texture;
    const palette=volume.material.uniforms.uPalette.value as THREE.DataTexture;
    const uploads=vi.mocked(api.copyTextureToTexture).mock.calls.length;
    const allocatedBytes=volume.bytes;
    expect(volume.setOverlayColors([{id:'a',color:[0,128,255]}])).toBe(true);
    expect(volume.material.uniforms.uPalette.value).toBe(palette);
    expect([...palette.image.data!.slice(0,3)]).toEqual([0,Math.fround(128/255),1]);
    expect(volume.binding!.texture).toBe(ctTexture); expect(volume.maskBinding!.texture).toBe(binding.texture);
    expect(volume.maskBinding!.info).toBe(binding.info); expect(binding.isCurrent()).toBe(true);
    expect(volume.bytes).toBe(allocatedBytes); expect(api.copyTextureToTexture).toHaveBeenCalledTimes(uploads);
    expect(mask.readSlab).toHaveBeenCalledTimes(1); expect(ct.readSlab).toHaveBeenCalledTimes(1);
    expect(volume.setOverlayColors([{id:'a',color:[0,128,255]}])).toBe(false);
    expect(() => volume.setOverlayColors([{id:'a',color:[0,256,0]}])).toThrow('color');
    volume.dispose();
  });
  it('uses the latest color when it changes during a pending mask upload', async () => {
    const volume=new GPUCTVolume(renderer()); await volume.prepare(source());
    let finish!:(bytes:ArrayBuffer)=>void;
    const mask={descriptor:descriptor('pending-color',[2,2,1],'mask'),readSlab:vi.fn(() => new Promise<ArrayBuffer>(resolve=>{finish=resolve;}))};
    const pending=volume.prepareOverlays({overlays:[{id:'a',current:mask,parent:null}]},[{id:'a',color:[255,0,0]}]);
    volume.setOverlayColors([{id:'a',color:[0,255,0]}]); finish(new ArrayBuffer(4)); await pending;
    const palette=volume.material.uniforms.uPalette.value as THREE.DataTexture;
    expect([...palette.image.data!.slice(0,3)]).toEqual([0,1,0]);
    expect(mask.readSlab).toHaveBeenCalledTimes(1); volume.dispose();
  });
});

import { describe, expect, it, vi } from 'vitest';
import * as THREE from 'three';
import { GPUCTVolume, GPU_CT_BUDGET, gpuMaskLayout } from '../src/lib/gpu-ct-plane';
import type { GPUVolumeSource } from '../src/lib/gpu-volume-data';
import type { VolumeDescriptor } from '../src/lib/ct-reslice';
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

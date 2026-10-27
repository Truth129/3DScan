import { describe,expect,it,vi } from 'vitest';
import * as THREE from 'three';
import { cinematicKeyDirection,CTVolumeRenderer,volumeIJKToWorld,volumeRenderSize,volumeShadowSamples,volumeStepMM,volumeStepOpacity } from '../src/lib/ct-volume-renderer';
import { freshVolumeSettings,getVolumePreset,samplePresetTransfer } from '../src/lib/volume-presets';
import type { GPUCTBinding, GPUCTMaskBinding } from '../src/lib/gpu-ct-plane';
import { rasToScene } from '../src/lib/geometry';
import { transformPoint } from '../src/lib/ct-reslice';

describe('direct CT volume physical contract',()=>{
  const affine=[[-.7,.2,.1,100],[.1,1.4,.15,-70],[.2,.3,2.6,230],[0,0,0,1]];
  it('maps reflected/oblique/sheared native voxel centres into the existing patient scene',()=>{
    const matrix=volumeIJKToWorld({affine_ras:affine}),ijk:[number,number,number]=[13,29,7];
    const expected=rasToScene(transformPoint(affine,ijk)),actual=new THREE.Vector3(...ijk).applyMatrix4(matrix);
    actual.toArray().forEach((value,index)=>expect(value).toBeCloseTo(expected[index],12));
    new THREE.Vector3(...expected).applyMatrix4(matrix.clone().invert()).toArray()
      .forEach((value,index)=>expect(value).toBeCloseTo(ijk[index],10));
  });
  it('uses the inverse-transpose gradient under anisotropic voxel spacing and shear',()=>{
    const matrix=volumeIJKToWorld({affine_ras:affine}),gradientIJK=new THREE.Vector3(2,3,-4),
      gradientWorld=gradientIJK.clone().applyMatrix3(new THREE.Matrix3().getNormalMatrix(matrix));
    const origin=new THREE.Vector3(8,9,10).applyMatrix4(matrix),deltaWorld=new THREE.Vector3(.0013,-.0007,.0021);
    const deltaIJK=origin.clone().add(deltaWorld).applyMatrix4(matrix.clone().invert()).sub(new THREE.Vector3(8,9,10));
    expect(gradientWorld.dot(deltaWorld)).toBeCloseTo(gradientIJK.dot(deltaIJK),10);
  });
  it('retains physical opacity when ray step or quality changes',()=>{
    const alpha=.12,distance=17.3,density=.8;
    const expected=volumeStepOpacity(alpha,distance,density);
    for (const steps of [1,7,32,1536]) {
      const perStep=volumeStepOpacity(alpha,distance/steps,density);
      expect(1-(1-perStep)**steps).toBeCloseTo(expected,12);
    }
    expect(volumeStepOpacity(alpha,distance,2)).toBeCloseTo(volumeStepOpacity(alpha,distance*2,1),12);
    expect(volumeStepOpacity(0,20)).toBe(0);expect(volumeStepOpacity(.8,0)).toBe(0);
  });
  it('bounds interactive and settled framebuffer memory while retaining the viewport aspect',()=>{
    expect(volumeRenderSize(3000,1500,true,'detail')).toEqual([320,160]);
    expect(volumeRenderSize(3000,1500,false,'auto')).toEqual([1024,512]);
    expect(volumeRenderSize(3000,1500,false,'detail')).toEqual([1536,768]);
    expect(volumeRenderSize(96,64,false,'detail')).toEqual([96,64]);
    for (const quality of ['auto','detail'] as const) {
      const [w,h]=volumeRenderSize(10000,10000,false,quality);expect(w*h*4).toBeLessThanOrEqual(9*1024**2);
    }
  });
  it('resolves submillimetre CTs when settled while retaining the bounded motion preview',()=>{
    expect(volumeStepMM(.3,false,'auto')).toBeCloseTo(.225,10);
    expect(volumeStepMM(.3,false,'detail')).toBeCloseTo(.15,10);
    expect(volumeStepMM(.3,true,'detail')).toBe(1.2);
    expect(volumeStepMM(.1,false,'auto')).toBe(.2);
    expect(volumeStepMM(.1,false,'detail')).toBe(.12);
    expect(volumeStepMM(1.25,false,'detail')).toBe(.625);
  });
});

describe('bounded cinematic camera lighting',()=>{
  it('keeps the key at the same upper-left camera angle through orbit',()=>{
    const camera=new THREE.PerspectiveCamera();
    for (const position of [[0,0,1],[-2,1,3],[1,-4,-2]]) {
      camera.position.set(...position as [number,number,number]);camera.lookAt(0,0,0);camera.updateMatrixWorld();
      const direction=cinematicKeyDirection(camera),local=direction.clone().transformDirection(camera.matrixWorldInverse);
      expect(direction.length()).toBeCloseTo(1,12);
      expect(local.x).toBeCloseTo(Math.sin(-20*Math.PI/180)*Math.cos(25*Math.PI/180),12);
      expect(local.y).toBeCloseTo(Math.sin(25*Math.PI/180),12);
      expect(local.z).toBeGreaterThan(.8);
    }
  });
  it('omits secondary rays during interaction, when disabled and for every ordinary preset',()=>{
    const cinematic=freshVolumeSettings({preset:'CT-Cinematic-Heart'});
    expect(volumeShadowSamples(cinematic,false)).toBe(8);
    expect(volumeShadowSamples({...cinematic,quality:'detail'},false)).toBe(12);
    expect(volumeShadowSamples(cinematic,true)).toBe(0);
    expect(volumeShadowSamples({...cinematic,cinematic:{...cinematic.cinematic,shadows:0}},false)).toBe(0);
    expect(volumeShadowSamples(freshVolumeSettings(),false)).toBe(0);
  });
  it('bounds the cinematic frame separately without changing ordinary quality',()=>{
    expect(volumeRenderSize(3000,1500,false,'auto',true)).toEqual([768,384]);
    expect(volumeRenderSize(3000,1500,false,'detail',true)).toEqual([1024,512]);
    expect(volumeRenderSize(3000,1500,true,'detail',true)).toEqual([320,160]);
    expect(volumeRenderSize(3000,1500,false,'detail')).toEqual([1536,768]);
  });
  it('updates the same LUT for HU changes and retains the borrowed CT',()=>{
    const renderer=new CTVolumeRenderer({} as THREE.WebGLRenderer),settings=freshVolumeSettings({preset:'CT-Cinematic-Heart'});
    renderer.setSettings(settings);
    const texture=renderer.material.uniforms.uTransfer.value as THREE.DataTexture;
    const pixels=Array.from(texture.image.data),version=texture.version;
    expect(renderer.material.uniforms.uOpacityCount.value).toBe(10);
    renderer.setSettings({...settings,cinematic:{...settings.cinematic,myocardiumHU:120,bloodHU:600}});
    expect(renderer.material.uniforms.uTransfer.value).toBe(texture);
    expect(Array.from(texture.image.data)).not.toEqual(pixels);
    expect(texture.version).toBeGreaterThan(version);
    const appearanceVersion=texture.version;
    renderer.setSettings({...settings,cinematic:{...settings.cinematic,myocardiumHU:120,bloodHU:600,shadows:0}});
    expect(texture.version).toBe(appearanceVersion);
    renderer.dispose();
  });
  it('retains VTK transfer RGB in cinematic lighting while ordinary presets keep linear light',()=>{
    const renderer=new CTVolumeRenderer({} as THREE.WebGLRenderer);
    for (const presetId of ['CT-Cinematic-Heart','CT-Cardiac'] as const) {
      const settings=freshVolumeSettings({preset:presetId}),preset=getVolumePreset(presetId,settings.cinematic);
      renderer.setSettings(settings);
      const texture=renderer.material.uniforms.uTransfer.value as THREE.DataTexture;
      const index=1300,hu=preset.transferRange[0]+index*(preset.transferRange[1]-preset.transferRange[0])/4095;
      const source=samplePresetTransfer(preset,hu),actual=texture.image.data.slice(index*4,index*4+4);
      const decode=(value:number)=>value<=.04045?value/12.92:((value+.055)/1.055)**2.4;
      source.forEach((value,channel)=>expect(actual[channel]).toBeCloseTo(channel===3||presetId==='CT-Cinematic-Heart'?value:decode(value),6));
    }
    renderer.dispose();
  });
});

describe('borrowed volume-mask authorization',()=>{
  function fixture() {
    const volume=new CTVolumeRenderer({} as THREE.WebGLRenderer),ct=new THREE.Data3DTexture(new Int16Array(8),2,2,2);
    const binding:GPUCTBinding={texture:ct,descriptor:{schema_version:1,key:'ct',kind:'ct',dtype:'int16',shape:[2,2,2],
      affine_ras:[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],slope:1,intercept:0,byte_length:16,chunk_depth:2,chunks:[]}};
    volume.setVolume(binding);
    const atlas=new THREE.DataTexture(new Uint8Array(8),8,1),info=new THREE.DataTexture(new Float32Array(40),5,2);
    let current=true;
    const mask:GPUCTMaskBinding={texture:atlas,info,width:8,key:'atlas',ctKey:'ct',ctTexture:ct,
      currentIndices:new Map([['a',0],['b',1]]),isCurrent:()=>current};
    return {volume,binding,mask,ct,atlas,info,invalidate:()=>{current=false;}};
  }
  it('fails closed for empty, missing, oversized or stale selections in both modes',()=>{
    const {volume,mask,invalidate}=fixture();
    for(const mode of ['inside','outside'] as const) {
      for(const ids of [[],['missing'],Array.from({length:129},(_,i)=>String(i))]) {
        volume.setMask(mask,ids,mode);expect(volume.diagnostics.maskReady).toBe(false);
        expect(volume.material.uniforms.uMaskCount.value).toBe(0);expect(volume.material.uniforms.uMaskReady.value).toBe(false);
      }
      volume.setMask(mask,['a','b','a'],mode);expect(volume.diagnostics.maskReady).toBe(true);
      expect(volume.diagnostics.maskSelectedCount).toBe(2);
      expect([...volume.material.uniforms.uMaskIndices.value.slice(0,2)]).toEqual([0,1]);
    }
    invalidate();expect(volume.diagnostics.maskReady).toBe(false);
    volume.setMask(mask,['a'],'outside');expect(volume.material.uniforms.uMaskReady.value).toBe(false);
    volume.dispose();
  });
  it('checks CT allocation identity and never disposes borrowed CT or mask textures',()=>{
    const {volume,binding,mask,ct,atlas,info}=fixture();
    const disposals=[ct,atlas,info].map(texture=>vi.spyOn(texture,'dispose'));
    volume.setMask(mask,['a'],'inside');expect(volume.diagnostics.maskReady).toBe(true);
    const replacement=new THREE.Data3DTexture(new Int16Array(8),2,2,2);
    volume.setVolume({...binding,texture:replacement});expect(volume.diagnostics.maskReady).toBe(false);
    expect(volume.material.uniforms.uVolumeMasks.value).not.toBe(atlas);
    expect(volume.material.uniforms.uVolumeMasks.value.internalFormat).toBe('R8UI');
    volume.setMask(mask,['a'],'off');expect(volume.diagnostics.maskReady).toBe(true);
    expect(volume.material.uniforms.uVolumeMasks.value).not.toBe(atlas);expect(volume.material.uniforms.uVolumeMaskInfo.value).toBeNull();
    volume.dispose();disposals.forEach(dispose=>expect(dispose).not.toHaveBeenCalled());
    replacement.dispose();ct.dispose();atlas.dispose();info.dispose();
  });
  it('releases every hidden render target while retaining borrowed source textures',()=>{
    const {volume,ct,atlas,info}=fixture(),borrowed=[ct,atlas,info].map(texture=>vi.spyOn(texture,'dispose'));
    const targets=Array.from({length:5},()=>({dispose:vi.fn(),width:1024,height:768}));
    Object.assign(volume,{target:targets[0],planeTarget:targets[1],surfaceLayers:{dispose:targets[2].dispose,bytes:1024},
      stackAccum:targets.slice(3),lastWidth:1024,lastHeight:768,planeActive:true,stackActive:true});
    volume.releaseTargets();targets.forEach(target=>expect(target.dispose).toHaveBeenCalledTimes(1));
    expect(volume.diagnostics.targetBytes).toBe(0);expect(volume.diagnostics.planeTargetBytes).toBe(0);
    expect(volume.diagnostics.stackTargetBytes).toBe(0);expect(volume.diagnostics.planeActive).toBe(false);
    expect(volume.diagnostics.ready).toBe(true);borrowed.forEach(dispose=>expect(dispose).not.toHaveBeenCalled());
    volume.dispose();ct.dispose();atlas.dispose();info.dispose();
  });
});

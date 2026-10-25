/** Controlled local comparison: immutable isolated scalar volume, saved camera. */
import * as THREE from 'three';
import { CTVolumeRenderer } from '../src/lib/ct-volume-renderer';
import { GPUCTVolume } from '../src/lib/gpu-ct-plane';
import { validateDescriptor, type VolumeDescriptor } from '../src/lib/ct-reslice';
import type { GPUVolumeSource } from '../src/lib/gpu-volume-data';
import { freshVolumeSettings,getVolumePreset } from '../src/lib/volume-presets';
import { rasToScene } from '../src/lib/geometry';

const endpoint='http://127.0.0.1:9027';
const renderer=new THREE.WebGLRenderer({antialias:false,alpha:true,premultipliedAlpha:false,preserveDrawingBuffer:true});
renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.setPixelRatio(1);renderer.setSize(768,768,false);renderer.setClearColor(0,1);
document.querySelector('#viewport')!.append(renderer.domElement);
const gl=renderer.getContext() as WebGL2RenderingContext,gpu=new GPUCTVolume(renderer),volume=new CTVolumeRenderer(renderer);
const position:[number,number,number]=[-406.927,148.744,-162.614],target:[number,number,number]=[-18,176,-252.9],up:[number,number,number]=[.234347,-.38016,.89474];
const distance=Math.hypot(...position.map((x,i)=>x-target[i])),halfHeight=distance*Math.tan(Math.PI/12)*.001;
const perspective=new THREE.PerspectiveCamera(30,1,.001,20);
const orthographic=new THREE.OrthographicCamera(-halfHeight,halfHeight,halfHeight,-halfHeight,.001,20);
for(const camera of [perspective,orthographic]) {camera.position.set(...rasToScene(position));camera.up.set(-up[0],up[2],up[1]).normalize();camera.lookAt(new THREE.Vector3(...rasToScene(target)));camera.updateProjectionMatrix();camera.updateMatrixWorld(true);}
let ready=false,reads=0,uploads=0;
for(const name of ['texImage3D','texSubImage3D'] as const){const original=gl[name];Reflect.set(gl,name,(...args:unknown[])=>{uploads++;return Reflect.apply(original,gl,args);});}
async function prepare(){
  if(ready)return;
  const response=await fetch(endpoint+'/descriptor');if(!response.ok)throw Error('No exact local descriptor');
  const descriptor=validateDescriptor(await response.json() as VolumeDescriptor,'ct');
  function source(descriptor:VolumeDescriptor):GPUVolumeSource {return {descriptor,async readSlab(z,depth,signal){
    const low=z*descriptor.byte_length/descriptor.shape[2],high=(z+depth)*descriptor.byte_length/descriptor.shape[2];
    const result=new Uint8Array(high-low);let copied=0;
    for(const chunk of descriptor.chunks){
      const start=Math.max(low,chunk.byte_offset),end=Math.min(high,chunk.byte_offset+chunk.byte_length);if(end<=start)continue;
      const response=await fetch(endpoint+chunk.url,{signal});if(!response.ok)throw Error('Exact slab unavailable');reads++;
      const bytes=new Uint8Array(await response.arrayBuffer());result.set(bytes.subarray(start-chunk.byte_offset,end-chunk.byte_offset),start-low);copied+=end-start;
    }
    if(copied!==high-low)throw Error('Missing exact slab bytes');return result.buffer;
  }};}
  await gpu.prepare(source(descriptor));
  volume.setVolume(gpu.binding);
  const maskResponse=await fetch(endpoint+'/masks');if(!maskResponse.ok)throw Error('No mask configuration');
  const masks=await maskResponse.json() as {id:string;descriptor:VolumeDescriptor}[];
  if(masks.length){await gpu.prepareOverlays({overlays:[]},[],undefined,{overlays:masks.map(mask=>({id:mask.id,current:source(validateDescriptor(mask.descriptor,'mask')),parent:null}))});volume.setMask(gpu.maskBinding,masks.map(mask=>mask.id),'inside');}
  ready=true;
}
async function render(options:{projection?:'perspective'|'orthographic';shadows?:number;shade?:boolean;ambient?:number;diffuse?:number;specular?:number}={}){
  await prepare();
  const projection=options.projection??'perspective',camera=projection==='orthographic'?orthographic:perspective;
  const settings=freshVolumeSettings({preset:'CT-Cinematic-Heart',shift:20,density:1,quality:'detail',cinematic:{myocardiumHU:86,bloodHU:483,shadows:options.shadows??1.5,shadowReach:.3}});
  volume.setSettings(settings);
  volume.setInteracting(false);
  const preset=getVolumePreset(settings.preset,settings.cinematic);
  volume.material.uniforms.uShade.value=preset.shade;
  volume.material.uniforms.uLighting.value.set(preset.ambient,preset.diffuse,preset.specular,preset.specularPower);
  if(options.shade!==undefined)volume.material.uniforms.uShade.value=options.shade;
  if(options.ambient!==undefined)volume.material.uniforms.uLighting.value.x=options.ambient;
  if(options.diffuse!==undefined)volume.material.uniforms.uLighting.value.y=options.diffuse;
  if(options.specular!==undefined)volume.material.uniforms.uLighting.value.z=options.specular;
  const began=performance.now();volume.render(camera);gl.finish();const elapsed=performance.now()-began;
  if(gl.getError()!==gl.NO_ERROR)throw Error('Exact rendering GL error');
  return {projection,options,render_ms:elapsed,diagnostics:volume.diagnostics,slab_reads:reads,uploads,camera:{position_ras:position,target_ras:target,up_ras:up,fov:30,parallel_scale_mm:halfHeight*1000},image:renderer.domElement.toDataURL('image/png')};
}
(window as unknown as {fixture:object}).fixture={render,gl,renderer,volume,prepare};

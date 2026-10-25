/** GPU regression against independently measured VTK display-RGB rendering. */
import * as THREE from 'three';
import { CTVolumeRenderer } from '../src/lib/ct-volume-renderer';
import { GPUCTVolume } from '../src/lib/gpu-ct-plane';
import { allocateVolume, type ResidentVolume } from '../src/lib/ct-reslice';
import { copyGPUVolumeSlab } from '../src/lib/gpu-volume-data';
import { freshVolumeSettings, sampleVolumeTransfer } from '../src/lib/volume-presets';

const renderer=new THREE.WebGLRenderer({antialias:false,alpha:true,premultipliedAlpha:false,preserveDrawingBuffer:true});
renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.setPixelRatio(1);renderer.setSize(96,96,false);renderer.setClearColor(0,1);
document.querySelector('#viewport')!.append(renderer.domElement);
const gl=renderer.getContext() as WebGL2RenderingContext,gpu=new GPUCTVolume(renderer),volume=new CTVolumeRenderer(renderer);
const affine=[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],shape:[number,number,number]=[16,16,16];
const settings=freshVolumeSettings({preset:'CT-Cinematic-Heart',quality:'detail',cinematic:{myocardiumHU:86,bloodHU:483,shadows:0,shadowReach:.3}});
const center=new THREE.Vector3(-.0075,.0075,.0075),camera=new THREE.OrthographicCamera(-.014,.014,.014,-.014,.0001,.4);
camera.position.copy(center).add(new THREE.Vector3(0,.15,0));camera.up.set(0,0,1);camera.lookAt(center);camera.updateMatrixWorld();
function data(key:string,hu:number):ResidentVolume {const bytes=16*16*16*2;const ct=allocateVolume({schema_version:1,key,kind:'ct',shape,affine_ras:affine,dtype:'int16',slope:1,intercept:0,quality:'full',byte_length:bytes,chunk_depth:16,chunks:[{index:0,start_k:0,depth:16,byte_offset:0,byte_length:bytes,url:'/unused'}]});ct.values.fill(hu);return ct;}
async function bind(ct:ResidentVolume){await gpu.prepare({descriptor:ct.descriptor,async readSlab(k,depth){return copyGPUVolumeSlab(ct,k,depth);}});volume.setVolume(gpu.binding);volume.setSettings(settings);volume.setInteracting(false);volume.material.uniforms.uLighting.value.set(.3,0,0,25);volume.material.uniforms.uLightAmbientFactor.value=1;}
function opacity(alpha:number){const u=volume.material.uniforms;u.uOpacityCount.value=2;u.uOpacityPoints.value[0].set(-5000,alpha);u.uOpacityPoints.value[1].set(5000,alpha);}
function pixel(){volume.render(camera);const bytes=new Uint8Array(4);gl.readPixels(48,48,1,1,gl.RGBA,gl.UNSIGNED_BYTE,bytes);if(gl.getError()!==gl.NO_ERROR)throw Error('GPU color readback failed');return [...bytes];}
function near(actual:number[],expected:number[],epsilon=2){return actual.slice(0,3).every((v,i)=>Math.abs(v-expected[i])<=epsilon);}
async function run(){const checks:Record<string,boolean>={},samples:object[]=[];
 for(const hu of [86,123,150,185.4,483]) {const rounded=Math.round(hu);await bind(data('color-'+rounded,rounded));opacity(1);const actual=pixel(),color=sampleVolumeTransfer(settings.preset,rounded,settings.cinematic),expected=color.slice(0,3).map(v=>255*v*.3);checks['display_RGB_ambient_'+rounded]=near(actual,expected);samples.push({hu:rounded,actual,expected});}
 await bind(data('translucent',86));opacity(.05);const translucent=pixel(),coverage=1-Math.pow(.95,16),expected=[.7,.11,.07].map(v=>255*v*.3*coverage);
 checks.translucent_RGB_matches_VTK_display_space_optical_integration=near(translucent,expected);samples.push({kind:'alpha=.05 per mm, 16mm path',coverage,actual:translucent,expected});
 checks.opaque_black_background=translucent[3]===255;
 volume.dispose();gpu.dispose();checks.resources_released=renderer.info.memory.textures===0;
 const report={passed:Object.values(checks).every(Boolean),checks,samples,reference:'Independent VTK ambient-only rendering: transfer RGB multiplied by ambient and physical opacity in display RGB; no sRGB-decoding before shading.'};document.querySelector('#report')!.textContent=JSON.stringify(report,null,2);return report;}
(window as unknown as {fixture:object}).fixture={run,gl};

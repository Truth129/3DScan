/** Real shader readback against the established CPU reslice/slice-shader path.
 * Tiny deterministic oblique/reflected CTs preserve the actual production
 * interpolation, masks, parent comparisons and display color conversion.
 */
import * as THREE from 'three';
import { GPUCTVolume } from '../src/lib/gpu-ct-plane';
import { copyGPUVolumeSlab, GPU_VOLUME_SLAB_BYTES, type GPUOverlaySources, type GPUVolumeSource } from '../src/lib/gpu-volume-data';
import { Review3DPlane } from '../src/lib/review3d-plane';
import { allocateVolume, nativeSliceMetadata, resliceVolume, transformPoint, type ResidentOverlay, type ResidentVolume, type VolumeDescriptor, type VolumeDType } from '../src/lib/ct-reslice';
import type { Geometry, Plane, Point } from '../src/types';

const renderer = new THREE.WebGLRenderer({ antialias: false, preserveDrawingBuffer: true });
renderer.outputColorSpace = THREE.SRGBColorSpace; renderer.setPixelRatio(1);
document.querySelector('#viewport')!.append(renderer.domElement);
const gl = renderer.getContext() as WebGL2RenderingContext;
const gpu = new GPUCTVolume(renderer), cpu = new Review3DPlane(renderer);
const scene = new THREE.Scene(), camera = new THREE.OrthographicCamera(-1, 1, 1, -1, .1, 10);
camera.position.z = 1;
const quad = new THREE.BufferGeometry();
quad.setAttribute('position', new THREE.Float32BufferAttribute([-1,-1,0, 1,-1,0, 1,1,0, -1,1,0], 3));
quad.setAttribute('uv', new THREE.Float32BufferAttribute([0,0, 1,0, 1,1, 0,1], 2)); quad.setIndex([0,1,2, 0,2,3]);
const gpuMesh = new THREE.Mesh(quad, gpu.material);
let uploads = 0, slabReads = 0, largestSlab = 0;
for (const name of ['texImage2D', 'texSubImage2D', 'texImage3D', 'texSubImage3D'] as const) {
  const original = gl[name];
  Reflect.set(gl, name, (...args: unknown[]) => { uploads++; return Reflect.apply(original, gl, args); });
}

const affineIdentity = [[1,0,0,0], [0,1,0,0], [0,0,1,0], [0,0,0,1]];
const affineOblique = [[-.9,-.3,.15,80], [.4,1.2,.21,-45], [.08,.1,2.6,120], [0,0,0,1]];
const colors: { id: string; color: [number, number, number] }[] = [
  { id: 'red', color: [230,70,90] }, { id: 'blue', color: [70,100,240] },
];

function geometry(shape: [number, number, number], affine: number[][]): Geometry {
  const low: Point = [Infinity,Infinity,Infinity], high: Point = [-Infinity,-Infinity,-Infinity];
  for (const i of [-.5,shape[0]-.5]) for (const j of [-.5,shape[1]-.5]) for (const k of [-.5,shape[2]-.5]) {
    const p = transformPoint(affine,[i,j,k]);
    for (let axis = 0; axis < 3; axis++) { low[axis] = Math.min(low[axis],p[axis]); high[axis] = Math.max(high[axis],p[axis]); }
  }
  return { shape, affine_ras: affine, bounds_ras: [low,high], spacing: [0,1,2].map(c => Math.hypot(...affine.slice(0,3).map(row => row[c]))),
    center_ras: low.map((n,i) => (n+high[i])/2) as Point };
}
function volume(key: string, shape: [number, number, number], affine: number[][], dtype: VolumeDType, slope = 1, intercept = 0): ResidentVolume {
  const byteLength = shape.reduce((a,b) => a*b, dtype === 'float32' ? 4 : dtype === 'uint8' ? 1 : 2);
  const descriptor: VolumeDescriptor = { schema_version: 1, key, kind: dtype === 'uint8' ? 'mask' : 'ct', shape,
    affine_ras: affine, dtype, slope, intercept, quality: 'full', byte_length: byteLength, chunk_depth: shape[2],
    chunks: [{ index: 0, start_k: 0, depth: shape[2], byte_offset: 0, byte_length: byteLength, url: '/fixture/'+key }], geometry: geometry(shape,affine) };
  return allocateVolume(descriptor);
}
function source(value: ResidentVolume): GPUVolumeSource {
  return { descriptor: value.descriptor, async readSlab(z,depth) {
    const bytes = copyGPUVolumeSlab(value,z,depth); slabReads++; largestSlab = Math.max(largestSlab,bytes.byteLength); return bytes;
  } };
}
function crop(ct: ResidentVolume, key: string, origin: Point, shape: [number,number,number], variant: number) {
  const affine = ct.descriptor.affine_ras.map(row => [...row]), ras = transformPoint(affine,origin);
  for (let r = 0; r < 3; r++) affine[r][3] = ras[r];
  const mask = volume(key,shape,affine,'uint8');
  mask.descriptor.origin_ijk = origin; mask.descriptor.reference_geometry = ct.descriptor.geometry;
  for (let k = 0; k < shape[2]; k++) for (let j = 0; j < shape[1]; j++) for (let i = 0; i < shape[0]; i++) {
    const filled = variant === 0 ? i > 0 && j > 0 && k > 0 && i < shape[0]-1 && j < shape[1]-1
      : variant === 1 ? i < shape[0]-1 && j > 1 && k < shape[2]-1
      : (i-3)**2 + (j-3)**2 + (k-2)**2 < 15;
    mask.values[i+shape[0]*(j+shape[1]*k)] = filled ? 1 : 0;
  }
  return mask;
}
function read(mesh: THREE.Mesh, width: number, height: number) {
  renderer.setSize(width,height,false); scene.clear(); scene.add(mesh); mesh.visible = true;
  renderer.render(scene,camera);
  const pixels = new Uint8Array(width*height*4); gl.readPixels(0,0,width,height,gl.RGBA,gl.UNSIGNED_BYTE,pixels);
  const code = gl.getError(); if (code !== gl.NO_ERROR) throw new Error('GPU CT shader readback failed: '+code); return pixels;
}
function difference(actual: Uint8Array, expected: Uint8Array) {
  let wrong = 0, maximum = 0;
  for (let i = 0; i < actual.length; i++) { const delta = Math.abs(actual[i]-expected[i]); maximum = Math.max(maximum,delta); if (delta > 2) wrong++; }
  return { wrong, maximum, channels: actual.length };
}

async function run() {
  const checks: Record<string,boolean> = {}, samples: object[] = [];
  let allPixelsCorrect = true, allMovementUploadFree = true, revisionReusedCT = true;
  for (const dtype of ['int16','uint16','float32'] as const) for (const [grid,affine] of [['identity',affineIdentity],['oblique-reflected',affineOblique]] as const) {
    const ct = volume(dtype+'-'+grid,[17,13,11],affine,dtype,1.125,-725.375), shape = ct.descriptor.shape;
    for (let k = 0; k < shape[2]; k++) for (let j = 0; j < shape[1]; j++) for (let i = 0; i < shape[0]; i++) {
      ct.values[i+shape[0]*(j+shape[1]*k)] = dtype === 'int16' ? -300 + i*25+j*11+k*41 : dtype === 'uint16' ? i*25+j*11+k*41 : i*25+j*11+k*41+Math.sin(i+j+k)*.25;
    }
    const current = crop(ct,ct.descriptor.key+'-current',[4,3,2],[8,7,6],0);
    const parent = crop(ct,ct.descriptor.key+'-parent',[5,2,2],[7,8,6],1);
    const blue = crop(ct,ct.descriptor.key+'-blue',[1,1,1],[9,9,7],2);
    const overlays: ResidentOverlay[] = [{ id: 'red', current, parent }, { id: 'blue', current: blue, parent: current }];
    const gpuSources: GPUOverlaySources = { overlays: overlays.map(layer => ({ id: layer.id, current: source(layer.current), parent: layer.parent ? source(layer.parent) : null })) };
    await gpu.prepare(source(ct)); await gpu.prepareOverlays(gpuSources,colors); gpu.setWindow(1800,0);
    const readCount = slabReads; await gpu.prepare(source(ct)); revisionReusedCT &&= slabReads === readCount;
    for (const axis of [0,1,2] as const) for (const index of [0,1,Math.floor(shape[axis]/2),shape[axis]-2,shape[axis]-1]) {
      const plane: Plane = axis === 0 ? 'sagittal' : axis === 1 ? 'coronal' : 'axial';
      const meta = nativeSliceMetadata(ct.descriptor,plane,{ axis,index });
      const frame = resliceVolume(ct,plane,index,null,null,overlays,{ axis,index });
      frame.overlays?.forEach(layer => { layer.color = colors.find(c => c.id === layer.id)!.color; });
      gpu.setMeta(meta,ct.descriptor.geometry!); read(gpuMesh,meta.width,meta.height); // compile/upload appearance once
      const before = uploads; gpu.setMeta(meta,ct.descriptor.geometry!); gpu.setWindow(1800,0);
      const actual = read(gpuMesh,meta.width,meta.height); allMovementUploadFree &&= uploads === before;
      cpu.setFrame(frame,ct.descriptor.geometry!); cpu.setWindow(1800,0);
      // Use a common screen quad: CT sampling retains each real patient affine.
      cpu.mesh.geometry.setAttribute('position',quad.getAttribute('position').clone());
      const expected = read(cpu.mesh,meta.width,meta.height), delta = difference(actual,expected);
      allPixelsCorrect &&= delta.wrong === 0; samples.push({ dtype,grid,axis,index,...delta });
    }
    await gpu.prepareOverlays({ overlays: [] });
    const meta = nativeSliceMetadata(ct.descriptor,'axial',{ axis: 2,index: 5 }); gpu.setMeta(meta,ct.descriptor.geometry!);
    const actual = read(gpuMesh,meta.width,meta.height), empty = resliceVolume(ct,'axial',5,null,null,[],{ axis: 2,index: 5 });
    cpu.setFrame(empty,ct.descriptor.geometry!); cpu.setWindow(1800,0); cpu.mesh.geometry.setAttribute('position',quad.getAttribute('position').clone());
    const delta = difference(actual,read(cpu.mesh,meta.width,meta.height)); allPixelsCorrect &&= delta.wrong === 0;
    samples.push({ dtype,grid,cleared_overlays:true,...delta });
  }
  // These three distinct ROIs cross atlas rows with a partial first row,
  // multiple complete rows and a partial last row for the same mask upload.
  const atlasCT = volume('atlas-rows',[23,19,17],affineOblique,'int16',1.125,-725.375);
  for (let i = 0; i < atlasCT.values.length; i++) atlasCT.values[i] = i%997;
  const atlasCurrent = crop(atlasCT,'atlas-current',[1,2,3],[20,16,12],0);
  const atlasParent = crop(atlasCT,'atlas-parent',[2,1,2],[19,17,13],1);
  const atlasBlue = crop(atlasCT,'atlas-blue',[0,0,0],[10,10,10],2);
  const atlasOverlays: ResidentOverlay[] = [{id:'red',current:atlasCurrent,parent:atlasParent},{id:'blue',current:atlasBlue,parent:atlasCurrent}];
  await gpu.prepare(source(atlasCT)); await gpu.prepareOverlays({overlays:atlasOverlays.map(layer => ({id:layer.id,current:source(layer.current),parent:layer.parent?source(layer.parent):null}))},colors);
  gpu.setWindow(1800,0);
  for (const axis of [0,1,2] as const) {
    const index = Math.floor(atlasCT.descriptor.shape[axis]/2), plane: Plane = axis === 0?'sagittal':axis === 1?'coronal':'axial';
    const meta = nativeSliceMetadata(atlasCT.descriptor,plane,{axis,index}); gpu.setMeta(meta,atlasCT.descriptor.geometry!);
    const actual = read(gpuMesh,meta.width,meta.height), frame = resliceVolume(atlasCT,plane,index,null,null,atlasOverlays,{axis,index});
    frame.overlays?.forEach(layer => { layer.color = colors.find(c => c.id === layer.id)!.color; });
    cpu.setFrame(frame,atlasCT.descriptor.geometry!); cpu.setWindow(1800,0); cpu.mesh.geometry.setAttribute('position',quad.getAttribute('position').clone());
    const delta = difference(actual,read(cpu.mesh,meta.width,meta.height)); allPixelsCorrect &&= delta.wrong === 0;
    samples.push({dtype:'int16',atlas_rows:true,axis,index,...delta});
  }
  // Odd 257-pixel rows cross the bounded 8 MiB slab limit. The last two
  // planes prove addressing survives the upload boundary without padding.
  const large = volume('bounded-slab',[257,257,65],affineIdentity,'int16');
  for (let k = 0; k < 65; k++) large.values.fill(k*17-500,k*257*257,(k+1)*257*257);
  const readsBefore = slabReads; await gpu.prepare(source(large)); await gpu.prepareOverlays({overlays:[]}); gpu.setWindow(1800,0);
  checks.upload_crosses_bounded_slab_boundary = slabReads-readsBefore > 1;
  for (const index of [63,64]) {
    const meta = nativeSliceMetadata(large.descriptor,'axial',{axis:2,index}); gpu.setMeta(meta,large.descriptor.geometry!);
    const actual = read(gpuMesh,meta.width,meta.height), frame = resliceVolume(large,'axial',index,null,null,[],{axis:2,index});
    cpu.setFrame(frame,large.descriptor.geometry!); cpu.setWindow(1800,0); cpu.mesh.geometry.setAttribute('position',quad.getAttribute('position').clone());
    const delta = difference(actual,read(cpu.mesh,meta.width,meta.height)); allPixelsCorrect &&= delta.wrong === 0;
    samples.push({dtype:'int16',slab_boundary:true,index,...delta});
  }
  checks.raw_signed_unsigned_float_native_axes_match_CPU_with_cropped_overlapping_comparison_masks = allPixelsCorrect;
  checks.moving_plane_or_window_requires_no_texture_upload = allMovementUploadFree;
  checks.same_CT_key_reuses_resident_texture = revisionReusedCT;
  checks.worker_slab_copies_stay_within_8_MiB = largestSlab <= GPU_VOLUME_SLAB_BYTES;
  const beforeClear = renderer.info.memory.textures; gpu.clear(); cpu.clear();
  checks.all_owned_textures_are_released = beforeClear > 0 && renderer.info.memory.textures === 0;
  const report = { passed: Object.values(checks).every(Boolean), checks, samples, uploads,slabReads,largestSlab };
  document.querySelector('#report')!.textContent = JSON.stringify(report,null,2); return report;
}
(window as unknown as { fixture: object }).fixture = { run,gl };

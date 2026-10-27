import * as THREE from 'three';
import { CTVolumeRenderer, volumeStepMM } from '../src/lib/ct-volume-renderer';
import { GPUCTVolume } from '../src/lib/gpu-ct-plane';
import { copyGPUVolumeSlab } from '../src/lib/gpu-volume-data';
import { inverseAffine } from '../src/lib/ct-reslice';
import { rasToScene } from '../src/lib/geometry';
import { freshVolumeSettings, getVolumePreset, type CTVolumeSettings } from '../src/lib/volume-presets';
import type { ComparisonCamera, ComparisonOptions, ComparisonRenderer } from './volume-comparison-types';

/** Exercise the production ray caster, without changing its live defaults. */
export async function createThreeComparison(element: HTMLElement, options: ComparisonOptions): Promise<ComparisonRenderer> {
  const renderer = new THREE.WebGLRenderer({ antialias: false, alpha: false, premultipliedAlpha: false, preserveDrawingBuffer: true });
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.setPixelRatio(1);
  renderer.setSize(options.width, options.height, false);
  renderer.setClearColor(0, 1);
  element.append(renderer.domElement);
  const gl = renderer.getContext() as WebGL2RenderingContext;
  const gpu = new GPUCTVolume(renderer),
    volume = new CTVolumeRenderer(renderer);
  const camera = new THREE.OrthographicCamera();
  const resident = { descriptor: options.descriptor, values: options.values, inverse: inverseAffine(options.descriptor.affine_ras) };
  let settings = freshVolumeSettings(options.settings),
    disposed = false,
    slabReads = 0;
  function setCamera(value: ComparisonCamera) {
    const scale = value.parallelScaleMM / 1000,
      aspect = options.width / options.height;
    camera.left = -scale * aspect;
    camera.right = scale * aspect;
    camera.top = scale;
    camera.bottom = -scale;
    camera.near = value.nearMM / 1000;
    camera.far = value.farMM / 1000;
    camera.position.set(...rasToScene(value.positionRAS));
    // Directions use the same axis conversion without millimetre scaling.
    camera.up.set(-value.viewUpRAS[0], value.viewUpRAS[2], value.viewUpRAS[1]).normalize();
    camera.lookAt(new THREE.Vector3(...rasToScene(value.focalPointRAS)));
    camera.updateProjectionMatrix();
    camera.updateMatrixWorld(true);
  }
  function setSettings(value: CTVolumeSettings) {
    settings = freshVolumeSettings(value);
    volume.setSettings(settings);
    const preset = getVolumePreset(settings.preset, settings.cinematic, settings.signal_window);
    // The prototype compares the same unshaded transfer functions first.
    // This override belongs only to this fixture's material.
    volume.material.uniforms.uShade.value = options.quality.lighting !== 'off' && preset.shade;
  }
  try {
    await gpu.prepare({
      descriptor: options.descriptor,
      readSlab: async (start, depth) => {
        slabReads++;
        return copyGPUVolumeSlab(resident, start, depth);
      },
    });
    volume.setVolume(gpu.binding);
    setSettings(settings);
    volume.setInteracting(false);
    setCamera(options.camera);
    const spacing = [0, 1, 2].map(axis => Math.hypot(...options.descriptor.affine_ras.slice(0, 3).map(row => row[axis])));
    const step = volumeStepMM(Math.min(...spacing), false, settings.quality);
    if (Math.abs(step - options.quality.sampleDistanceMM) > 1e-8) throw new Error('The production Three.js sampling must match the comparison request.');
    return {
      canvas: renderer.domElement,
      async render(next) {
        if (disposed) throw new Error('The Three.js comparison has been disposed.');
        if (next) setCamera(next);
        renderer.setRenderTarget(null);
        renderer.clear();
        if (!volume.render(camera)) throw new Error('The production volume did not render.');
      },
      setSettings,
      finishGPU: () => gl.finish(),
      readPixels() {
        const raw = new Uint8Array(options.width * options.height * 4);
        gl.readPixels(0, 0, options.width, options.height, gl.RGBA, gl.UNSIGNED_BYTE, raw);
        if (gl.getError() !== gl.NO_ERROR) throw new Error('Three.js comparison readback failed.');
        return raw;
      },
      stats: () => ({
        backend: 'production Three.js ray caster',
        renderer: gl.getParameter(gl.getExtension('WEBGL_debug_renderer_info')?.UNMASKED_RENDERER_WEBGL ?? gl.RENDERER),
        pixelOrder: 'bottom-left RGBA',
        retainedExtraCPUVolumeBytes: 0,
        ownedCTTextureBytes: gpu.bytes,
        contexts: 1,
        slabReads,
        sampleDistanceMM: step,
        maximumSamplesPerRay: 1536,
        adaptiveSampling: false,
        imageSampleDistance: 1,
        shade: volume.material.uniforms.uShade.value,
        scatteringModel: options.quality.lighting === 'scattering' ? 'existing bounded secondary rays' : 'off',
        ...volume.diagnostics,
      }),
      dispose() {
        if (disposed) return;
        disposed = true;
        volume.dispose();
        gpu.dispose();
        renderer.dispose();
        renderer.forceContextLoss();
        renderer.domElement.remove();
      },
    };
  } catch (error) {
    volume.dispose();
    gpu.dispose();
    renderer.dispose();
    renderer.forceContextLoss();
    renderer.domElement.remove();
    throw error;
  }
}

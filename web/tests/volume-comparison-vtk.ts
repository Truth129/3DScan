/** Isolated prototype: no imports from this file enter the live/offline viewer. */
import '@kitware/vtk.js/Rendering/OpenGL/Profiles/Volume';
import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray';
import vtkImageData from '@kitware/vtk.js/Common/DataModel/ImageData';
import vtkPlane from '@kitware/vtk.js/Common/DataModel/Plane';
import vtkRenderer from '@kitware/vtk.js/Rendering/Core/Renderer';
import vtkRenderWindow from '@kitware/vtk.js/Rendering/Core/RenderWindow';
import vtkRenderWindowInteractor from '@kitware/vtk.js/Rendering/Core/RenderWindowInteractor';
import vtkVolume from '@kitware/vtk.js/Rendering/Core/Volume';
import vtkVolumeMapper from '@kitware/vtk.js/Rendering/Core/VolumeMapper';
import vtkOpenGLRenderWindow from '@kitware/vtk.js/Rendering/OpenGL/RenderWindow';
import type { Point } from '../src/types';
import { rasToLps } from '../src/lib/cornerstone-frame';
import { cornerstoneVolumeGeometry } from '../src/lib/cornerstone-volume';
import { inverseAffine, transformPoint, validateDescriptor, type VolumeValues } from '../src/lib/ct-reslice';
import { freshVolumeSettings, type CTVolumeSettings } from '../src/lib/volume-presets';
import type { ComparisonCamera, ComparisonOptions, ComparisonRenderer } from './volume-comparison-types';
import { applyVTKComparisonPreset } from './volume-comparison-vtk-preset';

export async function createVTKComparison(element: HTMLElement, options: ComparisonOptions): Promise<ComparisonRenderer> {
  const descriptor = validateDescriptor(options.descriptor);
  const geometry = cornerstoneVolumeGeometry(descriptor);
  if (geometry.sheared) throw new Error('This matched three-renderer comparison requires an orthogonal source grid.');
  if (options.values.byteLength !== descriptor.byte_length || options.values.length !== descriptor.shape.reduce((count, size) => count * size, 1))
    throw new Error('The comparison samples do not match their descriptor.');
  if (![options.width, options.height].every(value => Number.isSafeInteger(value) && value > 0)) throw new Error('Invalid comparison viewport size.');
  let values: VolumeValues = options.values;
  if (descriptor.slope !== 1 || descriptor.intercept !== 0) {
    const scaled = new Float32Array(values.length);
    for (let index = 0; index < values.length; index++) scaled[index] = Math.fround(values[index] * descriptor.slope + descriptor.intercept);
    values = scaled;
  }
  if (values instanceof Float32Array && values.some(value => !Number.isFinite(value))) throw new Error('Nonfinite comparison scalar samples.');
  const ownsScaledCopy = values !== options.values;
  const canvas = document.createElement('canvas');
  canvas.width = options.width;
  canvas.height = options.height;
  canvas.style.width = '100%';
  canvas.style.height = '100%';
  const context = canvas.getContext('webgl2', { alpha: true, antialias: false, premultipliedAlpha: false, preserveDrawingBuffer: true, depth: true });
  if (!context) throw new Error('The vtk.js comparison requires WebGL2.');
  const gl: WebGL2RenderingContext = context;
  const gpuRenderer = gl.getParameter(gl.getExtension('WEBGL_debug_renderer_info')?.UNMASKED_RENDERER_WEBGL ?? gl.RENDERER) as string;
  const maxTexture3D = gl.getParameter(gl.MAX_3D_TEXTURE_SIZE) as number;
  if (descriptor.shape.some(size => size > maxTexture3D)) {
    gl.getExtension('WEBGL_lose_context')?.loseContext();
    throw new Error('The comparison volume exceeds the 3D texture size limit.');
  }
  const renderWindow = vtkRenderWindow.newInstance();
  const interactor = vtkRenderWindowInteractor.newInstance();
  const renderer = vtkRenderer.newInstance({ background: [0, 0, 0] });
  renderer.setBackground(0, 0, 0, 1);
  const view = vtkOpenGLRenderWindow.newInstance({ canvas });
  const image = vtkImageData.newInstance();
  const scalars = vtkDataArray.newInstance({ name: 'Synthetic comparison source', numberOfComponents: 1, values });
  const mapper = vtkVolumeMapper.newInstance();
  const actor = vtkVolume.newInstance();
  let disposed = false;
  let settings = freshVolumeSettings(options.settings);
  let propertyStats: ReturnType<typeof applyVTKComparisonPreset>;
  let clipping: ReturnType<typeof vtkPlane.newInstance>[] = [];
  let currentCamera = options.camera;
  let renders = 0;

  function crop() {
    mapper.removeAllClippingPlanes();
    for (const plane of clipping) plane.delete();
    clipping = [];
    if (settings.crop[0].every(value => value === 0) && settings.crop[1].every(value => value === 1)) return;
    const inverse = inverseAffine(descriptor.affine_ras);
    for (let axis = 0; axis < 3; axis++) {
      const gradient = inverse[axis].slice(0, 3),
        length = Math.hypot(...gradient);
      for (const side of [0, 1] as const) {
        const voxel: Point = [0, 0, 0];
        voxel[axis] = settings.crop[side][axis] * descriptor.shape[axis] - 0.5;
        const normal = rasToLps(gradient.map(value => (value / length) * (side === 0 ? 1 : -1)));
        const origin = rasToLps(transformPoint(descriptor.affine_ras, voxel));
        const plane = vtkPlane.newInstance({ normal, origin });
        mapper.addClippingPlane(plane);
        clipping.push(plane);
      }
    }
  }
  function setSettings(input: CTVolumeSettings) {
    if (disposed) throw new Error('The vtk.js comparison has been disposed.');
    settings = freshVolumeSettings(input);
    propertyStats = applyVTKComparisonPreset(actor, settings, options.quality);
    crop();
  }
  function setCamera(input: ComparisonCamera) {
    if (
      !input.positionRAS.every(Number.isFinite) ||
      !input.focalPointRAS.every(Number.isFinite) ||
      !input.viewUpRAS.every(Number.isFinite) ||
      !Number.isFinite(input.parallelScaleMM) ||
      input.parallelScaleMM <= 0 ||
      input.nearMM <= 0 ||
      input.farMM <= input.nearMM
    )
      throw new Error('Invalid comparison camera.');
    const camera = renderer.getActiveCamera();
    camera.setParallelProjection(true);
    camera.setPosition(...rasToLps(input.positionRAS));
    camera.setFocalPoint(...rasToLps(input.focalPointRAS));
    camera.setViewUp(...rasToLps(input.viewUpRAS));
    camera.setParallelScale(input.parallelScaleMM);
    camera.setClippingRange(input.nearMM, input.farMM);
    currentCamera = input;
  }
  function dispose() {
    if (disposed) return;
    disposed = true;
    renderer.removeVolume(actor);
    // The pinned implementation exposes this public operation but omits it
    // from the TypeScript declaration. Delete alone only removes the canvas.
    (view as typeof view & { releaseGraphicsResources(): void }).releaseGraphicsResources();
    renderWindow.removeRenderer(renderer);
    renderWindow.removeView(view);
    view.setContainer(null);
    view.delete();
    interactor.delete();
    actor.getProperty().getRGBTransferFunction(0).delete();
    actor.getProperty().getScalarOpacity(0).delete();
    actor.getProperty().delete();
    actor.delete();
    mapper.delete();
    for (const plane of clipping) plane.delete();
    clipping = [];
    scalars.delete();
    image.delete();
    renderer.getActiveCamera().delete();
    renderer.delete();
    renderWindow.delete();
    values = new Float32Array(0);
    gl.getExtension('WEBGL_lose_context')?.loseContext();
    canvas.remove();
  }
  try {
    image.setDimensions(...geometry.dimensions);
    image.setSpacing(geometry.spacing);
    image.setOrigin(geometry.origin);
    image.setDirection(geometry.direction);
    image.getPointData().setScalars(scalars);
    mapper.setInputData(image);
    actor.setMapper(mapper);
    renderer.addVolume(actor);
    renderWindow.addRenderer(renderer);
    renderWindow.addView(view);
    view.setContainer(element);
    view.setSize(options.width, options.height);
    // Native volume rendering queries the interactor even for programmatic
    // frames. No DOM events or animation loop are bound in this fixture.
    interactor.setView(view);
    setSettings(settings);
    setCamera(options.camera);
    interactor.initialize();
    renders++;
  } catch (reason) {
    dispose();
    throw reason;
  }
  return {
    canvas,
    async render(camera = currentCamera) {
      if (disposed) throw new Error('The vtk.js comparison has been disposed.');
      setCamera(camera);
      renderWindow.render();
      renders++;
    },
    setSettings,
    readPixels() {
      if (disposed) throw new Error('The vtk.js comparison has been disposed.');
      const pixels = new Uint8Array(options.width * options.height * 4);
      gl.readPixels(0, 0, options.width, options.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      if (gl.getError() !== gl.NO_ERROR) throw new Error('vtk.js comparison readback failed.');
      return pixels;
    },
    finishGPU() {
      if (!disposed) gl.finish();
    },
    stats() {
      return {
        renderer: 'vtk.js',
        gpuRenderer,
        pixelOrder: 'bottom-left RGBA',
        contexts: 1,
        vtkVersion: '36.4.1',
        sourceKey: descriptor.key,
        renders,
        scalarBytes: disposed ? 0 : values.byteLength,
        extraScalarCopyBytes: !disposed && ownsScaledCopy ? values.byteLength : 0,
        borrowedSourceSamples: !ownsScaledCopy,
        graphicsResourceBytes: disposed ? 0 : view.getGraphicsMemoryInfo(),
        maxTexture3D,
        frameBuffer: [options.width, options.height],
        geometry: { originLPS: geometry.origin, directionLPS: geometry.direction, spacingMM: geometry.spacing },
        ...propertyStats,
        disposed,
      };
    },
    dispose,
  };
}

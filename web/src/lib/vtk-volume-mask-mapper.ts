import macro from '@kitware/vtk.js/macros';
import vtkViewNodeFactory from '@kitware/vtk.js/Rendering/SceneGraph/ViewNodeFactory';
import type { vtkViewNode } from '@kitware/vtk.js/Rendering/SceneGraph/ViewNode';
import { version as cornerstoneVersion } from '@cornerstonejs/core/version';
import vtkPackage from '@kitware/vtk.js/package.json';
// Core does not export its streaming mapper extension. Keep this single pinned
// implementation dependency here; it preserves Cornerstone's shared texture
// upload/voxel-manager path instead of copying that implementation into the app.
import vtkStreamingMapper from '../../node_modules/@cornerstonejs/core/dist/esm/RenderingEngine/vtkClasses/vtkStreamingOpenGLVolumeMapper.js';

export interface VTKMaskShaderProgram {
  setUniformi(name: string, value: number): void;
  setUniformMatrix(name: string, value: number[]): void;
  setUniform3iv(name: string, value: number[]): void;
}
export interface VTKLabelShaderProgram extends VTKMaskShaderProgram {
  setUniformf(name: string, value: number): void;
  setUniform3fv(name: string, value: Float32Array): void;
}
interface ShaderCell {
  getProgram(): VTKMaskShaderProgram | null;
}
export interface VTKVolumeMaskHooks {
  draw(draw: () => void): void;
  parameters(program: VTKMaskShaderProgram | null): void;
}
export interface VTKVolumeLabelHooks {
  draw(draw: () => void): void;
  parameters(program: VTKLabelShaderProgram | null): void;
}
export interface VTKMaskMapperNode extends vtkViewNode {
  renderPieceDraw(renderer: unknown, actor: unknown): void;
  setMapperShaderParameters(cell: ShaderCell, renderer: unknown, actor: unknown): void;
  setMaskHooks(hooks?: VTKVolumeMaskHooks): void;
  /** Segment colouring binds its own texture beside the mask; both wrap one draw. */
  setLabelHooks(hooks?: VTKVolumeLabelHooks): void;
}

function extendMaskNode(publicAPI: object, model: object, initialValues: object) {
  if (cornerstoneVersion !== '5.13.1' || vtkPackage.version !== '36.4.1')
    throw new Error('The native volume mask mapper requires qualification for the installed Cornerstone/vtk.js versions.');
  vtkStreamingMapper.extend(publicAPI, model, initialValues);
  const node = publicAPI as VTKMaskMapperNode;
  const draw = node.renderPieceDraw,
    parameters = node.setMapperShaderParameters;
  let hooks: VTKVolumeMaskHooks | undefined, labelHooks: VTKVolumeLabelHooks | undefined;
  // macro.newInstance freezes the resulting public API. Install permanent
  // methods now and change only their private hook closures after construction.
  node.setMaskHooks = value => {
    hooks = value;
  };
  node.setLabelHooks = value => {
    labelHooks = value;
  };
  node.renderPieceDraw = (renderer, actor) => {
    const render = () => draw(renderer, actor),
      mask = hooks,
      labels = labelHooks;
    const masked = mask ? () => mask.draw(render) : render;
    if (labels) labels.draw(masked);
    else masked();
  };
  node.setMapperShaderParameters = (cell, renderer, actor) => {
    parameters(cell, renderer, actor);
    const program = cell.getProgram();
    hooks?.parameters(program);
    labelHooks?.parameters(program as VTKLabelShaderProgram | null);
  };
  const release = node.delete;
  node.delete = () => {
    hooks = labelHooks = undefined;
    release();
  };
}
export const createVTKMaskMapperNode = macro.newInstance(extendMaskNode, 'scan3dMaskedStreamingVolumeMapper') as (initialValues: {
  scalarTexture: unknown;
}) => VTKMaskMapperNode;

/** Restrict the override to one volume actor. Every other node delegates to its
 * original factory, including all MPR actors sharing this render window. */
export function attachVTKMaskMapperFactory(actorNode: vtkViewNode, mapper: { getScalarTexture(): unknown }) {
  const previousFactory = actorNode.getMyFactory();
  const factory = macro.newInstance((publicAPI, model, initialValues) => {
    vtkViewNodeFactory.extend(publicAPI, model, initialValues);
    const local = publicAPI as ReturnType<typeof vtkViewNodeFactory.newInstance>;
    local.createNode = dataObject => {
      if (dataObject !== mapper) return previousFactory.createNode(dataObject);
      const node = createVTKMaskMapperNode({ scalarTexture: mapper.getScalarTexture() });
      node.setMyFactory(local);
      return node;
    };
  }, 'scan3dVolumeMaskFactory')() as ReturnType<typeof vtkViewNodeFactory.newInstance>;
  // Usually this runs before the first frame. Recreating a previously visited
  // node releases its own transfer resources, never the borrowed CT texture.
  const oldNode = actorNode.getViewNodeFor(mapper);
  if (oldNode) actorNode.removeNode(oldNode);
  actorNode.setMyFactory(factory);
  const node = actorNode.addMissingNode(mapper) as VTKMaskMapperNode | undefined;
  if (!node || typeof node.setMaskHooks !== 'function' || typeof node.setLabelHooks !== 'function') {
    actorNode.setMyFactory(previousFactory);
    factory.delete();
    throw new Error('The native CT streaming mapper does not support bounded segmentation masking.');
  }
  let disposed = false;
  return {
    node,
    dispose() {
      if (disposed) return;
      disposed = true;
      node.setMaskHooks(undefined);
      node.setLabelHooks(undefined);
      actorNode.setMyFactory(previousFactory);
      factory.delete();
    },
  };
}

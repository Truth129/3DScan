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
export interface VTKLayerShaderProgram {
  setUniformi(name: string, value: number): void;
  isUniformUsed(name: string): boolean;
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
/** The private node state the surface layers need, and nothing more. */
export interface VTKVolumeLayerFrame {
  context: WebGL2RenderingContext;
  renderWindow: unknown;
  renderer: unknown;
  zBufferTexture: { activate(): void; deactivate(): void; getTextureUnit(): number } | null;
}
export interface VTKVolumeLayerHooks {
  /** Each frame's build pass, before any surface draws; `renderer` is the vtk OpenGL renderer node. */
  begin(renderer: unknown): void;
  /** Before the native start, while depth test is still on and no CT texture holds a unit. */
  start(frame: VTKVolumeLayerFrame): void;
  draw(draw: () => void): void;
  parameters(program: VTKLayerShaderProgram | null): void;
}
export interface VTKMaskMapperNode extends vtkViewNode {
  buildPass(prepass: boolean, renderPass: unknown): void;
  renderPieceStart(renderer: unknown, actor: unknown): void;
  renderPieceDraw(renderer: unknown, actor: unknown): void;
  setMapperShaderParameters(cell: ShaderCell, renderer: unknown, actor: unknown): void;
  setMaskHooks(hooks?: VTKVolumeMaskHooks): void;
  /** Segment colouring binds its own texture beside the mask; both wrap one draw. */
  setLabelHooks(hooks?: VTKVolumeLabelHooks): void;
  /** See-through segments are peeled before the CT starts and merged into its one march. */
  setLayerHooks(hooks?: VTKVolumeLayerHooks): void;
}
interface NodeModel {
  context: WebGL2RenderingContext;
  _openGLRenderWindow: unknown;
  _openGLRenderer: unknown;
  zBufferTexture: VTKVolumeLayerFrame['zBufferTexture'];
}

function extendMaskNode(publicAPI: object, nodeModel: object, initialValues: object) {
  if (cornerstoneVersion !== '5.13.1' || vtkPackage.version !== '36.4.1')
    throw new Error('The native volume mask mapper requires qualification for the installed Cornerstone/vtk.js versions.');
  vtkStreamingMapper.extend(publicAPI, nodeModel, initialValues);
  const node = publicAPI as VTKMaskMapperNode,
    model = nodeModel as NodeModel;
  const build = node.buildPass,
    start = node.renderPieceStart,
    draw = node.renderPieceDraw,
    parameters = node.setMapperShaderParameters;
  let hooks: VTKVolumeMaskHooks | undefined, labelHooks: VTKVolumeLabelHooks | undefined, layerHooks: VTKVolumeLayerHooks | undefined;
  // macro.newInstance freezes the resulting public API. Install permanent
  // methods now and change only their private hook closures after construction.
  node.setMaskHooks = value => {
    hooks = value;
  };
  node.setLabelHooks = value => {
    labelHooks = value;
  };
  node.setLayerHooks = value => {
    layerHooks = value;
  };
  node.buildPass = (prepass, renderPass) => {
    build(prepass, renderPass);
    if (prepass) layerHooks?.begin(node.getFirstAncestorOfType('vtkOpenGLRenderer'));
  };
  node.renderPieceStart = (renderer, actor) => {
    layerHooks?.start({
      context: model.context,
      renderWindow: model._openGLRenderWindow,
      renderer: model._openGLRenderer,
      zBufferTexture: model.zBufferTexture,
    });
    start(renderer, actor);
  };
  node.renderPieceDraw = (renderer, actor) => {
    const render = () => draw(renderer, actor),
      mask = hooks,
      labels = labelHooks,
      layers = layerHooks;
    const masked = mask ? () => mask.draw(render) : render;
    const labelled = labels ? () => labels.draw(masked) : masked;
    if (layers) layers.draw(labelled);
    else labelled();
  };
  node.setMapperShaderParameters = (cell, renderer, actor) => {
    parameters(cell, renderer, actor);
    const program = cell.getProgram();
    hooks?.parameters(program);
    labelHooks?.parameters(program as VTKLabelShaderProgram | null);
    layerHooks?.parameters(program as unknown as VTKLayerShaderProgram | null);
  };
  const release = node.delete;
  node.delete = () => {
    hooks = labelHooks = layerHooks = undefined;
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
  if (!node || typeof node.setMaskHooks !== 'function' || typeof node.setLabelHooks !== 'function' || typeof node.setLayerHooks !== 'function') {
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
      node.setLayerHooks(undefined);
      actorNode.setMyFactory(previousFactory);
      factory.delete();
    },
  };
}

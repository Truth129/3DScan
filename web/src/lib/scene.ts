import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { colorCss, rasToScene, sceneToRas } from './geometry';
import { surfaceOpacity, type SurfaceMaterial } from './material';
import { AnatomyTransparency } from './transparency';
import type { CameraView, Geometry, InteractivePrompt, Point, SliceMeta, Structure, StructureDisplay, SurfaceManifest } from '../types';
import type { CTFrame } from './ct-renderer';
import { intersectReviewPlane, intersectReviewSurface, reviewPlaneGesture, reviewSurfacePoint, type SceneReviewState } from './review3d';
import { Review3DPlane } from './review3d-plane';
import { Review3DMarkers } from './review3d-markers';
import type { GPUOverlaySources, GPUVolumeSource } from './gpu-volume-data';
import { GPUCTMaskBudgetError, type GPUOverlayColor } from './gpu-ct-plane';
import { CTVolumeRenderer } from './ct-volume-renderer';
import type { CTVolumeSettings } from './volume-presets';
import { transformPoint } from './ct-reslice';
import type { PortableCamera } from './portable-viewer';
import {
  captureScissors,
  createScissorsPrompt,
  scissorsCaptureMatches,
  scissorsPath,
  scissorsPolygon,
  scissorsProjection,
  scissorsProjectionMatches,
  scissorsScreenPoint,
  type ScissorsCapture,
} from './scissors';
import { apiFetch } from './api-fetch';

declare global {
  interface Window {
    __SCAN3D_RENDER_READY__?: boolean;
  }
}
type Pick = (id: string, point: Point) => void;
export interface OrientationAxis {
  label: string;
  x: number;
  y: number;
  front: boolean;
}
export interface SurfaceProgress {
  completed: number;
  total: number;
  reused: number;
}
interface CachedSurface {
  key: string;
  object: THREE.Group;
  meshes: THREE.Mesh[];
}
interface SurfaceLoadOptions {
  signal?: AbortSignal;
  onProgress?: (progress: SurfaceProgress) => void;
  preserveView?: boolean;
}
/** Display-only state: independent of review targets, drafts and editing tools. */
export interface SceneCTPlaneState {
  geometry: Geometry;
  frame?: CTFrame;
  gpuMeta?: SliceMeta;
  window: number;
  level: number;
  visible: boolean;
  contextKey?: string;
}
export interface SceneCTVolumeState {
  visible: boolean;
  geometry: Geometry;
  settings: CTVolumeSettings;
  mask?: { mode: 'off' | 'inside' | 'outside'; ids: readonly string[]; ready: boolean };
}
const directions: Record<CameraView, [number, number, number]> = {
  anterior: [0, 0, 1],
  posterior: [0, 0, -1],
  right: [-1, 0, 0],
  left: [1, 0, 0],
  superior: [0, 1, 0],
  oblique: [-0.65, 0.25, 1],
};

/** Owns the one WebGL context. No animation loop: controls, resize and state request a frame. */
export class AnatomyScene {
  readonly renderer: THREE.WebGLRenderer;
  readonly scene = new THREE.Scene();
  onGPUCTInvalidated?: (reason?: string) => void;
  onGPUCTRestored?: () => void;
  onCTVolumeError?: (reason: string) => void;
  readonly camera = new THREE.OrthographicCamera(-0.3, 0.3, 0.3, -0.3, 0.001, 20);
  controls: OrbitControls;
  private model?: THREE.Group;
  private meshes = new Map<string, THREE.Mesh[]>();
  private surfaces = new Map<string, CachedSurface>();
  private states: StructureDisplay = {};
  private segmentationLayer = { visible: true, opacity: 1 };
  private viewportControlsReserved = false;
  private observer: ResizeObserver;
  private frame = 0;
  private disposed = false;
  private height = 0.6;
  private center = new THREE.Vector3();
  private radius = 0.3;
  private generation = 0;
  private start = [0, 0];
  private selected = '';
  private crosshair: THREE.Mesh;
  private onError: (message: string) => void;
  private contextExtension: WEBGL_lose_context | null;
  private transparency: AnatomyTransparency;
  private keyLight: THREE.DirectionalLight;
  private fillLight: THREE.DirectionalLight;
  private review?: SceneReviewState;
  private ctPlane?: SceneCTPlaneState;
  private planeGeometry?: Geometry;
  private planeContext = '';
  private planeRenderCount = 0;
  private reviewPlane: Review3DPlane;
  private volumeRenderer?: CTVolumeRenderer;
  private ctVolume?: SceneCTVolumeState;
  private volumeDisabled = false;
  private volumeSettleTimer = 0;
  private surfaceHeight?: number;
  private surfaceView?: {
    position: THREE.Vector3;
    quaternion: THREE.Quaternion;
    up: THREE.Vector3;
    target: THREE.Vector3;
    zoom: number;
    name: string;
    hadSurfaces: boolean;
  };
  private currentViewName = 'oblique';
  private reviewMarkers = new Review3DMarkers();
  private gestureMarkers = new Review3DMarkers();
  private reviewPlaneValid = false;
  private gesture?: { pointerId: number; state: SceneReviewState; pixels: [number, number][]; surface?: Point; scissors?: ScissorsCapture };
  private gestureDirty = false;
  private controlsUp = new THREE.Vector3(0, 1, 0);
  private scissorsOverlay?: SVGSVGElement;
  private scissorsGroup?: SVGGElement;
  private scissorsOverlayKey = '';

  constructor(
    private element: HTMLElement,
    private onPick: Pick,
    onError: (message: string) => void,
    private onView?: (name: string) => void,
    private onOrientation?: (axes: OrientationAxis[]) => void,
    private onGesture?: (prompt: InteractivePrompt) => void,
  ) {
    this.onError = onError;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: false, powerPreference: 'high-performance' });
    this.renderer.debug.onShaderError = (gl, _program, _vertex, fragment) => {
      if (gl.getShaderSource(fragment)?.includes('SCAN3D_CT_VOLUME_RAYMARCH')) {
        queueMicrotask(() => this.failVolume('CT volume rendering is unavailable on this GPU. CT slice review remains available.'));
        return;
      }
      const gpuCT = gl.getShaderSource(fragment)?.includes('float huAt(vec3 point)');
      if (gpuCT)
        queueMicrotask(() => {
          if (this.disposed) return;
          this.clearGPUCT();
          this.onGPUCTInvalidated?.('This GPU could not render the CT volume. Using local slice rendering.');
        });
      else this.onError('This GPU could not compile a 3D rendering shader.');
    };
    this.contextExtension = this.renderer.getContext().getExtension('WEBGL_lose_context');
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    this.renderer.setClearColor(0x101217, 0);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.setSize(Math.max(element.clientWidth, 1), Math.max(element.clientHeight, 1));
    this.transparency = new AnatomyTransparency(this.renderer);
    this.renderer.domElement.setAttribute('aria-label', 'Interactive anatomical surface model. Drag to rotate; use camera controls for named views.');
    this.renderer.domElement.setAttribute('role', 'img');
    this.renderer.domElement.addEventListener('webglcontextlost', this.contextLost);
    this.renderer.domElement.addEventListener('webglcontextrestored', this.contextRestored);
    element.appendChild(this.renderer.domElement);
    this.scissorsOverlay = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    this.scissorsOverlay.classList.add('scissors-overlay');
    this.scissorsOverlay.setAttribute('viewBox', '-1 -1 2 2');
    this.scissorsOverlay.setAttribute('preserveAspectRatio', 'none');
    this.scissorsOverlay.setAttribute('aria-label', '3D scissors outlines');
    this.scissorsOverlay.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:hidden;z-index:2';
    this.scissorsGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    this.scissorsGroup.setAttribute('transform', 'scale(1,-1)');
    this.scissorsOverlay.appendChild(this.scissorsGroup);
    element.appendChild(this.scissorsOverlay);
    document.addEventListener('keydown', this.scissorsKeyboard, true);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = false;
    this.controls.minZoom = 0.25;
    this.controls.maxZoom = 20;
    this.controls.addEventListener('change', this.controlChange);
    this.controls.addEventListener('start', this.orbitStart);
    // The reference viewer used legacy Three lighting. Modern Phong lights are
    // expressed as irradiance, so PI preserves its intended diffuse brightness.
    this.scene.add(new THREE.AmbientLight(0xffffff, 0.45 * Math.PI));
    this.keyLight = new THREE.DirectionalLight(0xffffff, 0.75 * Math.PI);
    this.scene.add(this.keyLight, this.keyLight.target);
    this.fillLight = new THREE.DirectionalLight(0xffffff, 0.25 * Math.PI);
    this.scene.add(this.fillLight, this.fillLight.target);
    this.crosshair = new THREE.Mesh(
      new THREE.SphereGeometry(0.0025, 12, 8),
      new THREE.MeshBasicMaterial({ color: '#ffffff', depthTest: false, transparent: true, opacity: 0.9 }),
    );
    this.crosshair.renderOrder = 100;
    this.crosshair.visible = false;
    this.scene.add(this.crosshair);
    this.reviewPlane = new Review3DPlane(this.renderer);
    this.scene.add(this.reviewPlane.mesh, this.reviewMarkers.group, this.gestureMarkers.group);
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(element);
    this.renderer.domElement.addEventListener('pointerdown', this.pointerDown, true);
    this.renderer.domElement.addEventListener('pointermove', this.pointerMove, true);
    this.renderer.domElement.addEventListener('pointerup', this.pointerUp, true);
    this.renderer.domElement.addEventListener('pointercancel', this.pointerCancel, true);
    this.view('oblique');
  }

  private contextLost = (event: Event) => {
    event.preventDefault();
    window.__SCAN3D_RENDER_READY__ = false;
    this.cancelReviewGesture();
    // Remove disposal listeners belonging to the lost GL generation before
    // restoration. The ray marcher borrows CT storage and releases only its
    // own target, LUT and programs here; the shared owner releases CT below.
    clearTimeout(this.volumeSettleTimer);
    this.volumeRenderer?.dispose();
    this.volumeRenderer = undefined;
    this.transparency.dispose();
    this.reviewPlane.releaseContextResources();
    // Retained meshes/markers may be replaced after restoration. Drop their
    // previous-generation GL listeners while the context is lost, retaining
    // CPU geometry/material data for lazy upload by the restored renderer.
    const geometries = new Set<THREE.BufferGeometry>(),
      materials = new Set<THREE.Material>(),
      textures = new Set<THREE.Texture>();
    this.scene.traverse(object => {
      const drawable = object as THREE.Mesh;
      if (drawable.geometry instanceof THREE.BufferGeometry) geometries.add(drawable.geometry);
      for (const material of Array.isArray(drawable.material) ? drawable.material : drawable.material ? [drawable.material] : []) materials.add(material);
    });
    for (const material of materials) {
      for (const value of Object.values(material)) if (value instanceof THREE.Texture) textures.add(value);
      if (material instanceof THREE.ShaderMaterial)
        for (const uniform of Object.values(material.uniforms)) if (uniform.value instanceof THREE.Texture) textures.add(uniform.value);
    }
    for (const texture of textures) texture.dispose();
    for (const material of materials) material.dispose();
    for (const geometry of geometries) geometry.dispose();
    this.clearGPUCT();
    this.onGPUCTInvalidated?.();
    this.onError('The graphics context was lost. Reload the model to restore 3D. CT review remains available.');
  };
  private contextRestored = () => {
    this.transparency = new AnatomyTransparency(this.renderer);
    this.onError('');
    this.onGPUCTRestored?.();
    this.requestRender();
  };
  /** OrbitControls captures camera.up in its constructor; rebuild through public APIs when it changes. */
  private refreshControlsUp() {
    if (this.controlsUp.distanceToSquared(this.camera.up) < 1e-16) return;
    this.cancelReviewGesture();
    const old = this.controls,
      target = old.target.clone(),
      position = this.camera.position.clone(),
      rotation = this.camera.quaternion.clone(),
      zoom = this.camera.zoom;
    old.removeEventListener('change', this.controlChange);
    old.removeEventListener('start', this.orbitStart);
    old.dispose();
    const controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.camera.position.copy(position);
    this.camera.quaternion.copy(rotation);
    this.camera.zoom = zoom;
    this.camera.updateProjectionMatrix();
    Object.assign(controls, {
      enabled: old.enabled,
      enableDamping: old.enableDamping,
      dampingFactor: old.dampingFactor,
      minDistance: old.minDistance,
      maxDistance: old.maxDistance,
      minZoom: old.minZoom,
      maxZoom: old.maxZoom,
      minTargetRadius: old.minTargetRadius,
      maxTargetRadius: old.maxTargetRadius,
      zoomToCursor: old.zoomToCursor,
      minPolarAngle: old.minPolarAngle,
      maxPolarAngle: old.maxPolarAngle,
      minAzimuthAngle: old.minAzimuthAngle,
      maxAzimuthAngle: old.maxAzimuthAngle,
      enableZoom: old.enableZoom,
      zoomSpeed: old.zoomSpeed,
      enableRotate: old.enableRotate,
      rotateSpeed: old.rotateSpeed,
      enablePan: old.enablePan,
      panSpeed: old.panSpeed,
      screenSpacePanning: old.screenSpacePanning,
      mouseButtons: { ...old.mouseButtons },
      touches: { ...old.touches },
    });
    controls.target.copy(target);
    controls.cursor.copy(old.cursor);
    controls.addEventListener('change', this.controlChange);
    controls.addEventListener('start', this.orbitStart);
    this.controls = controls;
    this.controlsUp.copy(this.camera.up);
    controls.update();
  }
  private nameView(name: string) {
    this.currentViewName = name;
    this.onView?.(name);
  }
  private orbitStart = () => {
    this.cancelReviewGesture();
    this.nameView('Free orbit');
  };
  private controlChange = () => {
    if (this.gesture?.scissors && !scissorsCaptureMatches(this.gesture.scissors, this.camera, this.renderer.domElement.getBoundingClientRect()))
      this.cancelReviewGesture();
    if (this.volumePresentation) this.volumeInteraction();
    this.requestRender();
  };
  private volumeInteraction() {
    if (!this.volumePresentation) return;
    this.volumeRenderer!.setInteracting(true);
    clearTimeout(this.volumeSettleTimer);
    this.volumeSettleTimer = window.setTimeout(() => {
      if (this.disposed) return;
      this.volumeRenderer?.setInteracting(false);
      this.requestRender();
    }, 150);
  }
  private ray(event: PointerEvent) {
    this.scene.updateMatrixWorld(true);
    this.camera.updateMatrixWorld();
    const bounds = this.renderer.domElement.getBoundingClientRect();
    const mouse = new THREE.Vector2(((event.clientX - bounds.left) / bounds.width) * 2 - 1, (-(event.clientY - bounds.top) / bounds.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(mouse, this.camera);
    return ray;
  }
  private get marking() {
    return !!this.review && this.review.tool !== 'navigate';
  }
  private get reviewReady() {
    const review = this.review;
    if (!review || review.disabled || !review.targetId || review.tool === 'navigate') return false;
    if (review.tool === 'scissors') return true;
    if (review.mode === 'plane')
      return (
        review.showPlane !== false &&
        this.reviewPlaneValid &&
        (review.gpuMeta
          ? this.reviewPlane.gpuActive && this.reviewPlane.gpuReady && review.gpuMeta === this.reviewPlane.meta
          : !!review.frame && review.frame === this.reviewPlane.frame)
      );
    return (
      review.tool === 'point' &&
      !!this.meshes.get(review.targetId)?.some(mesh => {
        let node: THREE.Object3D | null = mesh;
        while (node) {
          if (!node.visible) return false;
          node = node.parent;
        }
        const material = mesh.material as SurfaceMaterial;
        return material.visible && material.opacity > 0.02;
      })
    );
  }
  private pointerDown = (event: PointerEvent) => {
    this.start = [event.clientX, event.clientY];
    if (event.button !== 0 || !this.marking) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    this.cancelReviewGesture();
    if (!this.reviewReady || !this.review) return;
    const state = this.review;
    if (state.tool === 'scissors') {
      const capture = captureScissors(this.camera, this.renderer.domElement.getBoundingClientRect());
      this.gesture = { pointerId: event.pointerId, state, pixels: [scissorsScreenPoint(capture.viewport, event.clientX, event.clientY)], scissors: capture };
    } else if (state.mode === 'surface') {
      const ray = this.ray(event);
      const hit = intersectReviewSurface(ray, this.meshes.get(state.targetId) ?? []);
      const ras = hit && reviewSurfacePoint(hit, state.geometry);
      if (!ras) return;
      this.gesture = { pointerId: event.pointerId, state, pixels: [], surface: ras };
    } else {
      const meta = state.gpuMeta ?? state.frame?.meta;
      if (!meta) return;
      const hit = intersectReviewPlane(this.ray(event).ray, meta);
      if (!hit) return;
      this.gesture = { pointerId: event.pointerId, state, pixels: [hit.pixel] };
    }
    this.renderer.domElement.setPointerCapture(event.pointerId);
    this.gestureDirty = true;
    this.updateReviewData();
    this.requestRender();
  };
  private pointerMove = (event: PointerEvent) => {
    const gesture = this.gesture;
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (gesture.scissors) {
      if (!scissorsCaptureMatches(gesture.scissors, this.camera, this.renderer.domElement.getBoundingClientRect())) {
        this.cancelReviewGesture();
        return;
      }
      const point = scissorsScreenPoint(gesture.scissors.viewport, event.clientX, event.clientY);
      if ((gesture.state.scissorsShape ?? 'freehand') !== 'freehand') gesture.pixels = [gesture.pixels[0], point];
      else {
        const previous = gesture.pixels.at(-1)!;
        if (
          Math.hypot(((point[0] - previous[0]) * gesture.scissors.viewport.width) / 2, ((point[1] - previous[1]) * gesture.scissors.viewport.height) / 2) >=
            1 &&
          gesture.pixels.length < 2048
        )
          gesture.pixels.push(point);
      }
      this.gestureDirty = true;
      this.requestRender();
      return;
    }
    const meta = gesture.state.gpuMeta ?? gesture.state.frame?.meta;
    if (gesture.surface || !meta) return;
    const hit = intersectReviewPlane(this.ray(event).ray, meta);
    if (!hit) return;
    if (gesture.state.tool === 'bbox') gesture.pixels = [gesture.pixels[0], hit.pixel];
    else {
      const previous = gesture.pixels.at(-1)!;
      if (Math.hypot(previous[0] - hit.pixel[0], previous[1] - hit.pixel[1]) >= 0.5 && gesture.pixels.length < 2048) gesture.pixels.push(hit.pixel);
    }
    this.gestureDirty = true;
    this.requestRender();
  };
  private pointerUp = (event: PointerEvent) => {
    if (event.button === 0 && this.marking) {
      event.preventDefault();
      event.stopImmediatePropagation();
      const gesture = this.gesture;
      if (!gesture || gesture.pointerId !== event.pointerId) return;
      this.pointerMove(event);
      let prompt: InteractivePrompt | undefined;
      if (this.reviewReady && gesture.state.contextKey === this.review?.contextKey) {
        if (gesture.scissors) {
          if (scissorsCaptureMatches(gesture.scissors, this.camera, this.renderer.domElement.getBoundingClientRect()))
            prompt = createScissorsPrompt(
              gesture.scissors,
              gesture.state.scissorsShape ?? 'freehand',
              gesture.state.scissorsOperation ?? 'erase_inside',
              gesture.pixels,
            );
        } else if (gesture.surface) {
          if (Math.hypot(event.clientX - this.start[0], event.clientY - this.start[1]) <= 5)
            prompt = { id: crypto.randomUUID(), kind: 'point', ras: gesture.surface, include: gesture.state.include };
        } else prompt = reviewPlaneGesture(gesture.state, gesture.pixels);
      }
      this.cancelReviewGesture();
      if (prompt) this.onGesture?.(prompt);
      return;
    }
    if (event.button !== 0 || Math.hypot(event.clientX - this.start[0], event.clientY - this.start[1]) > 5) return;
    const ray = this.ray(event);
    const visible = [...this.meshes.values()].flat().filter(mesh => {
      if ((mesh.material as SurfaceMaterial).opacity <= 0.02) return false;
      for (let node: THREE.Object3D | null = mesh; node; node = node.parent) if (!node.visible) return false;
      return true;
    });
    const hit = ray.intersectObjects(visible, false)[0];
    if (hit) this.onPick(hit.object.userData.structureId, sceneToRas(hit.point.toArray()));
  };
  private pointerCancel = () => this.cancelReviewGesture();
  private scissorsKeyboard = (event: KeyboardEvent) => {
    if (event.key !== 'Escape' || !this.gesture?.scissors) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    this.cancelReviewGesture();
  };
  private cancelReviewGesture() {
    const pointer = this.gesture?.pointerId;
    this.gesture = undefined;
    this.gestureDirty = false;
    this.gestureMarkers.clear();
    if (pointer !== undefined && this.renderer.domElement.hasPointerCapture(pointer)) this.renderer.domElement.releasePointerCapture(pointer);
    this.updateReviewData();
    this.requestRender();
  }
  private updateScissorsOverlay() {
    if (!this.scissorsGroup) return;
    const cuts = this.review?.prompts.filter(prompt => prompt.kind === 'scissors') ?? [];
    if (!cuts.length && !this.gesture?.scissors) {
      if (this.scissorsGroup.childElementCount) this.scissorsGroup.replaceChildren();
      this.scissorsOverlayKey = '';
      return;
    }
    const projection = scissorsProjection(this.camera);
    const saved = cuts.filter(prompt => prompt.kind === 'scissors' && scissorsProjectionMatches(prompt.ras_to_clip, projection));
    const live = this.gesture?.scissors ? this.gesture : undefined;
    const polygon = live ? scissorsPolygon(live.state.scissorsShape ?? 'freehand', live.pixels, live.scissors!.viewport) : [];
    const key = JSON.stringify([saved, polygon, live?.state.scissorsOperation]);
    if (key === this.scissorsOverlayKey) return;
    this.scissorsOverlayKey = key;
    this.scissorsGroup.replaceChildren();
    const add = (vertices: [number, number][], operation: 'erase_inside' | 'erase_outside', identity?: string) => {
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', scissorsPath(vertices, operation));
      path.setAttribute('fill-rule', 'evenodd');
      path.setAttribute('fill', '#ff8c9b18');
      path.setAttribute('stroke', '#ff9eae');
      path.setAttribute('stroke-width', '1.5');
      path.setAttribute('vector-effect', 'non-scaling-stroke');
      path.dataset.scissorsOperation = operation;
      if (identity) {
        path.dataset.scissorsPrompt = identity;
        path.setAttribute('stroke-dasharray', '5 4');
      } else path.dataset.scissorsLive = 'true';
      this.scissorsGroup!.appendChild(path);
    };
    for (const prompt of saved) if (prompt.kind === 'scissors') add(prompt.vertices, prompt.operation, prompt.id);
    if (live) add(polygon, live.state.scissorsOperation ?? 'erase_inside');
  }
  private updateReviewData() {
    const data = this.renderer.domElement.dataset,
      review = this.review;
    const plane = this.reviewPlane,
      planeVisible = plane.mesh.visible;
    data.ctPlaneReady = String(planeVisible && !!plane.meta);
    data.ctPlaneMode = plane.gpuActive ? 'gpu-volume' : 'slice';
    data.ctGpuReady = String(plane.gpuReady);
    data.ctGpuBytes = String(plane.gpuBytes);
    const volume = this.volumeRenderer?.diagnostics;
    data.ctVolumeReady = String(!!volume?.ready && !this.volumeDisabled);
    data.ctVolumeVisible = String(this.volumePresentation);
    data.ctVolumePreset = this.ctVolume?.settings.preset ?? '';
    data.ctVolumeQuality = volume?.interacting ? 'interactive' : 'detail';
    data.ctVolumeCinematic = String(volume?.cinematic ?? false);
    data.ctVolumeShadowSamples = String(volume?.shadowSamples ?? 0);
    data.ctVolumeRenderCount = String(volume?.frames ?? 0);
    data.ctVolumeWidth = String(volume?.width ?? 0);
    data.ctVolumeHeight = String(volume?.height ?? 0);
    data.ctVolumePlaneVisible = String(this.volumePresentation && planeVisible && !!volume?.planeActive);
    data.ctVolumePlaneTargetBytes = String(volume?.planeTargetBytes ?? 0);
    data.renderStackActive = String(volume?.stackActive ?? false);
    data.renderStackSurfaces = String(volume?.surfaceCount ?? 0);
    data.renderStackPeels = String(volume?.peelCount ?? 0);
    data.renderStackTail = String(volume?.tailActive ?? false);
    data.renderStackTargetBytes = String(volume?.stackTargetBytes ?? 0);
    data.segmentationLayerVisible = String(this.segmentationLayer.visible);
    data.segmentationLayerOpacity = String(this.segmentationLayer.opacity);
    data.ctVolumeMaskMode = volume?.maskMode ?? 'off';
    data.ctVolumeMaskReady = String(volume?.maskReady ?? false);
    data.ctVolumeMaskCount = String(volume?.maskSelectedCount ?? 0);
    data.ctPlaneAxis = planeVisible ? String(plane.meta?.native?.axis ?? '') : '';
    data.ctPlaneIndex = planeVisible ? String(plane.meta?.native?.index ?? '') : '';
    data.ctPlaneWidth = planeVisible ? String(plane.meta?.width ?? '') : '';
    data.ctPlaneHeight = planeVisible ? String(plane.meta?.height ?? '') : '';
    data.ctPlaneContext = planeVisible ? this.planeContext : '';
    let projection = '';
    if (review || planeVisible) {
      this.camera.updateMatrixWorld();
      const rasToWorld = new THREE.Matrix4().set(-0.001, 0, 0, 0, 0, 0, 0.001, 0, 0, 0.001, 0, 0, 0, 0, 0, 1);
      // Column-major RAS-mm → clip matrix, for read-only physical picking diagnostics.
      projection = JSON.stringify(this.camera.projectionMatrix.clone().multiply(this.camera.matrixWorldInverse).multiply(rasToWorld).elements);
    }
    data.ctPlaneProjection = planeVisible ? projection : '';
    this.updateScissorsOverlay();
    if (!review) {
      for (const key of [
        'reviewMode',
        'reviewTarget',
        'reviewTool',
        'reviewContext',
        'reviewReady',
        'reviewPlaneAxis',
        'reviewPlaneIndex',
        'reviewMarkers',
        'reviewGestureActive',
        'reviewProjection',
      ])
        delete data[key];
      return;
    }
    data.reviewMode = review.mode;
    data.reviewTarget = review.targetId;
    data.reviewTool = review.tool;
    data.reviewContext = review.contextKey;
    data.reviewReady = String(this.reviewReady);
    data.reviewMarkers = String(review.prompts.length);
    data.reviewGestureActive = String(!!this.gesture);
    const native = this.reviewPlane.mesh.visible ? this.reviewPlane.meta?.native : undefined;
    data.reviewPlaneAxis = native ? String(native.axis) : '';
    data.reviewPlaneIndex = native ? String(native.index) : '';
    data.reviewProjection = projection;
  }

  setReview(state?: SceneReviewState) {
    const old = this.review;
    if (
      !state ||
      !old ||
      state.contextKey !== old.contextKey ||
      state.mode !== old.mode ||
      state.targetId !== old.targetId ||
      state.tool !== old.tool ||
      state.include !== old.include ||
      state.radiusMm !== old.radiusMm ||
      state.scissorsShape !== old.scissorsShape ||
      state.scissorsOperation !== old.scissorsOperation ||
      state.disabled !== old.disabled ||
      (state.tool !== 'scissors' && (state.frame !== old.frame || state.gpuMeta !== old.gpuMeta)) ||
      state.showPlane !== old.showPlane
    )
      this.cancelReviewGesture();
    const oldLayout = this.planeLayout;
    this.review = state;
    if (oldLayout !== this.planeLayout) this.resize();
    this.controls.mouseButtons.LEFT = state && state.tool !== 'navigate' ? null : THREE.MOUSE.ROTATE;
    this.controls.mouseButtons.RIGHT = state && state.tool !== 'navigate' ? THREE.MOUSE.ROTATE : THREE.MOUSE.PAN;
    if (!state) {
      this.reviewMarkers.clear();
      this.gestureMarkers.clear();
    } else {
      if (state.prompts !== old?.prompts || state.geometry !== old?.geometry) this.reviewMarkers.set(state.prompts, state.geometry);
    }
    this.syncCTPlane();
    this.updateReviewData();
    this.requestRender();
  }
  /** Shares review's existing opaque CT quad and its four bounded GPU textures. */
  setCTPlane(state?: SceneCTPlaneState) {
    const oldLayout = this.planeLayout,
      previous = this.ctPlane;
    this.ctPlane = state;
    if (
      this.volumePresentation &&
      state?.visible &&
      (!previous?.visible ||
        previous.contextKey !== state.contextKey ||
        previous.frame !== state.frame ||
        previous.window !== state.window ||
        previous.level !== state.level)
    )
      this.volumeInteraction();
    if (oldLayout !== this.planeLayout) this.resize();
    // Review owns the shared plane while active; its exact-frame edit gate is unchanged.
    if (!this.review) {
      this.syncCTPlane();
      this.updateReviewData();
      this.requestRender();
    }
  }
  get gpuCTReady() {
    return this.reviewPlane.gpuReady;
  }
  async prepareGPUCT(source: GPUVolumeSource, signal?: AbortSignal) {
    if (this.reviewPlane.gpuKey !== source.descriptor.key) this.volumeRenderer?.setVolume(undefined);
    await this.reviewPlane.prepareGPUCT(source, signal);
    this.updateReviewData();
  }
  async prepareGPUOverlays(sources: GPUOverlaySources, colors: readonly GPUOverlayColor[], signal?: AbortSignal, maskSources?: GPUOverlaySources) {
    // An atlas may be replaced during upload. Active masks remain closed until
    // the matching revision/selection is published by the caller.
    const mask = this.ctVolume?.mask;
    this.volumeRenderer?.setMask(undefined, mask?.ids ?? [], mask?.mode ?? 'off');
    let planeReady = true;
    try {
      await this.reviewPlane.prepareGPUOverlays(sources, colors, signal, maskSources);
    } catch (reason) {
      if (!(reason instanceof GPUCTMaskBudgetError) || !sources.overlays.length || !maskSources?.overlays.length) throw reason;
      // Exact colored plane labels can be much larger than the presentation
      // union. Keep the CT and volume mask, and use local full slices for color.
      await this.reviewPlane.prepareGPUOverlays({ overlays: [] }, [], signal, maskSources);
      planeReady = false;
    }
    this.updateReviewData();
    return planeReady;
  }
  clearGPUOverlays() {
    this.volumeRenderer?.setMask(undefined, [], this.ctVolume?.mask?.mode ?? 'off');
    this.reviewPlane.clearGPUOverlays();
    this.updateReviewData();
    this.requestRender();
  }
  setGPUOverlayColors(colors: readonly GPUOverlayColor[]) {
    if (this.reviewPlane.setGPUOverlayColors(colors)) this.requestRender();
  }
  clearGPUCT() {
    this.volumeRenderer?.setVolume(undefined);
    this.volumeRenderer?.releaseTargets();
    this.reviewPlane.clearGPUCT();
    this.updateReviewData();
    this.requestRender();
  }
  setCTVolume(state?: SceneCTVolumeState) {
    const before = this.volumePresentation,
      oldLayout = this.planeLayout,
      previous = this.ctVolume;
    this.ctVolume = state;
    if (state?.visible && !previous?.visible) this.volumeDisabled = false;
    if (state?.visible && this.reviewPlane.gpuBinding && !this.volumeDisabled) {
      this.volumeRenderer ??= new CTVolumeRenderer(this.renderer);
      this.volumeRenderer.setVolume(this.reviewPlane.gpuBinding);
      this.volumeRenderer.setSettings(state.settings);
      this.volumeRenderer.setMask(state.mask?.ready ? this.reviewPlane.gpuMaskBinding : undefined, state.mask?.ids ?? [], state.mask?.mode ?? 'off');
    } else {
      // A hidden layer keeps no screen-sized raymarch/peeling allocations.
      this.volumeRenderer?.setVolume(undefined);
      this.volumeRenderer?.releaseTargets();
    }
    const after = this.volumePresentation;
    if (after && !before) {
      this.surfaceHeight = this.height;
      this.surfaceView = {
        position: this.camera.position.clone(),
        quaternion: this.camera.quaternion.clone(),
        up: this.camera.up.clone(),
        target: this.controls.target.clone(),
        zoom: this.camera.zoom,
        name: this.currentViewName,
        hadSurfaces: this.meshes.size > 0,
      };
      if (!this.meshes.size) this.focusVolume();
    } else if (!after && previous?.visible && this.surfaceHeight !== undefined) {
      const oldHeight = this.surfaceHeight;
      this.surfaceHeight = undefined;
      const saved = this.surfaceView;
      this.surfaceView = undefined;
      if (!saved?.hadSurfaces) this.height = oldHeight;
      if (saved && !saved.hadSurfaces) {
        this.camera.position.copy(saved.position);
        this.camera.quaternion.copy(saved.quaternion);
        this.camera.up.copy(saved.up);
        this.camera.zoom = saved.zoom;
        this.controls.target.copy(saved.target);
        this.refreshControlsUp();
        this.camera.updateProjectionMatrix();
        this.controls.update();
        this.nameView(saved.name);
        if (!saved.hadSurfaces && this.meshes.size) this.fitModel();
      }
      clearTimeout(this.volumeSettleTimer);
      this.volumeRenderer?.setInteracting(false);
      this.resize();
    }
    if (after && previous?.settings !== state?.settings) this.volumeInteraction();
    if (oldLayout !== this.planeLayout) this.resize();
    this.updateReviewData();
    this.requestRender();
  }
  private get volumePresentation() {
    return !!this.ctVolume?.visible && !this.review && !this.volumeDisabled && !!this.volumeRenderer?.ready;
  }
  private failVolume(reason: string) {
    if (this.disposed || this.volumeDisabled) return;
    this.volumeDisabled = true;
    clearTimeout(this.volumeSettleTimer);
    this.volumeRenderer?.dispose();
    this.volumeRenderer = undefined;
    this.onCTVolumeError?.(reason);
    this.updateReviewData();
    this.requestRender();
  }
  private volumeBounds() {
    if (!this.ctVolume) return;
    const { geometry, settings } = this.ctVolume,
      box = new THREE.Box3();
    for (let n = 0; n < 8; n++) {
      const ijk = geometry.shape.map((size, axis) => settings.crop[n & (1 << axis) ? 1 : 0][axis] * size - 0.5) as Point;
      box.expandByPoint(new THREE.Vector3(...rasToScene(transformPoint(geometry.affine_ras, ijk))));
    }
    return box;
  }
  private focusVolume() {
    const box = this.volumeBounds();
    if (!box) return;
    const center = box.getCenter(new THREE.Vector3()),
      radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 0.005);
    const direction = this.camera.position.clone().sub(this.controls.target).normalize();
    this.height = radius * 2.05;
    this.camera.zoom = 1;
    this.controls.target.copy(center);
    this.camera.position.copy(center).addScaledVector(direction, Math.max(radius * 4, 1));
    this.camera.lookAt(center);
    this.controls.update();
    this.resize();
  }
  private get planeLayout() {
    return this.viewportControlsReserved || !!this.review || !!this.ctPlane?.visible || !!this.ctVolume?.visible;
  }
  setViewportControlsReserved(reserved: boolean) {
    if (this.viewportControlsReserved === reserved) return;
    this.viewportControlsReserved = reserved;
    this.resize();
  }
  setSegmentationLayer(layer: { visible: boolean; opacity: number }) {
    const opacity = THREE.MathUtils.clamp(Number.isFinite(layer.opacity) ? layer.opacity : 1, 0, 1);
    if (this.segmentationLayer.visible === layer.visible && this.segmentationLayer.opacity === opacity) return;
    this.segmentationLayer = { visible: layer.visible, opacity };
    this.display(this.states, this.selected);
    this.volumeInteraction();
  }
  private syncCTPlane() {
    const review = this.review,
      state = review ?? this.ctPlane;
    this.reviewPlaneValid = false;
    if (!state) {
      this.reviewPlane.clear();
      this.planeGeometry = undefined;
      this.planeContext = '';
      return;
    }
    // Retain a previous slice during scrubbing, but never display a foreign CT grid.
    if (
      this.planeGeometry &&
      this.planeGeometry !== state.geometry &&
      (JSON.stringify(this.planeGeometry.shape) !== JSON.stringify(state.geometry.shape) ||
        JSON.stringify(this.planeGeometry.affine_ras) !== JSON.stringify(state.geometry.affine_ras))
    ) {
      this.reviewPlane.clear();
      this.planeContext = '';
    }
    if (state.gpuMeta && this.reviewPlane.gpuReady) {
      try {
        this.reviewPlane.setGPUFrame(state.gpuMeta, state.geometry);
        this.planeGeometry = state.geometry;
        this.planeContext = state.contextKey ?? '';
        this.reviewPlaneValid = !!review;
      } catch (reason) {
        this.onError(reason instanceof Error ? reason.message : 'The GPU CT plane could not be displayed.');
      }
    } else if (state.frame) {
      try {
        this.reviewPlane.setFrame(state.frame, state.geometry);
        this.planeGeometry = state.geometry;
        this.planeContext = state.contextKey ?? '';
        this.reviewPlaneValid = !!review;
      } catch (reason) {
        this.onError(reason instanceof Error ? reason.message : 'The 3D CT plane could not be displayed.');
      }
    }
    this.reviewPlane.setWindow(state.window, state.level);
    const visible = review ? (review.showPlane ?? review.mode === 'plane') : this.ctPlane!.visible;
    // A missing new exact overlay set must never leave an old GPU presentation editable.
    if (!state.gpuMeta && this.reviewPlane.gpuActive) this.reviewPlane.clear();
    this.reviewPlane.mesh.visible = visible && !!this.reviewPlane.meta;
  }
  focusReviewPlane() {
    const meta = this.reviewPlane.meta;
    if (!meta || !this.reviewPlane.mesh.visible) return;
    const origin = new THREE.Vector3(...rasToScene([meta.pixel_to_ras[0][3], meta.pixel_to_ras[1][3], meta.pixel_to_ras[2][3]]));
    const x = new THREE.Vector3(...rasToScene(meta.pixel_to_ras.slice(0, 3).map(row => row[0]) as Point));
    const y = new THREE.Vector3(...rasToScene(meta.pixel_to_ras.slice(0, 3).map(row => row[1]) as Point));
    const center = origin.addScaledVector(x, (meta.width - 1) / 2).addScaledVector(y, (meta.height - 1) / 2);
    const normal = x.clone().cross(y).normalize();
    if (normal.dot(this.camera.position.clone().sub(center)) < 0) normal.negate();
    this.cancelReviewGesture();
    this.controls.target.copy(center);
    this.camera.position.copy(center).addScaledVector(normal, Math.max(this.radius * 4, 1));
    this.camera.up.copy(y).normalize().negate();
    this.refreshControlsUp();
    this.camera.lookAt(center);
    this.camera.zoom = THREE.MathUtils.clamp(this.height / Math.max(meta.width * x.length(), meta.height * y.length()) / 1.3, 0.25, 20);
    this.camera.updateProjectionMatrix();
    this.controls.update();
    this.nameView('CT plane');
    this.requestRender();
  }

  async load(buffer: ArrayBuffer, structures: Structure[], preserveView = false) {
    const generation = ++this.generation;
    window.__SCAN3D_RENDER_READY__ = false;
    const gltf = await new GLTFLoader().parseAsync(buffer, '');
    if (generation !== this.generation || this.disposed) {
      this.disposeObject(gltf.scene);
      return;
    }
    const hadModel = this.meshes.size > 0;
    const ids = new Set(structures.map(s => s.id));
    const sanitized = new Map(structures.map(s => [THREE.PropertyBinding.sanitizeNodeName(s.id), s.id]));
    const meshes = new Map<string, THREE.Mesh[]>();
    gltf.scene.traverse(node => {
      if (!(node instanceof THREE.Mesh)) return;
      let current: THREE.Object3D | null = node;
      let id: string | undefined;
      while (current && !id) {
        const assoc = gltf.parser.associations.get(current);
        const original = assoc?.nodes !== undefined ? gltf.parser.json.nodes[assoc.nodes]?.name : undefined;
        const candidates = [current.userData.structure_id, current.userData.name, original, current.name];
        id = candidates.find(candidate => ids.has(candidate));
        if (!id) id = sanitized.get(current.name);
        current = current.parent;
      }
      if (!id) {
        node.visible = false;
        return;
      }
      const structure = structures.find(s => s.id === id)!;
      const old = Array.isArray(node.material) ? node.material : [node.material];
      old.forEach(m => m.dispose());
      node.material = new THREE.MeshPhongMaterial({ color: colorCss(structure.color), shininess: 35, specular: 0x333333, side: THREE.FrontSide });
      node.userData.structureId = id;
      if (!node.geometry.getAttribute('normal')) node.geometry.computeVertexNormals();
      meshes.set(id, [...(meshes.get(id) || []), node]);
    });
    if (!meshes.size) {
      this.disposeObject(gltf.scene);
      throw new Error('The model contains no named anatomical surfaces.');
    }
    this.clearModel();
    this.model = gltf.scene;
    this.meshes = meshes;
    this.scene.add(gltf.scene);
    if (!preserveView || !hadModel) this.fitModel();
    this.display(this.states, this.selected);
    this.requestRender();
  }

  /** Reuse unchanged scene objects and their GPU buffers; stage changed surfaces atomically. */
  async loadSurfaces(manifest: SurfaceManifest, structures: Structure[], options: SurfaceLoadOptions = {}) {
    const generation = ++this.generation;
    const controller = new AbortController();
    const abort = () => controller.abort();
    options.signal?.addEventListener('abort', abort, { once: true });
    if (options.signal?.aborted) controller.abort();
    const stale = () => this.disposed || generation !== this.generation || controller.signal.aborted;
    const records = new Map(structures.map(structure => [structure.id, structure]));
    const ids = new Set<string>();
    const staged = new Map<string, CachedSurface>();
    const changed = [] as SurfaceManifest['structures'];
    let completed = 0,
      reused = 0,
      committed = false;
    const progress = () => {
      if (!stale()) options.onProgress?.({ completed, total: manifest.structures.length, reused });
    };
    try {
      if (manifest.schema_version !== 1 || !Array.isArray(manifest.structures)) throw new Error('The surface manifest is unsupported.');
      for (const item of manifest.structures) {
        if (!item || !item.id || !item.key || !item.url || ids.has(item.id) || !records.has(item.id))
          throw new Error('The surface manifest contains invalid anatomical entries.');
        ids.add(item.id);
        if (this.surfaces.get(item.id)?.key === item.key) {
          completed++;
          reused++;
        } else changed.push(item);
      }
      progress();
      let cursor = 0;
      const worker = async () => {
        while (cursor < changed.length && !stale()) {
          const item = changed[cursor++];
          const response = await apiFetch(item.url, { signal: controller.signal });
          if (!response.ok) throw new Error(`Could not load ${records.get(item.id)!.name} (${response.status}).`);
          const buffer = await response.arrayBuffer();
          if (stale()) break;
          const gltf = await new GLTFLoader().parseAsync(buffer, '');
          if (stale()) {
            this.disposeObject(gltf.scene);
            break;
          }
          const meshes: THREE.Mesh[] = [];
          // Each immutable GLB is one structure; naming is still validated to catch stale artifacts.
          gltf.scene.traverse(node => {
            if (!(node instanceof THREE.Mesh)) return;
            let current: THREE.Object3D | null = node;
            let matched = false;
            while (current && !matched) {
              const assoc = gltf.parser.associations.get(current);
              const original = assoc?.nodes !== undefined ? gltf.parser.json.nodes[assoc.nodes]?.name : undefined;
              matched =
                [current.userData.structure_id, current.userData.name, original, current.name].includes(item.id) ||
                current.name === THREE.PropertyBinding.sanitizeNodeName(item.id);
              current = current.parent;
            }
            if (!matched) {
              node.visible = false;
              return;
            }
            (Array.isArray(node.material) ? node.material : [node.material]).forEach(material => material.dispose());
            node.material = new THREE.MeshPhongMaterial({
              color: colorCss(records.get(item.id)!.color),
              shininess: 35,
              specular: 0x333333,
              side: THREE.FrontSide,
            });
            node.userData.structureId = item.id;
            if (!node.geometry.getAttribute('normal')) node.geometry.computeVertexNormals();
            meshes.push(node);
          });
          if (!meshes.length) {
            this.disposeObject(gltf.scene);
            throw new Error(`The model contains no surface for ${item.id}.`);
          }
          staged.set(item.id, { key: item.key, object: gltf.scene, meshes });
          completed++;
          progress();
        }
      };
      // Bound network, decompression and GPU upload pressure, rather than launching 100 requests.
      let failure: unknown;
      const results = await Promise.allSettled(
        Array.from({ length: Math.min(3, changed.length) }, () =>
          worker().catch(reason => {
            if (!controller.signal.aborted) failure = reason;
            controller.abort();
            throw reason;
          }),
        ),
      );
      if (failure) throw failure;
      const failed = results.find(result => result.status === 'rejected');
      if (failed?.status === 'rejected') throw failed.reason;
      if (stale()) throw new DOMException('Surface load cancelled', 'AbortError');
      const hadModel = this.meshes.size > 0;
      if (this.model && !this.surfaces.size) this.clearModel();
      if (!this.model) {
        this.model = new THREE.Group();
        this.scene.add(this.model);
      }
      for (const [id, old] of this.surfaces) {
        if (!ids.has(id) || staged.has(id)) {
          old.object.removeFromParent();
          this.disposeObject(old.object);
          this.surfaces.delete(id);
          this.meshes.delete(id);
        }
      }
      for (const [id, surface] of staged) {
        this.model.add(surface.object);
        this.surfaces.set(id, surface);
        this.meshes.set(id, surface.meshes);
      }
      committed = true;
      if (!hadModel && !options.preserveView && this.meshes.size) this.fitModel();
      this.display(this.states, this.selected);
      this.requestRender();
    } finally {
      controller.abort();
      options.signal?.removeEventListener('abort', abort);
      if (!committed) for (const surface of staged.values()) this.disposeObject(surface.object);
    }
  }

  private fitModel() {
    const bounds = new THREE.Box3().setFromObject(this.model!);
    bounds.getCenter(this.center);
    this.radius = Math.max(bounds.getSize(new THREE.Vector3()).length() / 2, 0.02);
    if (this.volumePresentation) {
      this.surfaceHeight = this.radius * 2.05;
      return;
    }
    this.height = this.radius * 2.05;
    this.view('oblique');
  }

  /** Presentation updates reuse mesh objects, geometry and current camera. */
  setStructureColors(structures: readonly Structure[]) {
    let changed = false;
    for (const structure of structures) {
      const color = new THREE.Color(colorCss(structure.color));
      for (const mesh of this.meshes.get(structure.id) ?? []) {
        const material = mesh.material as SurfaceMaterial;
        if (!material.color.equals(color)) {
          material.color.copy(color);
          changed = true;
        }
      }
    }
    if (changed) this.requestRender();
  }

  display(states: StructureDisplay, selected: string) {
    this.states = states;
    this.selected = selected;
    for (const [id, meshes] of this.meshes)
      for (const mesh of meshes) {
        const state = states[id] || { visible: true, opacity: 0.5 };
        const opacity = state.opacity * this.segmentationLayer.opacity;
        mesh.visible = this.segmentationLayer.visible && state.visible && opacity > 0;
        const material = mesh.material as SurfaceMaterial;
        surfaceOpacity(material, opacity);
        material.emissive.set(id === selected ? 0x182132 : 0x000000);
      }
    if (this.gesture && !this.reviewReady) this.cancelReviewGesture();
    this.updateReviewData();
    this.requestRender();
  }

  setPoint(point?: Point) {
    this.crosshair.visible = !!point;
    if (point) this.crosshair.position.fromArray(rasToScene(point));
    this.requestRender();
  }
  view(name: CameraView) {
    this.cancelReviewGesture();
    const direction = new THREE.Vector3(...directions[name]).normalize();
    this.camera.up.set(0, name === 'superior' ? 0 : 1, name === 'superior' ? 1 : 0);
    this.refreshControlsUp();
    const volume = this.volumePresentation ? this.volumeBounds() : undefined;
    const center = volume?.getCenter(new THREE.Vector3()) ?? this.center;
    const radius = volume ? Math.max(volume.getSize(new THREE.Vector3()).length() / 2, 0.005) : this.radius;
    if (volume) this.height = radius * 2.05;
    this.camera.position.copy(center).addScaledVector(direction, Math.max(radius * 4, 1));
    this.camera.zoom = 1;
    this.controls.target.copy(center);
    this.camera.lookAt(center);
    this.controls.update();
    this.resize();
    this.nameView(name);
  }
  focus(id = this.selected) {
    this.cancelReviewGesture();
    if (this.volumePresentation) {
      this.focusVolume();
      return;
    }
    const meshes = this.meshes.get(id);
    if (!meshes?.length) return;
    const box = new THREE.Box3();
    meshes.forEach(m => box.expandByObject(m));
    const center = box.getCenter(new THREE.Vector3());
    const delta = this.camera.position.clone().sub(this.controls.target);
    this.controls.target.copy(center);
    this.camera.position.copy(center).add(delta);
    this.camera.zoom = Math.max(1, Math.min(12, this.height / (box.getSize(new THREE.Vector3()).length() * 1.3)));
    this.camera.updateProjectionMatrix();
    this.controls.update();
    this.requestRender();
  }
  zoom(factor: number) {
    this.cancelReviewGesture();
    this.camera.zoom = THREE.MathUtils.clamp(this.camera.zoom * factor, 0.25, 20);
    this.camera.updateProjectionMatrix();
    this.requestRender();
  }
  captureCamera(): PortableCamera {
    return {
      position_ras: sceneToRas(this.camera.position.toArray()),
      target_ras: sceneToRas(this.controls.target.toArray()),
      up_ras: [-this.camera.up.x, this.camera.up.z, this.camera.up.y],
      zoom: this.camera.zoom,
      height_mm: this.height * 1000,
      name: this.currentViewName,
    };
  }
  restoreCamera(state: PortableCamera) {
    const position = new THREE.Vector3(...rasToScene(state.position_ras)),
      target = new THREE.Vector3(...rasToScene(state.target_ras));
    const up = new THREE.Vector3(-state.up_ras[0], state.up_ras[2], state.up_ras[1]).normalize();
    if (
      position.distanceToSquared(target) < 1e-12 ||
      up.lengthSq() < 0.5 ||
      new THREE.Vector3().subVectors(position, target).normalize().cross(up).lengthSq() < 1e-12
    )
      return;
    this.height = state.height_mm / 1000;
    this.camera.zoom = state.zoom;
    this.camera.position.copy(position);
    this.controls.target.copy(target);
    this.camera.up.copy(up);
    this.refreshControlsUp();
    this.camera.lookAt(target);
    this.controls.update();
    this.resize();
    this.nameView(state.name?.replaceAll('-', ' ') ?? 'Saved view');
  }
  restoreContext() {
    if (this.renderer.getContext().isContextLost()) this.contextExtension?.restoreContext();
  }
  get transparencyWarning() {
    return this.transparency.warning;
  }
  /** One pipeline for interactive frames, pixel checks and PNG capture. */
  render() {
    const started = performance.now();
    if (this.gestureDirty && this.gesture) {
      const gesture = this.gesture;
      this.gestureDirty = false;
      const prompt: InteractivePrompt | undefined = gesture.scissors
        ? undefined
        : gesture.surface
          ? { id: 'active-gesture', kind: 'point', ras: gesture.surface, include: gesture.state.include }
          : reviewPlaneGesture(gesture.state, gesture.pixels, 'active-gesture');
      this.gestureMarkers.set(prompt ? [prompt] : [], gesture.state.geometry);
    }
    this.keyLight.position.copy(this.camera.position);
    this.keyLight.target.position.copy(this.controls.target);
    this.fillLight.position.copy(this.controls.target).multiplyScalar(2).sub(this.camera.position);
    this.fillLight.target.position.copy(this.controls.target);
    if (this.volumePresentation) {
      try {
        this.volumeRenderer!.render(
          this.camera,
          this.reviewPlane.mesh.visible ? this.reviewPlane.mesh : undefined,
          this.segmentationLayer.visible && this.segmentationLayer.opacity > 0 ? this.scene : undefined,
        );
      } catch {
        this.failVolume('CT volume rendering failed on this GPU. CT slice review remains available.');
        this.transparency.render(this.scene, this.camera);
      }
    } else this.transparency.render(this.scene, this.camera);
    // Actual completed render submissions, rather than slider inputs or worker replies.
    // CPU submission time is diagnostic only; it is not a GPU timer or frame-rate promise.
    const data = this.renderer.domElement.dataset,
      native = this.reviewPlane.mesh.visible ? this.reviewPlane.meta?.native : undefined;
    this.updateScissorsOverlay();
    data.ctPlaneRenderedAxis = native ? String(native.axis) : '';
    data.ctPlaneRenderedIndex = native ? String(native.index) : '';
    data.ctPlaneRenderCount = String(++this.planeRenderCount);
    data.ctPlaneRenderCpuMs = (performance.now() - started).toFixed(2);
  }
  /** Render and read back in one tick: a frame's pixels are only readable before it yields. */
  capturePNG() {
    if (this.volumePresentation) this.volumeRenderer!.setInteracting(false);
    this.render();
    return this.renderer.domElement.toDataURL('image/png');
  }
  screenshot(label = '3dscan') {
    const url = this.capturePNG(),
      link = document.createElement('a');
    link.download = `${label.replace(/[^a-z\d-]/gi, '_')}.png`;
    link.href = url;
    link.click();
  }
  private resize() {
    if (this.disposed) return;
    if (this.gesture?.scissors) this.cancelReviewGesture();
    const width = Math.max(this.element.clientWidth, 1),
      height = Math.max(this.element.clientHeight, 1);
    this.renderer.setSize(width, height);
    // Reserve screen space for camera controls so anatomy never sits behind the toolbars.
    const ratio = width / height,
      halfHeight = (this.height / 2 / Math.min(ratio, 1)) * (this.planeLayout ? 1 : height / Math.max(height - 120, height * 0.5));
    const shift = this.planeLayout ? 0 : halfHeight * Math.min(80 / height, 0.24);
    this.camera.left = -halfHeight * ratio;
    this.camera.right = halfHeight * ratio;
    this.camera.top = halfHeight - shift;
    this.camera.bottom = -halfHeight - shift;
    this.camera.updateProjectionMatrix();
    this.requestRender();
  }
  private requestRender = () => {
    if (this.frame || this.disposed) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      if (this.disposed) return;
      if (this.renderer.getContext().isContextLost()) return;
      this.render();
      this.updateReviewData();
      const inverse = this.camera.quaternion.clone().invert();
      this.onOrientation?.(
        [
          ['R', -1, 0, 0],
          ['A', 0, 0, 1],
          ['S', 0, 1, 0],
        ].map(([label, x, y, z]) => {
          const axis = new THREE.Vector3(Number(x), Number(y), Number(z)).applyQuaternion(inverse);
          return { label: String(label), x: axis.x * 22, y: -axis.y * 22, front: axis.z >= 0 };
        }),
      );
      window.__SCAN3D_RENDER_READY__ = !!(
        this.meshes.size ||
        this.volumePresentation ||
        (this.reviewPlane.mesh.visible && (this.reviewPlane.frame || this.reviewPlane.gpuReady))
      );
    });
  };
  private disposeObject(object: THREE.Object3D) {
    object.traverse(node => {
      if (!(node instanceof THREE.Mesh)) return;
      node.geometry.dispose();
      (Array.isArray(node.material) ? node.material : [node.material]).forEach(material => {
        for (const value of Object.values(material)) if (value instanceof THREE.Texture) value.dispose();
        material.dispose();
      });
    });
  }
  clearSurfaces() {
    this.generation++;
    this.clearModel();
    window.__SCAN3D_RENDER_READY__ = false;
    this.requestRender();
  }
  private clearModel() {
    if (this.model) {
      this.scene.remove(this.model);
      this.disposeObject(this.model);
      this.model = undefined;
    }
    this.meshes.clear();
    this.surfaces.clear();
  }
  dispose() {
    this.disposed = true;
    this.generation++;
    cancelAnimationFrame(this.frame);
    this.observer.disconnect();
    clearTimeout(this.volumeSettleTimer);
    this.volumeRenderer?.dispose();
    this.controls.removeEventListener('change', this.controlChange);
    this.controls.removeEventListener('start', this.orbitStart);
    this.controls.dispose();
    this.renderer.domElement.removeEventListener('pointerdown', this.pointerDown, true);
    this.renderer.domElement.removeEventListener('pointerup', this.pointerUp, true);
    this.renderer.domElement.removeEventListener('pointermove', this.pointerMove, true);
    this.renderer.domElement.removeEventListener('pointercancel', this.pointerCancel, true);
    this.renderer.domElement.removeEventListener('webglcontextlost', this.contextLost);
    this.renderer.domElement.removeEventListener('webglcontextrestored', this.contextRestored);
    document.removeEventListener('keydown', this.scissorsKeyboard, true);
    this.scissorsOverlay?.remove();
    this.clearModel();
    this.crosshair.geometry.dispose();
    (this.crosshair.material as THREE.Material).dispose();
    this.cancelReviewGesture();
    this.reviewPlane.dispose();
    this.reviewMarkers.dispose();
    this.gestureMarkers.dispose();
    this.transparency.dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    this.renderer.domElement.remove();
    window.__SCAN3D_RENDER_READY__ = false;
  }
}

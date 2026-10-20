import * as THREE from 'three';
import type { SurfaceMaterial } from './material';

type Surface = THREE.Mesh<THREE.BufferGeometry, SurfaceMaterial>;
interface Variants { accumulation: SurfaceMaterial; revealage: THREE.MeshBasicMaterial; release: () => void }

/**
 * Weighted blended order-independent transparency (McGuire / Bavoil, JCGT 2013).
 * Two transparent draws avoid requiring an optional indexed-blending extension.
 * Both color accumulation and revealage commute: intersecting/nested anatomy is
 * smooth and never reorders whole organs when the camera changes.
 */
export class AnatomyTransparency {
  readonly supported: boolean;
  readonly warning: string;
  private opaque?: THREE.WebGLRenderTarget;
  private accumulation?: THREE.WebGLRenderTarget;
  private revealage?: THREE.WebGLRenderTarget;
  private variants = new Map<SurfaceMaterial, Variants>();
  private quad: THREE.Mesh<THREE.PlaneGeometry, THREE.ShaderMaterial>;
  private composite = new THREE.Scene();
  private screenCamera = new THREE.Camera();
  private size = new THREE.Vector2();
  private depthRange = new THREE.Vector2(0, 1);
  private depthUniform = { value: null as THREE.Texture | null };
  private inverseSize = { value: new THREE.Vector2(1, 1) };
  private bounds = new THREE.Box3();
  private point = new THREE.Vector3();

  constructor(private renderer: THREE.WebGLRenderer) {
    const gl = renderer.getContext();
    // Half float renderability includes blending in WebGL2; EXT_float_blend is
    // needed for float32, which this implementation deliberately does not use.
    this.supported = !!gl.getExtension('EXT_color_buffer_float');
    this.warning = this.supported ? '' : 'Smooth transparency is unavailable on this device. Structures are shown solid; use visibility and solo controls to inspect inside.';
    this.quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), new THREE.ShaderMaterial({
      uniforms: { opaqueImage: { value: null }, accumulatedImage: { value: null }, revealageImage: { value: null } },
      vertexShader: `varying vec2 vUv; void main() { vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }`,
      fragmentShader: `
        uniform sampler2D opaqueImage;
        uniform sampler2D accumulatedImage;
        uniform sampler2D revealageImage;
        varying vec2 vUv;
        void main() {
          vec4 opaque = texture2D(opaqueImage, vUv);
          vec4 accumulated = texture2D(accumulatedImage, vUv);
          float revealage = clamp(texture2D(revealageImage, vUv).r, 0.0, 1.0);
          float coverage = 1.0 - revealage;
          vec3 transparentColor = accumulated.rgb / max(accumulated.a, 0.00001);
          float alpha = coverage + opaque.a * revealage;
          vec3 premultiplied = transparentColor * coverage + opaque.rgb * revealage;
          // Color conversion must happen before canvas premultiplication.
          gl_FragColor = vec4(alpha > 0.00001 ? premultiplied / alpha : vec3(0.0), alpha);
          #include <tonemapping_fragment>
          #include <colorspace_fragment>
          gl_FragColor.rgb *= gl_FragColor.a;
        }`,
      depthTest: false, depthWrite: false, blending: THREE.NoBlending,
    }));
    this.quad.frustumCulled = false; this.composite.add(this.quad);
  }

  private targets() {
    this.renderer.getDrawingBufferSize(this.size);
    const width = Math.max(1, this.size.x), height = Math.max(1, this.size.y);
    if (!this.opaque) {
      const gl = this.renderer.getContext() as WebGL2RenderingContext;
      const supportedSamples = gl.getInternalformatParameter(gl.RENDERBUFFER, gl.RGBA16F, gl.SAMPLES) as Int32Array;
      const samples = [...supportedSamples].filter(count => count <= 4).sort((a, b) => b - a)[0] || 0;
      this.opaque = new THREE.WebGLRenderTarget(width, height, {
        type: THREE.HalfFloatType, minFilter: THREE.NearestFilter, magFilter: THREE.NearestFilter,
        depthTexture: new THREE.DepthTexture(width, height, THREE.UnsignedIntType), samples,
      });
      this.accumulation = new THREE.WebGLRenderTarget(width, height, {
        type: THREE.HalfFloatType, minFilter: THREE.NearestFilter, magFilter: THREE.NearestFilter, depthBuffer: false,
      });
      // A single normalized channel is sufficient for multiplicative coverage.
      this.revealage = new THREE.WebGLRenderTarget(width, height, {
        format: THREE.RedFormat, type: THREE.UnsignedByteType,
        minFilter: THREE.NearestFilter, magFilter: THREE.NearestFilter, depthBuffer: false,
      });
      this.depthUniform.value = this.opaque.depthTexture;
      const uniforms = this.quad.material.uniforms;
      uniforms.opaqueImage.value = this.opaque.texture;
      uniforms.accumulatedImage.value = this.accumulation.texture;
      uniforms.revealageImage.value = this.revealage.texture;
    }
    for (const target of [this.opaque, this.accumulation!, this.revealage!]) {
      if (target.width !== width || target.height !== height) target.setSize(width, height);
    }
    this.inverseSize.value.set(1 / width, 1 / height);
  }

  private variant(base: SurfaceMaterial): Variants {
    let variants = this.variants.get(base);
    if (!variants) {
      const accumulation = base.clone() as SurfaceMaterial;
      accumulation.transparent = true; accumulation.alphaHash = false;
      accumulation.depthTest = false; accumulation.depthWrite = false;
      accumulation.blending = THREE.CustomBlending;
      accumulation.blendEquation = THREE.AddEquation;
      accumulation.blendSrc = THREE.OneFactor; accumulation.blendDst = THREE.OneFactor;
      accumulation.onBeforeCompile = shader => {
        shader.uniforms.opaqueDepth = this.depthUniform;
        shader.uniforms.inverseSize = this.inverseSize;
        shader.uniforms.anatomyDepthRange = { value: this.depthRange };
        shader.fragmentShader = `uniform sampler2D opaqueDepth; uniform vec2 inverseSize; uniform vec2 anatomyDepthRange;\n${shader.fragmentShader}`
          .replace('#include <opaque_fragment>', `
            if (gl_FragCoord.z > texture2D(opaqueDepth, gl_FragCoord.xy * inverseSize).r + 0.000001) discard;
            float alpha = clamp(diffuseColor.a, 0.0, 1.0);
            float depth = clamp((gl_FragCoord.z - anatomyDepthRange.x) / max(anatomyDepthRange.y - anatomyDepthRange.x, 0.00001), 0.0, 1.0);
            float weight = max(0.01, pow(alpha + 0.01, 3.0) * 32.0) * exp(-4.0 * depth);
            gl_FragColor = vec4(outgoingLight * alpha, alpha) * weight;
          `)
          .replace('#include <tonemapping_fragment>', '')
          .replace('#include <colorspace_fragment>', '')
          .replace('#include <premultiplied_alpha_fragment>', '');
      };
      accumulation.customProgramCacheKey = () => 'anatomy-weighted-oit-v1';
      const revealage = new THREE.MeshBasicMaterial({
        side: THREE.FrontSide, transparent: true, depthTest: false, depthWrite: false,
        blending: THREE.CustomBlending, blendEquation: THREE.AddEquation,
        blendSrc: THREE.ZeroFactor, blendDst: THREE.OneMinusSrcAlphaFactor,
        toneMapped: false,
      });
      revealage.onBeforeCompile = shader => {
        shader.uniforms.opaqueDepth = this.depthUniform;
        shader.uniforms.inverseSize = this.inverseSize;
        shader.fragmentShader = `uniform sampler2D opaqueDepth; uniform vec2 inverseSize;\n${shader.fragmentShader}`
          .replace('#include <opaque_fragment>', `
            if (gl_FragCoord.z > texture2D(opaqueDepth, gl_FragCoord.xy * inverseSize).r + 0.000001) discard;
            gl_FragColor = vec4(0.0, 0.0, 0.0, diffuseColor.a);
          `)
          .replace('#include <colorspace_fragment>', '')
          .replace('#include <premultiplied_alpha_fragment>', '');
      };
      revealage.customProgramCacheKey = () => 'anatomy-revealage-v1';
      const release = () => {
        accumulation.dispose(); revealage.dispose(); base.removeEventListener('dispose', release); this.variants.delete(base);
      };
      variants = { accumulation, revealage, release }; this.variants.set(base, variants); base.addEventListener('dispose', release);
    }
    // Selection/color/opacity can change without replacing an immutable surface.
    variants.accumulation.color.copy(base.color); variants.accumulation.emissive.copy(base.emissive);
    variants.accumulation.opacity = base.opacity; variants.revealage.opacity = base.opacity;
    if (base instanceof THREE.MeshPhongMaterial && variants.accumulation instanceof THREE.MeshPhongMaterial) {
      variants.accumulation.specular.copy(base.specular); variants.accumulation.shininess = base.shininess;
    }
    return variants;
  }

  render(scene: THREE.Scene, camera: THREE.Camera) {
    const surfaces: Surface[] = [], overlays: THREE.Mesh[] = [], opaqueHelpers: THREE.Mesh[] = [];
    scene.traverse(node => {
      if (!(node instanceof THREE.Mesh) || !node.visible) return;
      if (node.userData.structureId && !Array.isArray(node.material)) surfaces.push(node as Surface);
      else if (!(node.material as THREE.Material).depthTest) overlays.push(node);
      else opaqueHelpers.push(node);
    });
    const translucent = surfaces.filter(mesh => mesh.material.opacity > 0 && mesh.material.opacity < 1);
    // A solid-only frame retains the ordinary single-pass/MSAA renderer path.
    if (!translucent.length || !this.supported) { this.renderer.render(scene, camera); return; }
    this.targets();
    const renderer = this.renderer;
    const oldTarget = renderer.getRenderTarget(), oldAutoClear = renderer.autoClear, oldAutoReset = renderer.info.autoReset;
    const oldColor = renderer.getClearColor(new THREE.Color()), oldAlpha = renderer.getClearAlpha();
    const originalMaterials = translucent.map(mesh => mesh.material);
    const originallyVisible = [...translucent, ...overlays];
    try {
      renderer.autoClear = false; renderer.info.autoReset = false; renderer.info.reset();
      for (const mesh of originallyVisible) mesh.visible = false;
      renderer.setRenderTarget(this.opaque!); renderer.setClearColor(0x000000, 0); renderer.clear(); renderer.render(scene, camera);

      // Normalize weights over this anatomy's projected depth, independent of
      // camera clipping planes and physical patient/world coordinates.
      this.bounds.makeEmpty();
      for (const mesh of translucent) this.bounds.expandByObject(mesh);
      let near = 1, far = 0;
      for (let i = 0; i < 8; i++) {
        this.point.set(i & 1 ? this.bounds.max.x : this.bounds.min.x, i & 2 ? this.bounds.max.y : this.bounds.min.y, i & 4 ? this.bounds.max.z : this.bounds.min.z).project(camera);
        const depth = this.point.z * .5 + .5; near = Math.min(near, depth); far = Math.max(far, depth);
      }
      this.depthRange.set(near, Math.max(far, near + .00001));

      // Hide opaque surfaces for both transparent passes. Their resolved depth
      // is sampled instead of sharing ownership of a depth attachment.
      for (const mesh of [...surfaces, ...opaqueHelpers]) mesh.visible = false;
      for (let i = 0; i < translucent.length; i++) {
        const mesh = translucent[i]; mesh.visible = true; mesh.material = this.variant(originalMaterials[i]).accumulation;
      }
      renderer.setRenderTarget(this.accumulation!); renderer.setClearColor(0x000000, 0); renderer.clear(); renderer.render(scene, camera);
      for (let i = 0; i < translucent.length; i++) translucent[i].material = this.variant(originalMaterials[i]).revealage as unknown as SurfaceMaterial;
      renderer.setRenderTarget(this.revealage!); renderer.setClearColor(0xffffff, 1); renderer.clear(); renderer.render(scene, camera);
      for (const mesh of translucent) mesh.visible = false;
      renderer.setRenderTarget(null); renderer.setClearColor(0x000000, 0); renderer.clear(); renderer.render(this.composite, this.screenCamera);
      if (overlays.length) {
        for (const overlay of overlays) overlay.visible = true;
        renderer.render(scene, camera);
      }
    } finally {
      for (let i = 0; i < translucent.length; i++) translucent[i].material = originalMaterials[i];
      for (const mesh of [...surfaces, ...overlays, ...opaqueHelpers]) mesh.visible = true;
      renderer.setClearColor(oldColor, oldAlpha); renderer.setRenderTarget(oldTarget);
      renderer.autoClear = oldAutoClear; renderer.info.autoReset = oldAutoReset;
    }
  }

  dispose() {
    for (const variants of [...this.variants.values()]) variants.release();
    this.opaque?.dispose(); this.accumulation?.dispose(); this.revealage?.dispose();
    this.quad.geometry.dispose(); this.quad.material.dispose();
  }
}

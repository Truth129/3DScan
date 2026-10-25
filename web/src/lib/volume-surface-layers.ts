import * as THREE from 'three';

type Surface = THREE.Mesh<THREE.BufferGeometry, THREE.Material>;
interface Variants { peel: THREE.Material; tail: THREE.Material; solid: THREE.Material; release(): void }
export interface VolumeDepthLayer { color: THREE.Texture; depth: THREE.Texture }

/** Ordered front fragments plus a deterministic weighted tail; no voxel copies. */
export class VolumeSurfaceLayers {
  readonly supported: boolean;
  private opaque?: THREE.WebGLRenderTarget;
  private peels: THREE.WebGLRenderTarget[] = [];
  private groupColors: THREE.WebGLRenderTarget[] = [];
  private tail?: THREE.WebGLRenderTarget;
  private variants = new Map<THREE.Material,Variants>();
  private previousDepth = {value:null as THREE.Texture|null};
  private opaqueDepth = {value:null as THREE.Texture|null};
  private first = {value:true};
  private groupDepth = {value:null as THREE.Texture|null};
  private grouping = {value:false};
  private inverseSize = {value:new THREE.Vector2(1,1)};
  private tailResolve: THREE.Mesh<THREE.PlaneGeometry,THREE.ShaderMaterial>;
  private screenScene = new THREE.Scene();
  private screenCamera = new THREE.Camera();
  private planeScene = new THREE.Scene();
  private planeMirror?: THREE.Mesh;
  private surfaces: Surface[] = [];
  private translucent: Surface[] = [];
  private source?: THREE.Scene;
  private plane?: THREE.Mesh;
  private width=0;
  private height=0;

  constructor(private renderer:THREE.WebGLRenderer) {
    this.supported=!!renderer.getContext().getExtension('EXT_color_buffer_float');
    this.tailResolve=new THREE.Mesh(new THREE.PlaneGeometry(2,2),new THREE.ShaderMaterial({
      glslVersion:THREE.GLSL3,uniforms:{uAccum:{value:null},uWeightedDepth:{value:null}},
      vertexShader:'out vec2 vUv; void main(){vUv=uv;gl_Position=vec4(position.xy,0.,1.);}',
      fragmentShader:`precision highp float; uniform sampler2D uAccum; uniform sampler2D uWeightedDepth;
        in vec2 vUv; out vec4 outColor;
        void main(){vec4 a=texture(uAccum,vUv);float coverage=1.-exp(-texture(uWeightedDepth,vUv).g);
          if(a.a<=.000001||coverage<=.000001)discard;
          outColor=vec4(a.rgb/a.a,coverage);gl_FragDepth=clamp(texture(uWeightedDepth,vUv).r/a.a,0.,1.);}`,
      depthTest:true,depthWrite:true,depthFunc:THREE.AlwaysDepth,blending:THREE.NoBlending,toneMapped:false,
    }));
    this.tailResolve.frustumCulled=false;this.screenScene.add(this.tailResolve);
  }
  get count(){return this.surfaces.length;}
  get transparentCount(){return this.supported?this.translucent.length:0;}
  get bytes(){return [this.opaque,...this.peels,...this.groupColors,this.tail].reduce((sum,target)=>sum+(target?target.width*target.height*(target===this.tail?12:target.depthTexture?8:4):0),0);}

  begin(scene:THREE.Scene,camera:THREE.Camera,plane:THREE.Mesh|undefined,width:number,height:number){
    this.source=scene;this.plane=plane?.visible?plane:undefined;this.surfaces=[];
    scene.traverseVisible(node=>{
      if(node instanceof THREE.Mesh && node.userData.structureId && !Array.isArray(node.material)
          && node.material.visible && node.material.opacity>0 && node.layers.test(camera.layers))this.surfaces.push(node as Surface);
    });
    this.translucent=this.surfaces.filter(mesh=>mesh.material.opacity<1);
    if(!this.count)return;
    this.width=width;this.height=height;this.inverseSize.value.set(1/width,1/height);
    this.opaque??=this.depthTarget(width,height);
    this.resize(this.opaque,width,height);this.opaqueDepth.value=this.opaque.depthTexture;
  }
  private depthTarget(width:number,height:number){
    const target=new THREE.WebGLRenderTarget(width,height,{format:THREE.RGBAFormat,type:THREE.UnsignedByteType,
      minFilter:THREE.NearestFilter,magFilter:THREE.NearestFilter,depthBuffer:true,stencilBuffer:false,generateMipmaps:false});
    target.texture.colorSpace=THREE.SRGBColorSpace;target.depthTexture=new THREE.DepthTexture(width,height,THREE.UnsignedIntType);
    return target;
  }
  private resize(target:THREE.WebGLRenderTarget,width:number,height:number){if(target.width!==width||target.height!==height)target.setSize(width,height);}
  private draw(camera:THREE.Camera,selected:readonly Surface[],variant?:(base:THREE.Material)=>THREE.Material){
    const allowed=new Set<THREE.Object3D>(selected),snapshot:{node:THREE.Object3D;visible:boolean;material?:THREE.Material|THREE.Material[]}[]=[];
    this.source!.traverse(node=>{
      if(!(node instanceof THREE.Mesh || node instanceof THREE.Line || node instanceof THREE.Points || node instanceof THREE.Sprite))return;
      snapshot.push({node,visible:node.visible,material:node.material});node.visible=allowed.has(node);
      if(node.visible&&variant)node.material=variant(node.material as THREE.Material);
    });
    try{this.renderer.render(this.source!,camera);}finally{
      for(const item of snapshot){item.node.visible=item.visible;(item.node as THREE.Mesh).material=item.material!;}
    }
  }
  renderOpaque(camera:THREE.Camera):VolumeDepthLayer {
    this.renderer.setRenderTarget(this.opaque!);this.renderer.setClearColor(0,0);this.renderer.clear(true,true,false);
    const selected=this.supported?this.surfaces.filter(mesh=>mesh.material.opacity>=1):this.surfaces;
    this.draw(camera,selected,this.supported?undefined:base=>this.variant(base).solid);
    if(this.plane){
      this.plane.updateWorldMatrix(true,false);this.planeMirror??=new THREE.Mesh(this.plane.geometry,this.plane.material);
      const mirror=this.planeMirror;mirror.geometry=this.plane.geometry;mirror.material=this.plane.material;
      mirror.matrixAutoUpdate=false;mirror.matrixWorldAutoUpdate=false;mirror.matrixWorld.copy(this.plane.matrixWorld);
      mirror.layers.mask=this.plane.layers.mask;mirror.frustumCulled=false;if(!mirror.parent)this.planeScene.add(mirror);
      this.renderer.render(this.planeScene,camera);
    }
    return {color:this.opaque!.texture,depth:this.opaque!.depthTexture!};
  }
  peel(camera:THREE.Camera,index:number,previous?:THREE.Texture):VolumeDepthLayer{
    this.previousDepth.value=previous??null;this.first.value=!previous;
    const slot=index%2;this.peels[slot]??=this.depthTarget(this.width,this.height);const target=this.peels[slot];
    this.resize(target,this.width,this.height);this.renderer.setRenderTarget(target);this.renderer.setClearColor(0,0);this.renderer.clear(true,true,false);
    this.draw(camera,this.translucent,base=>this.variant(base).peel);
    // All fragments tied at the selected depth form one weighted color group.
    // Their combined coverage retains duplicate/coplanar anatomical boundaries.
    return this.renderWeighted(camera,index,target.depthTexture!,true);
  }
  renderTail(camera:THREE.Camera,index:number,previous:THREE.Texture):VolumeDepthLayer{
    return this.renderWeighted(camera,index,previous,false);
  }
  private renderWeighted(camera:THREE.Camera,index:number,previous:THREE.Texture,group:boolean):VolumeDepthLayer{
    this.previousDepth.value=previous;this.first.value=false;
    this.groupDepth.value=group?previous:null;this.grouping.value=group;
    if(!this.tail){
      this.tail=new THREE.WebGLRenderTarget(this.width,this.height,{count:2,type:THREE.HalfFloatType,
        minFilter:THREE.NearestFilter,magFilter:THREE.NearestFilter,depthBuffer:false,stencilBuffer:false,generateMipmaps:false});
      this.tail.textures[1].format=THREE.RGFormat;
      Object.assign(this.tailResolve.material.uniforms,{uAccum:{value:this.tail.textures[0]},
        uWeightedDepth:{value:this.tail.textures[1]}});
    }
    this.resize(this.tail,this.width,this.height);
    this.renderer.setRenderTarget(this.tail);this.renderer.setClearColor(0,0);this.renderer.clear(true,false,false);
    this.draw(camera,this.translucent,base=>this.variant(base).tail);
    const slot=index%2;
    let target:THREE.WebGLRenderTarget;
    if(group){
      this.groupColors[slot]??=new THREE.WebGLRenderTarget(this.width,this.height,{format:THREE.RGBAFormat,
        minFilter:THREE.NearestFilter,magFilter:THREE.NearestFilter,depthBuffer:false,stencilBuffer:false,generateMipmaps:false});
      target=this.groupColors[slot];target.texture.colorSpace=THREE.SRGBColorSpace;
    }else{this.peels[slot]??=this.depthTarget(this.width,this.height);target=this.peels[slot];}
    this.resize(target,this.width,this.height);this.renderer.setRenderTarget(target);this.renderer.setClearColor(0,0);this.renderer.clear(true,true,false);
    this.renderer.render(this.screenScene,this.screenCamera);
    this.grouping.value=false;
    return {color:target.texture,depth:group?previous:target.depthTexture!};
  }
  private variant(base:THREE.Material):Variants{
    let variants=this.variants.get(base);
    if(!variants){
      const shared=(shader:THREE.WebGLProgramParametersWithUniforms)=>{
        Object.assign(shader.uniforms,{stackPreviousDepth:this.previousDepth,stackOpaqueDepth:this.opaqueDepth,
          stackFirst:this.first,stackInverseSize:this.inverseSize,stackGroupDepth:this.groupDepth,stackGrouping:this.grouping});
        shader.fragmentShader=`uniform sampler2D stackPreviousDepth;uniform sampler2D stackOpaqueDepth;
          uniform bool stackFirst;uniform vec2 stackInverseSize;uniform sampler2D stackGroupDepth;uniform bool stackGrouping;\n${shader.fragmentShader}`;
        return `vec2 stackUV=gl_FragCoord.xy*stackInverseSize;
          if(stackGrouping){if(abs(gl_FragCoord.z-texture2D(stackGroupDepth,stackUV).r)>.0000001)discard;}
          else if(!stackFirst&&gl_FragCoord.z<=texture2D(stackPreviousDepth,stackUV).r+.0000001)discard;
          if(gl_FragCoord.z>texture2D(stackOpaqueDepth,stackUV).r+.0000001)discard;`;
      };
      const peel=base.clone();peel.transparent=true;peel.blending=THREE.NoBlending;peel.depthTest=true;peel.depthWrite=true;
      peel.onBeforeCompile=shader=>{const discard=shared(shader);shader.fragmentShader=shader.fragmentShader.replace('#include <opaque_fragment>',`${discard}\n#include <opaque_fragment>`);};
      peel.customProgramCacheKey=()=>`stack-peel-v1-${base.type}`;
      const tail=base.clone();tail.transparent=true;tail.depthTest=false;tail.depthWrite=false;
      tail.blending=THREE.CustomBlending;tail.blendEquation=THREE.AddEquation;tail.blendSrc=tail.blendDst=THREE.OneFactor;
      tail.onBeforeCompile=shader=>{
        const discard=shared(shader);shader.fragmentShader=`layout(location=1) out vec4 stackWeightedDepth;\n${shader.fragmentShader}`
          .replace('#include <opaque_fragment>',`${discard}
            float a=clamp(diffuseColor.a,0.,1.);float w=max(.01,pow(a+.01,3.)*32.)*exp(-4.*gl_FragCoord.z);
            gl_FragColor=vec4(outgoingLight*a,a)*w;stackWeightedDepth=vec4(gl_FragCoord.z*a*w,-log(max(1.-a,.0000001)),0.,0.);`)
          .replace('#include <tonemapping_fragment>','').replace('#include <colorspace_fragment>','').replace('#include <premultiplied_alpha_fragment>','');
      };
      tail.customProgramCacheKey=()=>`stack-tail-v1-${base.type}`;
      const solid=base.clone();solid.opacity=1;solid.transparent=false;solid.blending=THREE.NoBlending;solid.depthTest=true;solid.depthWrite=true;
      const release=()=>{peel.dispose();tail.dispose();solid.dispose();base.removeEventListener('dispose',release);this.variants.delete(base);};
      variants={peel,tail,solid,release};this.variants.set(base,variants);base.addEventListener('dispose',release);
    }
    for(const material of [variants.peel,variants.tail,variants.solid]){
      material.opacity=material===variants.solid?1:base.opacity;
      for(const field of ['color','emissive','specular'] as const){
        const a=(material as unknown as Record<string,unknown>)[field],b=(base as unknown as Record<string,unknown>)[field];
        if(a instanceof THREE.Color&&b instanceof THREE.Color)a.copy(b);
      }
      if('shininess'in base&&'shininess'in material)material.shininess=base.shininess;
    }
    return variants;
  }
  dispose(){
    for(const variant of [...this.variants.values()])variant.release();
    this.opaque?.dispose();this.peels.forEach(target=>target.dispose());this.groupColors.forEach(target=>target.dispose());this.tail?.dispose();
    this.tailResolve.geometry.dispose();this.tailResolve.material.dispose();this.screenScene.clear();this.planeScene.clear();
    this.planeMirror=undefined;this.source=undefined;this.surfaces=[];this.translucent=[];
  }
}

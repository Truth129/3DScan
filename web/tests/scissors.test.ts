import { describe, expect, it } from 'vitest';
import * as THREE from 'three';
import { captureScissors, createScissorsPrompt, scissorsCaptureMatches, scissorsPath, scissorsPolygon, scissorsProjection, scissorsProjectionMatches, scissorsScreenPoint } from '../src/lib/scissors';
import { rasToScene, sceneToRas } from '../src/lib/geometry';
import { transformPoint } from '../src/lib/ct-reslice';
import { createReviewGesture } from '../src/lib/interactive-review';
import type { Geometry, Point } from '../src/types';

const viewport = { left: 120, top: 80, width: 1200, height: 600 };
function camera() {
  const value = new THREE.OrthographicCamera(-.3,.3,.15,-.15,.001,10);
  value.position.set(.2,.3,.8); value.up.set(0,1,0); value.lookAt(.01,-.02,.03); value.zoom=1.7; value.updateProjectionMatrix(); value.updateMatrixWorld(); return value;
}
function project(matrix: number[][], point: Point) { const out=matrix.map(row=>row[0]*point[0]+row[1]*point[1]+row[2]*point[2]+row[3]);return out.slice(0,3).map(value=>value/out[3]); }

describe('camera-frozen full-depth 3D scissors',()=>{
  it('matches Three.js physical RAS projection for oblique reflected CT voxels after pan and zoom',()=>{
    const view=camera(),matrix=scissorsProjection(view),affine=[[-.8,.3,.2,31],[.1,1.4,.2,-52],[.2,.1,-2.1,87],[0,0,0,1]];
    for(const voxel of [[0,0,0],[13,21,9],[2,18,33]] as Point[]){
      const ras=transformPoint(affine,voxel),actual=project(matrix,ras),expected=new THREE.Vector3(...rasToScene(ras)).project(view);
      actual.forEach((value,axis)=>expect(value).toBeCloseTo(expected.toArray()[axis],12));
    }
    expect(matrix[3]).toEqual([0,0,0,1]);
  });
  it('keeps the same screen footprint through the full camera depth rather than a CT slice',()=>{
    const view=camera(),matrix=scissorsProjection(view),origin=new THREE.Vector3(.01,-.02,.03),direction=view.getWorldDirection(new THREE.Vector3());
    const points=[-.2,0,.2].map(depth=>sceneToRas(origin.clone().addScaledVector(direction,depth).toArray()));
    const projected=points.map(point=>project(matrix,point));
    projected.slice(1).forEach(point=>{expect(point[0]).toBeCloseTo(projected[0][0],12);expect(point[1]).toBeCloseTo(projected[0][1],12);});
    expect(projected[0][2]).not.toBeCloseTo(projected[2][2],6);
  });
  it('freezes projection and viewport values and owns the persisted matrix',()=>{
    const view=camera(),rect={...viewport},capture=captureScissors(view,rect),original=structuredClone(capture.rasToClip);
    const prompt=createScissorsPrompt(capture,'rectangle','erase_inside',[[-.3,-.2],[.4,.5]],'cut')!;
    rect.width=1;capture.rasToClip[0][0]=99;view.position.x+=.3;view.updateMatrixWorld();
    expect(prompt.ras_to_clip).toEqual(original);expect(capture.viewport.width).toBe(1200);expect(prompt.include).toBe(false);
  });
  it('cancels a capture when orbit, pan, zoom or the viewport changes',()=>{
    for(const change of ['orbit','pan','zoom','resize','move'] as const){
      const view=camera(),capture=captureScissors(view,viewport),rect={...viewport};
      expect(scissorsCaptureMatches(capture,view,rect)).toBe(true);
      if(change==='orbit'){view.position.x+=.1;view.lookAt(.01,-.02,.03);}
      if(change==='pan')view.position.y+=.1;
      if(change==='zoom'){view.zoom=2;view.updateProjectionMatrix();}
      if(change==='resize')rect.width+=1;
      if(change==='move')rect.left+=1;
      expect(scissorsCaptureMatches(capture,view,rect)).toBe(false);
    }
  });
  it('recognizes persisted outlines only in the same camera projection',()=>{
    const view=camera(),saved=scissorsProjection(view);expect(scissorsProjectionMatches(saved,scissorsProjection(view))).toBe(true);
    view.zoom=2;view.updateProjectionMatrix();expect(scissorsProjectionMatches(saved,scissorsProjection(view))).toBe(false);
    view.zoom=1.7;view.updateProjectionMatrix();expect(scissorsProjectionMatches(saved,scissorsProjection(view))).toBe(true);
  });
  it('converts CSS pointer positions to bounded NDC independent of device pixel ratio',()=>{
    expect(scissorsScreenPoint(viewport,120,80)).toEqual([-1,1]);expect(scissorsScreenPoint(viewport,1320,680)).toEqual([1,-1]);
    expect(scissorsScreenPoint(viewport,720,380)).toEqual([0,0]);expect(scissorsScreenPoint(viewport,-500,900)).toEqual([-1,-1]);
  });
  it('creates four exact axis-aligned rectangle corners regardless of drag direction',()=>{
    const result=scissorsPolygon('rectangle',[[.4,.5],[-.3,-.2]],viewport);
    expect(result).toEqual([[-.3,-.2],[.4,-.2],[.4,.5],[-.3,.5]]);
    const prompt=createScissorsPrompt(captureScissors(camera(),viewport),'rectangle','erase_outside',[[.4,.5],[-.3,-.2]],'rectangle');
    expect(prompt).toMatchObject({kind:'scissors',shape:'rectangle',operation:'erase_outside',include:false,vertices:result});
  });
  it('uses a 64-sided circle with equal physical screen radii on a wide viewport',()=>{
    const circle=scissorsPolygon('circle',[[0,0],[.2,0]],viewport);expect(circle).toHaveLength(64);
    circle.forEach(([x,y])=>expect(Math.hypot(x*viewport.width/2,y*viewport.height/2)).toBeCloseTo(120,10));
  });
  it('closes freehand shapes and shades the complementary viewport only for erase outside',()=>{
    const points:[number,number][]=[[-.5,-.5],[.4,-.2],[.2,.5]],capture=captureScissors(camera(),viewport);
    const prompt=createScissorsPrompt(capture,'freehand','erase_inside',points,'free')!;points[0][0]=0;
    expect(prompt.vertices[0][0]).toBe(-.5);expect(scissorsPath(prompt.vertices,'erase_inside')).toMatch(/^M.* Z$/);
    expect(scissorsPath(prompt.vertices,'erase_outside')).toMatch(/^M-1 -1 L1 -1 L1 1 L-1 1 Z M/);
  });
  it('rejects empty, degenerate and tiny gestures without creating a cut',()=>{
    const capture=captureScissors(camera(),viewport);
    expect(createScissorsPrompt(capture,'freehand','erase_inside',[[0,0],[.2,.2]],'short')).toBeUndefined();
    expect(createScissorsPrompt(capture,'rectangle','erase_inside',[[0,0],[0,.3]],'flat')).toBeUndefined();
    expect(createScissorsPrompt(capture,'circle','erase_inside',[[0,0],[1e-7,0]],'tiny')).toBeUndefined();
  });
  it('rejects invalid viewports and perspective cameras',()=>{
    expect(()=>captureScissors(camera(),{...viewport,width:0})).toThrow('viewport');
    expect(()=>scissorsProjection(new THREE.PerspectiveCamera() as never)).toThrow('orthographic');
  });
  it('never interprets the scissors tool as a native-slice gesture',()=>{
    const geometry:Geometry={shape:[5,5,5],affine_ras:[[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]],spacing:[1,1,1],bounds_ras:[[0,0,0],[5,5,5]],center_ras:[2,2,2]};
    expect(createReviewGesture(geometry,{plane:'axial',native:{axis:2,index:2},width:5,height:5,position_mm:2,extent_mm:[5,5],pixel_to_ras:geometry.affine_ras},'scissors',false,[[0,0],[4,4]],3,'wrong-view')).toBeUndefined();
  });
});

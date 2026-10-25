import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../src/lib/api';
import { InteractiveController } from '../src/lib/interactive-controller';
import { normalizeRelationship, relationshipError, reviewCandidateHasChanges } from '../src/lib/interactive-review';
import type { InteractiveOptions, InteractivePrompt, InteractiveReview, SegmentRelationship, Structure } from '../src/types';

const independent = (): SegmentRelationship => ({ kind: 'independent', exclusive_with: [] });
const subdivision = (): SegmentRelationship => ({ kind: 'subdivision', exclusive_with: ['liver'] });
const opts: InteractiveOptions = { autozoom: true, start_empty: false };
const summary = { changed_voxels: 0, added_voxels: 0, removed_voxels: 0, before_volume_ml: 10, after_volume_ml: 10, after_voxels: 100 };
const ready = (): InteractiveReview => ({ id:'draft',case_id:'case',revision_id:'base',structure_id:'custom:one',generation:1,status:'ready',prompts:[],options:opts,relationship:independent(),relationship_dirty:false,dirty:false,applied_prompt_count:0,
  candidate:{generation:1,revision_id:'review-draft-1',relationship:independent(),summary:{...summary,changed_voxels:1},total_changed_voxels:1} });
const clone = <T>(value: T): T => structuredClone(value);
const controllers: InteractiveController[] = [];
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; controllers.splice(0).forEach(value => value.dispose()); });

function fixture(initial?: InteractiveReview, defaultPolicy = independent()) {
  let remote = initial ? clone(initial) : undefined;
  const job = {id:'job',case_id:'case',kind:'review_predict',status:'queued' as const,stage:'Queued',created_at:''};
  const reader = {
    interactiveReviews:vi.fn(async () => remote ? [clone(remote)] : []),
    startInteractive:vi.fn(async (_case: string, _base: string, _target: string, relationship?: SegmentRelationship) => {
      remote = {...ready(),generation:0,status:'draft',candidate:undefined,dirty:true,relationship:clone(relationship ?? defaultPolicy)}; return clone(remote);
    }),
    interactivePrompts:vi.fn(async (_case: string, _sid: string, generation: number, prompts: InteractivePrompt[], options?: InteractiveOptions, relationship?: SegmentRelationship) => {
      expect(generation).toBe(remote!.generation);
      remote = {...remote!,generation:generation+1,prompts:clone(prompts),options:clone(options ?? opts),relationship:normalizeRelationship(relationship),relationship_dirty:true,dirty:true}; return clone(remote);
    }),
    interactiveReview:vi.fn(async () => clone(remote!)),predictInteractive:vi.fn(async () => job),acceptInteractive:vi.fn(async () => ({...job,kind:'review_accept'})),
    discardInteractive:vi.fn(async () => ({...remote!,status:'discarded' as const})),cancel:vi.fn(async () => ({...job,status:'cancelled' as const})),job:vi.fn(async () => job),
  };
  const controller = new InteractiveController('case','base','custom:one',vi.fn(),reader); controllers.push(controller);
  return {controller,reader};
}

describe('explicit segment relationships', () => {
  it('canonicalizes only explicit IDs without sharing mutable sibling arrays', () => {
    const policy: SegmentRelationship = {kind:'subdivision',exclusive_with:['b','a','b']};
    const normalized = normalizeRelationship(policy); policy.exclusive_with.push('c');
    expect(normalized.exclusive_with).toEqual(['a','b']);
    expect(normalizeRelationship({kind:'independent',parent_id:'liver',exclusive_with:['b']})).toEqual(independent());
  });

  it('protects parent/child overlap and rejects cycles while permitting explicit sibling replacement', () => {
    const structure = (id: string, relationship?: SegmentRelationship): Structure => ({id,name:id,task:'custom',group:'Custom',color:'#fff',volume_ml:1,voxel_count:1,centroid_ras:null,bounds_ras:null,qc:[],relationship});
    const structures = [structure('parent'),structure('child',{kind:'subregion',parent_id:'parent',exclusive_with:[]}),structure('peer')];
    expect(relationshipError({kind:'subdivision',exclusive_with:['parent']},'child',structures)).toContain('Parent and child');
    expect(relationshipError({kind:'subregion',parent_id:'child',exclusive_with:[]},'parent',structures)).toContain('descendant');
    expect(relationshipError({kind:'subdivision',parent_id:'parent',exclusive_with:['peer']},'child',structures)).toBe('');
    expect(relationshipError({kind:'subregion',exclusive_with:[]},'child',structures)).toContain('Choose the parent');
  });

  it('serializes policy with the generation-bound draft rather than inference options', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(ready()),{status:200})); globalThis.fetch = fetch;
    await api.interactivePrompts('case','draft',4,[],opts,subdivision());
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({expected_generation:4,prompts:[],options:opts,relationship:subdivision()});
  });

  it('keeps the previous preview visible, blocks stale Save, and applies one policy edit without new prompts', async () => {
    const {controller,reader} = fixture(ready()); await controller.load();
    controller.editRelationship(subdivision());
    expect(controller.candidate?.revision_id).toBe('review-draft-1'); expect(controller.relationshipDirty).toBe(true); expect(controller.pendingCount).toBe(1);
    expect(await controller.accept()).toBeUndefined(); expect(reader.acceptInteractive).not.toHaveBeenCalled();
    await controller.apply();
    expect(reader.interactivePrompts).toHaveBeenCalledExactlyOnceWith('case','draft',1,[],opts,subdivision());
    expect(reader.predictInteractive).toHaveBeenCalledExactlyOnceWith('case','draft',2,false);
  });

  it('restores policy-only pending work after reload and undoes/redoes whole relationship selections', async () => {
    const initial = ready(); initial.generation = 2; initial.relationship = subdivision(); initial.relationship_dirty = true; initial.dirty = true;
    const {controller} = fixture(initial); await controller.load();
    expect(controller.relationshipDirty).toBe(true); expect(controller.pendingCount).toBe(1);
    controller.editRelationship({kind:'subregion',parent_id:'liver',exclusive_with:[]});
    controller.undo(); expect(controller.relationship).toEqual(subdivision());
    controller.redo(); expect(controller.relationship).toEqual({kind:'subregion',parent_id:'liver',exclusive_with:[]});
    controller.relationship.exclusive_with.push('external'); controller.undo(); expect(controller.relationship).toEqual(subdivision());
    controller.redo(); expect(controller.relationship.exclusive_with).toEqual([]);
  });

  it('adopts authoritative reciprocal defaults and serializes concurrent draft creation once', async () => {
    const {controller,reader} = fixture(undefined,subdivision()); await controller.load();
    await Promise.all([controller.ensureDraft(),controller.ensureDraft()]);
    expect(reader.startInteractive).toHaveBeenCalledExactlyOnceWith('case','base','custom:one',undefined,'nninteractive');
    expect(controller.relationship).toEqual(subdivision()); expect(controller.unsaved).toBe(false);
    controller.append({id:'point',kind:'point',include:true,ras:[1,2,3]}); await controller.flush();
    expect(reader.interactivePrompts.mock.calls[0][5]).toEqual(subdivision());
  });

  it('preserves policy edits made while the initial WAN create is in flight', async () => {
    const {controller,reader} = fixture(undefined,subdivision()); await controller.load();
    let release!: () => void; const start = reader.startInteractive.getMockImplementation()!;
    reader.startInteractive.mockImplementation(async (...args) => { await new Promise<void>(resolve => {release=resolve;}); return start(...args); });
    const opening = controller.ensureDraft();
    const explicit: SegmentRelationship = {kind:'subregion',parent_id:'liver',exclusive_with:[]}; controller.editRelationship(explicit);
    const saving = controller.flush(); release(); await Promise.all([opening,saving]);
    expect(reader.startInteractive).toHaveBeenCalledTimes(1); expect(controller.relationship).toEqual(explicit);
    expect(reader.interactivePrompts.mock.calls[0][5]).toEqual(explicit);
    controller.undo(); expect(controller.relationship).toEqual(subdivision());
  });

  it('allows sibling-only and metadata-only previews to save, while rejecting empty or unchanged masks', () => {
    const candidate = {...ready().candidate!,summary};
    expect(reviewCandidateHasChanges({...candidate,total_changed_voxels:25})).toBe(true);
    expect(reviewCandidateHasChanges({...candidate,total_changed_voxels:0,relationship_changed:true})).toBe(true);
    expect(reviewCandidateHasChanges({...candidate,total_changed_voxels:0})).toBe(false);
    expect(reviewCandidateHasChanges({...candidate,total_changed_voxels:25,summary:{...summary,after_voxels:0}})).toBe(false);
  });

  it('allows complete erasure only when explicitly reviewing an existing direct target', () => {
    const empty = {...ready().candidate!,total_changed_voxels:100,summary:{...summary,changed_voxels:100,removed_voxels:100,after_voxels:0,after_volume_ml:0}};
    expect(reviewCandidateHasChanges(empty)).toBe(false);
    expect(reviewCandidateHasChanges(empty, true)).toBe(true);
    expect(reviewCandidateHasChanges({...empty,total_changed_voxels:0,summary:{...empty.summary,changed_voxels:0}}, true)).toBe(false);
    expect(reviewCandidateHasChanges(undefined, true)).toBe(false);
  });

  it('sends an explicit direct method with both saved and new target creation', async () => {
    const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({...ready(),method:'direct'}),{status:200})); globalThis.fetch = fetch;
    await api.startInteractive('case','base','custom:one',undefined,'direct');
    expect(JSON.parse(fetch.mock.calls[0][1]!.body as string)).toEqual({revision_id:'base',structure_id:'custom:one',method:'direct'});
    await api.createInteractiveStructure('case','base',{name:'Vessel',group:'Custom',color:'#c18dff',method:'direct'});
    expect(JSON.parse(fetch.mock.calls[1][1]!.body as string)).toEqual({revision_id:'base',name:'Vessel',group:'Custom',color:'#c18dff',method:'direct'});
  });
});

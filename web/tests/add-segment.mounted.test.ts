import { afterEach, describe, expect, it, vi } from 'vitest';
import { h, ref } from 'vue';
import InteractiveReviewPanel from '../src/components/InteractiveReviewPanel.vue';
import { api } from '../src/lib/api';
import type { InteractiveReview, Structure } from '../src/types';
import { createApp, FakeElement, settled } from './fake-dom';

const target: Structure = {
  id: 'custom:new',
  name: 'Hand painted',
  task: 'custom',
  group: 'Custom',
  color: '#c18dff',
  volume_ml: 0,
  voxel_count: 0,
  centroid_ras: null,
  bounds_ras: null,
  qc: [],
};
const draft: InteractiveReview = {
  id: 'new',
  case_id: 'case',
  revision_id: 'scan',
  structure_id: target.id,
  method: 'direct',
  generation: 0,
  status: 'draft',
  prompts: [],
  options: { autozoom: true, start_empty: true },
  new_structure: true,
  target,
  preview_revision_id: 'review-new-0',
  applied_prompt_count: 0,
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** A scan with no segments, as the page passes it: nothing saved and nothing selected. */
async function openScan(drafts: InteractiveReview[]) {
  vi.stubGlobal('localStorage', { getItem: () => null, setItem() {}, removeItem() {} });
  vi.spyOn(api, 'interactiveReviews').mockResolvedValue(drafts);
  const start = vi.spyOn(api, 'startInteractive');
  const structures = ref<Structure[]>([]),
    selected = ref(''),
    active = ref(true);
  const root = new FakeElement('root');
  const app = createApp({
    render: () =>
      h(InteractiveReviewPanel, {
        caseId: 'case',
        revisionId: 'scan',
        selected: selected.value,
        structures: structures.value,
        busy: false,
        active: active.value,
        onSelect: (id: string) => void (selected.value = id),
        onTargets: (targets: Structure[]) => void (structures.value = targets),
      }),
  });
  app.mount(root);
  for (let index = 0; index < 4; index++) await settled();
  return { root, selected, active, start, unmount: () => app.unmount() };
}

describe('adding a segment to a scan with no segments', () => {
  it('opens on the form that adds one, in Direct edit, with no draft for a missing segment', async () => {
    const view = await openScan([]);
    expect(view.root.named('review-add-form')).toBeDefined();
    expect(view.root.button('Direct edit')!.props['aria-pressed']).toBe(true);
    expect(view.root.button('AI assist')!.props.disabled).toBeFalsy();
    expect(view.root.button('Paint')!.props.disabled).toBe(true);
    expect(view.root.select('Review structure')!.optionTexts()).toEqual(['No segments yet']);
    expect(view.start).not.toHaveBeenCalled();
    view.unmount();
  });
  it('returns to an unsaved new segment instead of the form, also after Exit review', async () => {
    const view = await openScan([draft]);
    expect(view.selected.value).toBe(target.id);
    expect(view.root.named('review-add-form')).toBeUndefined();
    expect(view.root.button('Paint')!.props.disabled).toBeFalsy();
    // Exit review drops an unsaved segment from the selection, as the page does.
    view.active.value = false;
    view.selected.value = '';
    await settled();
    view.active.value = true;
    for (let index = 0; index < 4; index++) await settled();
    expect(view.selected.value).toBe(target.id);
    expect(view.root.named('review-add-form')).toBeUndefined();
    view.unmount();
  });
});

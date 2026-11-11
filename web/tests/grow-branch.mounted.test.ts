import { afterEach, describe, expect, it, vi } from 'vitest';
import { h, ref } from 'vue';
import InteractiveReviewPanel from '../src/components/InteractiveReviewPanel.vue';
import { api } from '../src/lib/api';
import type { Job, Structure } from '../src/types';
import { choose, createApp, FakeElement, press, settled } from './fake-dom';

const coronary: Structure = {
  id: 'imagecasx_cas_net:coronary_arteries',
  name: 'Coronary arteries (ImageCAS-X CAS-Net)',
  task: 'imagecasx_cas_net',
  group: 'Coronary arteries',
  color: '#e77676',
  volume_ml: 3,
  voxel_count: 1000,
  centroid_ras: null,
  bounds_ras: null,
  qc: [],
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function open(structure: Structure) {
  vi.stubGlobal('localStorage', { getItem: () => null, setItem() {}, removeItem() {} });
  vi.spyOn(api, 'interactiveReviews').mockResolvedValue([]);
  const panel = ref<InstanceType<typeof InteractiveReviewPanel>>(),
    accepted: Job[] = [],
    tools: string[] = [];
  const root = new FakeElement('root');
  const app = createApp({
    render: () =>
      h(InteractiveReviewPanel, {
        ref: panel,
        caseId: 'case',
        revisionId: 'scan',
        selected: structure.id,
        structures: [structure],
        busy: false,
        active: true,
        onAccept: (job: Job) => void accepted.push(job),
        onTool: (tool: string) => void tools.push(tool),
      }),
  });
  app.mount(root);
  for (let index = 0; index < 4; index++) await settled();
  return { root, panel, accepted, tools, unmount: () => app.unmount() };
}

describe('Grow branch', () => {
  it('grows from one click at the chosen threshold, shows a refusal, and saves through the page', async () => {
    const view = await open({ ...coronary, growable: true });
    const grow = vi
      .spyOn(api, 'growBranch')
      .mockRejectedValueOnce(new Error('CAS-Net saw nothing here; use nnInteractive or Direct edit.'))
      .mockResolvedValueOnce({ id: 'job', kind: 'grow_branch', status: 'queued' } as Job);
    await press(view.root.button('Grow branch'));
    // The viewers see an ordinary point tool.
    expect(view.tools.at(-1)).toBe('point');
    await choose(view.root.select('Grow branch probability'), '0.05');
    view.panel.value!.addGesture({ id: 'a', ras: [1, 2, 3], include: true });
    await settled();
    expect(view.root.named('grow-error')!.content).toContain('CAS-Net saw nothing here');
    view.panel.value!.addGesture({ id: 'b', ras: [4, 5, 6], include: true });
    await settled();
    expect(grow.mock.calls.map(call => call[4])).toEqual([0.05, 0.05]);
    expect(grow).toHaveBeenLastCalledWith('case', 'scan', coronary.id, [4, 5, 6], 0.05);
    expect(view.accepted.map(job => job.id)).toEqual(['job']);
    expect(view.root.named('grow-error')).toBeUndefined();
    view.unmount();
  });
  it('is shown but off for a CAS-Net segment from before the probability was stored', async () => {
    const view = await open(coronary);
    expect(view.root.button('Grow branch')!.props.disabled).toBe(true);
    expect(view.root.content).toContain('Run CAS-Net on this scan again');
    view.unmount();
  });
});

import { describe, expect, it } from 'vitest';
import { reactive } from 'vue';
import AnatomyPanel from '../src/components/AnatomyPanel.vue';
import type { Structure } from '../src/types';
import { click, mount, settled, type MountedElement } from './mounted';

const structure = (name: string, group: string): Structure => ({
  id: `total:${name}`,
  name,
  task: 'total',
  group,
  color: '#aa5500',
  volume_ml: 1,
  voxel_count: 1,
  centroid_ras: null,
  bounds_ras: null,
  qc: [],
});

describe('the segment list', () => {
  it('opens the row of a structure picked elsewhere, even in a folded group or behind a search', async () => {
    const props = reactive({ structures: [structure('liver', 'abdomen'), structure('spine', 'bones')], display: {}, selected: 'total:liver' });
    const view = mount(AnatomyPanel, () => props);
    click(view.all(element => !!element.getAttribute('aria-label')?.startsWith('Collapse Bones'))[0]);
    const search = view.labelled('Search anatomical structures') as MountedElement & { value: string };
    search.value = 'liver';
    search.dispatchEvent(new Event('input'));
    await settled();
    expect(view.labelled('Select Spine')).toBeUndefined();
    props.selected = 'total:spine';
    await settled();
    expect(view.labelled('Select Spine')).toBeDefined();
  });
});

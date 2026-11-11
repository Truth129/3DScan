import { describe, expect, it, vi } from 'vitest';
import { defineComponent, h, shallowReactive } from 'vue';
import ModelViewport from '../src/components/ModelViewport.vue';
import type { Geometry } from '../src/types';
import { click, mount, settled, type MountedElement } from './mounted';

const geometry: Geometry = {
  shape: [4, 4, 4],
  affine_ras: [
    [1, 0, 0, 0],
    [0, 1, 0, 0],
    [0, 0, 1, 0],
    [0, 0, 0, 1],
  ],
  spacing: [1, 1, 1],
  bounds_ras: [
    [0, 0, 0],
    [3, 3, 3],
  ],
  center_ras: [1.5, 1.5, 1.5],
};
// The settings only need a CT that Cornerstone would draw, not a drawing.
const VolumeView = defineComponent({ render: () => h('div') });
const settingsBlock = (element: MountedElement) => element.descendants().find(node => String(node.className).split(' ').includes('stack-volume-options'));
const settingsIn = (element: MountedElement) => !!settingsBlock(element);
const drawn = (element: MountedElement) => (settingsBlock(element)!.style as { display?: string }).display !== 'none';

describe('docked volume settings', () => {
  it('render inline without a target, and move to it while it shows and the view is not expanded', async () => {
    const target = document.createElement('div') as unknown as MountedElement,
      ask = vi.fn(),
      state = shallowReactive<Record<string, unknown>>({
        structures: [],
        display: {},
        selected: '',
        point: [1, 1, 1],
        caseId: 'case-a',
        revisionId: 'revision-a',
        geometry,
        imageContext: { modality: 'CT' },
        volumeViewComponent: VolumeView,
        viewportActive: true,
        onShowVolumeSettings: ask,
      });
    const inline = mount(ModelViewport, () => ({ ...state }));
    await settled();
    expect(settingsIn(inline.root)).toBe(true);
    inline.unmount();

    Object.assign(state, { volumeSettingsTarget: target, volumeSettingsDock: 'shown' });
    const shown = mount(ModelViewport, () => ({ ...state }));
    await settled();
    expect(settingsIn(target)).toBe(true);
    expect(settingsIn(shown.root)).toBe(false);
    // Hidden, the side panel gives them back to the Layers card, folded, until Settings shows it again.
    state.volumeSettingsDock = 'hidden';
    await settled();
    expect(settingsIn(shown.root)).toBe(true);
    expect(drawn(shown.root)).toBe(false);
    click(shown.button('Settings'));
    expect(ask).toHaveBeenCalledOnce();
    state.volumeSettingsDock = 'shown';
    await settled();
    expect(settingsIn(target)).toBe(true);
    // Expanding makes everything outside the view inert, so the settings come with it.
    click(shown.labelled('Expand 3D view'));
    await settled();
    expect(settingsIn(shown.root)).toBe(true);
    click(shown.button('Settings'));
    await settled();
    expect(drawn(shown.root)).toBe(true);
    click(shown.labelled('Restore 3D view'));
    await settled();
    expect(settingsIn(target)).toBe(true);
    // Settings opened while expanded stay folded once the side panel hides again.
    state.volumeSettingsDock = 'hidden';
    await settled();
    expect(drawn(shown.root)).toBe(false);
  });
});

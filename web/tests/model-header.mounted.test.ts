import { describe, expect, it, vi } from 'vitest';
import { shallowReactive } from 'vue';
import ModelViewport from '../src/components/ModelViewport.vue';
import { click, mount, settled } from './mounted';

describe('3D header', () => {
  it('focuses the tile in review instead of expanding over the review tools', async () => {
    const focus = vi.fn(),
      state = shallowReactive<Record<string, unknown>>({
        structures: [],
        display: {},
        selected: '',
        viewportActive: true,
        tileFocus: 'shared',
        onFocusTile: focus,
      });
    const shown = mount(ModelViewport, () => ({ ...state }));
    await settled();
    click(shown.labelled('Focus 3D view'));
    await settled();
    expect(focus).toHaveBeenLastCalledWith(true);
    expect(shown.root.children[0].getAttribute('data-expanded')).toBe('false');
    state.tileFocus = 'focused';
    await settled();
    click(shown.labelled('Show CT and 3D views'));
    expect(focus).toHaveBeenLastCalledWith(false);
    // Outside review it expands, as before.
    state.tileFocus = undefined;
    await settled();
    click(shown.labelled('Expand 3D view'));
    await settled();
    expect(shown.root.children[0].getAttribute('data-expanded')).toBe('true');
  });
  it('closes Capture ▾ when the view expands or restores, since the view takes Esc first', async () => {
    const shown = mount(ModelViewport, () => ({ structures: [], display: {}, selected: '', viewportActive: true }));
    await settled();
    const capture = shown.all(element => element.getAttribute('title') === 'Save or export this view')[0]!;
    click(capture);
    await settled();
    expect(capture.getAttribute('aria-expanded')).toBe('true');
    click(shown.labelled('Expand 3D view'));
    await settled();
    expect(capture.getAttribute('aria-expanded')).toBe('false');
  });
});

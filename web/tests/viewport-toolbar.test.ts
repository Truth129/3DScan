import { describe, expect, it } from 'vitest';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import ModelViewport from '../src/components/ModelViewport.vue';
import OfflineApp from '../src/OfflineApp.vue';

const viewport = (values: Record<string, unknown> = {}) =>
  renderToString(createSSRApp(ModelViewport, { structures: [], display: {}, selected: '', viewportActive: true, ...values }));
const PNG_ENABLED = /<button title="Save image" data-v-\w+>PNG ↗<\/button>/;
const tools = (html: string) => html.slice(html.indexOf('class="viewport-tools"'));

describe('3D view toolbar', () => {
  it('offers DICOM ↗ only when the live app asks for it', async () => {
    expect(await viewport()).not.toContain('DICOM ↗');
    const html = tools(await viewport({ dicomViewBlockReason: '' }));
    expect(html.indexOf('DICOM ↗')).toBeGreaterThan(html.indexOf('PNG ↗'));
    expect(html).toContain('title="Add this view to the original study as a DICOM picture"');
  });
  it('never appears in the portable viewer', async () => {
    const html = await renderToString(
      createSSRApp(OfflineApp, {
        manifest: { label: 'Synthetic', review_status: 'unreviewed', structures: [] },
        payload: undefined,
      }),
    );
    expect(html).toContain('title="Save image"');
    expect(html).not.toContain('DICOM ↗');
  });
  it('explains why it is disabled, and leaves PNG ↗ alone', async () => {
    const blocked = tools(await viewport({ dicomViewBlockReason: 'Wait for the current operation to finish before exporting.' }));
    expect(blocked).toMatch(/title="Wait for the current operation to finish before exporting\."[^>]*disabled/);
    expect(blocked).toMatch(PNG_ENABLED);
  });
  it('is disabled during review, while PNG ↗ still works', async () => {
    const html = tools(await viewport({ dicomViewBlockReason: '', reviewActive: true }));
    expect(html).toMatch(/title="Leave review to export this view\."[^>]*disabled/);
    expect(html).toMatch(PNG_ENABLED);
  });
});

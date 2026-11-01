import { afterEach, describe, expect, it, vi } from 'vitest';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import InteractiveReviewPanel from '../src/components/InteractiveReviewPanel.vue';
import { api } from '../src/lib/api';

const props = { caseId: 'case', revisionId: 'revision', selected: 'liver', structures: [], busy: false };

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('review toolbar', () => {
  it('offers Direct edit its own tools and no AI hints', async () => {
    const html = await renderToString(createSSRApp(InteractiveReviewPanel, props));
    expect(html).toContain('aria-label="Direct editing tools"');
    for (const label of ['Paint', 'Erase', 'Scissors']) expect(html).toContain(label);
    for (const label of ['AI annotation tool', 'AI hint polarity', 'Hint brush', 'Lasso', 'Include', 'Exclude']) expect(html).not.toContain(label);
  });
  it('reports the 3D preview with the other messages, never inside a viewer', async () => {
    const messages = (html: string) => html.slice(html.indexOf('class="review-messages"'), html.indexOf('class="review-toolbar-rows"'));
    const failed = messages(await renderToString(createSSRApp(InteractiveReviewPanel, { ...props, surfaceError: 'Mesh failed', surfaceLoading: true })));
    expect(failed).toContain('role="alert"');
    expect(failed).toContain('Mesh failed');
    expect(failed).toContain('Retry preview');
    expect(failed).not.toContain('Preparing the 3D candidate preview');
    const loading = messages(await renderToString(createSSRApp(InteractiveReviewPanel, { ...props, surfaceLoading: true })));
    expect(loading).toContain('Preparing the 3D candidate preview');
    expect(loading).not.toContain('role="alert"');
  });
  // SSR stays on the opening render: it never reaches AI assist and never waits for a status
  // reply, so a missing card here says little. Opening for real does run, so the request can
  // be pinned. inference_path_smoke.py covers what a card does once AI assist has answered.
  it('opens Direct edit without asking for AI readiness', async () => {
    const status = vi.spyOn(api, 'interactiveStatus');
    vi.spyOn(api, 'interactiveReviews').mockResolvedValue([]);
    vi.stubGlobal('localStorage', { getItem: () => null });
    const html = await renderToString(createSSRApp(InteractiveReviewPanel, { ...props, active: true }));
    expect(status).not.toHaveBeenCalled();
    for (const text of ['Run on', 'Re-run', 'AI ·', 'data-inference-path']) expect(html).not.toContain(text);
  });
});

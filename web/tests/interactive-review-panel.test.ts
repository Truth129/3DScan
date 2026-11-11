import { describe, expect, it } from 'vitest';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import InteractiveReviewPanel from '../src/components/InteractiveReviewPanel.vue';

const props = { caseId: 'case', revisionId: 'revision', selected: 'liver', structures: [], busy: false };

describe('review toolbar', () => {
  it('offers Direct edit its own tools and no AI hints', async () => {
    const html = await renderToString(createSSRApp(InteractiveReviewPanel, props));
    expect(html).toContain('aria-label="Direct editing tools"');
    for (const label of ['Paint', 'Erase', 'Scissors']) expect(html).toContain(label);
    for (const label of ['AI annotation tool', 'AI hint polarity', 'Hint brush', 'Lasso', 'Remove mark', 'Include', 'Exclude'])
      expect(html).not.toContain(label);
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
  it('reads down one column, with Apply and Save in the footer under the tools and nothing over a viewer', async () => {
    const html = await renderToString(createSSRApp(InteractiveReviewPanel, props));
    const order = ['review-panel-header', 'review-messages', 'review-toolbar-rows', 'review-footer'].map(name => html.indexOf(`class="${name}"`));
    expect(order.every((at, index) => at >= 0 && (!index || at > order[index - 1]))).toBe(true);
    const footer = html.slice(html.indexOf('class="review-footer"'));
    for (const label of ['↶ Undo', '↷ Redo', 'Apply edits', 'Save correction']) expect(footer).toContain(label);
    expect(html).not.toContain('review-floating-panels');
  });
});

import { describe, expect, it } from 'vitest';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import { gzipSync } from 'node:zlib';
import OfflineApp from '../src/OfflineApp.vue';
import { normalizePortableState, type PortablePayload } from '../src/lib/portable-viewer';
import type { Geometry, OfflineManifest } from '../src/types';

describe('embedded CT viewport activation', () => {
  it('keeps the shared viewport active when optional live-app props are absent', async () => {
    const geometry: Geometry = { shape: [2, 2, 2], affine_ras: [[1,0,0,0],[0,1,0,0],[0,0,1,0],[0,0,0,1]], spacing: [1,1,1], bounds_ras: [[-.5,-.5,-.5],[1.5,1.5,1.5]], center_ras: [.5,.5,.5] };
    const manifest: OfflineManifest = { label: 'Synthetic', review_status: 'unreviewed', structures: [], geometry };
    const payload: PortablePayload = { schema_version: 1, layers: { segmentation: false, ct_volume: false, ct_plane: true }, state: normalizePortableState(undefined, [], geometry), ct: {
      descriptor: { schema_version: 1, key: 'embedded-ct', kind: 'ct', shape: [2,2,2], affine_ras: geometry.affine_ras, dtype: 'int16', slope: 1, intercept: 0, quality: 'full', byte_length: 16, chunk_depth: 2,
        chunks: [{ index: 0, start_k: 0, depth: 2, byte_offset: 0, byte_length: 16 }] },
      chunks: [{ index: 0, gzip_base64: gzipSync(new Uint8Array(16)).toString('base64') }],
    } };
    const html = await renderToString(createSSRApp(OfflineApp, { manifest, payload }));
    expect(html).toContain('data-offline-ct-ready="true"');
    expect(html).toContain('data-ct-plane-visible="true"');
    expect(html).toContain('aria-label="Show CT plane" checked');
  });
});

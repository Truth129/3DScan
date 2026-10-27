/** Actual module worker, transferable buffers and sampling. Only HTTP is mocked. */
import { LocalCTSession, LocalCTUnsupportedError } from '../src/lib/ct-volume';
import type { LocalCTProgress, VolumeDescriptor } from '../src/lib/ct-volume';
import type { Plane } from '../src/types';

const nativeFetch = fetch.bind(window);
const requests: string[] = [];
let active = 0, maximum = 0, loadedBytes = 0;
const sleep = (duration: number) => new Promise<void>(resolve => setTimeout(resolve, duration));
const affine = [[.7, 0, 0, -22], [0, .9, 0, -20], [0, 0, 1.3, -15], [0, 0, 0, 1]];
function descriptor(path: string, quality: 'preview' | 'full', mask = false): VolumeDescriptor {
  const shape: [number, number, number] = mask ? [8, 8, 8] : [64, 48, 32];
  const transform = affine.map(row => [...row]);
  if (mask) { transform[0][3] += 20 * .7; transform[1][3] += 15 * .9; transform[2][3] += 10 * 1.3; }
  if (mask && path.includes('/kidney/')) transform[0][3] += 4 * .7;
  const depth = mask ? 4 : 8, sliceBytes = shape[0] * shape[1] * (mask ? 1 : 2);
  const key = mask ? path : `same-ct-${quality}`;
  const descriptor: VolumeDescriptor = { schema_version: 1, key, kind: mask ? 'mask' : 'ct', shape,
    affine_ras: transform, dtype: mask ? 'uint8' : 'int16', slope: 1, intercept: 0,
    byte_length: sliceBytes * shape[2], chunk_depth: depth, quality,
    chunks: Array.from({ length: shape[2] / depth }, (_, index) => ({ index, start_k: index * depth,
      depth, byte_offset: index * depth * sliceBytes, byte_length: depth * sliceBytes,
      url: `${path}/chunk/${index}?quality=${quality}${mask ? '&mask=1' : ''}` })) };
  if (!mask) descriptor.reference_geometry = { shape: [128, 96, 64],
    affine_ras: [[.35, 0, 0, -22.175], [0, .45, 0, -20.225], [0, 0, .65, -15.325], [0, 0, 0, 1]],
    spacing: [.35, .45, .65], center_ras: [0, 0, 0], bounds_ras: [[0, 0, 0], [1, 1, 1]] };
  if (!mask && path.includes('/oblique/')) {
    descriptor.affine_ras = [[0,-.9,.13,22],[.7,0,.21,-20],[.05,.1,-1.3,40],[0,0,0,1]];
    descriptor.reference_geometry = { shape, affine_ras: descriptor.affine_ras, spacing: [.7,.9,1.3], center_ras: [0,0,0], bounds_ras: [[-100,-100,-100],[100,100,100]] };
  }
  return descriptor;
}
window.fetch = async (input, init) => {
  const url = new URL(input instanceof Request ? input.url : String(input), location.href);
  if (!url.pathname.startsWith('/local-fixture/')) return nativeFetch(input, init);
  requests.push(url.pathname + url.search);
  const quality = url.searchParams.get('quality') === 'full' ? 'full' : 'preview';
  if (/\/(?:ct|image)-volume$/.test(url.pathname)) return new Response(JSON.stringify(descriptor(url.pathname.includes('/oblique/') ? '/local-fixture/oblique/ct' : '/local-fixture/ct', quality)));
  if (url.pathname.endsWith('/mask-volume')) {
    const root = url.pathname.replace('/mask-volume', '') + '/' + url.searchParams.get('structure_id');
    return new Response(JSON.stringify({ schema_version: 1, current: descriptor(root + '/current', quality, true),
      parent: url.searchParams.get('compare') === 'true' && !root.endsWith('/no-parent')
        ? descriptor(root + '/parent', quality, true) : null }));
  }
  if (!url.pathname.includes('/chunk/')) throw new Error(`Unexpected fixture URL ${url}`);
  active++; maximum = Math.max(maximum, active);
  const signal = init?.signal || (input instanceof Request ? input.signal : undefined);
  try {
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(resolve, 8);
      const abort = () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')); };
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) abort();
    });
    const index = Number(url.pathname.split('/').at(-1)), mask = url.searchParams.has('mask');
    if (mask) {
      const values = new Uint8Array(8 * 8 * 4);
      values.fill(url.pathname.includes('/revision-b/') && !url.pathname.includes('/parent/') ? 0 : 1);
      loadedBytes += values.byteLength;
      return new Response(values);
    }
    const values = new Int16Array(64 * 48 * 8);
    for (let k = 0; k < 8; k++) for (let j = 0; j < 48; j++) for (let i = 0; i < 64; i++) values[i + 64 * (j + 48 * k)] = i + 10 * j + 100 * (k + index * 8) - 1024;
    loadedBytes += values.byteLength;
    return new Response(values);
  } finally { active--; }
};

const checks: Record<string, boolean> = {}, measurements: Record<string, unknown> = {};
function check(name: string, value: boolean, detail?: unknown) {
  checks[name] = value;
  if (!value) throw new Error(`${name}: ${JSON.stringify(detail)}`);
}
async function run() {
  const progress: LocalCTProgress[] = [], sessions: LocalCTSession[] = [];
  try {
    const session = new LocalCTSession('/local-fixture/revision-a', { onProgress: value => progress.push(value) });
    sessions.push(session);
    // Starting overlays immediately also tests the shared two-download gate.
    await Promise.all([session.ready, session.setOverlay('liver', true)]);
    check('all_chunk_transfers_bounded_to_two', maximum <= 2 && maximum > 0, maximum);
    check('preview_default_is_explicit', requests.filter(url => /(?:ct|image)-volume/.test(url)).every(url => url.includes('quality=preview')));
    check('CT_progress_reaches_exact_byte_count', progress.some(value => value.stage === 'ct' && value.ready && value.loadedBytes === 64 * 48 * 32 * 2));
    check('preview_HU_never_reported_as_exact', (await session.probe([0, 0, 0])).hu === null);
    const frames = await Promise.all((['axial', 'coronal', 'sagittal'] as Plane[]).map(plane => session.requestSlice(plane, 0, 'liver', true)));
    check('three_planes_share_one_CT_download', requests.filter(url => url.includes('/ct/chunk/')).length === 4);
    check('original_CT_pixel_grid_preserved', frames[0].meta.width === 128 && frames[0].meta.height === 124,
      frames[0].meta);
    check('selected_and_parent_masks_are_sampled', frames.some(frame => frame.current.some(Boolean) && frame.parent?.some(Boolean)));
    const liverChunks = requests.filter(url => url.includes('/liver/') && url.includes('/chunk/')).length;
    const beforeMultiple = requests.length;
    const multiple = await session.requestSlice('axial', 0, ['liver', 'kidney', 'no-parent'], true);
    check('adding_labels_reuses_existing_mask_chunks', requests.filter(url => url.includes('/liver/') && url.includes('/chunk/')).length === liverChunks);
    check('adding_labels_fetches_only_new_descriptors', requests.slice(beforeMultiple).filter(url => url.includes('/mask-volume')).length === 2);
    check('multiple_overlays_have_stable_ids', JSON.stringify(multiple.overlays?.map(layer => layer.id)) === JSON.stringify(['liver', 'kidney', 'no-parent']));
    check('each_label_has_an_independent_parent', !!multiple.overlays?.[0].parent && !!multiple.overlays?.[1].parent && !multiple.overlays?.[2].parent);
    const a = multiple.overlays![0].current, b = multiple.overlays![1].current;
    check('multiple_masks_preserve_overlap_and_distinct_regions', a.some((value, index) => !!value && !!b[index])
      && a.some((value, index) => !!value && !b[index]) && b.some((value, index) => !!value && !a[index]));
    check('legacy_slice_aliases_transfer_without_duplicates', multiple.current === multiple.overlays![0].current
      && multiple.parent === multiple.overlays![0].parent && multiple.hu.byteLength > 0);
    const multipleWarmRequests = requests.length;
    for (let index = 0; index < 8; index++) await session.requestSlice('axial', index * .5, ['liver', 'kidney', 'no-parent'], true);
    check('multiple_warm_scrolling_is_network_free', requests.length === multipleWarmRequests);
    await session.setOverlay(['liver', 'kidney'], true);
    const beforeReAdd = requests.length;
    await session.setOverlay(['liver', 'kidney', 'no-parent'], true);
    check('deselected_masks_are_released_and_reloaded', requests.slice(beforeReAdd).filter(url => url.includes('/no-parent/') && url.includes('/chunk/')).length === 2);
    await session.setOverlay('liver', true);
    const warmRequests = requests.length;
    const timings: number[] = [];
    for (let i = 0; i < 20; i++) {
      const start = performance.now(); await session.requestSlice('axial', i * .5, 'liver', true); timings.push(performance.now() - start);
    }
    check('warm_scrolling_uses_no_network', requests.length === warmRequests);
    const burst = await Promise.allSettled(Array.from({ length: 120 }, (_, index) => session.requestSlice('axial', index * .1, 'liver', true)));
    const fulfilled = burst.filter(value => value.status === 'fulfilled');
    check('burst_coalesces_stale_slices', fulfilled.length <= 2 && fulfilled.length > 0, fulfilled.length);
    check('burst_finishes_at_latest_position', fulfilled.at(-1)?.status === 'fulfilled'
      && Math.abs(fulfilled.at(-1)!.value.meta.position_mm - 11.9) < 1e-8);
    const sharedPlane = await Promise.all([
      session.requestSlice('axial', 3, 'liver', true, undefined, 'ct'),
      session.requestSlice('axial', 5, 'liver', true, undefined, 'model-review'),
    ]);
    check('model_plane_and_CT_rail_have_independent_request_slots', sharedPlane[0].meta.position_mm === 3 && sharedPlane[1].meta.position_mm === 5);
    const modelRequest = session.requestSlice('axial', 7, 'liver', true, undefined, 'model-review');
    const railBurst = Promise.allSettled(Array.from({ length: 40 }, (_, index) => session.requestSlice('axial', index * .1, 'liver', true)));
    check('CT_rail_burst_does_not_cancel_model_plane', (await modelRequest).meta.position_mm === 7);
    await railBurst;
    check('second_consumer_reuses_CT_without_network', requests.length === warmRequests);
    const ctChunks = requests.filter(url => url.includes('/ct/chunk/')).length;
    await session.setBase('/local-fixture/revision-b');
    const afterRevision = await session.requestSlice('axial', 0, 'liver', true);
    check('revision_reuses_resident_CT', requests.filter(url => url.includes('/ct/chunk/')).length === ctChunks);
    check('revision_replaces_only_selected_masks', !afterRevision.current.some(Boolean) && !!afterRevision.parent?.some(Boolean));
    const old = session.requestSlice('axial', 0, 'old', true).then(() => false, error => error.name.includes('Superseded'));
    await sleep(1); await session.setOverlay('new', false);
    check('changing_selection_rejects_stale_mask_result', await old);
    const rapid = session.setOverlay(['liver', 'kidney', 'old-rapid'], true).then(() => false, error => error.name.includes('Superseded'));
    await sleep(1); await session.setOverlay(['liver', 'new-rapid'], false);
    check('multiple_selection_rejects_superseded_downloads', await rapid);
    const newest = await session.requestSlice('axial', 0, ['liver', 'new-rapid'], false);
    check('multiple_selection_finishes_with_only_latest_labels', newest.overlays?.length === 2
      && newest.overlays[1].id === 'new-rapid' && newest.overlays.every(layer => !layer.parent));
    const full = new LocalCTSession('/local-fixture/revision-a', { quality: 'full' }); sessions.push(full); await full.ready;
    const exact = await full.probe([-22 + 20 * .7, -20 + 15 * .9, -15 + 10 * 1.3]);
    check('full_CT_probe_is_exact_native_voxel', exact.hu === 20 + 150 + 1000 - 1024, exact);
    const oblique = new LocalCTSession('/local-fixture/oblique/revision-a', { quality: 'full' }); sessions.push(oblique); await oblique.ready;
    for (const axis of [0, 1, 2] as const) for (const index of [0, oblique.descriptor!.shape[axis] - 1]) {
      const native = await oblique.requestSlice('axial', 0, [], false, { axis, index });
      check(`native_oblique_axis_${axis}_edge_${index}_has_CT_pixels`, native.meta.native?.index === index && native.hu.filter(value => value !== -1024).length > native.hu.length * .7);
    }
    const limited = new LocalCTSession('/local-fixture/revision-a', { maxCTBytes: 16 }); sessions.push(limited);
    check('memory_limit_is_explicit_fallback', await limited.ready.then(() => false, error => error instanceof LocalCTUnsupportedError));
    const limitedMasks = new LocalCTSession('/local-fixture/revision-a', { maxOverlayBytes: 1024 }); sessions.push(limitedMasks);
    await Promise.all([limitedMasks.ready, limitedMasks.setOverlay('liver', true)]);
    const beforeLimit = requests.length;
    check('aggregate_mask_memory_limit_is_explicit', await limitedMasks.setOverlay(['liver', 'kidney'], true)
      .then(() => false, error => error instanceof LocalCTUnsupportedError));
    check('over_budget_selection_downloads_no_mask_chunks', requests.slice(beforeLimit).every(url => !url.includes('/chunk/')));
    const restored = await limitedMasks.requestSlice('axial', 0, 'liver', true);
    check('mask_memory_fallback_leaves_retained_selection_usable', restored.current.some(Boolean));
    const pending = full.requestSlice('coronal', 0).then(() => false, error => error.name.includes('Superseded'));
    full.dispose(); check('dispose_rejects_pending_work', await pending);
    measurements.requests = requests.length; measurements.bytes = loadedBytes; measurements.maximum_chunk_transfers = maximum;
    measurements.burst_completed = fulfilled.length; measurements.mean_warm_slice_ms = timings.reduce((a, b) => a + b, 0) / timings.length;
    measurements.max_warm_slice_ms = Math.max(...timings);
    const report = { passed: true, checks, measurements };
    document.querySelector('#result')!.textContent = JSON.stringify(report, null, 2);
    return report;
  } finally { for (const session of sessions) session.dispose(); }
}
Object.assign(window, { fixture: { run } });

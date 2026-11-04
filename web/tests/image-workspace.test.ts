import { describe, expect, it, vi, afterEach } from 'vitest';
import { api } from '../src/lib/api';
import { LEGACY_CT_PRESENTATION, type ImageAsset } from '../src/lib/image-domain';
import {
  formatImageValue,
  preparedImageChoices,
  sameImageSource,
  scanRevisionOf,
  sequenceSummary,
  sourceScope,
  SourceRequestGate,
  validWindow,
  validLevel,
  seriesIsReady,
  seriesIssues,
  seriesIssueSummary,
  filterSeries,
  sourceViewLabel,
} from '../src/lib/image-workspace';
import { freshVolumeSettings, getVolumePreset, MR_SIGNAL_PRESET, samplePresetTransfer } from '../src/lib/volume-presets';
import { CTVolumeRenderer } from '../src/lib/ct-volume-renderer';
import { normalizePortableState } from '../src/lib/portable-viewer';
import type { Revision, Series, Structure } from '../src/types';
import type * as THREE from 'three';

const geometry = {
  shape: [8, 8, 8],
  affine_ras: [
    [1, 0, 0, 0],
    [0, 1, 0, 0],
    [0, 0, 1, 0],
    [0, 0, 0, 1],
  ],
  spacing: [1, 1, 1],
  bounds_ras: [
    [-0.5, -0.5, -0.5],
    [7.5, 7.5, 7.5],
  ] as [[number, number, number], [number, number, number]],
  center_ras: [3.5, 3.5, 3.5] as [number, number, number],
};
const signal = {
  ...LEGACY_CT_PRESENTATION,
  units: 'signal',
  background_value: -0.5,
  default_window: 0.25,
  default_level: 0.125,
  preview_quantization_step: 0,
  preset_group: 'MR',
};
function asset(id: string): ImageAsset {
  return {
    schema_version: 1,
    id,
    case_id: 'case-a',
    kind: 'volume3d',
    modality: 'MR',
    content_sha256: 'a'.repeat(64),
    source: { series_uid: id },
    frame_selector: {},
    geometry,
    scalar: { dtype: 'float32', slope: 1, intercept: 0, units: 'signal' },
    presentation: { ...signal },
    capabilities: ['mpr', 'volume_rendering', 'segmentation', 'voxel_measurements'],
  };
}
function revision(id: string, source: string, created_at = '2026-01-01'): Revision {
  return {
    id,
    case_id: 'case-a',
    created_at,
    source: 'converted',
    kind: 'image',
    review_status: 'unreviewed',
    geometry,
    reference_image_id: source,
    image_asset: asset(source),
    structures: [],
    qc: [],
    model_url: null,
  };
}
afterEach(() => vi.unstubAllGlobals());

describe('source-owned workspace state', () => {
  it('preserves state only within the same case and source, irrespective of coincident grids', () => {
    const a = revision('r1', 'T1'),
      b = revision('r2', 'T2'),
      refined = revision('r3', 'T1');
    expect(sameImageSource(a, b)).toBe(false);
    expect(sameImageSource(a, refined)).toBe(true);
    expect(sameImageSource({ ...a, case_id: 'other-case' }, refined)).toBe(false);
    expect(sameImageSource(undefined, refined)).toBe(false);
  });
  it('chooses the latest saved revision independently for each prepared sequence', () => {
    const revisions = [revision('t1-first', 'T1'), revision('t2', 'T2'), revision('t1-corrected', 'T1', '2026-02-01')];
    const choices = preparedImageChoices([asset('T1'), asset('T2'), asset('unused')], revisions, [
      { uid: 'T1', description: 'T1 precontrast', modality: 'MR', slice_count: 8, spacing: [1, 1, 1], warnings: [] },
      { uid: 'T2', description: 'T2 axial', modality: 'MR', slice_count: 8, spacing: [1, 1, 1], warnings: [] },
    ]);
    expect(choices.map(choice => [choice.label, choice.revision.id])).toEqual([
      ['MR · T1 precontrast', 't1-corrected'],
      ['MR · T2 axial', 't2'],
    ]);
  });
  it('rejects late catalog responses after a same-grid sequence or case switch, including ignored aborts', async () => {
    const gate = new SourceRequestGate();
    let current = sourceScope('case-a', revision('r1', 'T1'));
    const old = gate.begin(current, () => current),
      promise = Promise.resolve('old CT task catalog');
    current = sourceScope('case-a', revision('r2', 'T2'));
    await promise;
    expect(old.current()).toBe(false);
    const next = gate.begin(current, () => current);
    expect(old.signal.aborted).toBe(true);
    expect(next.current()).toBe(true);
    current = sourceScope('case-b', revision('r2', 'T2'));
    expect(next.current()).toBe(false);
    gate.invalidate();
    expect(next.signal.aborted).toBe(true);
  });
  it('requests the modality explicitly and passes the cancellation signal to fetch', async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ tasks: [] }) });
    vi.stubGlobal('fetch', fetch);
    const controller = new AbortController();
    await api.segmentationOptions('MR', controller.signal);
    expect(fetch).toHaveBeenLastCalledWith('/api/segmentation-options?modality=MR', { signal: controller.signal });
    await api.segmentationOptions();
    expect(fetch).toHaveBeenLastCalledWith('/api/segmentation-options?modality=CT', { signal: undefined });
  });
});

describe('MR signal display without HU assumptions', () => {
  it('keeps fractional positive windows and signal probes while retaining CT defaults and rounding', () => {
    expect(validWindow(0.0001, signal)).toBe(0.0001);
    expect(validWindow(0, signal)).toBe(0.25);
    expect(validWindow(Infinity, signal)).toBe(0.25);
    expect(validWindow(NaN, LEGACY_CT_PRESENTATION)).toBe(400);
    expect(validLevel(0, signal)).toBe(0);
    expect(validLevel(NaN, signal)).toBe(0.125);
    expect(formatImageValue(0.1234567, 'signal')).toBe('0.1234567');
    expect(formatImageValue(40.7, 'HU')).toBe('41');
    expect(formatImageValue(null, 'signal')).toBe('—');
  });
  it('keeps sequence, echo and timing labels apart from CT phases, with bounded precision and unmodified metadata', () => {
    const sequence: Series = {
      uid: 'x',
      modality: 'MR',
      description: '',
      slice_count: 8,
      spacing: [1, 1, 1],
      warnings: [],
      sequence_name: 'T2',
      acquisition_type: '3D',
      echo_time_ms: 1.92270004749298,
      repetition_time_ms: 2500,
      inversion_time_ms: 142.399993896,
      echo_number: 2,
    };
    expect(sequenceSummary(sequence)).toBe('T2 · 3D · TE 1.9227 ms · TR 2500 ms · TI 142.4 ms · Echo 2');
    expect(sequence.echo_time_ms).toBe(1.92270004749298);
    expect(sourceViewLabel('CT plane', 'MR')).toBe('MR plane');
    expect(sourceViewLabel('CT-plane', 'MR')).toBe('MR plane');
    expect(sourceViewLabel('CT plane')).toBe('CT plane');
    expect(sourceViewLabel('Oblique', 'MR')).toBe('Oblique');
  });
  it('builds source-relative grayscale knots and retains them through portable state normalization', () => {
    const settings = freshVolumeSettings({ preset: MR_SIGNAL_PRESET, signal_window: { window: 0.25, level: 0.125, invert: false } });
    const preset = getVolumePreset(settings.preset, settings.cinematic, settings.signal_window);
    expect(preset.transferRange).toEqual([0, 0.25]);
    expect(samplePresetTransfer(preset, 0.125)).toEqual([0.5, 0.5, 0.5, 0.5]);
    const inverted = getVolumePreset(MR_SIGNAL_PRESET, undefined, { window: 0.25, level: 0.125, invert: true });
    expect(samplePresetTransfer(inverted, 0.25)).toEqual([0, 0, 0, 1]);
    expect(
      normalizePortableState({ volume: settings, plane: { plane: 'axial', axis: 2, index: 2, window: 0.25, level: 0.125, overlays: [] } }, [], geometry).volume,
    ).toEqual(settings);
  });
  it('updates only the small GPU transfer state for fractional MR window changes', () => {
    const renderer = new CTVolumeRenderer({} as THREE.WebGLRenderer);
    const settings = freshVolumeSettings({ preset: MR_SIGNAL_PRESET, signal_window: { window: 0.25, level: 0.125, invert: false } });
    renderer.setSettings(settings);
    const texture = renderer.material.uniforms.uTransfer.value;
    const volume = renderer.material.uniforms.uVolume.value;
    expect(renderer.material.uniforms.uTransferRange.value.toArray()).toEqual([0, 0.25]);
    renderer.setSettings({ ...settings, signal_window: { window: 0.5, level: 0.25, invert: false } });
    expect(renderer.material.uniforms.uTransferRange.value.toArray()).toEqual([0, 0.5]);
    expect(renderer.material.uniforms.uTransfer.value).toBe(texture);
    expect(renderer.material.uniforms.uVolume.value).toBe(volume);
    renderer.dispose();
  });
});

describe('the scan behind a revision', () => {
  const liver = { id: 'liver' } as Structure;
  const scan = (id: string): Revision => revision(id, 'T1');
  const grown = (id: string, parent_id: string): Revision => ({ ...scan(id), kind: 'segmentation', parent_id, structures: [liver] });

  it("finds this image's own root, because each Process run makes one", () => {
    const older = scan('older'),
      newer = scan('newer'),
      first = grown('first', 'newer'),
      second = grown('second', 'first');
    expect(scanRevisionOf([second, older, newer, first, grown('other', 'older')], second)).toBe(newer);
  });
  it('offers nothing on an image-only revision, a root with structures as the demo has, or a broken chain', () => {
    const root = scan('root'),
      demo = { ...scan('demo'), structures: [liver] },
      looped = grown('looped', 'looped'),
      orphan = grown('orphan', 'missing');
    expect(scanRevisionOf([root], root)).toBeUndefined();
    expect(scanRevisionOf([demo, grown('result', 'demo')], grown('result', 'demo'))).toBeUndefined();
    expect(scanRevisionOf([orphan], orphan)).toBeUndefined();
    expect(scanRevisionOf([looped], looped)).toBeUndefined();
    expect(scanRevisionOf([], undefined)).toBeUndefined();
  });
});

describe('large study series selection', () => {
  const series: Series[] = [
    {
      uid: 't1',
      modality: 'MR',
      description: 'T1 postcontrast phase 1',
      sequence_name: 't1_3d',
      slice_count: 96,
      spacing: [1, 1, 1],
      supported: true,
      warnings: [],
    },
    { uid: 'stir', modality: 'MR', description: 'STIR', sequence_name: 't2', slice_count: 40, spacing: [1, 1, 3], supported: true, warnings: [] },
    {
      uid: 'map',
      modality: 'MR',
      description: 'T1 anatomical-looking name',
      slice_count: 80,
      spacing: [1, 1, 1],
      supported: false,
      warnings: ['Diffusion BValue is not supported by the static import', 'Temporal dimension must be separated'],
    },
    {
      uid: 'rgb',
      modality: 'MR',
      description: 'Summary',
      slice_count: 1,
      spacing: [],
      supported: false,
      warnings: [],
      unsupported_reason: 'RGB summary images are not scalar images',
    },
  ];
  it('filters availability using explicit validation, independently of sequence names', () => {
    expect(series.map(seriesIsReady)).toEqual([true, true, false, false]);
    expect(filterSeries(series, '', 'ready').map(item => item.uid)).toEqual(['t1', 'stir']);
    expect(filterSeries(series, 'T1', 'unavailable').map(item => item.uid)).toEqual(['map']);
    expect(filterSeries(series, 'phase 1', 'all').map(item => item.uid)).toEqual(['t1']);
    expect(filterSeries(series, 'not present', 'all')).toEqual([]);
  });
  it('keeps complete original reasons behind concise issue summaries', () => {
    expect(seriesIssues(series[2])).toEqual(series[2].warnings);
    expect(seriesIssueSummary(series[2])).toBe('Diffusion images and derived maps need a dedicated import workflow.');
    expect(seriesIssues(series[3])).toEqual(['RGB summary images are not scalar images']);
    expect(seriesIssueSummary(series[3])).toContain('Color or summary images');
    expect(seriesIsReady({ ...series[2], supported: undefined })).toBe(false);
  });
});

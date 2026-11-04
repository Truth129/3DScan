import { describe, expect, it } from 'vitest';
import {
  agreementHeadline,
  agreementRows,
  countingNote,
  formatDice,
  formatDistance,
  formatPercent,
  formatVolume,
  formatVolumeChange,
  formatVoxelChange,
  originLabel,
  poolTasks,
  skippedSummary,
  type AgreementPool,
  type RevisionAgreement,
  type StructureAgreement,
  type StructurePool,
} from '../src/lib/agreement';

function structure(id: string, dice: number, change = 0, name = id): StructureAgreement {
  const unchanged = dice === 1 && !change;
  return {
    id,
    name,
    task: 'total',
    group: 'Organs',
    basis: unchanged ? 'same_file' : 'voxels',
    unchanged,
    dice,
    hd95_mm: unchanged ? 0 : 2.5,
    original_voxels: 100,
    current_voxels: 100 + change,
    added_voxels: Math.max(0, change),
    removed_voxels: Math.max(0, -change),
    original_volume_ml: 1,
    current_volume_ml: 1 + change / 100,
    volume_change_ml: change / 100,
    origin: { kind: 'model_output', revision_id: 'a'.repeat(32), run_id: `run-${'b'.repeat(32)}`, task: 'total', engine_version: '2.18.0' },
  };
}

function report(structures: StructureAgreement[], overrides: Partial<RevisionAgreement> = {}): RevisionAgreement {
  const unchanged = structures.filter(row => row.unchanged).length;
  return {
    schema_version: 1,
    case_id: 'c'.repeat(32),
    revision_id: 'd'.repeat(32),
    review_status: 'corrected',
    synthetic: false,
    active: true,
    aggregate_pool: 'real',
    implementation: 'mask-agreement-v1',
    hd95_exact: true,
    grid: { voxel_volume_ml: 0.01, spacing_mm: [1, 1, 10], sheared: false },
    summary: {
      structures: structures.length,
      compared: structures.length,
      unchanged,
      changed: structures.length - unchanged,
      excluded: 0,
      dice_mean: null,
      dice_median: null,
      abs_volume_change_ml_mean: null,
    },
    structures,
    excluded: [],
    cache: { hits: 0, computed: 0 },
    ...overrides,
  };
}

function pooled(id: string, unchanged_percent: number, dice_mean: number, reviewed = 4, name = id): StructurePool {
  return {
    id,
    name,
    task: id.split(':')[0],
    reviewed,
    unchanged: Math.round((reviewed * unchanged_percent) / 100),
    unchanged_percent,
    dice_mean,
    dice_median: dice_mean,
    abs_volume_change_ml_mean: 1,
  };
}

describe('agreement formatting', () => {
  it('never rounds a changed structure up to perfect agreement', () => {
    expect(formatDice(1)).toBe('1.000');
    expect(formatDice(0.99951)).toBe('>0.999');
    expect(formatDice(0.9994)).toBe('0.999');
    expect(formatDice(null)).toBe('—');
    expect(formatDice(Number.NaN)).toBe('—');
  });

  it('formats distances, absent distances and large distances', () => {
    expect(formatDistance(0, 'en-US')).toBe('0 mm');
    expect(formatDistance(2.345, 'en-US')).toBe('2.3 mm');
    expect(formatDistance(17.6, 'en-US')).toBe('18 mm');
    expect(formatDistance(null)).toBe('—');
  });

  it('keeps meaningful precision for small structures and signs every change', () => {
    expect(formatVolume(1234.56, 'en-US')).toBe('1,235 mL');
    expect(formatVolume(1.2345, 'en-US')).toBe('1.23 mL');
    expect(formatVolume(0.0001, 'en-US')).toBe('<0.001 mL');
    expect(formatVolumeChange(0)).toBe('0 mL');
    expect(formatVolumeChange(-74.41, 'en-US')).toBe('−74.4 mL');
    expect(formatVolumeChange(0.0002, 'en-US')).toBe('+<0.001 mL');
    expect(formatVolumeChange(undefined)).toBe('—');
    expect(formatVolume(1.5, 'de-DE')).toBe('1,5 mL');
    expect(formatVoxelChange(1204, 87, 'en-US')).toBe('+1,204 / −87');
  });

  it('does not overstate acceptance rates near the ends', () => {
    expect(formatPercent(100)).toBe('100%');
    expect(formatPercent(99.96)).toBe('>99.9%');
    expect(formatPercent(200 / 3, 'en-US')).toBe('67%');
    expect(formatPercent(0.04)).toBe('<0.1%');
    expect(formatPercent(0)).toBe('0%');
    expect(formatPercent(null)).toBe('—');
  });
});

describe('revision agreement presentation', () => {
  const rows = [
    structure('total:spleen', 1),
    structure('total:liver', 0.91, -40, 'Liver'),
    structure('total:aorta', 1, 0, 'Aorta'),
    structure('total:kidney', 0.91, 60, 'Kidney'),
    structure('total:heart', 0.97, 5, 'Heart'),
  ];

  it('lists changed structures by least agreement, then larger volume change', () => {
    expect(agreementRows(report(rows)).map(row => row.id)).toEqual(['total:kidney', 'total:liver', 'total:heart']);
    expect(agreementRows(report(rows), true).map(row => row.id)).toEqual(['total:kidney', 'total:liver', 'total:heart', 'total:aorta', 'total:spleen']);
    expect(agreementRows(undefined)).toEqual([]);
  });

  it('names the baseline and distinguishes synthetic phantoms', () => {
    expect(agreementHeadline(report(rows))).toBe('3 of 5 compared structures differ from the model output.');
    expect(agreementHeadline(report([rows[0]]))).toBe('All 1 compared structure matches the model output exactly.');
    expect(agreementHeadline(report(rows.slice(1, 2), { synthetic: true }))).toBe('1 of 1 compared structure differs from the demo phantom.');
    const excluded = [{ id: 'custom:x', name: 'Lesion', task: 'custom', group: 'Custom', code: 'reviewer_created', reason: 'Added by a reviewer.' }];
    expect(agreementHeadline(report([], { excluded }))).toBe('No structure here has a model original to compare.');
    expect(agreementHeadline(report([]))).toBe('This revision has no segmented structures.');
  });

  it('explains whether and where a revision counts across cases', () => {
    expect(countingNote(report(rows))).toMatch(/^Counted in the all-cases summary/);
    expect(countingNote(report(rows, { aggregate_pool: 'synthetic', synthetic: true }))).toMatch(/never with real cases/);
    expect(countingNote(report(rows, { aggregate_pool: null }))).toMatch(/marked Reviewed or Corrected/);
    expect(countingNote(report(rows, { aggregate_pool: null, active: false }))).toMatch(/active revision/);
  });

  it('labels model and phantom origins without filesystem details', () => {
    expect(originLabel(rows[0].origin)).toBe('Model output · total v2.18.0 · run-bbbbbbbb');
    expect(originLabel({ kind: 'model_output', revision_id: 'a'.repeat(32) })).toBe('Model output');
    expect(originLabel({ kind: 'synthetic_phantom', revision_id: 'a'.repeat(32) })).toBe('Demo phantom (not a model output)');
  });
});

describe('cross-case agreement presentation', () => {
  const pool: AgreementPool = {
    cases: 3,
    excluded: 1,
    summary: null,
    tasks: [
      {
        ...pooled('total:all', 70, 0.95, 12),
        task: 'total',
        structures: [
          pooled('total:liver', 100, 1, 4, 'Liver'),
          pooled('total:pancreas', 25, 0.82, 4, 'Pancreas'),
          pooled('total:adrenal_gland_left', 25, 0.9, 4, 'Adrenal gland left'),
        ],
      },
      { ...pooled('lung_vessels:all', 50, 0.9, 2), task: 'lung_vessels', structures: [pooled('lung_vessels:lung_arteries', 50, 0.9, 2, 'Lung arteries')] },
    ],
  };

  it('orders structures that reviewers change most often first', () => {
    expect(poolTasks(pool).map(task => task.structures.map(row => row.id))).toEqual([
      ['total:pancreas', 'total:adrenal_gland_left', 'total:liver'],
      ['lung_vessels:lung_arteries'],
    ]);
    expect(poolTasks(pool, '', 'name')[0].structures.map(row => row.name)).toEqual(['Adrenal gland left', 'Liver', 'Pancreas']);
  });

  it('filters by task or structure without changing server totals', () => {
    expect(poolTasks(pool, 'LUNG').map(task => task.task)).toEqual(['lung_vessels']);
    expect(poolTasks(pool, 'total adrenal')[0].structures.map(row => row.id)).toEqual(['total:adrenal_gland_left']);
    expect(poolTasks(pool, 'gland left')[0].reviewed).toBe(12);
    expect(poolTasks(pool, 'nothing')).toEqual([]);
    expect(poolTasks(undefined)).toEqual([]);
    expect(pool.tasks[0].structures.map(row => row.id)).toEqual(['total:liver', 'total:pancreas', 'total:adrenal_gland_left']);
  });

  it('summarizes skipped cases only when there are any', () => {
    expect(skippedSummary({ unreviewed: 3, no_segmentation: 1, no_active_revision: 0, unavailable: 0 })).toBe(
      'Not counted: 3 awaiting review · 1 without segmentation.',
    );
    expect(skippedSummary({ unreviewed: 0, future_reason: 2 })).toBe('Not counted: 2 future reason.');
    expect(skippedSummary({ unreviewed: 0 })).toBe('');
  });
});

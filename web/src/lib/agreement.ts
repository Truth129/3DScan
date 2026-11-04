import type { ReviewStatus } from '../types';

/** Agreement between saved model outputs and accepted masks (schema 1). */
export type AgreementPoolName = 'real' | 'synthetic';
export type AgreementBasis = 'same_file' | 'same_digest' | 'voxels';
export interface AgreementOrigin {
  kind: 'model_output' | 'synthetic_phantom';
  revision_id: string;
  run_id?: string;
  task?: string;
  engine_version?: string;
}
/** What saved a changed mask. AI assist keeps the model's own rewrite, so it is counted apart. */
export type EditKind = 'direct_edit' | 'ai_assisted' | 'other';
export interface KindAgreement extends AgreementAverages {
  changed: number;
}
export type EditKindTotals = Partial<Record<EditKind, KindAgreement>>;
export interface StructureAgreement {
  id: string;
  name: string;
  task: string | null;
  group: string | null;
  origin: AgreementOrigin;
  basis: AgreementBasis;
  unchanged: boolean;
  edit_kind: EditKind | null;
  /** Revision sources that saved a new mask since the original, oldest first. */
  edits: string[];
  dice: number;
  hd95_mm: number | null;
  original_voxels: number;
  current_voxels: number;
  added_voxels: number;
  removed_voxels: number;
  original_volume_ml: number;
  current_volume_ml: number;
  volume_change_ml: number;
}
export interface AgreementExclusion {
  id: string;
  name: string;
  task: string | null;
  group: string | null;
  code: string;
  reason: string;
}
export interface AgreementAverages {
  dice_mean: number | null;
  dice_median: number | null;
  abs_volume_change_ml_mean: number | null;
}
export interface RevisionAgreement {
  schema_version: 1;
  case_id: string;
  revision_id: string;
  review_status: ReviewStatus;
  synthetic: boolean;
  active: boolean;
  aggregate_pool: AgreementPoolName | null;
  implementation: string;
  hd95_exact: boolean;
  grid: { voxel_volume_ml: number; spacing_mm: number[]; sheared: boolean };
  summary: AgreementAverages & { structures: number; compared: number; unchanged: number; changed: number; excluded: number; edit_kinds: EditKindTotals };
  structures: StructureAgreement[];
  excluded: AgreementExclusion[];
  cache: { hits: number; computed: number };
}
export interface PooledAgreement {
  reviewed: number;
  unchanged: number;
  unchanged_percent: number;
  dice_mean: number;
  dice_median: number;
  abs_volume_change_ml_mean: number;
  edit_kinds: EditKindTotals;
}
export interface StructurePool extends PooledAgreement {
  id: string;
  name: string;
  task: string | null;
}
export interface TaskPool extends PooledAgreement {
  task: string | null;
  structures: StructurePool[];
}
export interface AgreementPool {
  cases: number;
  excluded: number;
  summary: PooledAgreement | null;
  tasks: TaskPool[];
}
export interface AgreementAggregate {
  schema_version: 1;
  implementation: string;
  selection: { revision: 'active'; review_statuses: ReviewStatus[] };
  cases: { total: number; included: number; skipped: Record<string, number> };
  pools: Record<AgreementPoolName, AgreementPool>;
  revisions: { case_id: string; revision_id: string; review_status: ReviewStatus; pool: AgreementPoolName }[];
}

const MINUS = '−',
  DASH = '—';
const finite = (value: number | null | undefined): value is number => typeof value === 'number' && Number.isFinite(value);

/** Three decimals; a changed structure never displays as a perfect 1.000. */
export function formatDice(value: number | null | undefined): string {
  if (!finite(value)) return DASH;
  if (value >= 1) return '1.000';
  return value >= 0.9995 ? '>0.999' : Math.max(0, value).toFixed(3);
}

export function formatDistance(mm: number | null | undefined, locale?: string): string {
  if (!finite(mm)) return DASH;
  return `${mm.toLocaleString(locale, { maximumFractionDigits: mm < 10 ? 1 : 0 })} mm`;
}

/** Precision follows magnitude so small structures keep meaningful changes. */
function digits(magnitude: number) {
  return magnitude >= 100 ? 0 : magnitude >= 10 ? 1 : magnitude >= 1 ? 2 : 3;
}

export function formatVolume(ml: number | null | undefined, locale?: string): string {
  if (!finite(ml)) return DASH;
  const magnitude = Math.abs(ml),
    places = digits(magnitude);
  if (magnitude && Number(magnitude.toFixed(places)) === 0) return `<${(10 ** -places).toLocaleString(locale)} mL`;
  return `${ml.toLocaleString(locale, { maximumFractionDigits: places })} mL`;
}

/** A signed change; rounding never turns a real change into zero. */
export function formatVolumeChange(ml: number | null | undefined, locale?: string): string {
  if (!finite(ml)) return DASH;
  if (!ml) return '0 mL';
  return `${ml > 0 ? '+' : MINUS}${formatVolume(Math.abs(ml), locale)}`;
}

export function formatVoxelChange(added: number, removed: number, locale?: string): string {
  return `+${added.toLocaleString(locale)} / ${MINUS}${removed.toLocaleString(locale)}`;
}

/** Whole percentages, except near the ends where rounding would overstate. */
export function formatPercent(value: number | null | undefined, locale?: string): string {
  if (!finite(value)) return DASH;
  if (value >= 100) return '100%';
  if (value > 99.9) return '>99.9%';
  if (value > 0 && value < 0.1) return '<0.1%';
  return `${value.toLocaleString(locale, { maximumFractionDigits: value < 1 || value > 99 ? 1 : 0 })}%`;
}

const byName = (a: { name: string; id: string }, b: { name: string; id: string }) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id);

/** Changed structures first, least agreement first; unchanged follow by name. */
export function agreementRows(report: RevisionAgreement | undefined, includeUnchanged = false): StructureAgreement[] {
  if (!report) return [];
  const changed = report.structures
    .filter(row => !row.unchanged)
    .sort((a, b) => a.dice - b.dice || Math.abs(b.volume_change_ml) - Math.abs(a.volume_change_ml) || byName(a, b));
  return includeUnchanged ? [...changed, ...report.structures.filter(row => row.unchanged).sort(byName)] : changed;
}

export function agreementHeadline(report: RevisionAgreement): string {
  const { compared, changed } = report.summary,
    baseline = report.synthetic ? 'demo phantom' : 'model output';
  if (!compared) return report.excluded.length ? 'No structure here has a model original to compare.' : 'This revision has no segmented structures.';
  if (!changed) return `All ${compared} compared ${compared === 1 ? 'structure matches' : 'structures match'} the ${baseline} exactly.`;
  return `${changed} of ${compared} compared ${compared === 1 ? 'structure differs' : 'structures differ'} from the ${baseline}.`;
}

export function countingNote(report: RevisionAgreement): string {
  if (report.aggregate_pool === 'synthetic') return 'Counted only with other synthetic demo cases, never with real cases.';
  if (report.aggregate_pool === 'real') return "Counted in the all-cases summary as this case's reviewed result.";
  if (!report.active) return "Not counted: the summary uses each case's active revision.";
  return 'Not counted until this revision is marked Reviewed or Corrected.';
}

export function originLabel(origin: AgreementOrigin): string {
  if (origin.kind === 'synthetic_phantom') return 'Demo phantom (not a model output)';
  const detail = [origin.task, origin.engine_version && `v${origin.engine_version}`].filter(Boolean).join(' ');
  return `Model output${detail ? ` · ${detail}` : ''}${origin.run_id ? ` · ${origin.run_id.slice(0, 12)}` : ''}`;
}

export const basisLabels: Record<AgreementBasis, string> = {
  same_file: 'Same saved mask as the model output',
  same_digest: 'Byte-identical copy of the model output',
  voxels: 'Compared voxel by voxel on the reference grid',
};

/** The asterisk points at AI_REWRITE_NOTE wherever AI-assisted figures are shown. */
export const editKindLabels: Record<EditKind, string> = { direct_edit: 'Direct edit', ai_assisted: 'AI assist*', other: 'Other' };
export const AI_REWRITE_NOTE = "* AI assist figures include voxels the model rewrote away from the reader's marks, so they overstate the reader's change.";
const EDIT_KINDS: EditKind[] = ['direct_edit', 'ai_assisted', 'other'];
const SOURCES: Record<string, string> = {
  nninteractive_correction: 'AI assist',
  nninteractive_new_structure: 'new segment (AI assist)',
  direct_edit_correction: 'direct edit',
  direct_edit_new_structure: 'new segment (direct edit)',
  slicer_correction: 'Slicer import',
};

export function editKindTotals(totals: EditKindTotals | undefined): (KindAgreement & { kind: EditKind })[] {
  return EDIT_KINDS.flatMap(kind => {
    const item = totals?.[kind];
    return item ? [{ kind, ...item }] : [];
  });
}

export function editKindSummary(item: KindAgreement, locale?: string): string {
  return `${item.changed.toLocaleString(locale)} changed · median Dice ${formatDice(item.dice_median)} · mean |ΔV| ${formatVolume(item.abs_volume_change_ml_mean, locale)}`;
}

/** The saves behind a row, oldest first. */
export function editsLabel(edits: string[]): string {
  return edits.length ? `Saved by ${edits.map(source => SOURCES[source] ?? source.replaceAll('_', ' ')).join(', then ')}` : 'No saved edit';
}

export function poolLabel(name: AgreementPoolName): string {
  return name === 'real' ? 'Reviewed cases' : 'Synthetic demo cases';
}

const SKIPPED: Record<string, string> = {
  unreviewed: 'awaiting review',
  no_segmentation: 'without segmentation',
  no_active_revision: 'not yet opened',
  unavailable: 'unavailable',
};

export function skippedSummary(skipped: Record<string, number>): string {
  const parts = Object.entries(skipped)
    .filter(([, count]) => count > 0)
    .map(([reason, count]) => `${count} ${SKIPPED[reason] ?? reason.replaceAll('_', ' ')}`);
  return parts.length ? `Not counted: ${parts.join(' · ')}.` : '';
}

/**
 * Filter a pool by task or structure text and order its rows for attention:
 * structures most often changed first, then lower Dice. Task totals are the
 * server's; filtering only narrows which rows are shown.
 */
export function poolTasks(pool: AgreementPool | undefined, query = '', order: 'attention' | 'name' = 'attention'): TaskPool[] {
  if (!pool) return [];
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const matches = (text: string) => terms.every(term => text.toLocaleLowerCase().includes(term));
  const sort =
    order === 'name'
      ? byName
      : (a: StructurePool, b: StructurePool) =>
          a.unchanged_percent - b.unchanged_percent || a.dice_mean - b.dice_mean || b.reviewed - a.reviewed || byName(a, b);
  return pool.tasks
    .map(task => ({ ...task, structures: task.structures.filter(row => matches(`${task.task ?? ''} ${row.name} ${row.id}`.replaceAll('_', ' '))).sort(sort) }))
    .filter(task => task.structures.length);
}

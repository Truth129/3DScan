import type { Revision, Series } from '../types';
import { referenceImageKey, type ImageAsset, type ImagePresentation } from './image-domain';

/** A new sequence owns selection, cursor, display state and request results even on an identical grid. */
export function sameImageSource(previous: Revision | undefined, next: Revision): boolean {
  return !!previous && previous.case_id === next.case_id && referenceImageKey(previous) === referenceImageKey(next);
}
export function sourceScope(caseId: string, revision?: Revision): string {
  return `${caseId}:${revision ? referenceImageKey(revision) : ''}:${revision?.image_asset?.modality ?? 'CT'}`;
}
/** Superseded requests may finish after abort; their result must still be rejected. */
export class SourceRequestGate {
  private version = 0;
  private controller?: AbortController;
  invalidate() {
    this.version++;
    this.controller?.abort();
    this.controller = undefined;
  }
  begin(scope: string, currentScope: () => string) {
    this.invalidate();
    const version = this.version,
      controller = (this.controller = new AbortController());
    return { signal: controller.signal, current: () => version === this.version && !controller.signal.aborted && scope === currentScope() };
  }
}
export function preparedImageChoices(assets: readonly ImageAsset[], revisions: readonly Revision[], series: readonly Series[]) {
  return assets.flatMap(asset => {
    const candidates = revisions.filter(item => (item.reference_image_id ?? item.image_asset?.id) === asset.id);
    const latest = candidates.reduce<Revision | undefined>((a, b) => (!a || b.created_at > a.created_at ? b : a), undefined);
    if (!latest) return [];
    const source = series.find(item => item.uid === asset.source.series_uid);
    return [
      {
        id: asset.id,
        revision: latest,
        label: `${asset.modality} · ${source?.description || source?.sequence_name || asset.source.series_uid || asset.id.slice(0, 8)}`,
      },
    ];
  });
}
/** The image-only revision this one grew from. Each Process run makes a root, so walking parents finds this image's own. */
export function scanRevisionOf(revisions: readonly Revision[], active: Revision | undefined) {
  const byId = new Map(revisions.map(item => [item.id, item])),
    seen = new Set<string>();
  let current = active;
  while (current?.parent_id && !seen.has(current.id)) {
    seen.add(current.id);
    current = byId.get(current.parent_id);
  }
  return current && current.id !== active?.id && !current.structures.length ? current : undefined;
}
export function validWindow(value: unknown, presentation: ImagePresentation): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : presentation.default_window;
}
export function validLevel(value: unknown, presentation: ImagePresentation): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : presentation.default_level;
}
export function formatImageValue(value: number | null, units: string): string {
  if (value === null || !Number.isFinite(value)) return '—';
  return units === 'HU' ? String(Math.round(value)) : value.toLocaleString(undefined, { maximumSignificantDigits: 7 });
}
const sequenceTiming = (value: number) => Number(value.toPrecision(5)).toString();
export function sourceViewLabel(name: string, modality = 'CT'): string {
  return /^(?:CT|MR)[ -]plane$/i.test(name) ? `${modality} plane` : name;
}
export function sequenceSummary(series?: Series): string {
  if (!series || series.modality !== 'MR') return '';
  return [
    series.sequence_name,
    series.acquisition_type,
    series.echo_time_ms != null ? `TE ${sequenceTiming(series.echo_time_ms)} ms` : '',
    series.repetition_time_ms != null ? `TR ${sequenceTiming(series.repetition_time_ms)} ms` : '',
    series.inversion_time_ms != null ? `TI ${sequenceTiming(series.inversion_time_ms)} ms` : '',
    series.echo_number != null ? `Echo ${series.echo_number}` : '',
  ]
    .filter(Boolean)
    .join(' · ');
}

export function seriesIsReady(series: Series): boolean {
  return ['CT', 'MR'].includes(series.modality) && series.supported !== false && (series.supported === true || !series.warnings?.length);
}
export function seriesIssues(series: Series): string[] {
  const warnings = [...new Set((series.warnings ?? []).filter(Boolean))];
  return warnings.length ? warnings : series.unsupported_reason ? [series.unsupported_reason] : [];
}
/** Summaries classify supplied validation reasons; they never decide support. */
export function seriesIssueSummary(series: Series): string {
  const reasons = seriesIssues(series),
    text = reasons.join(' ');
  if (/diffusion|b.?value|ADC\b/i.test(text)) return 'Diffusion images and derived maps need a dedicated import workflow.';
  if (/parametric|quantitative|derived.*map/i.test(text)) return 'Quantitative maps need a dedicated import workflow.';
  if (/dynamic|temporal|multiple time|cine/i.test(text)) return 'Multiple time points must be separated into static volumes.';
  if (/RGB|color|colour|SamplesPerPixel/i.test(text)) return 'Color or summary images cannot be opened as a scalar 3D volume.';
  if (/geometry|orientation|spacing|missing slice|duplicate/i.test(text)) return 'The image geometry needs attention before this series can open.';
  const first = reasons[0] ?? 'This image representation is not supported yet.';
  return first.length > 135 ? first.slice(0, 132).trimEnd() + '…' : first;
}
export function filterSeries(series: readonly Series[], query: string, filter: 'all' | 'ready' | 'unavailable'): Series[] {
  const words = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
  return series.filter(
    item =>
      (filter === 'all' || seriesIsReady(item) === (filter === 'ready')) &&
      words.every(word => `${item.description} ${item.modality} ${item.sequence_name ?? ''} ${sequenceSummary(item)}`.toLowerCase().includes(word)),
  );
}

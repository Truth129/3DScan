import type { Acquisition, AcquisitionSelection, ProcessRequest } from '../types';

export const SCAN_REGION_OPTIONS = [
  { group: 'Cardiac', options: [
    { id: 'corcta', label: 'Coronary CTA (CorCTA)' }, { id: 'cardiac', label: 'Cardiac CT' },
  ] },
  { group: 'Chest and abdomen', options: [
    { id: 'chest', label: 'Chest' }, { id: 'chest_abdomen', label: 'Chest + abdomen' },
    { id: 'chest_abdomen_pelvis', label: 'Chest + abdomen + pelvis' },
    { id: 'abdomen', label: 'Abdomen' }, { id: 'abdomen_pelvis', label: 'Abdomen + pelvis' }, { id: 'pelvis', label: 'Pelvis' },
  ] },
  { group: 'Other coverage', options: [
    { id: 'head_neck', label: 'Head + neck' }, { id: 'whole_body', label: 'Whole body' },
    { id: 'other', label: 'Other' }, { id: 'unknown', label: 'Unknown' },
  ] },
] as const;
export const CONTRAST_PHASE_OPTIONS = [
  { group: 'CTA', options: [
    { id: 'coronary_arterial', label: 'Coronary arterial (CorCTA)' }, { id: 'pulmonary_arterial', label: 'Pulmonary arterial' },
  ] },
  { group: 'Contrast phases', options: [
    { id: 'arterial', label: 'Arterial' }, { id: 'portal_venous', label: 'Portal venous' },
    { id: 'venous', label: 'Venous' }, { id: 'delayed', label: 'Delayed' }, { id: 'equilibrium', label: 'Equilibrium' },
  ] },
  { group: 'Other', options: [
    { id: 'noncontrast', label: 'Non-contrast / unenhanced' }, { id: 'unknown', label: 'Unknown' }, { id: 'other', label: 'Other' },
  ] },
] as const;
export const CARDIAC_PHASE_OPTIONS = [
  { id: 'unknown', label: 'Unknown / not recorded' },
  { id: 'best_diastolic', label: 'Best diastolic reconstruction' },
  { id: 'best_systolic', label: 'Best systolic reconstruction' },
  { id: 'diastolic', label: 'Diastolic' }, { id: 'systolic', label: 'Systolic' }, { id: 'other', label: 'Other' },
] as const;

export const SCAN_REGION_IDS = SCAN_REGION_OPTIONS.flatMap(group => group.options.map(item => item.id));
export const CONTRAST_PHASE_IDS = CONTRAST_PHASE_OPTIONS.flatMap(group => group.options.map(item => item.id));
export const CARDIAC_PHASE_IDS = CARDIAC_PHASE_OPTIONS.map(item => item.id);
const regions: Readonly<Record<string, string>> = Object.fromEntries(SCAN_REGION_OPTIONS.flatMap(group => group.options.map(item => [item.id, item.label])));
const phases: Readonly<Record<string, string>> = Object.fromEntries(CONTRAST_PHASE_OPTIONS.flatMap(group => group.options.map(item => [item.id, item.label])));
const cardiac: Readonly<Record<string, string>> = Object.fromEntries(CARDIAC_PHASE_OPTIONS.map(item => [item.id, item.label]));
export const isCardiacRegion = (profile: string) => profile === 'corcta' || profile === 'cardiac';
export const regionLabel = (profile: string) => regions[profile] ?? profile.replaceAll('_', ' ');
export const contrastPhaseLabel = (phase: string) => phases[phase] ?? `Saved phase: ${phase}`;

/** Restore recorded choices without interpreting a series name or R–R percentage. */
export function acquisitionSelection(value?: Partial<Acquisition>, fallbackPhase = ''): AcquisitionSelection {
  const profile = value?.profile || 'chest_abdomen', cardiacRegion = isCardiacRegion(profile);
  return { profile, phase: value?.phase || fallbackPhase,
    cardiac_phase: cardiacRegion && value?.cardiac_phase ? value.cardiac_phase : 'unknown',
    rr_percent: cardiacRegion && typeof value?.rr_percent === 'number' ? value.rr_percent : null };
}

export function acquisitionSelectionError(value: AcquisitionSelection): string {
  if (!Object.hasOwn(regions, value.profile)) return 'Choose a supported scan region or study.';
  if (!value.phase.trim()) return 'Choose a contrast phase, or select Unknown.';
  if (value.phase.length > 80) return 'The recorded contrast phase is too long; choose another label.';
  if (isCardiacRegion(value.profile)) {
    if (!Object.hasOwn(cardiac, value.cardiac_phase)) return 'Choose a cardiac reconstruction phase, or select Unknown.';
    if (value.rr_percent !== null && (!Number.isFinite(value.rr_percent) || value.rr_percent < 0 || value.rr_percent > 100)) {
      return 'Reconstruction phase must be between 0 and 100% R–R, or left blank.';
    }
  }
  return '';
}

/** Only acquisition labels travel here; CT and inference settings are independent. */
export function acquisitionRequest(seriesUid: string, value: AcquisitionSelection): ProcessRequest {
  const invalid = acquisitionSelectionError(value);
  if (invalid) throw new Error(invalid);
  return { series_uid: seriesUid, profile: value.profile, phase: value.phase,
    ...(isCardiacRegion(value.profile) ? { cardiac_phase: value.cardiac_phase,
      ...(value.rr_percent !== null ? { rr_percent: value.rr_percent } : {}) } : {}) };
}

export function acquisitionBadges(value?: Partial<Acquisition>, fallbackPhase = ''): string[] {
  if (!value) return fallbackPhase ? [contrastPhaseLabel(fallbackPhase)] : [];
  const labels = [regionLabel(value.profile || 'unknown')];
  if (value.phase || fallbackPhase) labels.push(contrastPhaseLabel(value.phase || fallbackPhase));
  if (isCardiacRegion(value.profile || '')) {
    if (value.cardiac_phase && value.cardiac_phase !== 'unknown') labels.push(cardiac[value.cardiac_phase] ?? `Saved cardiac phase: ${value.cardiac_phase}`);
    if (typeof value.rr_percent === 'number' && Number.isFinite(value.rr_percent)) labels.push(`${value.rr_percent}% R–R`);
  }
  return labels;
}

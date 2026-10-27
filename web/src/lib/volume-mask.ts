import type { Structure, StructureDisplay } from '../types';

export const MAX_VOLUME_MASK_SEGMENTS = 128;

export interface VolumeMaskStatus {
  ready: boolean;
  loading: boolean;
  error: string;
}

/** Explicit volume-only selection; preset resets and surface visibility do not change it. */
export interface VolumeMaskSettings {
  enabled: boolean;
  mode: 'inside' | 'outside';
  ids: string[];
  /** Grow only the volume-display mask, in physical millimetres. */
  margin_mm: number;
}

/** Own the selected IDs and optionally remove structures unavailable in this revision. */
export function freshVolumeMaskSettings(settings: Partial<VolumeMaskSettings> = {}, structures?: readonly Pick<Structure, 'id'>[]): VolumeMaskSettings {
  const available = structures ? new Set(structures.map(structure => structure.id)) : undefined;
  return {
    enabled: settings.enabled === true,
    mode: settings.mode === 'outside' ? 'outside' : 'inside',
    ids: [...new Set(settings.ids || [])].filter(id => typeof id === 'string' && !!id && (!available || available.has(id))),
    margin_mm: Number.isFinite(settings.margin_mm) ? normalizeVolumeMaskMargin(Math.max(0, Math.min(5, settings.margin_mm!))) : 0,
  };
}

/** Shared nonnegative half-up precision; padded assets have one canonical key. */
export function normalizeVolumeMaskMargin(margin: number): number {
  if (!Number.isFinite(margin) || margin < 0 || margin > 5) throw new Error('Volume mask margin must be between 0 and 5 mm');
  return Math.round(margin * 1000) / 1000;
}

export function volumeMaskSourceId(id: string, margin = 0): string {
  const canonical = normalizeVolumeMaskMargin(margin);
  return canonical ? `volume-margin:${canonical}:${id}` : id;
}

/** A snapshot taken only when the user chooses “Use visible”. */
export function visibleVolumeMaskIds(structures: readonly Pick<Structure, 'id'>[], display: StructureDisplay): string[] {
  return structures.filter(structure => display[structure.id]?.visible === true).map(structure => structure.id);
}

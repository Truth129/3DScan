import type { Structure } from '../types';
import { normalizeStructureColor } from './display-appearance';

/** The native shader holds one colour per label in a fixed uniform array. */
export const MAX_VOLUME_COLOR_SEGMENTS = 64;
export const DEFAULT_VOLUME_COLOR_STRENGTH = 0.85;

/** Presentation-only colouring of the CT volume by segment. Masks, measurements
 * and the CT itself are unchanged; each segment keeps its anatomy-list colour. */
export interface VolumeColorSettings {
  enabled: boolean;
  ids: string[];
  /** 0 keeps the CT appearance; 1 shows each segment's colour, shaded by the CT. */
  strength: number;
}

/** What the native viewer needs: label n is `ids[n - 1]`, coloured `colors[n - 1]`. */
export interface VolumeColorRequest {
  enabled: boolean;
  ids: string[];
  colors: [number, number, number][];
  strength: number;
}

export function freshVolumeColorSettings(settings: Partial<VolumeColorSettings> = {}, structures?: readonly Pick<Structure, 'id'>[]): VolumeColorSettings {
  const available = structures ? new Set(structures.map(structure => structure.id)) : undefined;
  const strength = Number(settings.strength ?? DEFAULT_VOLUME_COLOR_STRENGTH);
  return {
    enabled: settings.enabled === true,
    ids: [...new Set(settings.ids || [])].filter(id => typeof id === 'string' && !!id && (!available || available.has(id))).slice(0, MAX_VOLUME_COLOR_SEGMENTS),
    strength: Number.isFinite(strength) ? Math.max(0, Math.min(1, strength)) : DEFAULT_VOLUME_COLOR_STRENGTH,
  };
}

/** Larger structures are painted first, so smaller ones inside them (vessels,
 * subregions) keep their own colour where segments overlap. */
export function volumeColorOrder(ids: readonly string[], structures: readonly Pick<Structure, 'id' | 'volume_ml'>[]): string[] {
  const volumes = new Map(structures.map(structure => [structure.id, Number(structure.volume_ml) || 0]));
  return [...new Set(ids)]
    .filter(id => volumes.has(id))
    .sort((a, b) => volumes.get(b)! - volumes.get(a)! || (a < b ? -1 : a > b ? 1 : 0))
    .slice(0, MAX_VOLUME_COLOR_SEGMENTS);
}

/** Linear 0–1 RGB for the shader; an invalid stored colour falls back to neutral grey. */
export function volumeColorRGB(color: Structure['color']): [number, number, number] {
  const hex = normalizeStructureColor(color) ?? '#999999';
  return [1, 3, 5].map(offset => parseInt(hex.slice(offset, offset + 2), 16) / 255) as [number, number, number];
}

export function volumeColorRequest(settings: VolumeColorSettings, structures: readonly Pick<Structure, 'id' | 'volume_ml' | 'color'>[]): VolumeColorRequest {
  const fresh = freshVolumeColorSettings(settings, structures),
    byId = new Map(structures.map(structure => [structure.id, structure]));
  const ids = fresh.enabled ? volumeColorOrder(fresh.ids, structures) : [];
  return { enabled: fresh.enabled, ids, colors: ids.map(id => volumeColorRGB(byId.get(id)!.color)), strength: fresh.strength };
}

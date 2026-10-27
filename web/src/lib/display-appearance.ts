import type { Structure } from '../types';

/** Browser-local presentation preferences; native masks and measurements never change. */
export type StructureColorOverrides = Record<string, string>;
export const STRUCTURE_COLORS_STORAGE_PREFIX = 'scan3d-structure-colors-v1:';
const MAX_COLORS = 2048, MAX_STORAGE_CHARS = 1024 * 1024, MAX_ID_CHARS = 256;

/** Canonical RGB shared by HTML color controls, materials and portable exports. */
export function normalizeStructureColor(value: unknown): string | undefined {
  if (typeof value === 'string') return /^#[0-9a-f]{6}$/i.test(value) ? value.toLowerCase() : undefined;
  if (!Array.isArray(value) || (value.length !== 3 && value.length !== 4)
      || value.some(channel => typeof channel !== 'number' || !Number.isFinite(channel))
      || value.slice(0, 3).some(channel => channel < 0 || channel > 255)) return undefined;
  const rgb = value.slice(0, 3) as number[], scale = rgb.some(channel => channel > 1) ? 1 : 255;
  return '#' + rgb.map(channel => Math.round(channel * scale).toString(16).padStart(2, '0')).join('');
}

function validStructureId(id: string): boolean {
  return id.length > 0 && id.length <= MAX_ID_CHARS && id.trim() === id
    && !/[\u0000-\u001f\u007f]/.test(id) && !['__proto__', 'constructor', 'prototype'].includes(id);
}

/** Read own valid entries only; stored preferences cannot inject CSS or phantom IDs. */
export function normalizeStructureColors(value: unknown): StructureColorOverrides {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const entries: [string, string][] = [];
  for (const id in value) {
    if (entries.length >= MAX_COLORS) break;
    if (!Object.hasOwn(value, id) || !validStructureId(id)) continue;
    const color = normalizeStructureColor((value as Record<string, unknown>)[id]);
    if (color) entries.push([id, color]);
  }
  return Object.fromEntries(entries);
}

/** Preserve all source metadata, cloning only a structure with a different RGB.
 * Callers must update rendering materials, not reload meshes, for this view. */
export function displayStructures<T extends Pick<Structure, 'id' | 'color'>>(
  structures: readonly T[], overrides: Readonly<StructureColorOverrides>,
): T[] {
  return structures.map(structure => {
    const color = Object.hasOwn(overrides, structure.id) ? normalizeStructureColor(overrides[structure.id]) : undefined;
    return color && color !== normalizeStructureColor(structure.color) ? { ...structure, color } : structure;
  });
}

export function setStructureColor(overrides: StructureColorOverrides, id: string, value: unknown): StructureColorOverrides {
  const color = normalizeStructureColor(value);
  if (!validStructureId(id) || !color || (Object.hasOwn(overrides, id) && overrides[id] === color)
      || (!Object.hasOwn(overrides, id) && Object.keys(overrides).length >= MAX_COLORS)) return overrides;
  return { ...overrides, [id]: color };
}

export function resetStructureColor(overrides: StructureColorOverrides, id: string): StructureColorOverrides {
  return Object.hasOwn(overrides, id) ? Object.fromEntries(Object.entries(overrides).filter(([key]) => key !== id)) : overrides;
}

function storageKey(scope: string): string | undefined {
  return typeof scope === 'string' && scope.length > 0 && scope.length <= 4096 && scope.trim()
    ? STRUCTURE_COLORS_STORAGE_PREFIX + scope : undefined;
}

/** Scope must contain the case and immutable image identity, never just a path. */
export function readStructureColors(scope: string): StructureColorOverrides {
  try {
    const key = storageKey(scope);
    if (!key) return {};
    const raw = globalThis.localStorage.getItem(key);
    return raw && raw.length <= MAX_STORAGE_CHARS ? normalizeStructureColors(JSON.parse(raw)) : {};
  } catch { return {}; }
}

/** Persist settled edits or debounce writes in the caller. Storage failures do
 * not prevent the current tab from updating its presentation. */
export function saveStructureColors(scope: string, overrides: Readonly<StructureColorOverrides>): boolean {
  try {
    const key = storageKey(scope);
    if (!key) return false;
    const colors = normalizeStructureColors(overrides);
    if (!Object.keys(colors).length) globalThis.localStorage.removeItem(key);
    else {
      const raw = JSON.stringify(colors);
      if (raw.length > MAX_STORAGE_CHARS) return false;
      globalThis.localStorage.setItem(key, raw);
    }
    return true;
  } catch { return false; }
}

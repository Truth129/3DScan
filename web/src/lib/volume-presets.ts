import { SLICER_VOLUME_PRESET_DATA } from './slicer-volume-presets.generated';
export { SLICER_PRESET_SOURCE_URL, SLICER_PRESET_SOURCE_SHA256 } from './slicer-volume-presets.generated';

/** Display settings only. CT samples, segmentation masks and measurements stay immutable. */
export type SlicerVolumePresetId = keyof typeof SLICER_VOLUME_PRESET_DATA;
export const VOLUME_PRESET_IDS = Object.keys(SLICER_VOLUME_PRESET_DATA) as SlicerVolumePresetId[];
/** Earlier recipes keep their original appearance, including soft-tissue → CT-Cardiac. */
export const VOLUME_PRESET_ALIASES = {
  angiography: 'CT-AAA',
  bone: 'CT-Bone',
  'soft-tissue': 'CT-Cardiac',
  lung: 'CT-Lung',
} as const satisfies Record<string, SlicerVolumePresetId>;
export const MR_SIGNAL_PRESET = 'MR-SignalWindow' as const;
export interface SignalWindow {
  window: number;
  level: number;
  invert: boolean;
}
export const DEFAULT_SIGNAL_WINDOW: Readonly<SignalWindow> = { window: 400, level: 40, invert: false };
export const CINEMATIC_HEART_PRESET = 'CT-Cinematic-Heart' as const;
export type CTVolumePresetId = SlicerVolumePresetId | keyof typeof VOLUME_PRESET_ALIASES | typeof CINEMATIC_HEART_PRESET | typeof MR_SIGNAL_PRESET;
export interface CTVolumeCinematicSettings {
  myocardiumHU: number;
  bloodHU: number;
  shadows: number;
  shadowReach: number;
}
export const DEFAULT_CINEMATIC_SETTINGS: Readonly<CTVolumeCinematicSettings> = { myocardiumHU: 80, bloodHU: 400, shadows: 1.5, shadowReach: 0.3 };
export function cinematicReferenceError(myocardium: number, blood: number): string {
  if (!Number.isFinite(myocardium) || myocardium < -500 || myocardium > 1500) return 'Enter a myocardium reference between −500 and 1500 HU.';
  if (!Number.isFinite(blood) || blood < -400 || blood > 3000) return 'Enter a blood pool reference between −400 and 3000 HU.';
  return blood - myocardium < 100 ? 'Blood pool must be at least 100 HU brighter than myocardium.' : '';
}
export type CTVolumeCrop = [[number, number, number], [number, number, number]];
export interface CTVolumeSettings {
  preset: CTVolumePresetId;
  /** Source-relative signal appearance; does not rescale or replace voxel data. */
  signal_window: SignalWindow;
  /** Shift the transfer function along the native HU axis. */
  shift: number;
  density: number;
  quality: 'auto' | 'detail';
  /** Display calibration and bounded volumetric-shadow settings. */
  cinematic: CTVolumeCinematicSettings;
  /** Bounds on native i,j,k voxel footprint, normalized to [0,1]. */
  crop: CTVolumeCrop;
}
export interface CTVolumePreset {
  id: SlicerVolumePresetId | typeof CINEMATIC_HEART_PRESET | typeof MR_SIGNAL_PRESET;
  label: string;
  sourcePreset: SlicerVolumePresetId | typeof CINEMATIC_HEART_PRESET | typeof MR_SIGNAL_PRESET;
  opacity: readonly (readonly [number, number])[];
  color: readonly (readonly [number, number, number, number])[];
  gradientOpacity: readonly (readonly [number, number])[];
  shade: boolean;
  interpolation: 'linear' | 'nearest';
  /** Native scalar bounds; MRI, DTI and micro-CT presets use different ranges. */
  transferRange: readonly [number, number];
  ambient: number;
  diffuse: number;
  specular: number;
  specularPower: number;
}
export const DEFAULT_VOLUME_SETTINGS: Readonly<CTVolumeSettings> = {
  preset: 'angiography',
  signal_window: { ...DEFAULT_SIGNAL_WINDOW },
  shift: 0,
  density: 1,
  quality: 'auto',
  cinematic: { ...DEFAULT_CINEMATIC_SETTINGS },
  crop: [
    [0, 0, 0],
    [1, 1, 1],
  ],
};

/**
 * All built-in 3D Slicer volume properties, with independent color, scalar
 * opacity and gradient-opacity knots. Preserve duplicate source knots, which
 * carry VTK's default linear interpolation and clamped endpoint behavior.
 * Source data/hash: slicer-volume-presets.generated.ts.
 * Copyright and redistribution terms: /licenses/Slicer-LICENSE.txt.
 * CT-MIP/MR-MIP are appearance names. Projection technique is a separate
 * renderer setting in Slicer; this viewer uses composite volume rendering.
 */
const canonicalPresets = Object.fromEntries<CTVolumePreset>(
  VOLUME_PRESET_IDS.map(id => [
    id,
    {
      ...SLICER_VOLUME_PRESET_DATA[id],
      id,
      label: id,
      sourcePreset: id,
    },
  ]),
) as Readonly<Record<SlicerVolumePresetId, CTVolumePreset>>;
export const VOLUME_PRESETS: Readonly<Record<CTVolumePresetId, CTVolumePreset>> = {
  ...canonicalPresets,
  [MR_SIGNAL_PRESET]: signalWindowPreset(DEFAULT_SIGNAL_WINDOW),
  angiography: canonicalPresets['CT-AAA'],
  bone: canonicalPresets['CT-Bone'],
  'soft-tissue': canonicalPresets['CT-Cardiac'],
  lung: canonicalPresets['CT-Lung'],
  [CINEMATIC_HEART_PRESET]: cinematicHeartPreset(DEFAULT_CINEMATIC_SETTINGS),
};
export const VOLUME_PRESET_LABELS = Object.fromEntries(Object.entries(VOLUME_PRESETS).map(([id, preset]) => [id, preset.label])) as Readonly<
  Record<CTVolumePresetId, string>
>;
export const VOLUME_PRESET_GROUPS = [
  { label: 'Cinematic', ids: [CINEMATIC_HEART_PRESET] },
  { label: 'CT', ids: VOLUME_PRESET_IDS.filter(id => id.startsWith('CT-')) },
  { label: 'MR', ids: VOLUME_PRESET_IDS.filter(id => id.startsWith('MR-')) },
  { label: 'Other modalities', ids: VOLUME_PRESET_IDS.filter(id => !id.startsWith('CT-') && !id.startsWith('MR-')) },
] as const;

/** Relative HU/color recipe from the supplied slicer_cinematic_heart.py. */
export function cinematicHeartPreset(settings: CTVolumeCinematicSettings): CTVolumePreset {
  const reference = freshCinematicSettings(settings),
    myocardium = reference.myocardiumHU,
    blood = reference.bloodHU;
  const delta = blood - myocardium,
    fat = -100;
  const knots: [number, number, number, number, number][] = [
    [fat - 150, 0.02, 0.04, 0.12, 0],
    [fat, 0.04, 0.08, 0.22, 0.006],
    [(fat + myocardium) / 2, 0.3, 0.05, 0.05, 0.02],
    [myocardium - 0.13 * delta, 0.55, 0.08, 0.05, 0.1],
    [myocardium, 0.7, 0.11, 0.07, 0.25],
    [myocardium + 0.05 * delta, 0.82, 0.32, 0.12, 0.4],
    [myocardium + 0.11 * delta, 0.88, 0.77, 0.42, 0.65],
    [myocardium + 0.2 * delta, 0.82, 0.77, 0.62, 0.9],
    [blood, 0.68, 0.64, 0.56, 0.97],
    [blood + Math.max(400, 0.6 * delta), 1, 1, 1, 1],
  ];
  // vtkPiecewiseFunction inserts knots in scalar order and replaces equal x.
  const unique = new Map(knots.map(knot => [knot[0], knot]));
  const ordered = [...unique.values()].sort((a, b) => a[0] - b[0]);
  return {
    id: CINEMATIC_HEART_PRESET,
    label: 'Cinematic heart',
    sourcePreset: CINEMATIC_HEART_PRESET,
    color: ordered.map(([hu, r, g, b]) => [hu, r, g, b]),
    opacity: ordered.map(([hu, , , , alpha]) => [hu, alpha]),
    gradientOpacity: [[0, 1]],
    shade: true,
    interpolation: 'linear',
    transferRange: [ordered[0][0], ordered.at(-1)![0]],
    ambient: 0.3,
    diffuse: 0.8,
    specular: 0.25,
    specularPower: 25,
  };
}
export function signalWindowPreset(input: SignalWindow): CTVolumePreset {
  const { window, level, invert } = freshSignalWindow(input),
    low = level - window / 2,
    high = level + window / 2;
  const dark = invert ? 1 : 0,
    bright = invert ? 0 : 1;
  return {
    id: MR_SIGNAL_PRESET,
    sourcePreset: MR_SIGNAL_PRESET,
    label: 'Signal window (grayscale)',
    color: [
      [low, dark, dark, dark],
      [high, bright, bright, bright],
    ],
    opacity: [
      [low, 0],
      [high, 1],
    ],
    gradientOpacity: [[0, 1]],
    shade: false,
    interpolation: 'linear',
    transferRange: [low, high],
    ambient: 1,
    diffuse: 0,
    specular: 0,
    specularPower: 1,
  };
}
export function freshSignalWindow(input?: Partial<SignalWindow>): SignalWindow {
  return {
    window: Number.isFinite(input?.window) && input!.window! > 0 ? input!.window! : DEFAULT_SIGNAL_WINDOW.window,
    level: Number.isFinite(input?.level) ? input!.level! : DEFAULT_SIGNAL_WINDOW.level,
    invert: input?.invert === true,
  };
}
export function getVolumePreset(id: CTVolumePresetId, cinematic?: CTVolumeCinematicSettings, signal?: SignalWindow): CTVolumePreset {
  if (id === MR_SIGNAL_PRESET) return signalWindowPreset(signal ?? DEFAULT_SIGNAL_WINDOW);
  return id === CINEMATIC_HEART_PRESET && cinematic ? cinematicHeartPreset(cinematic) : (VOLUME_PRESETS[id] ?? VOLUME_PRESETS.angiography);
}

function interpolate(points: readonly (readonly number[])[], value: number, channels: number): number[] {
  if (value <= points[0][0]) return points[0].slice(1, channels + 1);
  for (let index = 1; index < points.length; index++) {
    const high = points[index];
    if (value > high[0]) continue;
    if (value === high[0]) return high.slice(1, channels + 1);
    const low = points[index - 1],
      t = (value - low[0]) / (high[0] - low[0]);
    return Array.from({ length: channels }, (_, channel) => low[channel + 1] + (high[channel + 1] - low[channel + 1]) * t);
  }
  return points[points.length - 1].slice(1, channels + 1);
}

/** Reference sampling for the GPU LUT. Colors are display-encoded RGB. */
export function sampleVolumeTransfer(id: CTVolumePresetId, scalar: number, cinematic?: CTVolumeCinematicSettings): [number, number, number, number] {
  return samplePresetTransfer(getVolumePreset(id, cinematic), scalar);
}

/** Sample an already resolved recipe while building a LUT, without per-texel allocations of the recipe. */
export function samplePresetTransfer(preset: CTVolumePreset, scalar: number): [number, number, number, number] {
  if (!Number.isFinite(scalar)) return [0, 0, 0, 0];
  const rgb = interpolate(preset.color, scalar, 3),
    alpha = interpolate(preset.opacity, scalar, 1)[0];
  return [rgb[0], rgb[1], rgb[2], alpha];
}

/** Gradient opacity multiplies scalar opacity before physical step correction. */
export function sampleVolumeGradientOpacity(id: CTVolumePresetId, magnitude: number): number {
  return Number.isFinite(magnitude) ? interpolate(getVolumePreset(id).gradientOpacity, magnitude, 1)[0] : 0;
}

function bounded(value: number, low: number, high: number, fallback: number): number {
  return Number.isFinite(value) ? Math.max(low, Math.min(high, value)) : fallback;
}

export function freshCinematicSettings(settings: Partial<CTVolumeCinematicSettings> = {}): CTVolumeCinematicSettings {
  const given = { ...DEFAULT_CINEMATIC_SETTINGS, ...settings };
  const valid = !cinematicReferenceError(given.myocardiumHU, given.bloodHU);
  return {
    myocardiumHU: valid ? given.myocardiumHU : DEFAULT_CINEMATIC_SETTINGS.myocardiumHU,
    bloodHU: valid ? given.bloodHU : DEFAULT_CINEMATIC_SETTINGS.bloodHU,
    shadows: bounded(given.shadows, 0, 2, 1.5),
    shadowReach: bounded(given.shadowReach, 0, 1, 0.3),
  };
}

/** Always return owned crop and cinematic state; view resets cannot mutate a retained recipe. */
export function freshVolumeSettings(settings: Partial<CTVolumeSettings> = {}): CTVolumeSettings {
  const input = { ...DEFAULT_VOLUME_SETTINGS, ...settings };
  const low = [0, 1, 2].map(axis => bounded(input.crop?.[0]?.[axis], 0, 0.99, 0)) as [number, number, number];
  const high = [0, 1, 2].map(axis => bounded(input.crop?.[1]?.[axis], low[axis] + 0.01, 1, 1)) as [number, number, number];
  return {
    signal_window: freshSignalWindow(input.signal_window),
    preset: Object.hasOwn(VOLUME_PRESETS, input.preset) ? input.preset : 'angiography',
    shift: bounded(input.shift, -500, 500, 0),
    density: bounded(input.density, 0.1, 3, 1),
    quality: input.quality === 'detail' ? 'detail' : 'auto',
    cinematic: freshCinematicSettings(input.cinematic),
    crop: [low, high],
  };
}

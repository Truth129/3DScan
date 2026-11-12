import type { Revision, Structure } from '../types';

// Presentation colors, shared by the local viewer and embedded offline viewer.
// Existing mask revisions and surface cache identities remain unchanged.
const tissueColors: Record<string, string> = {
  lung_upper_lobe_right: '#f29919',
  lung_middle_lobe_right: '#33b34d',
  lung_lower_lobe_right: '#1a80e6',
  lung_upper_lobe_left: '#f27fbf',
  lung_lower_lobe_left: '#7f66e6',
  heart: '#d95772',
  liver: '#cc6619',
  spleen: '#8019e6',
  kidney_left: '#d86a6a',
  kidney_right: '#d86a6a',
  kidney_cyst_left: '#edbc85',
  kidney_cyst_right: '#edbc85',
  pancreas: '#f29919',
  gallbladder: '#80cc19',
  stomach: '#f27fbf',
  small_bowel: '#c39ad9',
  duodenum: '#d9a3c6',
  colon: '#9c83cc',
  urinary_bladder: '#e6c86a',
  adrenal_gland_left: '#d9aa59',
  adrenal_gland_right: '#d9aa59',
  autochthon_left: '#a47782',
  autochthon_right: '#a47782',
  iliopsoas_left: '#b55c73',
  iliopsoas_right: '#b55c73',
  prostate: '#d9909d',
  spinal_cord: '#dfcfa5',
  esophagus: '#c881aa',
  thyroid_gland: '#bd77bf',
};
// Saturated yellow: ivory vanishes on bright CT bone in the slices. Matches scan3d BONE_COLOR.
const boneColor = '#f5cc00';
// A pinned model kept its own group before anatomical groups, so its bones are found by name.
// Names are matched in lowercase. Matches scan3d _MODEL_BONES.
const spineLevels = (region: string, count: number) => Array.from({ length: count }, (_, i) => `vertebrae_${region}${i + 1}`);
const modelBones = new Map([
  ['mrsegmentator', new Set(['spine', 'sacrum', 'left_hip', 'right_hip', 'left_femur', 'right_femur'])],
  ['totalspineseg', new Set(['sacrum', ...spineLevels('c', 7), ...spineLevels('t', 12), ...spineLevels('l', 6)])],
]);
// The saved colour of TotalSegmentator's arteries, veins and airways, which no other output has. Since anatomical
// groups, an Arteries group also holds MRSegmentator's aorta or CAS-Net's coronaries, which keep their own colours,
// so the saved colour, not the group, picks these display colours. Matches scan3d _legacy_appearance.
const vesselColors: Record<string, (name: string) => string> = {
  '#eb655d': () => '#ed2222',
  '#4d82e8': () => '#224ce6',
  '#f1dfb1': name => (name.includes('wall') ? '#cccccc' : '#eeeeee'),
};
const nameOf = (structure: Structure) => structure.id.split(':').at(-1)!.toLowerCase();

export function presentStructures(structures: Structure[]): Structure[] {
  return structures.map(structure => {
    // A custom or imported segment keeps the colour it was given.
    if (structure.task === 'custom' || structure.task === 'dicom_seg' || structure.id.startsWith('custom:')) return structure;
    const name = nameOf(structure),
      vessel = typeof structure.color === 'string' ? vesselColors[structure.color.toLowerCase()] : undefined;
    let color = tissueColors[name];
    if (vessel) color = vessel(name);
    // A bone's saved colour may be the ivory of older revisions. The Bones group holds only outputs that were bones before.
    else if (structure.group.toLowerCase() === 'bones' || modelBones.get(structure.task)?.has(name)) color = boneColor;
    return color ? { ...structure, color } : structure;
  });
}

export function presentRevision(revision: Revision): Revision {
  return { ...revision, structures: presentStructures(revision.structures) };
}

export function defaultOpacity(_structure: Structure): number {
  return 0.5;
}

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
// A pinned model keeps its own group, so its bones are found by name.
const modelBones = new Map([['mrsegmentator', new Set(['spine', 'sacrum', 'left_hip', 'right_hip', 'left_femur', 'right_femur'])]]);
const nameOf = (structure: Structure) => structure.id.split(':').at(-1)!.toLowerCase();

export function presentStructures(structures: Structure[]): Structure[] {
  return structures.map(structure => {
    if (structure.task === 'custom' || structure.id.startsWith('custom:')) return structure;
    const name = nameOf(structure),
      group = structure.group.toLowerCase();
    let color = tissueColors[name];
    if (group === 'arteries') color = '#ed2222';
    else if (group === 'veins') color = '#224ce6';
    else if (group === 'airways') color = name.includes('wall') ? '#cccccc' : '#eeeeee';
    else if (group === 'bones' || modelBones.get(structure.task)?.has(name)) color = boneColor;
    return color ? { ...structure, color } : structure;
  });
}

export function presentRevision(revision: Revision): Revision {
  return { ...revision, structures: presentStructures(revision.structures) };
}

export function defaultOpacity(_structure: Structure): number {
  return 0.5;
}

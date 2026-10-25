import { describe, expect, it } from 'vitest';
import { CinematicReferenceGate, hasCinematicReferenceMasks } from '../src/lib/cinematic-reference';

describe('automatic cinematic references', () => {
  it('requires myocardium and a matching native blood-pool label', () => {
    const structures = (names: string[]) => names.map(name => ({ name }));
    for (const blood of ['Aorta', 'heart_ventricle_left', 'left-ventricle of heart', 'left_atrium', 'atrium_left']) {
      expect(hasCinematicReferenceMasks(structures(['Heart Myocardium', blood]))).toBe(true);
    }
    expect(hasCinematicReferenceMasks(structures(['heart', 'aorta', 'left lung']))).toBe(false);
    expect(hasCinematicReferenceMasks(structures(['myocardium', 'heart_ventricle_right']))).toBe(false);
    expect(hasCinematicReferenceMasks([])).toBe(false);
  });

  it('does not repeat failed, cancelled or successful requests when presets are revisited', () => {
    const gate = new CinematicReferenceGate();
    expect(gate.claim('case-1/revision-1')).toBe(true);
    expect(gate.claim('case-1/revision-1')).toBe(false);
    gate.select('case-1/revision-1');
    expect(gate.claim('case-1/revision-1')).toBe(false);
    expect(gate.claim('case-1/revision-2')).toBe(true);
    expect(gate.claim('case-2/revision-1')).toBe(true);
  });

  it('preserves manual reference edits and starts fresh for a new revision', () => {
    const gate = new CinematicReferenceGate();
    gate.edited('case-1/revision-1');
    expect(gate.claim('case-1/revision-1')).toBe(false);
    expect(gate.claim('case-1/revision-2')).toBe(true);
    expect(gate.claim('')).toBe(false);
  });
});

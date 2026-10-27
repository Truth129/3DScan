import { describe, expect, it } from 'vitest';
import {
  acquisitionBadges,
  acquisitionRequest,
  acquisitionSelection,
  acquisitionSelectionError,
  CARDIAC_PHASE_IDS,
  CONTRAST_PHASE_IDS,
  isCardiacRegion,
  SCAN_REGION_IDS,
} from '../src/lib/acquisition';

describe('acquisition labels and cardiac timing', () => {
  it('offers CorCTA, calcium-score and CTA phase choices without a multi-series phase', () => {
    expect(SCAN_REGION_IDS).toContain('corcta');
    expect(SCAN_REGION_IDS).toContain('cardiac');
    expect(SCAN_REGION_IDS).toContain('abdomen_pelvis');
    expect(SCAN_REGION_IDS).toContain('head_neck');
    expect(SCAN_REGION_IDS).toContain('breast');
    expect(CONTRAST_PHASE_IDS).toContain('coronary_arterial');
    expect(CONTRAST_PHASE_IDS).toContain('pulmonary_arterial');
    expect(CONTRAST_PHASE_IDS).toContain('noncontrast');
    expect(CONTRAST_PHASE_IDS).not.toContain('dual_phase');
    expect(CARDIAC_PHASE_IDS).toContain('best_diastolic');
    expect(CARDIAC_PHASE_IDS).toContain('best_systolic');
    expect(new Set(SCAN_REGION_IDS).size).toBe(SCAN_REGION_IDS.length);
    expect(new Set(CONTRAST_PHASE_IDS).size).toBe(CONTRAST_PHASE_IDS.length);
  });

  it('restores a CorCTA reconstruction and sends only the chosen series and recorded labels', () => {
    const recorded = { series_uid: 'series-69', profile: 'corcta', phase: 'coronary_arterial', cardiac_phase: 'best_diastolic', rr_percent: 69 };
    const state = acquisitionSelection(recorded);
    expect(acquisitionRequest('series-69', state)).toEqual(recorded);
    expect(acquisitionBadges(recorded)).toEqual(['Coronary CTA (CorCTA)', 'Coronary arterial (CorCTA)', 'Best diastolic reconstruction', '69% R–R']);
    state.rr_percent = 70;
    expect(recorded.rr_percent).toBe(69);
  });

  it('accepts timing endpoints and fractional percentages without inferring a cardiac phase', () => {
    const state = acquisitionSelection({ profile: 'corcta', phase: 'noncontrast' });
    expect(state.cardiac_phase).toBe('unknown');
    expect(state.rr_percent).toBeNull();
    expect(acquisitionRequest('s', state)).toEqual({ series_uid: 's', profile: 'corcta', phase: 'noncontrast', cardiac_phase: 'unknown' });
    for (const rr_percent of [0, 69, 69.5, 100]) {
      const request = acquisitionRequest('s', { ...state, rr_percent });
      expect(request.rr_percent).toBe(rr_percent);
      expect(request.cardiac_phase).toBe('unknown');
    }
  });

  it('blocks invalid cardiac timing before constructing a processing request', () => {
    const state = acquisitionSelection({ profile: 'cardiac', phase: 'arterial' });
    for (const rr_percent of [-1, 100.01, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(acquisitionSelectionError({ ...state, rr_percent })).toContain('0 and 100');
      expect(() => acquisitionRequest('s', { ...state, rr_percent })).toThrow('0 and 100');
    }
    expect(acquisitionSelectionError({ ...state, phase: '' })).toContain('contrast phase');
    expect(acquisitionSelectionError({ ...state, cardiac_phase: 'invented' })).toContain('cardiac reconstruction');
  });

  it('drops cardiac metadata for another scan region even if stale form values exist', () => {
    const state = acquisitionSelection({ profile: 'corcta', phase: 'arterial', cardiac_phase: 'diastolic', rr_percent: 69 });
    expect(acquisitionRequest('new-series', { ...state, profile: 'chest' })).toEqual({ series_uid: 'new-series', profile: 'chest', phase: 'arterial' });
    expect(acquisitionSelection({ profile: 'abdomen', phase: 'portal_venous', rr_percent: 69, cardiac_phase: 'diastolic' })).toEqual({
      profile: 'abdomen',
      phase: 'portal_venous',
      cardiac_phase: 'unknown',
      rr_percent: null,
    });
    expect(isCardiacRegion('chest_abdomen')).toBe(false);
    expect(acquisitionSelection()).toEqual({ profile: 'chest_abdomen', phase: '', cardiac_phase: 'unknown', rr_percent: null });
  });

  it('retains bounded legacy and custom phase labels when reopening', () => {
    const old = acquisitionSelection({ series_uid: 'old', profile: 'chest_abdomen', phase: 'venous' });
    expect(acquisitionRequest('old', old).phase).toBe('venous');
    const custom = acquisitionSelection({ profile: 'chest', phase: 'venous_late_70s' });
    expect(acquisitionRequest('custom', custom).phase).toBe('venous_late_70s');
    expect(acquisitionBadges({ profile: 'chest', phase: 'venous_late_70s' })).toEqual(['Chest', 'Saved phase: venous_late_70s']);
    expect(acquisitionSelection(undefined, 'unknown').phase).toBe('unknown');
    expect(acquisitionSelectionError({ ...custom, phase: 'a'.repeat(81) })).toContain('too long');
  });
});

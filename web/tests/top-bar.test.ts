import { describe, expect, it } from 'vitest';
import { topBarFold, topBarPlace } from '../src/lib/top-bar';

describe('top bar folding', () => {
  it('keeps both exports in the bar from 1400 px, folds them into Export ▾ below it, and into ⋯ at 1000 px and narrower', () => {
    const exports = (width: number) => (['seg-export', 'export-viewer'] as const).map(action => topBarPlace(topBarFold(width), action));
    expect(exports(1920)).toEqual(['bar', 'bar']);
    expect(exports(1400)).toEqual(['bar', 'bar']);
    expect(exports(1399)).toEqual(['export', 'export']);
    expect(exports(1001)).toEqual(['export', 'export']);
    expect(exports(1000)).toEqual(['more', 'more']);
    expect(exports(390)).toEqual(['more', 'more']);
  });
  it('folds Import DICOM SEG… beside Segment, and keeps Segment and Review in the bar until the window is narrow', () => {
    expect(topBarPlace('menus', 'seg-import')).toBe('segment');
    expect((['segment', 'review', 'series', 'import-scan'] as const).map(action => topBarPlace('menus', action))).toEqual(['bar', 'bar', 'bar', 'bar']);
    expect(topBarPlace('compact', 'review')).toBe('bar');
    expect(topBarPlace('compact', 'segment')).toBe('more');
  });
  it('moves Series… and + Import scan into ⋯ below 1200 px, or below 1400 px beside a job chip, and never from 1400 px', () => {
    const extras = (width: number, crowded = false) => (['series', 'import-scan'] as const).map(action => topBarPlace(topBarFold(width, crowded), action));
    expect(extras(1200)).toEqual(['bar', 'bar']);
    expect(extras(1199)).toEqual(['more', 'more']);
    expect(extras(1399, true)).toEqual(['more', 'more']);
    expect(extras(1400, true)).toEqual(['bar', 'bar']);
    expect((['segment', 'seg-import', 'review', 'seg-export'] as const).map(action => topBarPlace('tight', action))).toEqual([
      'bar',
      'segment',
      'bar',
      'export',
    ]);
  });
});

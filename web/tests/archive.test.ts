import { afterEach, describe, expect, it, vi } from 'vitest';
import { effectScope } from 'vue';
import { readArchiveLaunch, useQuietCountdown, withoutArchiveLaunch } from '../src/lib/archive';
import type { OrthancSeries } from '../src/lib/orthanc';

const STUDY = '1.2.826.0.1.3680043.10.543.1',
  SERIES = '1.2.826.0.1.3680043.10.543.2',
  SOURCE = 'a'.repeat(64);
const series = (values: Partial<OrthancSeries> = {}): OrthancSeries => ({
  id: SERIES,
  series_uid: SERIES,
  description: 'Synthetic CT',
  modality: 'CT',
  instance_count: 3,
  reported_instances: 3,
  stable: true,
  completeness: 'unknown',
  ready_in: 0,
  snapshot: 'b'.repeat(64),
  supported: true,
  ...values,
});

afterEach(() => vi.useRealTimers());

describe('DICOMweb launch link', () => {
  it('reads one study, series and archive from the fragment, and nothing malformed', () => {
    const link = `#dicom-study=${STUDY}&dicom-series=${SERIES}&dicom-source=${SOURCE}`;
    expect(readArchiveLaunch(link)).toEqual({ kind: 'series', request: { study_uid: STUDY, series_uid: SERIES, source_key: SOURCE } });
    expect(readArchiveLaunch('#panel=review')).toEqual({ kind: 'none' });
    for (const bad of [
      `#dicom-study=${STUDY}&dicom-series=${SERIES}`,
      `#dicom-study=${STUDY}&dicom-series=${SERIES}&dicom-series=${SERIES}&dicom-source=${SOURCE}`,
      `#DICOM-study=${STUDY}&dicom-series=${SERIES}&dicom-source=${SOURCE}`,
      `#dicom-study=Smith&dicom-series=${SERIES}&dicom-source=${SOURCE}`,
      `#dicom-study=${STUDY}&dicom-series=${SERIES}&dicom-source=${SOURCE.slice(1)}`,
    ])
      expect(readArchiveLaunch(bad).kind).toBe('invalid');
    expect(withoutArchiveLaunch(`${link}&panel=review`)).toBe('#panel=review');
  });
});

describe('quiet-period countdown', () => {
  it('counts each waiting series down and reads the list again once, asking again while the view is busy', () => {
    vi.useFakeTimers();
    // Busy, as during an import, for the first two asks.
    const refresh = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(false).mockReturnValue(true),
      scope = effectScope();
    const rows = [series({ stable: false, completeness: 'receiving', ready_in: 3 }), series({ id: 'ready' })];
    const { remaining } = scope.run(() => useQuietCountdown(() => rows, refresh))!;
    expect(remaining(rows[0])).toBe(3);
    vi.advanceTimersByTime(1000);
    expect(remaining(rows[0])).toBe(2);
    vi.advanceTimersByTime(3000);
    expect(remaining(rows[0])).toBe(0);
    expect(refresh).toHaveBeenCalledTimes(2);
    vi.advanceTimersByTime(5000);
    expect(refresh).toHaveBeenCalledTimes(3);
    scope.stop();
  });
});

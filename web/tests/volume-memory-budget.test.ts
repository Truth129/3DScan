import { describe, expect, it, vi } from 'vitest';
import type { VolumeDescriptor } from '../src/lib/ct-reslice';
import {
  defaultVolumeMemoryLimit,
  DESKTOP_VOLUME_MEMORY_BYTES,
  LOW_MEMORY_VOLUME_BYTES,
  VolumeMemoryBudget,
  VolumeMemoryBudgetError,
  volumeMemoryRequest,
} from '../src/lib/volume-memory-budget';

const descriptor = {
  key: 'source-content',
  reference_image_id: 'source-a',
  shape: [512, 512, 100],
  byte_length: 512 * 512 * 100 * 2,
  kind: 'ct',
  dtype: 'int16',
  slope: 1,
  intercept: 0,
} as VolumeDescriptor;
const request = (sourceKey = 'a') => ({ sourceKey, workerBytes: 100, cpuBytes: 20, gpuBytes: 80 });

describe('shared source-volume memory budget', () => {
  it('charges the worker once while retaining each renderer CPU and GPU copy', () => {
    const budget = new VolumeMemoryBudget(1000);
    const three = budget.reserve('three', request());
    const mpr = budget.reserve('mpr', { ...request(), cpuBytes: 200, gpuBytes: 200, overlayBytes: 50 });
    expect(budget.stats).toEqual({ limitBytes: 1000, usedBytes: 650, workerBytes: 100, cpuBytes: 220, gpuBytes: 280, overlayBytes: 50, consumers: 2 });
    three.release();
    expect(budget.stats.usedBytes).toBe(550);
    expect(budget.stats.workerBytes).toBe(100);
    mpr.release();
    expect(budget.stats.usedBytes).toBe(0);
  });

  it('does not share coincident grids from different immutable source images', () => {
    const budget = new VolumeMemoryBudget(1024 ** 3);
    budget.reserve('first', volumeMemoryRequest(descriptor, 'three'));
    budget.reserve('second', volumeMemoryRequest({ ...descriptor, reference_image_id: 'source-b' }, 'three'));
    expect(budget.stats.workerBytes).toBe(descriptor.byte_length * 2);
  });

  it('rejects over-budget replacement before mutation and preserves the usable allocation', () => {
    const budget = new VolumeMemoryBudget(400);
    const first = budget.reserve('three', request());
    const prior = budget.stats;
    expect(() => budget.reserve('mpr', { ...request(), cpuBytes: 300 })).toThrow(VolumeMemoryBudgetError);
    expect(() => budget.reserve('three', { ...request(), gpuBytes: 400 })).toThrow('volume budget');
    expect(budget.stats).toEqual(prior);
    first.release();
    expect(budget.stats.usedBytes).toBe(0);
  });

  it('releases hidden views idempotently, notifying only on change, without letting stale cleanup erase a new allocation', () => {
    const budget = new VolumeMemoryBudget(1000);
    const changed = vi.fn();
    const unsubscribe = budget.onChange(changed);
    const old = budget.reserve('three', request());
    const replacement = budget.reserve('three', request('b'));
    expect(changed).toHaveBeenCalledTimes(2);
    old.release();
    expect(budget.stats.usedBytes).toBe(200);
    replacement.release();
    replacement.release();
    expect(budget.stats.usedBytes).toBe(0);
    expect(changed).toHaveBeenCalledTimes(3);
    budget.reserve('three', request());
    budget.reserve('three', request());
    expect(changed).toHaveBeenCalledTimes(4);
    unsubscribe();
    budget.reserve('three', request('c'));
    expect(changed).toHaveBeenCalledTimes(4);
  });

  it('estimates retained Cornerstone copies, a bounded upload slab and scaled float samples', () => {
    const three = volumeMemoryRequest(descriptor, 'three');
    const mpr = volumeMemoryRequest(descriptor, 'cornerstone', 50);
    expect(three).toMatchObject({ workerBytes: descriptor.byte_length, cpuBytes: 8 * 1024 ** 2, gpuBytes: descriptor.byte_length });
    expect(mpr).toMatchObject({ workerBytes: descriptor.byte_length, cpuBytes: 8 * 1024 ** 2 + descriptor.byte_length, gpuBytes: descriptor.byte_length * 2 });
    expect(mpr.overlayBytes).toBe(50);
    // Retained copies stay charged after upload staging ends.
    expect(volumeMemoryRequest(descriptor, 'cornerstone', 50, false)).toEqual({ ...mpr, cpuBytes: descriptor.byte_length });
    const scaled = volumeMemoryRequest({ ...descriptor, slope: 0.5, intercept: -1024 }, 'cornerstone');
    const floating = volumeMemoryRequest({ ...descriptor, dtype: 'float32', byte_length: descriptor.byte_length * 2 }, 'cornerstone');
    expect(scaled.cpuBytes).toBe(8 * 1024 ** 2 + descriptor.byte_length * 2);
    expect(floating.cpuBytes).toBe(scaled.cpuBytes);
    expect(floating.gpuBytes).toBe(scaled.gpuBytes);
    expect(defaultVolumeMemoryLimit(4)).toBe(LOW_MEMORY_VOLUME_BYTES);
    expect(defaultVolumeMemoryLimit(8)).toBe(DESKTOP_VOLUME_MEMORY_BYTES);
  });

  it('rejects malformed byte estimates', () => {
    const budget = new VolumeMemoryBudget(1000);
    for (const bytes of [-1, Infinity, NaN, 1.5]) expect(() => budget.reserve('three', { ...request(), cpuBytes: bytes })).toThrow('reservation');
    expect(() => volumeMemoryRequest({ ...descriptor, byte_length: Infinity }, 'three')).toThrow('estimate');
  });
});

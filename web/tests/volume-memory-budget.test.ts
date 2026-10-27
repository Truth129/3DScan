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

  it('releases hidden views idempotently without letting stale async cleanup erase a new allocation', () => {
    const budget = new VolumeMemoryBudget(1000);
    const changed = vi.fn();
    const unsubscribe = budget.onChange(changed);
    const old = budget.reserve('three', request());
    const replacement = budget.reserve('three', request('b'));
    old.release();
    expect(budget.stats.usedBytes).toBe(200);
    replacement.release();
    replacement.release();
    expect(budget.stats.usedBytes).toBe(0);
    expect(changed).toHaveBeenCalledTimes(3);
    unsubscribe();
    budget.reserve('three', request());
    expect(changed).toHaveBeenCalledTimes(3);
  });

  it('estimates all retained Cornerstone copies and a bounded upload slab', () => {
    const three = volumeMemoryRequest(descriptor, 'three');
    const mpr = volumeMemoryRequest(descriptor, 'cornerstone', 50);
    expect(three.workerBytes).toBe(descriptor.byte_length);
    expect(three.cpuBytes).toBe(8 * 1024 ** 2);
    expect(three.gpuBytes).toBe(descriptor.byte_length);
    expect(mpr.workerBytes).toBe(descriptor.byte_length);
    expect(mpr.cpuBytes).toBe(8 * 1024 ** 2 + descriptor.byte_length);
    expect(mpr.gpuBytes).toBe(descriptor.byte_length * 2);
    expect(mpr.overlayBytes).toBe(50);
    expect(defaultVolumeMemoryLimit(2)).toBe(LOW_MEMORY_VOLUME_BYTES);
    expect(defaultVolumeMemoryLimit(4)).toBe(LOW_MEMORY_VOLUME_BYTES);
    expect(defaultVolumeMemoryLimit(8)).toBe(DESKTOP_VOLUME_MEMORY_BYTES);
  });

  it('reserves scaled float image samples only when the source needs rescaling', () => {
    const scaled = volumeMemoryRequest({ ...descriptor, slope: 0.5, intercept: -1024 }, 'cornerstone');
    const floating = volumeMemoryRequest({ ...descriptor, dtype: 'float32', byte_length: descriptor.byte_length * 2 }, 'cornerstone');
    expect(scaled.cpuBytes).toBe(8 * 1024 ** 2 + descriptor.byte_length * 2);
    expect(floating.cpuBytes).toBe(scaled.cpuBytes);
    expect(scaled.gpuBytes).toBe(floating.gpuBytes);
    expect(floating.workerBytes).toBe(descriptor.byte_length * 2);
  });

  it('keeps retained CT copies charged after bounded upload staging ends', () => {
    const uploading = volumeMemoryRequest(descriptor, 'cornerstone', 50);
    const retained = volumeMemoryRequest(descriptor, 'cornerstone', 50, false);
    expect(uploading.cpuBytes - retained.cpuBytes).toBe(8 * 1024 ** 2);
    expect(retained.workerBytes).toBe(uploading.workerBytes);
    expect(retained.gpuBytes).toBe(uploading.gpuBytes);
    expect(retained.overlayBytes).toBe(50);
    expect(retained.cpuBytes).toBe(descriptor.byte_length);
  });

  it('rejects malformed byte estimates and notifies only when memory changes', () => {
    const budget = new VolumeMemoryBudget(1000);
    const changed = vi.fn();
    budget.onChange(changed);
    const old = budget.reserve('three', request());
    const next = budget.reserve('three', request());
    expect(changed).toHaveBeenCalledOnce();
    old.release();
    next.release();
    expect(changed).toHaveBeenCalledTimes(2);
    for (const bytes of [-1, Infinity, NaN, 1.5]) expect(() => budget.reserve('three', { ...request(), cpuBytes: bytes })).toThrow('reservation');
    expect(() => volumeMemoryRequest({ ...descriptor, byte_length: Infinity }, 'three')).toThrow('estimate');
  });
});

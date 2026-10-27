import { acceptChunk, allocateVolume, probeVolume, resliceVolume } from './ct-reslice';
import type { ResidentOverlay, ResidentVolume, VolumeDescriptor } from './ct-reslice';
import type { Plane, Point } from '../types';
import { copyGPUVolumeSlab } from './gpu-volume-data';
import { assertReferenceImage } from './image-domain';

// The volume has one owner. Only completed slices or bounded upload slabs leave.
let ct: ResidentVolume | null = null;
let overlays: ResidentOverlay[] = [];
let masks = new Map<string, { volume: ResidentVolume; loaded: Set<number> }>();
let ctVersion = 0, overlayVersion = 0;
interface Command {
  id: number;
  type: 'ct' | 'overlay' | 'chunk' | 'slice' | 'probe' | 'gpu-slab';
  ctVersion: number;
  overlayVersion?: number;
  descriptor?: VolumeDescriptor;
  current?: VolumeDescriptor | null;
  parent?: VolumeDescriptor | null;
  overlays?: { id: string; current: VolumeDescriptor; parent: VolumeDescriptor | null }[];
  maxOverlayBytes?: number;
  slot?: string;
  maskKey?: string;
  index?: number;
  bytes?: ArrayBuffer;
  plane?: Plane;
  position?: number;
  ras?: Point;
  native?: { axis: 0 | 1 | 2; index: number };
  consumer?: 'ct' | 'model-review';
  includeOverlays?: boolean;
  volumeKey?: string;
  zStart?: number;
  depth?: number;
}
const scope = globalThis as unknown as {
  onmessage: ((event: MessageEvent<Command>) => void) | null;
  postMessage(message: unknown, transfer?: Transferable[]): void;
};

scope.onmessage = ({ data: command }) => {
  try {
    let result: unknown = null;
    const transfer: Transferable[] = [];
    if (command.type === 'ct') {
      // Release the old scan before requesting a replacement allocation.
      ct = null; overlays = []; masks.clear();
      ctVersion = command.ctVersion;
      overlayVersion = 0;
      ct = allocateVolume(command.descriptor!);
    } else {
      if (!ct || command.ctVersion !== ctVersion) throw new Error('Superseded local CT request');
      if (command.type === 'overlay') {
        const entries = command.overlays ?? (command.current
          ? [{ id: 'selected', current: command.current, parent: command.parent ?? null }] : []);
        const descriptors = new Map<string, VolumeDescriptor>();
        for (const entry of entries) {
          assertReferenceImage(ct.descriptor, entry.current);
          if (entry.parent) assertReferenceImage(ct.descriptor, entry.parent);
          descriptors.set(entry.current.key, entry.current);
          if (entry.parent) descriptors.set(entry.parent.key, entry.parent);
        }
        const allocatedBytes = [...descriptors.values()].reduce((sum, descriptor) => sum + descriptor.byte_length, 0);
        if (allocatedBytes > (command.maxOverlayBytes ?? 192 * 1024 ** 2)) throw new Error('Selected overlays exceed the local mask memory budget');
        // Drop deselected volumes before allocating replacements. Shared immutable
        // masks, including unchanged parent/current pairs, have only one owner.
        overlays = [];
        for (const key of masks.keys()) if (!descriptors.has(key)) masks.delete(key);
        for (const [key, descriptor] of descriptors) {
          if (!masks.has(key)) masks.set(key, { volume: allocateVolume(descriptor), loaded: new Set() });
        }
        overlayVersion = command.overlayVersion!;
        overlays = entries.map(entry => ({ id: entry.id, current: masks.get(entry.current.key)!.volume,
          parent: entry.parent ? masks.get(entry.parent.key)!.volume : null }));
        result = { allocatedBytes, missing: [...descriptors].map(([key, descriptor]) => ({ key,
          indices: descriptor.chunks.filter(chunk => !masks.get(key)!.loaded.has(chunk.index)).map(chunk => chunk.index) })) };
      } else if (command.type === 'chunk') {
        if (command.slot !== 'ct' && command.overlayVersion !== overlayVersion) throw new Error('Superseded local CT overlay');
        const mask = command.maskKey ? masks.get(command.maskKey) : null;
        const volume = command.slot === 'ct' ? ct : mask?.volume
          ?? (command.slot === 'current' ? overlays[0]?.current : overlays[0]?.parent);
        if (!volume) throw new Error('Local CT volume is unavailable');
        acceptChunk(volume, command.index!, command.bytes!);
        mask?.loaded.add(command.index!);
      } else if (command.type === 'slice') {
        const includeOverlays = command.includeOverlays !== false;
        if (includeOverlays && command.overlayVersion !== overlayVersion) throw new Error('Superseded local CT overlay');
        if (includeOverlays && [...masks.values()].some(mask => mask.loaded.size !== mask.volume.descriptor.chunks.length)) throw new Error('Local CT overlay is incomplete');
        const slice = resliceVolume(ct, command.plane!, command.position!, null, null, includeOverlays ? overlays : [], command.native);
        result = slice;
        const buffers = new Set<ArrayBuffer>([slice.hu.buffer as ArrayBuffer, slice.current.buffer as ArrayBuffer]);
        if (slice.parent) buffers.add(slice.parent.buffer as ArrayBuffer);
        for (const layer of slice.overlays ?? []) {
          buffers.add(layer.current.buffer as ArrayBuffer);
          if (layer.parent) buffers.add(layer.parent.buffer as ArrayBuffer);
        }
        transfer.push(...buffers);
      } else if (command.type === 'probe') result = probeVolume(ct, command.ras!);
      else if (command.type === 'gpu-slab') {
        if (command.slot !== 'ct' && command.overlayVersion !== overlayVersion) throw new Error('Superseded local CT overlay');
        const mask = command.slot === 'mask' ? masks.get(command.volumeKey!) : null;
        const volume = command.slot === 'ct' ? ct : mask?.volume;
        if (!volume || volume.descriptor.key !== command.volumeKey) throw new Error('Superseded local GPU volume');
        if (mask && mask.loaded.size !== volume.descriptor.chunks.length) throw new Error('Local CT overlay is incomplete');
        result = copyGPUVolumeSlab(volume, command.zStart!, command.depth!);
        transfer.push(result as ArrayBuffer);
      }
    }
    scope.postMessage({ id: command.id, result }, transfer);
  } catch (error) {
    scope.postMessage({ id: command.id, error: error instanceof Error ? error.message : String(error),
      allocation: error instanceof RangeError });
  }
};

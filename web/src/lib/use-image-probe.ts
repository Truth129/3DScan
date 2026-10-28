import { onScopeDispose, ref } from 'vue';
import { request } from './api';
import type { Point } from '../types';

interface ProbeSource {
  probe(point: Point): Promise<{ hu: number | null }>;
}

interface ImageProbeOptions {
  /** Revision source URL for remote samples. */
  base: () => string;
  point: () => Point;
  /** A full-resolution resident session answers locally and immediately. */
  local: () => ProbeSource | undefined;
}

const REMOTE_DEBOUNCE_MS = 120;

/** Sample the image under the crosshair one request at a time; the newest point wins. */
export function useImageProbe(options: ImageProbeOptions) {
  const hu = ref<number | null>(null);
  let generation = 0,
    next: { url: string; generation: number } | undefined,
    running = false,
    timer = 0,
    controller: AbortController | undefined,
    disposed = false;

  function probe() {
    const base = options.base();
    if (!base) return;
    const [x, y, z] = options.point();
    next = { url: `${base}/probe?x=${x}&y=${y}&z=${z}`, generation: ++generation };
    hu.value = null;
    clearTimeout(timer);
    timer = window.setTimeout(
      () => {
        timer = 0;
        void pump();
      },
      options.local() ? 0 : REMOTE_DEBOUNCE_MS,
    );
  }
  async function pump() {
    if (running) return;
    running = true;
    try {
      while (next && !disposed) {
        const current = next;
        next = undefined;
        controller = new AbortController();
        try {
          const local = options.local();
          const result = local ? await local.probe([...options.point()]) : await request<{ hu: number | null }>(current.url, { signal: controller.signal });
          if (current.generation === generation && !disposed) hu.value = result.hu;
        } catch {
          if (current.generation === generation && !disposed) hu.value = null;
        }
      }
    } finally {
      running = false;
      controller = undefined;
    }
  }
  /** Drop queued and in-flight samples, e.g. when the case or revision changes. */
  function invalidate() {
    generation++;
    next = undefined;
    controller?.abort();
    hu.value = null;
  }

  onScopeDispose(() => {
    disposed = true;
    clearTimeout(timer);
    invalidate();
  });
  return { hu, probe, invalidate };
}

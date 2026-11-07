import { computed, nextTick, onScopeDispose, ref, shallowRef, watch } from 'vue';
import { LocalCTSession, type LocalCTProgress } from './ct-volume';
import { preloadCTSource } from './ct-preload';

interface LocalCTOptions {
  /** Revision source URL; an empty base releases the session. */
  base: () => string;
  /** Image identity: a new base on the same grid keeps the resident frame. */
  grid: () => string;
  /** Mask selection preloaded alongside the source. */
  overlays: () => { ids: string[]; compare: boolean };
  /** Modality label used in error messages. */
  label: () => string;
  /** A session became ready, or the full-resolution copy replaced the preview. */
  onReady: () => void;
}

const FULL_OVERLAY_BYTES = 192 * 1024 ** 2;

function percent(progress?: LocalCTProgress) {
  return Math.round((100 * (progress?.loadedBytes || 0)) / Math.max(1, progress?.totalBytes || 1));
}

/** One resident source session: a small navigation preview first, then a full copy warmed in the background. */
export function useLocalCT(options: LocalCTOptions) {
  const localCT = shallowRef<LocalCTSession>(),
    localCTReady = ref(false),
    localCTFull = ref(false);
  const ctProgress = ref<LocalCTProgress>(),
    fullCTProgress = ref<LocalCTProgress>();
  const ctLocalError = ref(''),
    ctOverlayError = ref(''),
    ctOverlayRetrying = ref(false),
    ctCaching = ref(false);
  let generation = 0,
    fullCandidate: LocalCTSession | undefined,
    preparedGrid = '',
    disposed = false;

  async function prepareLocalCT(url: string) {
    const id = ++generation,
      grid = options.grid();
    const retainFrame = !!url && !!localCT.value && localCTReady.value && preparedGrid === grid;
    cancelFullCT();
    ctLocalError.value = '';
    ctOverlayError.value = '';
    ctProgress.value = undefined;
    if (!retainFrame) {
      localCTReady.value = false;
      localCT.value?.dispose();
      localCT.value = undefined;
      localCTFull.value = false;
    }
    preparedGrid = grid;
    if (!url) return;
    let session = localCT.value;
    try {
      if (session) void session.setBase(url);
      else {
        session = new LocalCTSession(url, {
          quality: 'preview',
          // Mask downloads report through here too. Only CT chunks move the percentage, which
          // otherwise blinked off at each mask report and reflowed the toolbar.
          onProgress: value => {
            if (localCT.value === session && value.stage === 'ct') ctProgress.value = value;
          },
          onOverlayError: reason => {
            if (session && localCT.value === session) ctOverlayError.value = reason?.message ?? '';
          },
          onError: reason => {
            if (session && localCT.value === session) failLocalCT(reason.message);
          },
        });
        localCT.value = session;
      }
      // Start the selected mask alongside the CT. Neither download depends on a
      // slice component mounting or the first slider/crosshair interaction.
      const active = session;
      if (!(await preloadCTSource(active, options.overlays, () => id === generation && !disposed && localCT.value === active))) return;
      localCTReady.value = true;
      options.onReady();
      // Show the small navigation volume first, then warm exact local review in
      // the background using the existing bounded, low-priority download path.
      await nextTick();
      if (id === generation && !disposed) void cacheFullCT();
    } catch (reason) {
      if (id !== generation || disposed) return;
      session?.dispose();
      localCT.value = undefined;
      localCTFull.value = false;
      localCTReady.value = false;
      ctLocalError.value = reason instanceof Error ? reason.message : `Local ${options.label()} navigation is unavailable. Retry loading the source images.`;
    }
  }
  function failLocalCT(reason: string) {
    generation++;
    cancelFullCT();
    localCT.value?.dispose();
    localCT.value = undefined;
    localCTReady.value = false;
    localCTFull.value = false;
    ctLocalError.value = reason;
    ctOverlayError.value = '';
  }
  function cancelFullCT() {
    fullCandidate?.dispose();
    fullCandidate = undefined;
    ctCaching.value = false;
    fullCTProgress.value = undefined;
  }
  async function cacheFullCT() {
    const base = options.base();
    if (!base || !localCTReady.value || localCTFull.value || ctCaching.value) return;
    const id = generation;
    ctCaching.value = true;
    ctLocalError.value = '';
    let candidate: LocalCTSession | undefined;
    try {
      candidate = new LocalCTSession(base, {
        quality: 'full',
        maxOverlayBytes: FULL_OVERLAY_BYTES,
        onProgress: value => {
          if (fullCandidate === candidate && value.stage === 'ct') fullCTProgress.value = value;
        },
        onOverlayError: reason => {
          if (candidate && localCT.value === candidate) ctOverlayError.value = reason?.message ?? '';
        },
        onError: reason => {
          if (candidate && localCT.value === candidate) failLocalCT(reason.message);
        },
      });
      fullCandidate = candidate;
      const active = candidate;
      if (!(await preloadCTSource(active, options.overlays, () => id === generation && !disposed && fullCandidate === active))) return;
      if (disposed || id !== generation || candidate !== fullCandidate) {
        candidate.dispose();
        return;
      }
      const previous = localCT.value;
      localCT.value = candidate;
      localCTFull.value = true;
      fullCandidate = undefined;
      ctCaching.value = false;
      fullCTProgress.value = undefined;
      ctOverlayError.value = candidate.overlayError?.message ?? '';
      previous?.dispose();
      options.onReady();
    } catch (reason) {
      if (candidate !== fullCandidate || disposed || id !== generation) return;
      candidate?.dispose();
      fullCandidate = undefined;
      ctCaching.value = false;
      ctLocalError.value = `Full ${options.label()} caching was unavailable. Fast navigation remains active. ${reason instanceof Error ? reason.message : ''}`;
    }
  }
  async function retryCTOverlays() {
    const session = localCT.value;
    if (!session || ctOverlayRetrying.value) return;
    ctOverlayRetrying.value = true;
    try {
      const { ids, compare } = options.overlays();
      await session.retryOverlay(ids, compare);
    } catch {
      /* The session reports mask errors without replacing its source image. */
    } finally {
      ctOverlayRetrying.value = false;
    }
  }

  watch(
    options.base,
    value => {
      void prepareLocalCT(value);
    },
    { immediate: true },
  );
  onScopeDispose(() => {
    disposed = true;
    generation++;
    localCT.value?.dispose();
    cancelFullCT();
  });
  return {
    localCT,
    localCTReady,
    localCTFull,
    ctProgress,
    fullCTProgress,
    ctLocalError,
    ctOverlayError,
    ctOverlayRetrying,
    ctCaching,
    ctDeliveryMode: computed(() => (localCTReady.value ? (localCTFull.value ? 'full' : 'progressive') : ctLocalError.value ? 'error' : 'loading')),
    ctPercent: computed(() => percent(ctProgress.value)),
    fullCTPercent: computed(() => percent(fullCTProgress.value)),
    prepareLocalCT,
    cacheFullCT,
    cancelFullCT,
    retryCTOverlays,
  };
}

import { computed, onScopeDispose, ref, type Ref } from 'vue';
import { api } from './api';
import { CinematicReferenceGate } from './cinematic-reference';
import { cinematicReferenceError, CINEMATIC_HEART_PRESET, freshVolumeSettings, type CTVolumeSettings } from './volume-presets';

/** Own measurement cancellation separately from renderer and source lifetimes. */
export function useCinematicReference(options: {
  source: () => { caseId?: string; revisionId?: string };
  settings: Ref<CTVolumeSettings>;
  available: Ref<boolean>;
  changed: () => void;
}) {
  const loading = ref(false),
    message = ref(''),
    error = ref(false);
  const key = computed(() => {
    const { caseId, revisionId } = options.source();
    return caseId && revisionId ? JSON.stringify([caseId, revisionId]) : '';
  });
  const gate = new CinematicReferenceGate();
  let controller: AbortController | undefined,
    disposed = false;

  function cancel() {
    controller?.abort();
    controller = undefined;
    loading.value = false;
  }
  function selectSource() {
    gate.select(key.value);
    cancel();
    message.value = '';
    error.value = false;
  }
  function setSettings(settings: CTVolumeSettings) {
    const current = options.settings.value.cinematic;
    const manual = settings.cinematic.myocardiumHU !== current.myocardiumHU || settings.cinematic.bloodHU !== current.bloodHU;
    if (manual) gate.edited(key.value);
    const cancelled = loading.value;
    cancel();
    if (manual) {
      message.value = 'Using manually selected reference HU. Automatic measurement will not replace this pair.';
      error.value = false;
    } else if (cancelled) {
      message.value = 'Reference measurement cancelled. The current HU pair is unchanged; use Measure from segments to retry, or enter both values manually.';
      error.value = false;
    }
    options.settings.value = freshVolumeSettings(settings);
    options.changed();
  }
  async function measure(automatic = false) {
    if (!options.available.value || options.settings.value.preset !== CINEMATIC_HEART_PRESET) return;
    cancel();
    const request = new AbortController(),
      { caseId, revisionId } = options.source();
    if (!caseId || !revisionId) return;
    controller = request;
    loading.value = true;
    message.value = automatic ? 'Measuring myocardium and blood-pool HU from this segmentation…' : '';
    error.value = false;
    try {
      const measured = await api.cinematicReference(caseId, revisionId, request.signal);
      const source = options.source();
      if (request.signal.aborted || disposed || caseId !== source.caseId || revisionId !== source.revisionId) return;
      const invalid = cinematicReferenceError(measured.myocardium_hu, measured.blood_hu);
      if (invalid) throw new Error(`${invalid} Set both reference values manually.`);
      options.settings.value = freshVolumeSettings({
        ...options.settings.value,
        cinematic: { ...options.settings.value.cinematic, myocardiumHU: measured.myocardium_hu, bloodHU: measured.blood_hu },
      });
      message.value = `Measured medians: myocardium ${measured.myocardium_hu.toFixed(1)} HU; ${measured.blood_source === 'aorta' ? 'aorta' : 'left heart'} ${measured.blood_hu.toFixed(1)} HU.`;
      options.changed();
    } catch (reason) {
      if (request.signal.aborted || disposed) return;
      error.value = true;
      message.value = `${reason instanceof Error ? reason.message : 'Reference measurement failed.'} The current reference HU pair is unchanged; enter both values manually or retry measurement.`;
    } finally {
      if (controller === request) {
        controller = undefined;
        loading.value = false;
      }
    }
  }

  onScopeDispose(() => {
    disposed = true;
    cancel();
  });
  return { key, loading, message, error, cancel, selectSource, setSettings, measure, claimAutomatic: () => gate.claim(key.value) };
}

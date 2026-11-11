<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue';
import {
  cinematicReferenceError,
  CINEMATIC_HEART_PRESET,
  MR_SIGNAL_PRESET,
  freshVolumeSettings,
  getVolumePreset,
  presetSwatch,
  VOLUME_PRESET_GROUPS,
  VOLUME_PRESET_LABELS,
  type CTVolumePresetId,
  type CTVolumeSettings,
} from '../lib/volume-presets';
import { freshVolumeMaskSettings, MAX_VOLUME_MASK_SEGMENTS, visibleVolumeMaskIds, type VolumeMaskSettings } from '../lib/volume-mask';
import { colorCss, prettyName } from '../lib/geometry';
import { freshVolumeColorSettings, MAX_VOLUME_COLOR_SEGMENTS, type VolumeColorSettings } from '../lib/volume-colors';
import type { Structure, StructureDisplay } from '../types';
import NativeVolumeLightingControls from './NativeVolumeLightingControls.vue';
import VolumeSection from './VolumeSection.vue';
import InfoTip from './InfoTip.vue';
import { ChevronDown, ChevronUp } from '@lucide/vue';
import { freshNativeVolumeLighting, type NativeVolumeLightingSettings } from '../lib/volume-lighting';

const props = withDefaults(
  defineProps<{
    enabled: boolean;
    settings: CTVolumeSettings;
    nativeLighting?: NativeVolumeLightingSettings;
    modality?: string;
    /** The state line beside the title while the volume shows, such as 'Visible · Cornerstone'. */
    status?: string;
    ready?: boolean;
    loading?: boolean;
    message?: string;
    disabled?: boolean;
    referenceAvailable?: boolean;
    referenceLoading?: boolean;
    referenceMessage?: string;
    referenceError?: boolean;
    heartViewAvailable?: boolean;
    mask?: VolumeMaskSettings;
    structures?: readonly Structure[];
    selected?: string;
    display?: StructureDisplay;
    maskLoading?: boolean;
    maskMessage?: string;
    maskError?: boolean;
    maskMarginFixed?: boolean;
    /** Review masks by the saved revision, which the draft does not change. */
    review?: boolean;
    /** Review leaves Cinematic heart out, because its reference HU comes from the masks being edited. */
    cinematicAvailable?: boolean;
    colors?: VolumeColorSettings;
    /** Segment colours need the Cornerstone3D renderer, outside review. */
    colorsAvailable?: boolean;
    /** Why they are off here, shown on the disabled section. */
    colorsReason?: string;
    colorsLoading?: boolean;
    colorsError?: string;
  }>(),
  {
    mask: () => freshVolumeMaskSettings(),
    structures: () => [],
    selected: '',
    display: () => ({}),
    cinematicAvailable: true,
    colors: () => freshVolumeColorSettings(),
  },
);
const emit = defineEmits<{
  'update:enabled': [value: boolean];
  'update:settings': [value: CTVolumeSettings];
  'update:nativeLighting': [value: NativeVolumeLightingSettings];
  'update:mask': [value: VolumeMaskSettings];
  'retry-mask': [];
  'measure-reference': [];
  'heart-view': [];
  retry: [];
  'update:colors': [value: VolumeColorSettings];
  'retry-colors': [];
}>();
const tip = useId();
const axes = ['i', 'j', 'k'] as const;
// The sections stay listed while the volume is off, greyed, so turning it on finds them where they were.
const inert = computed(() => !!props.disabled || !props.enabled);
const maskOpen = ref(props.mask.enabled),
  maskSearch = ref('');
const maskIds = computed(() => new Set(props.mask.ids));
const maskStructures = computed(() => {
  const query = maskSearch.value.trim().toLowerCase().replaceAll('_', ' ');
  return props.structures.filter(
    structure => !query || `${prettyName(structure.name)} ${prettyName(structure.group)} ${structure.task}`.toLowerCase().includes(query),
  );
});
const currentMaskTarget = computed(() => props.structures.some(structure => structure.id === props.selected));
const visibleMaskIds = computed(() => visibleVolumeMaskIds(props.structures, props.display));
const maskCanRetry = computed(
  () =>
    props.maskError &&
    !props.maskLoading &&
    props.mask.ids.length > 0 &&
    props.mask.ids.length <= MAX_VOLUME_MASK_SEGMENTS &&
    props.mask.ids.every(id => props.structures.some(structure => structure.id === id)),
);
const signalVolume = computed(() => !!props.modality && props.modality !== 'CT');
const presetGroups = computed(() =>
  signalVolume.value ? [] : VOLUME_PRESET_GROUPS.filter(group => props.cinematicAvailable || !group.ids.some(id => id === CINEMATIC_HEART_PRESET)),
);
const modalityLabel = computed(() => props.modality ?? 'CT');
// The swatches cover the presets made for CT. The menu still lists the others.
const swatches = computed(() =>
  presetGroups.value
    .filter(group => group.label !== 'MR' && group.label !== 'Other modalities')
    .flatMap(group => group.ids)
    .map(id => ({ id, label: VOLUME_PRESET_LABELS[id], background: presetSwatch(getVolumePreset(id, props.settings.cinematic)) })),
);
const intensityLabel = computed(() => `${props.settings.shift > 0 ? '+' : ''}${props.settings.shift} HU`);
const opacityLabel = computed(() => `${Math.round(props.settings.density * 100)}%`);
const preset = computed(() => getVolumePreset(props.settings.preset, props.settings.cinematic, props.settings.signal_window));
const cinematic = computed(() => props.settings.preset === CINEMATIC_HEART_PRESET);
const myocardiumInput = ref(String(props.settings.cinematic.myocardiumHU)),
  bloodInput = ref(String(props.settings.cinematic.bloodHU));
const referenceInputError = ref('');
const otherModality = computed(() => !signalVolume.value && !preset.value.sourcePreset.startsWith('CT-'));
const presetHelp = computed(() =>
  signalVolume.value
    ? 'Grayscale uses the linked slice window and level. MR signal values depend on the sequence; they are not HU.'
    : preset.value.sourcePreset.endsWith('-MIP')
      ? `Slicer’s ${preset.value.label} appearance, shown with composite volume rendering.`
      : '',
);
const stateLine = computed(() => (!props.enabled ? 'Off' : props.status || (props.loading ? 'Preparing…' : props.ready ? 'Visible' : 'Waiting')));
const stateMessage = computed(
  () => props.message || (props.loading ? `Preparing ${modalityLabel.value} volume…` : `Waiting for the full ${modalityLabel.value} to finish loading.`),
);
const lightingLabel = computed(() => {
  const mode = props.nativeLighting?.mode;
  return mode === 'cinematic' ? 'Cinematic' : mode === 'standard' ? 'Standard' : mode === 'ambient-occlusion' ? 'Ambient occlusion' : 'Preset lighting';
});
const cropped = computed(() => props.settings.crop[0].some(value => value > 0) || props.settings.crop[1].some(value => value < 1));
const segments = (count: number) => `${count} ${count === 1 ? 'segment' : 'segments'}`;
const maskSummary = computed(() =>
  !props.mask.enabled
    ? props.structures.length
      ? 'Off'
      : 'No segments yet'
    : `${props.mask.mode === 'outside' ? 'Outside' : 'Inside'} · ${segments(props.mask.ids.length)} · ${props.mask.margin_mm.toFixed(1)} mm`,
);
const colorsSummary = computed(() =>
  !props.colorsAvailable
    ? props.review
      ? 'Off in review'
      : 'Unavailable here'
    : props.colors.enabled
      ? `${segments(props.colors.ids.length)} · ${Math.round(props.colors.strength * 100)}%`
      : props.structures.length
        ? 'Off'
        : 'No segments yet',
);

/*
 * Each section's open state is the reader's, per browser. A section also opens by itself when what
 * it holds changes from its default, without saving that, so a folded section never hides a live setting.
 */
type SectionId = 'appearance' | 'lighting' | 'mask' | 'colors' | 'crop';
const SECTIONS_KEY = 'scan3d-volume-sections-v1';
const SECTION_DEFAULTS: Record<SectionId, boolean> = { appearance: true, lighting: false, mask: false, colors: false, crop: false };
function readSections(): Record<SectionId, boolean> {
  const sections = { ...SECTION_DEFAULTS };
  try {
    const saved: unknown = JSON.parse(globalThis.localStorage?.getItem(SECTIONS_KEY) ?? 'null');
    if (saved && typeof saved === 'object')
      for (const id of Object.keys(sections) as SectionId[]) {
        const value = (saved as Record<string, unknown>)[id];
        if (typeof value === 'boolean') sections[id] = value;
      }
  } catch {
    /* Sections fall back to their defaults without storage. */
  }
  return sections;
}
const remembered = readSections();
const sections = ref({ ...remembered });
function toggleSection(id: SectionId, open: boolean) {
  sections.value = { ...sections.value, [id]: open };
  remembered[id] = open;
  try {
    globalThis.localStorage?.setItem(SECTIONS_KEY, JSON.stringify(remembered));
  } catch {
    /* Sections fall back to their defaults without storage. */
  }
}
function openWhen(id: SectionId, changed: () => boolean) {
  watch(
    changed,
    value => {
      if (value) sections.value = { ...sections.value, [id]: true };
    },
    { immediate: true },
  );
}
openWhen('lighting', () => !!props.nativeLighting && props.nativeLighting.mode !== 'preset');
openWhen('mask', () => props.mask.enabled);
openWhen('colors', () => !!props.colorsAvailable && props.colors.enabled);
openWhen('crop', () => cropped.value);

function update(change: Partial<CTVolumeSettings>) {
  emit('update:settings', freshVolumeSettings({ ...props.settings, ...change }));
}
function number(event: Event) {
  return Number((event.target as HTMLInputElement).value);
}
function changePreset(event: Event) {
  update({ preset: (event.target as HTMLSelectElement).value as CTVolumePresetId, shift: 0, density: 1 });
}
function applyReference() {
  const myocardiumHU = String(myocardiumInput.value).trim() ? Number(myocardiumInput.value) : NaN;
  const bloodHU = String(bloodInput.value).trim() ? Number(bloodInput.value) : NaN;
  referenceInputError.value = cinematicReferenceError(myocardiumHU, bloodHU);
  if (!referenceInputError.value) update({ cinematic: { ...props.settings.cinematic, myocardiumHU, bloodHU } });
}
function changeCrop(axis: number, end: 0 | 1, event: Event) {
  const crop = freshVolumeSettings(props.settings).crop;
  crop[end][axis] = end === 0 ? Math.min(number(event), crop[1][axis] - 0.01) : Math.max(number(event), crop[0][axis] + 0.01);
  update({ crop });
}
function reset() {
  emit('update:settings', freshVolumeSettings({ preset: props.settings.preset, signal_window: props.settings.signal_window }));
}
function updateMask(change: Partial<VolumeMaskSettings>) {
  emit('update:mask', freshVolumeMaskSettings({ ...props.mask, ...change }, props.structures));
}
const colorsOpen = ref(false),
  colorSearch = ref('');
const colorIds = computed(() => new Set(props.colors.ids));
const colorStructures = computed(() => {
  const query = colorSearch.value.trim().toLowerCase().replaceAll('_', ' ');
  return props.structures.filter(
    structure => !query || `${prettyName(structure.name)} ${prettyName(structure.group)} ${structure.task}`.toLowerCase().includes(query),
  );
});
function updateColors(change: Partial<VolumeColorSettings>) {
  emit('update:colors', freshVolumeColorSettings({ ...props.colors, ...change }, props.structures));
}
function toggleColor(id: string, checked: boolean) {
  updateColors({ ids: checked ? [...props.colors.ids, id] : props.colors.ids.filter(value => value !== id) });
}
function enableColors(enabled: boolean) {
  // Starting with the selected segment gives an immediate, predictable result.
  const ids = enabled && !props.colors.ids.length && currentMaskTarget.value ? [props.selected] : props.colors.ids;
  updateColors({ enabled, ids });
  colorsOpen.value = enabled;
}
function toggleMask(id: string, checked: boolean) {
  updateMask({ ids: checked ? [...props.mask.ids, id] : props.mask.ids.filter(value => value !== id) });
}
function useMaskIds(ids: string[]) {
  updateMask({ ids });
  maskOpen.value = false;
}
watch([() => props.settings.cinematic.myocardiumHU, () => props.settings.cinematic.bloodHU], ([myocardium, blood]) => {
  myocardiumInput.value = String(myocardium);
  bloodInput.value = String(blood);
  referenceInputError.value = '';
});
watch(
  () => props.mask.enabled,
  enabled => {
    maskOpen.value = enabled;
  },
);
watch(
  () => props.maskError,
  failed => {
    if (failed) maskOpen.value = false;
  },
);
</script>

<template>
  <section class="volume-controls" :class="{ enabled, off: !enabled }" :aria-label="`${modalityLabel} volume rendering controls`">
    <div class="volume-controls-header">
      <div class="volume-title">
        <strong>{{ modalityLabel }} volume</strong
        ><span class="volume-state" :class="{ problem: enabled && !loading && !ready && !!message }">{{ stateLine }}</span>
      </div>
      <!-- The Layers card keeps the only Show checkbox; this header turns the volume on without a second one. -->
      <button
        v-if="!enabled"
        type="button"
        class="text-button volume-turn-on"
        :aria-label="`Turn on ${modalityLabel} volume`"
        :disabled="disabled"
        @click="emit('update:enabled', true)"
      >
        Turn on
      </button>
      <button v-else type="button" class="text-button volume-reset" title="Reset this preset's appearance and crop" @click="reset">Reset volume</button>
    </div>

    <VolumeSection title="Appearance" :summary="preset.label" :open="sections.appearance" @toggle="toggleSection('appearance', $event)">
      <div class="volume-row">
        <label :for="`${tip}-preset`">Preset</label
        ><select
          :id="`${tip}-preset`"
          :aria-label="`${modalityLabel} volume preset`"
          :aria-describedby="presetHelp ? `${tip}-preset-help` : undefined"
          :value="preset.id"
          :disabled="inert"
          @change="changePreset"
        >
          <option v-if="signalVolume" :value="MR_SIGNAL_PRESET">Signal window (grayscale)</option>
          <optgroup v-for="group in presetGroups" :key="group.label" :label="group.label">
            <option v-for="id in group.ids" :key="id" :value="id">{{ VOLUME_PRESET_LABELS[id] }}</option>
          </optgroup></select
        ><InfoTip v-if="presetHelp" :id="`${tip}-preset-help`" :text="presetHelp" />
      </div>
      <!-- The Preset menu above is the keyboard's way to every preset, so the swatches take no Tab stops. -->
      <div v-if="swatches.length" class="volume-swatches" role="group" :aria-label="`${modalityLabel} volume preset swatches`">
        <button
          v-for="swatch in swatches"
          :key="swatch.id"
          type="button"
          class="volume-swatch"
          tabindex="-1"
          :style="{ backgroundImage: swatch.background }"
          :aria-label="swatch.label"
          :title="swatch.label"
          :aria-pressed="preset.id === swatch.id"
          :disabled="inert"
          @click="update({ preset: swatch.id, shift: 0, density: 1 })"
        ></button>
      </div>
      <p v-if="otherModality" class="volume-note">Made for another modality; it may need adjusting for CT.</p>
      <label v-if="!signalVolume" class="volume-row"
        ><span>Intensity</span
        ><input
          type="range"
          aria-label="CT volume intensity shift"
          min="-500"
          max="500"
          step="5"
          :value="settings.shift"
          :disabled="inert"
          @input="update({ shift: number($event) })"
        /><output>{{ intensityLabel }}</output></label
      >
      <label class="volume-row"
        ><span>Volume opacity</span
        ><input
          type="range"
          :aria-label="`${modalityLabel} volume opacity`"
          :aria-valuetext="opacityLabel"
          min="0.1"
          max="3"
          step="0.05"
          :value="settings.density"
          :disabled="inert"
          @input="update({ density: number($event) })"
        /><output>{{ opacityLabel }}</output></label
      >
      <label class="volume-row"
        ><span>Quality</span
        ><select
          :aria-label="`${modalityLabel} volume quality`"
          :value="settings.quality"
          :disabled="inert"
          @change="update({ quality: ($event.target as HTMLSelectElement).value as 'auto' | 'detail' })"
        >
          <option value="auto">Auto</option>
          <option value="detail">Detail</option>
        </select></label
      >
      <div v-if="cinematic && !signalVolume" class="volume-cinematic" role="group" aria-label="Cinematic heart appearance" :aria-describedby="`${tip}-heart`">
        <div class="volume-subhead">
          Heart<InfoTip
            :id="`${tip}-heart`"
            text="Crimson myocardium and ivory blood. For an isolated heart, turn on Limit to segments and choose its segments."
          />
        </div>
        <div v-if="heartViewAvailable" class="volume-row">
          <span>View</span
          ><button
            class="button secondary compact"
            type="button"
            aria-label="Use Slicer heart orientation"
            :aria-describedby="`${tip}-heart-view`"
            :disabled="inert"
            @click="emit('heart-view')"
          >
            Heart view</button
          ><InfoTip
            :id="`${tip}-heart-view`"
            text="Faces the same side as the supplied Slicer scene. Oblique shows a different face and can show brighter blood or chamber interiors."
          />
        </div>
        <!-- prettier-ignore -->
        <label class="volume-row"><span>Myocardium</span><input v-model="myocardiumInput" type="number" aria-label="Myocardium reference HU" min="-500" max="1500" step="1" :disabled="inert || referenceLoading" /><small>HU</small></label>
        <!-- prettier-ignore -->
        <label class="volume-row"><span>Blood pool</span><input v-model="bloodInput" type="number" aria-label="Blood pool reference HU" min="-400" max="3000" step="1" :disabled="inert || referenceLoading" /><small>HU</small></label>
        <div class="volume-actions">
          <button
            class="button secondary compact"
            type="button"
            :aria-describedby="`${tip}-reference`"
            :disabled="inert || referenceLoading"
            @click="applyReference"
          >
            Apply reference HU</button
          ><button
            v-if="referenceAvailable"
            class="button secondary compact"
            type="button"
            :aria-describedby="`${tip}-reference`"
            :disabled="inert || referenceLoading"
            @click="emit('measure-reference')"
          >
            {{ referenceLoading ? 'Measuring…' : 'Measure from segments' }}</button
          ><InfoTip
            :id="`${tip}-reference`"
            text="Starts at 80 / 400 HU. Measure from myocardium and aorta or left-heart segments when available, or enter both values. They change appearance only."
          />
        </div>
        <p v-if="referenceInputError" class="volume-message problem" role="alert">{{ referenceInputError }}</p>
        <p v-if="referenceMessage" class="volume-message" :class="{ problem: referenceError }" :role="referenceError ? 'alert' : 'status'">
          {{ referenceMessage }}
        </p>
        <template v-if="!nativeLighting || nativeLighting.mode === 'preset'">
          <label class="volume-row"
            ><span>Shadows</span
            ><input
              type="range"
              aria-label="Cinematic shadows"
              :aria-describedby="nativeLighting ? undefined : `${tip}-shadows`"
              min="0"
              max="2"
              step="0.1"
              :value="settings.cinematic.shadows"
              :disabled="inert"
              @input="update({ cinematic: { ...settings.cinematic, shadows: number($event) } })"
            /><output>{{ settings.cinematic.shadows === 0 ? 'Off' : settings.cinematic.shadows.toFixed(1) }}</output></label
          >
          <label class="volume-row"
            ><span>Shadow reach</span
            ><input
              type="range"
              aria-label="Cinematic shadow reach"
              min="0"
              max="1"
              step="0.05"
              :value="settings.cinematic.shadowReach"
              :disabled="inert || settings.cinematic.shadows === 0"
              @input="update({ cinematic: { ...settings.cinematic, shadowReach: number($event) } })"
            /><output>{{ Math.round(settings.cinematic.shadowReach * 100) }}%</output></label
          >
          <InfoTip
            v-if="!nativeLighting"
            :id="`${tip}-shadows`"
            text="Shadows settle after rotation stops; dragging uses a lighter preview. This is an interactive WebGL approximation of the supplied Slicer look."
          />
        </template>
      </div>
    </VolumeSection>

    <VolumeSection
      v-if="nativeLighting && !signalVolume"
      title="Lighting"
      :summary="lightingLabel"
      :open="sections.lighting"
      @toggle="toggleSection('lighting', $event)"
    >
      <template #actions>
        <button type="button" class="text-button volume-section-action" :disabled="inert" @click="emit('update:nativeLighting', freshNativeVolumeLighting())">
          Reset lighting
        </button>
      </template>
      <NativeVolumeLightingControls :settings="nativeLighting" :disabled="inert" @update="emit('update:nativeLighting', $event)" />
    </VolumeSection>

    <VolumeSection title="Limit to segments" :summary="maskSummary" :open="sections.mask" @toggle="toggleSection('mask', $event)">
      <template #actions>
        <input
          type="checkbox"
          role="switch"
          class="volume-switch"
          :aria-label="`Mask ${modalityLabel} volume to segmentation`"
          :title="structures.length ? `Show the ${modalityLabel} volume only inside or outside chosen segments.` : 'Run segmentation first to create masks.'"
          :checked="mask.enabled"
          :disabled="inert || !structures.length"
          @change="updateMask({ enabled: ($event.target as HTMLInputElement).checked })"
        />
      </template>
      <p v-if="review" class="volume-note">In review, the mask follows the saved revision, not your draft.</p>
      <template v-if="mask.enabled">
        <label class="volume-row"
          ><span>Show</span
          ><select
            :aria-label="`${modalityLabel} volume mask region`"
            :value="mask.mode"
            :disabled="inert"
            @change="updateMask({ mode: ($event.target as HTMLSelectElement).value as VolumeMaskSettings['mode'] })"
          >
            <option value="inside">Inside segments</option>
            <option value="outside">Outside segments</option>
          </select></label
        >
        <div class="volume-row">
          <span>Segments</span
          ><button
            type="button"
            class="text-button volume-picker-toggle"
            :aria-expanded="maskOpen"
            aria-label="Choose volume mask segments"
            @click="maskOpen = !maskOpen"
          >
            {{ maskOpen ? 'Done' : 'Choose segments' }} · {{ mask.ids.length }}<ChevronUp v-if="maskOpen" /><ChevronDown v-else />
          </button>
        </div>
        <div v-if="maskOpen" class="volume-picker">
          <div class="volume-actions">
            <button
              type="button"
              class="button secondary compact"
              :disabled="inert || !currentMaskTarget"
              title="Use the segment selected in the anatomy list"
              @click="useMaskIds([selected])"
            >
              Use selected</button
            ><button
              type="button"
              class="button secondary compact"
              :disabled="inert || !visibleMaskIds.length"
              title="Use a snapshot of all currently visible segments"
              @click="useMaskIds(visibleMaskIds)"
            >
              Use visible</button
            ><button type="button" class="text-button" :disabled="inert || !mask.ids.length" @click="updateMask({ ids: [] })">Clear</button
            ><InfoTip
              :id="`${tip}-mask`"
              :text="`Chosen segments form one region. This mask changes only the ${modalityLabel} volume; ${modalityLabel} slices stay unchanged.`"
            />
          </div>
          <input
            v-model="maskSearch"
            class="volume-search"
            type="search"
            aria-label="Find volume mask segments"
            :aria-describedby="`${tip}-mask`"
            placeholder="Find segments…"
            :disabled="inert"
          />
          <div class="volume-segments" role="group" aria-label="Volume mask segments">
            <label v-for="structure in maskStructures" :key="structure.id" class="volume-mask-segment"
              ><input
                type="checkbox"
                :aria-label="`Mask segment ${prettyName(structure.name)}`"
                :checked="maskIds.has(structure.id)"
                :disabled="inert"
                @change="toggleMask(structure.id, ($event.target as HTMLInputElement).checked)"
              /><i :style="{ background: colorCss(structure.color) }" aria-hidden="true"></i><span>{{ prettyName(structure.name) }}</span
              ><small>{{ prettyName(structure.group) }}</small></label
            >
            <p v-if="!maskStructures.length" class="volume-note">No matching segments.</p>
          </div>
        </div>
        <div class="volume-row volume-margin">
          <span>Margin</span
          ><input
            type="range"
            :aria-label="`${modalityLabel} volume mask margin`"
            :aria-describedby="`${tip}-margin`"
            min="0"
            max="5"
            step="0.5"
            :value="mask.margin_mm"
            :disabled="inert || maskMarginFixed"
            @input="updateMask({ margin_mm: number($event) })"
          /><output>{{ mask.margin_mm.toFixed(1) }} mm</output
          ><button
            v-if="!maskMarginFixed"
            class="text-button"
            type="button"
            aria-label="Use 2.5 mm volume mask margin"
            :disabled="inert"
            @click="updateMask({ margin_mm: 2.5 })"
          >
            2.5 mm</button
          ><InfoTip
            v-if="!maskMarginFixed"
            :id="`${tip}-margin`"
            :text="`Grows the display region to keep thin walls and surface vessels. ${modalityLabel} overlays and segmentation measurements stay unchanged.`"
          />
        </div>
        <p v-if="maskMarginFixed" :id="`${tip}-margin`" class="volume-note">This offline export retains its prepared display margin.</p>
        <p v-if="!mask.ids.length" class="volume-message" role="status">
          Choose at least one segment. The {{ modalityLabel }} volume stays hidden until a mask is ready.
        </p>
        <p v-else-if="mask.ids.length > MAX_VOLUME_MASK_SEGMENTS" class="volume-message" role="status">
          Choose up to {{ MAX_VOLUME_MASK_SEGMENTS }} segments. The {{ modalityLabel }} volume stays hidden until you reduce the selection.
        </p>
        <p v-else-if="maskLoading" class="volume-message" role="status">Preparing volume mask…</p>
        <p v-else-if="maskMessage || maskError" class="volume-message" :class="{ problem: maskError }" role="status">
          {{ maskMessage || 'The volume mask could not be loaded.' }}
          <button v-if="maskCanRetry" type="button" class="text-button" aria-label="Retry volume mask" :disabled="disabled" @click="emit('retry-mask')">
            Retry mask
          </button>
        </p>
      </template>
      <p v-else-if="structures.length" class="volume-note">Shows the volume only inside, or only outside, the segments you choose.</p>
      <p v-else class="volume-note">Run segmentation first to create masks.</p>
    </VolumeSection>

    <VolumeSection title="Color segments" :summary="colorsSummary" :open="sections.colors" @toggle="toggleSection('colors', $event)">
      <template #actions>
        <input
          type="checkbox"
          role="switch"
          class="volume-switch"
          :aria-label="`Color segments in the ${modalityLabel} volume`"
          :aria-describedby="colorsAvailable ? undefined : `${tip}-colors-off`"
          :title="
            !colorsAvailable
              ? colorsReason
              : structures.length
                ? `Tint the ${modalityLabel} volume inside chosen segments with their anatomy-list colours.`
                : 'Run segmentation first to create anatomical segments.'
          "
          :checked="colorsAvailable && colors.enabled"
          :disabled="inert || !colorsAvailable || !structures.length"
          @change="enableColors(($event.target as HTMLInputElement).checked)"
        />
      </template>
      <p v-if="!colorsAvailable" :id="`${tip}-colors-off`" class="volume-note">{{ colorsReason || 'Segment colours are not available in this view.' }}</p>
      <template v-else-if="colors.enabled">
        <div class="volume-row">
          <span>Segments</span
          ><button
            type="button"
            class="text-button volume-picker-toggle"
            :aria-expanded="colorsOpen"
            aria-label="Choose colored segments"
            @click="colorsOpen = !colorsOpen"
          >
            {{ colorsOpen ? 'Done' : 'Choose segments' }} · {{ colors.ids.length }}<ChevronUp v-if="colorsOpen" /><ChevronDown v-else />
          </button>
        </div>
        <div v-if="colorsOpen" class="volume-picker">
          <div class="volume-actions">
            <button
              type="button"
              class="button secondary compact"
              :disabled="inert || !currentMaskTarget"
              title="Color the segment selected in the anatomy list"
              @click="updateColors({ ids: [selected] })"
            >
              Use selected</button
            ><button
              type="button"
              class="button secondary compact"
              :disabled="inert || !visibleMaskIds.length"
              title="Color a snapshot of all currently visible segments"
              @click="updateColors({ ids: visibleMaskIds })"
            >
              Use visible</button
            ><button type="button" class="text-button" :disabled="inert || !colors.ids.length" @click="updateColors({ ids: [] })">Clear</button
            ><InfoTip
              :id="`${tip}-colors`"
              :text="`Each segment uses its anatomy-list colour; change it under Selected structure. Colours change only how the ${modalityLabel} volume looks, never masks or measurements. Up to ${MAX_VOLUME_COLOR_SEGMENTS} segments.`"
            />
          </div>
          <input
            v-model="colorSearch"
            class="volume-search"
            type="search"
            aria-label="Find colored segments"
            :aria-describedby="`${tip}-colors`"
            placeholder="Find segments…"
            :disabled="inert"
          />
          <div class="volume-segments" role="group" aria-label="Colored segments">
            <label v-for="structure in colorStructures" :key="structure.id" class="volume-color-segment"
              ><input
                type="checkbox"
                :aria-label="`Color segment ${prettyName(structure.name)}`"
                :checked="colorIds.has(structure.id)"
                :disabled="inert || (!colorIds.has(structure.id) && colors.ids.length >= MAX_VOLUME_COLOR_SEGMENTS)"
                @change="toggleColor(structure.id, ($event.target as HTMLInputElement).checked)"
              /><i :style="{ background: colorCss(structure.color) }" aria-hidden="true"></i><span>{{ prettyName(structure.name) }}</span
              ><small>{{ prettyName(structure.group) }}</small></label
            >
            <p v-if="!colorStructures.length" class="volume-note">No matching segments.</p>
          </div>
        </div>
        <label class="volume-row"
          ><span>Color strength</span
          ><input
            type="range"
            aria-label="Segment color strength"
            min="0"
            max="1"
            step="0.05"
            :value="colors.strength"
            :disabled="inert"
            @input="updateColors({ strength: number($event) })"
          /><output>{{ Math.round(colors.strength * 100) }}%</output></label
        >
        <p v-if="!colors.ids.length" class="volume-message" role="status">Choose at least one segment to color.</p>
        <p v-else-if="colorsLoading" class="volume-message" role="status">Preparing segment colours…</p>
        <p v-else-if="colorsError" class="volume-message problem" role="status">
          {{ colorsError }}
          <button type="button" class="text-button" aria-label="Retry segment colors" :disabled="disabled" @click="emit('retry-colors')">Retry colours</button>
        </p>
      </template>
      <p v-else-if="structures.length" class="volume-note">Tints chosen segments with their anatomy-list colours.</p>
      <p v-else class="volume-note">Run segmentation first to create anatomical segments.</p>
    </VolumeSection>

    <VolumeSection title="Crop" :summary="cropped ? 'Cropped' : 'Full volume'" :open="sections.crop" @toggle="toggleSection('crop', $event)">
      <p class="volume-note">
        Along the scan’s own i / j / k axes<InfoTip
          :id="`${tip}-crop`"
          text="Native voxel axes, so a crop stays exact on oblique and tilted-gantry grids. Reset volume restores the full volume."
        />
      </p>
      <div v-for="(axis, index) in axes" :key="axis" class="volume-crop-axis">
        <strong>{{ axis }}</strong>
        <label
          ><span>Start</span
          ><input
            type="range"
            :aria-label="`${modalityLabel} volume crop ${axis} minimum`"
            :aria-describedby="`${tip}-crop`"
            min="0"
            max="1"
            step="0.01"
            :value="settings.crop[0][index]"
            :disabled="inert"
            @input="changeCrop(index, 0, $event)"
        /></label>
        <label
          ><span>End</span
          ><input
            type="range"
            :aria-label="`${modalityLabel} volume crop ${axis} maximum`"
            :aria-describedby="`${tip}-crop`"
            min="0"
            max="1"
            step="0.01"
            :value="settings.crop[1][index]"
            :disabled="inert"
            @input="changeCrop(index, 1, $event)"
        /></label>
        <output>{{ Math.round(settings.crop[0][index] * 100) }}–{{ Math.round(settings.crop[1][index] * 100) }}%</output>
      </div>
    </VolumeSection>

    <!-- Progress and errors only: the header already says when the volume is visible. -->
    <p class="volume-status" :class="{ problem: !loading && !!message }" role="status">
      <template v-if="enabled && !ready"
        >{{ stateMessage }} <button v-if="!loading && message" type="button" class="text-button" @click="emit('retry')">Retry volume</button></template
      >
    </p>
  </section>
</template>

<style scoped>
.volume-controls {
  min-width: 0;
  color: var(--text-secondary);
  font-size: 12px;
}
.volume-controls-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 8px;
}
.volume-title {
  display: grid;
  flex: 1;
  min-width: 0;
}
.volume-title > strong {
  color: var(--white);
  font-size: 12px;
  font-weight: 600;
}
.volume-state {
  overflow: hidden;
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.problem {
  color: var(--warning);
}
.volume-controls .text-button {
  font-size: 11px;
}
.volume-controls-header > .text-button {
  flex: none;
}
/* While the volume is off, its sections stay where they are, greyed, with every control disabled. */
.volume-controls.off :deep(.volume-section-body),
.volume-controls.off :deep(.volume-section-summary) {
  opacity: 0.5;
}
.volume-controls.off .volume-switch {
  opacity: 0.5;
}

/* One column: the label on the left, the control in the middle and its value on the right. */
.volume-controls :deep(.volume-row) {
  display: grid;
  grid-template-columns: 86px minmax(0, 1fr) auto;
  align-items: center;
  gap: 4px 8px;
  min-height: 26px;
  color: var(--text-secondary);
  font-size: 11px;
}
.volume-controls :deep(.volume-row > :first-child) {
  color: var(--text-secondary);
}
.volume-controls :deep(.volume-row > select) {
  width: 100%;
  min-width: 0;
  padding: 4px 6px;
  border: 1px solid var(--line-strong);
  border-radius: 4px;
  background: var(--panel);
  color: var(--white);
  font-size: 11px;
}
/* A row without a value lets its select or button take the rest of the line. */
.volume-controls :deep(.volume-row > select:last-child),
.volume-controls :deep(.volume-row > .volume-picker-toggle:last-child) {
  grid-column: 2 / -1;
}
.volume-controls :deep(.volume-row > input[type='range']) {
  width: 100%;
  min-width: 0;
  margin: 0;
}
.volume-controls :deep(.volume-row > input[type='number']) {
  width: 100%;
  min-width: 0;
  padding: 4px 6px;
  border: 1px solid var(--line-strong);
  border-radius: 4px;
  background: var(--panel);
  font: 11px var(--mono);
}
.volume-controls :deep(.volume-row > output),
.volume-controls :deep(.volume-row > small) {
  min-width: 40px;
  color: var(--white);
  font: 11px var(--mono);
  text-align: right;
  white-space: nowrap;
}
.volume-controls :deep(.volume-row > .button) {
  justify-self: start;
}
.volume-controls :deep(.volume-check-row) {
  grid-template-columns: minmax(0, 1fr) auto;
}
.volume-controls :deep(.volume-check-row > label) {
  display: flex;
  align-items: center;
  gap: 6px;
}
.volume-controls :deep(.volume-check-row input) {
  margin: 0;
}
.volume-controls :deep(.volume-note) {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin: 0;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.45;
}
/* Each CT preset's colours as a swatch under the menu, in the control column. */
.volume-swatches {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(26px, 1fr));
  gap: 4px;
  margin: 2px 0 6px 94px;
}
.volume-swatch {
  height: 16px;
  padding: 0;
  border: 1px solid var(--line-strong);
  border-radius: 3px;
  background-color: var(--bg);
  cursor: pointer;
}
.volume-swatch:hover:not(:disabled) {
  border-color: var(--control-border-hover);
}
.volume-swatch[aria-pressed='true'] {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent);
}

/* 2.5 mm and its note sit under the slider, so the slider keeps the row's width. */
.volume-controls .volume-margin > .text-button {
  grid-column: 2;
  justify-self: start;
  padding: 2px 6px;
}
.volume-message {
  margin: 0;
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 1.45;
}
.volume-message .text-button {
  margin-left: 4px;
}
.volume-subhead {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
  color: var(--white);
  font-size: 11px;
  font-weight: 600;
}
.volume-cinematic {
  display: grid;
  gap: 6px;
  margin-top: 4px;
  padding-top: 8px;
  border-top: 1px dashed var(--line);
}
.volume-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.volume-actions .button {
  min-height: 26px;
  padding: 4px 8px;
  font-size: 11px;
}
.volume-actions .text-button {
  padding: 3px 8px;
}

/* Header switches: a checkbox drawn as a switch, so tests and assistive technology see a checkbox. */
.volume-switch {
  flex: none;
  width: 28px;
  height: 16px;
  margin: 0;
  padding: 0;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: radial-gradient(circle at 7px 50%, var(--text-secondary) 0 4.5px, transparent 5px) var(--surface-active);
  appearance: none;
  cursor: pointer;
}
.volume-switch:checked {
  border-color: var(--accent);
  background: radial-gradient(circle at 19px 50%, var(--ink) 0 4.5px, transparent 5px) var(--accent);
}
.volume-switch:disabled {
  opacity: 0.4;
  cursor: default;
}
.volume-switch:focus-visible {
  outline: 2px solid var(--control-focus, var(--accent));
  outline-offset: 2px;
}

.volume-picker {
  display: grid;
  gap: 6px;
  padding: 6px 0 2px;
}
.volume-search {
  width: 100%;
  padding: 5px 8px;
  border-color: var(--line-strong);
  background: var(--panel);
  font-size: 11px;
}
.volume-segments {
  max-height: 168px;
  overflow: auto;
  padding: 2px 4px;
  border: 1px solid var(--line-strong);
  border-radius: 4px;
  scrollbar-width: thin;
}
.volume-mask-segment,
.volume-color-segment {
  display: flex;
  align-items: center;
  gap: 7px;
  min-height: 28px;
  padding: 3px;
  cursor: pointer;
}
.volume-mask-segment:hover,
.volume-color-segment:hover {
  background: var(--surface-hover);
}
.volume-mask-segment input,
.volume-color-segment input {
  flex-shrink: 0;
  width: 13px;
  height: 13px;
  margin: 0;
}
.volume-mask-segment i,
.volume-color-segment i {
  display: block;
  flex-shrink: 0;
  width: 7px;
  height: 7px;
  border-radius: 50%;
}
.volume-color-segment i {
  width: 10px;
  height: 10px;
  border-radius: 3px;
}
.volume-mask-segment > span,
.volume-color-segment > span {
  color: var(--white);
  font-size: 11px;
  line-height: 1.4;
  overflow-wrap: anywhere;
}
.volume-mask-segment small,
.volume-color-segment small {
  flex-shrink: 0;
  max-width: 40%;
  margin-left: auto;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.4;
  text-align: right;
}

/* Each native axis on one line: its name, the start and end of the kept range, and that range. */
.volume-crop-axis {
  display: grid;
  grid-template-columns: 14px minmax(0, 1fr) minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  min-height: 28px;
}
.volume-crop-axis > strong {
  color: var(--white);
  font: 11px var(--mono);
}
/* The range thumb rises above its thin track, so the label keeps clear of it. */
.volume-crop-axis > label {
  display: grid;
  gap: 6px;
  min-width: 0;
  color: var(--muted);
  font-size: 11px;
}
.volume-crop-axis input {
  width: 100%;
  margin: 0;
}
.volume-crop-axis > output {
  min-width: 56px;
  color: var(--white);
  font: 11px var(--mono);
  text-align: right;
  white-space: nowrap;
}

.volume-status {
  margin: 8px 0 0;
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 1.45;
}
.volume-status:empty {
  display: none;
}
.volume-status.problem {
  color: var(--warning);
}
</style>

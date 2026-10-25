<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { cinematicReferenceError, CINEMATIC_HEART_PRESET, freshVolumeSettings, getVolumePreset, VOLUME_PRESET_GROUPS, VOLUME_PRESET_LABELS, type CTVolumePresetId, type CTVolumeSettings } from '../lib/volume-presets';
import { freshVolumeMaskSettings, MAX_VOLUME_MASK_SEGMENTS, visibleVolumeMaskIds, type VolumeMaskSettings } from '../lib/volume-mask';
import { colorCss, prettyName } from '../lib/geometry';
import type { Structure, StructureDisplay } from '../types';

const props = withDefaults(defineProps<{
  enabled: boolean;
  headerless?: boolean;
  settings: CTVolumeSettings;
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
}>(), { mask: () => freshVolumeMaskSettings(), structures: () => [], selected: '', display: () => ({}) });
const emit = defineEmits<{
  'update:enabled': [value: boolean];
  'update:settings': [value: CTVolumeSettings];
  'update:mask': [value: VolumeMaskSettings];
  'retry-mask': [];
  'measure-reference': [];
  'heart-view': [];
  retry: [];
  layout: [];
}>();
const axes = ['i', 'j', 'k'] as const;
const maskOpen = ref(props.mask.enabled), maskSearch = ref('');
const maskIds = computed(() => new Set(props.mask.ids));
const maskStructures = computed(() => {
  const query = maskSearch.value.trim().toLowerCase().replaceAll('_', ' ');
  return props.structures.filter(structure => !query || `${prettyName(structure.name)} ${prettyName(structure.group)} ${structure.task}`.toLowerCase().includes(query));
});
const currentMaskTarget = computed(() => props.structures.some(structure => structure.id === props.selected));
const visibleMaskIds = computed(() => visibleVolumeMaskIds(props.structures, props.display));
const maskCanRetry = computed(() => props.maskError && !props.maskLoading && props.mask.ids.length > 0
  && props.mask.ids.length <= MAX_VOLUME_MASK_SEGMENTS && props.mask.ids.every(id => props.structures.some(structure => structure.id === id)));
const intensityLabel = computed(() => `${props.settings.shift > 0 ? '+' : ''}${props.settings.shift} HU`);
const opacityLabel = computed(() => `${Math.round(props.settings.density * 100)}%`);
const preset = computed(() => getVolumePreset(props.settings.preset, props.settings.cinematic));
const cinematic = computed(() => props.settings.preset === CINEMATIC_HEART_PRESET);
const myocardiumInput = ref(String(props.settings.cinematic.myocardiumHU)), bloodInput = ref(String(props.settings.cinematic.bloodHU));
const referenceInputError = ref('');
const otherModality = computed(() => !preset.value.sourcePreset.startsWith('CT-'));
const projectionNamed = computed(() => preset.value.sourcePreset.endsWith('-MIP'));
const stateMessage = computed(() => props.message || (props.loading ? 'Preparing CT volume…' : props.ready ? 'CT volume · drag to rotate; scroll to zoom.' : 'Waiting for the full CT to finish loading.'));
function update(change: Partial<CTVolumeSettings>) {
  emit('update:settings', freshVolumeSettings({ ...props.settings, ...change }));
}
function number(event: Event) { return Number((event.target as HTMLInputElement).value); }
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
  crop[end][axis] = end === 0 ? Math.min(number(event), crop[1][axis] - .01) : Math.max(number(event), crop[0][axis] + .01);
  update({ crop });
}
function reset() { emit('update:settings', freshVolumeSettings({ preset: props.settings.preset })); }
function updateMask(change: Partial<VolumeMaskSettings>) {
  emit('update:mask', freshVolumeMaskSettings({ ...props.mask, ...change }, props.structures));
}
function toggleMask(id: string, checked: boolean) {
  updateMask({ ids: checked ? [...props.mask.ids, id] : props.mask.ids.filter(value => value !== id) });
}
function useMaskIds(ids: string[]) {
  updateMask({ ids }); maskOpen.value = false;
}
async function layout() { await nextTick(); emit('layout'); }
watch(() => props.enabled, layout);
watch(cinematic, layout);
watch([() => props.settings.cinematic.myocardiumHU, () => props.settings.cinematic.bloodHU], ([myocardium, blood]) => {
  myocardiumInput.value = String(myocardium); bloodInput.value = String(blood); referenceInputError.value = '';
});
watch(() => props.mask.enabled, enabled => { maskOpen.value = enabled; void layout(); });
watch(() => props.maskError, failed => { if (failed) maskOpen.value = false; });
watch(maskOpen, layout);
</script>

<template>
  <section class="volume-controls" :class="{ enabled }" aria-label="CT volume rendering controls">
    <div class="volume-controls-header">
      <label v-if="!headerless" class="volume-toggle"><input type="checkbox" aria-label="Show CT volume" :checked="enabled" :disabled="disabled" @change="emit('update:enabled', ($event.target as HTMLInputElement).checked)" /> CT volume</label>
      <span v-if="enabled && loading" class="volume-loading">Preparing…</span>
      <button v-if="enabled" type="button" class="text-button volume-reset" title="Reset this preset's appearance and crop" @click="reset">Reset volume</button>
    </div>
    <template v-if="enabled">
      <div class="volume-appearance">
        <label class="volume-preset">Appearance
          <select aria-label="CT volume preset" :value="preset.id" :disabled="disabled" @change="changePreset">
            <optgroup v-for="group in VOLUME_PRESET_GROUPS" :key="group.label" :label="group.label"><option v-for="id in group.ids" :key="id" :value="id">{{ VOLUME_PRESET_LABELS[id] }}</option></optgroup>
          </select>
        </label>
        <label class="volume-slider">Intensity <output>{{ intensityLabel }}</output>
          <input type="range" aria-label="CT volume intensity shift" min="-500" max="500" step="5" :value="settings.shift" :disabled="disabled" @input="update({ shift: number($event) })" />
        </label>
        <label class="volume-slider">Opacity <output>{{ opacityLabel }}</output>
          <input type="range" aria-label="CT volume opacity" :aria-valuetext="opacityLabel" min="0.1" max="3" step="0.05" :value="settings.density" :disabled="disabled" @input="update({ density: number($event) })" />
        </label>
        <label class="volume-quality">Quality
          <select aria-label="CT volume quality" :value="settings.quality" :disabled="disabled" @change="update({ quality: ($event.target as HTMLSelectElement).value as 'auto' | 'detail' })"><option value="auto">Auto</option><option value="detail">Detail</option></select>
        </label>
      </div>
      <div v-if="cinematic" class="volume-cinematic" aria-label="Cinematic heart appearance">
        <button v-if="heartViewAvailable" class="button secondary compact" type="button" aria-label="Use Slicer heart orientation" :disabled="disabled" @click="emit('heart-view')">Heart view</button>
        <p v-if="heartViewAvailable" class="volume-preset-note">Heart view faces the same side as the supplied Slicer scene. Oblique exposes a different face and can show brighter blood or chamber interiors.</p>
        <p class="volume-preset-note">Crimson myocardium, ivory blood and a camera-following key light. For an isolated heart, enable Mask to segmentation below and choose its segments.</p>
        <div class="volume-cinematic-reference">
          <label>Myocardium <input v-model="myocardiumInput" type="number" aria-label="Myocardium reference HU" min="-500" max="1500" step="1" :disabled="disabled || referenceLoading" /> <span>HU</span></label>
          <label>Blood pool <input v-model="bloodInput" type="number" aria-label="Blood pool reference HU" min="-400" max="3000" step="1" :disabled="disabled || referenceLoading" /> <span>HU</span></label>
          <button class="button secondary compact" type="button" :disabled="disabled || referenceLoading" @click="applyReference">Apply reference HU</button>
          <button v-if="referenceAvailable" class="button secondary compact" type="button" :disabled="disabled || referenceLoading" @click="emit('measure-reference')">{{ referenceLoading ? 'Measuring…' : 'Measure from segments' }}</button>
        </div>
        <p class="volume-preset-note">Initial defaults: 80 / 400 HU. Measure from myocardium and aorta or left-heart segments when available, or enter both values manually. These values change appearance only.</p>
        <p v-if="referenceInputError" class="volume-cinematic-message error" role="alert">{{ referenceInputError }}</p>
        <p v-if="referenceMessage" class="volume-cinematic-message" :class="{ error: referenceError }" :role="referenceError ? 'alert' : 'status'">{{ referenceMessage }}</p>
        <div class="volume-cinematic-lighting">
          <label class="volume-slider">Shadows <output>{{ settings.cinematic.shadows === 0 ? 'Off' : settings.cinematic.shadows.toFixed(1) }}</output><input type="range" aria-label="Cinematic shadows" min="0" max="2" step="0.1" :value="settings.cinematic.shadows" :disabled="disabled" @input="update({ cinematic: { ...settings.cinematic, shadows: number($event) } })" /></label>
          <label class="volume-slider">Shadow reach <output>{{ Math.round(settings.cinematic.shadowReach * 100) }}%</output><input type="range" aria-label="Cinematic shadow reach" min="0" max="1" step="0.05" :value="settings.cinematic.shadowReach" :disabled="disabled || settings.cinematic.shadows === 0" @input="update({ cinematic: { ...settings.cinematic, shadowReach: number($event) } })" /></label>
        </div>
        <p class="volume-preset-note">Shadows resolve after rotation stops; dragging uses a lighter preview. This is an interactive WebGL approximation of the supplied Slicer look.</p>
      </div>
      <p v-if="otherModality" class="volume-preset-note">This preset is designed for another modality and may need adjustment for CT.</p>
      <p v-if="projectionNamed" class="volume-preset-note">Slicer’s {{ preset.label }} appearance, shown with composite volume rendering.</p>
      <div class="volume-mask">
        <div class="volume-mask-header"><label class="volume-toggle" :title="structures.length ? 'Limit the CT volume to the selected anatomical masks.' : 'Run segmentation first to create anatomical masks.'"><input type="checkbox" aria-label="Mask CT volume to segmentation" :checked="mask.enabled" :disabled="disabled || !structures.length" @change="updateMask({ enabled: ($event.target as HTMLInputElement).checked })" />Mask to segmentation</label><span v-if="!structures.length" class="volume-mask-help">Run segmentation first.</span><button v-if="mask.enabled" type="button" class="text-button" :aria-expanded="maskOpen" aria-label="Choose volume mask segments" @click="maskOpen = !maskOpen">{{ maskOpen ? 'Done' : 'Choose segments' }} · {{ mask.ids.length }} {{ maskOpen ? '▴' : '▾' }}</button></div>
        <template v-if="mask.enabled">
          <div v-if="maskOpen" class="volume-mask-picker">
            <label class="volume-mask-mode">Show volume<select aria-label="CT volume mask region" :value="mask.mode" :disabled="disabled" @change="updateMask({ mode: ($event.target as HTMLSelectElement).value as VolumeMaskSettings['mode'] })"><option value="inside">Inside selected segments</option><option value="outside">Outside selected segments</option></select></label>
            <div class="volume-mask-actions"><button type="button" class="button secondary compact" :disabled="disabled || !currentMaskTarget" title="Use the segment selected in the anatomy list" @click="useMaskIds([selected])">Use selected</button><button type="button" class="button secondary compact" :disabled="disabled || !visibleMaskIds.length" title="Use a snapshot of all currently visible segments" @click="useMaskIds(visibleMaskIds)">Use visible</button><button type="button" class="text-button" :disabled="disabled || !mask.ids.length" @click="updateMask({ ids: [] })">Clear</button></div>
            <input v-model="maskSearch" class="volume-mask-search" type="search" aria-label="Search volume mask segments" placeholder="Search segments…" :disabled="disabled" />
            <div class="volume-mask-segments" role="group" aria-label="Volume mask segments"><label v-for="structure in maskStructures" :key="structure.id" class="volume-mask-segment"><input type="checkbox" :aria-label="`Mask segment ${prettyName(structure.name)}`" :checked="maskIds.has(structure.id)" :disabled="disabled" @change="toggleMask(structure.id, ($event.target as HTMLInputElement).checked)" /><i :style="{ background: colorCss(structure.color) }" aria-hidden="true"></i><span>{{ prettyName(structure.name) }}</span><small>{{ prettyName(structure.group) }}</small></label><p v-if="!maskStructures.length" class="volume-mask-help">No matching segments.</p></div>
            <p class="volume-mask-help">Selected segments form one combined region. This mask affects only the CT volume; CT slices stay unchanged.</p>
          </div>
          <div class="volume-mask-margin">
            <label class="volume-slider">Display margin <output>{{ mask.margin_mm.toFixed(1) }} mm</output>
              <input type="range" aria-label="CT volume mask margin" min="0" max="5" step="0.5" :value="mask.margin_mm"
                :disabled="disabled || maskMarginFixed" @input="updateMask({ margin_mm: number($event) })" />
            </label>
            <button v-if="!maskMarginFixed" class="button secondary compact" type="button" aria-label="Use 2.5 mm volume mask margin" :disabled="disabled" @click="updateMask({ margin_mm: 2.5 })">2.5 mm</button>
          </div>
          <p class="volume-mask-help">{{ maskMarginFixed ? 'This offline export retains its prepared display margin.' : 'Grow the display region to retain thin walls and surface vessels. CT overlays and segmentation measurements stay unchanged.' }}</p>
          <p v-if="!mask.ids.length" class="volume-mask-message" role="status">Choose at least one segment. The CT volume stays hidden until a mask is ready.</p>
          <p v-else-if="mask.ids.length > MAX_VOLUME_MASK_SEGMENTS" class="volume-mask-message" role="status">Choose up to {{ MAX_VOLUME_MASK_SEGMENTS }} segments. The CT volume stays hidden until you reduce the selection.</p>
          <p v-else-if="maskLoading" class="volume-mask-message" role="status">Preparing volume mask…</p>
          <p v-else-if="maskMessage || maskError" class="volume-mask-message" role="status">{{ maskMessage || 'The volume mask could not be loaded.' }} <button v-if="maskCanRetry" type="button" class="text-button" aria-label="Retry volume mask" :disabled="disabled" @click="emit('retry-mask')">Retry mask</button></p>
        </template>
      </div>
      <details class="volume-crop" @toggle="layout">
        <summary>Crop volume <span>Native i / j / k</span></summary>
        <div class="volume-crop-grid">
          <div v-for="(axis, index) in axes" :key="axis" class="volume-crop-axis">
            <div class="volume-crop-axis-header"><strong>{{ axis }}</strong><output>{{ Math.round(settings.crop[0][index] * 100) }}–{{ Math.round(settings.crop[1][index] * 100) }}%</output></div>
            <label>Start <input type="range" :aria-label="`CT volume crop ${axis} minimum`" min="0" max="1" step="0.01" :value="settings.crop[0][index]" :disabled="disabled" @input="changeCrop(index, 0, $event)" /></label>
            <label>End <input type="range" :aria-label="`CT volume crop ${axis} maximum`" min="0" max="1" step="0.01" :value="settings.crop[1][index]" :disabled="disabled" @input="changeCrop(index, 1, $event)" /></label>
          </div>
        </div>
      </details>
      <p class="volume-status" role="status">{{ stateMessage }} <button v-if="!loading && !ready && message" type="button" class="text-button" @click="emit('retry')">Retry volume</button></p>
    </template>
  </section>
</template>

<style scoped>
.volume-controls{min-width:0;color:#bac7d9;font-size:10px}
.volume-controls-header{display:flex;align-items:center;gap:10px;min-height:21px}
.volume-toggle{display:flex;align-items:center;gap:5px;white-space:nowrap;font-size:10px}
.volume-toggle input{accent-color:#8aabff}
.volume-loading{color:#94a5bd;font-size:9px}
.volume-reset{margin-left:auto;font-size:10px;white-space:nowrap}
.volume-appearance{display:grid;grid-template-columns:minmax(130px,1.1fr) minmax(100px,1fr) minmax(95px,1fr) 72px;align-items:end;gap:12px;margin-top:9px}
.volume-appearance>label{display:grid;gap:5px;min-width:0;font-size:9px;color:#a2b2c8}
.volume-appearance select{width:100%;min-width:0;border:1px solid #414b5d;border-radius:4px;padding:4px 5px;background:#252e3d;color:#e2eaf6;font-size:10px}
.volume-appearance .volume-slider{grid-template-columns:1fr auto;align-items:center}
.volume-slider input{grid-column:1 / -1;width:100%;height:18px;margin:0;accent-color:#8aabff}
output{font:9px var(--mono);white-space:nowrap;color:#c3d0e4}
.volume-crop{margin-top:8px;border-top:1px solid #343e50;padding-top:6px}
.volume-crop summary{cursor:pointer;user-select:none;color:#c3d0e4;font-size:10px}
.volume-crop summary>span{margin-left:8px;color:#8596ae;font:8px var(--mono)}
.volume-crop-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:8px}
.volume-crop-axis{min-width:0}
.volume-crop-axis-header{display:flex;align-items:center;justify-content:space-between;gap:6px;margin-bottom:3px}
.volume-crop-axis strong{font:10px var(--mono);color:#d3ddeb}
.volume-crop-axis>label{display:grid;grid-template-columns:29px minmax(0,1fr);align-items:center;gap:4px;font-size:8px;color:#8e9eb5}
.volume-crop-axis input{width:100%;height:16px;margin:0;accent-color:#8aabff}
.volume-status{font-size:10px;line-height:1.4;color:#a2b2c8;margin:7px 0 0}
.volume-status .text-button{font-size:10px;white-space:nowrap}
.volume-cinematic{margin-top:8px;padding-top:7px;border-top:1px solid #343e50}
.volume-cinematic-reference{display:flex;align-items:end;gap:7px;flex-wrap:wrap;margin-top:8px}
.volume-cinematic-reference label{display:grid;grid-template-columns:minmax(60px,1fr) auto;gap:4px;font-size:9px;color:#a2b2c8;max-width:135px}
.volume-cinematic-reference input{grid-column:1;grid-row:2;width:100%;min-width:0;padding:5px 7px;font:10px var(--mono);background:#252e3d;border:1px solid #414b5d;border-radius:4px;color:#e2eaf6}
.volume-cinematic-reference label>span{grid-column:2;grid-row:2;align-self:center;font:9px var(--mono)}
.volume-cinematic-reference .button{min-height:28px;font-size:9px;padding:4px 8px}
.volume-cinematic-lighting{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:9px}
.volume-cinematic-lighting label{display:grid;grid-template-columns:1fr auto;gap:4px;font-size:9px;color:#a2b2c8}
.volume-cinematic-message{font-size:10px;line-height:1.5;margin:7px 0 0;color:#bdcce0}
.volume-cinematic-message.error{color:#edb5b5}
.volume-preset-note{font-size:9px;line-height:1.4;color:#a2b2c8;margin:7px 0 0}
.volume-mask{margin-top:8px;padding-top:7px;border-top:1px solid #343e50}
.volume-mask-header{display:flex;align-items:center;gap:9px;flex-wrap:wrap;min-height:23px}
.volume-mask-header>.text-button{margin-left:auto;font-size:10px}
.volume-mask-picker{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:7px 10px;margin-top:7px;max-height:200px;overflow:auto;padding-right:3px;scrollbar-width:thin}
.volume-mask-picker>.volume-mask-mode,.volume-mask-picker>.volume-mask-segments,.volume-mask-picker>.volume-mask-help{grid-column:1/-1}
.volume-mask-mode{display:flex;align-items:center;gap:8px;font-size:9px;color:#a2b2c8}
.volume-mask-mode select{min-width:0;max-width:100%;font-size:10px;padding:5px 7px;background:#252e3d;color:#e2eaf6}
.volume-mask-actions{display:flex;align-items:center;gap:7px;flex-wrap:wrap;grid-column:2;grid-row:2}
.volume-mask-actions .button{font-size:9px;min-height:26px;padding:4px 7px}
.volume-mask-actions .text-button{font-size:10px;padding:4px 6px}
.volume-mask-search{width:100%;padding:6px 8px;font-size:10px;background:#252e3d;grid-column:1;grid-row:2}
.volume-mask-segments{max-height:110px;overflow:auto;border:1px solid var(--line-strong);border-radius:4px;padding:3px 5px;scrollbar-width:thin}
.volume-mask-segment{display:flex;align-items:center;gap:7px;min-height:28px;padding:4px 3px;cursor:pointer}
.volume-mask-segment:hover{background:var(--surface-hover)}
.volume-mask-segment input{margin:0;flex-shrink:0;width:13px;height:13px}
.volume-mask-segment i{display:block;flex-shrink:0;width:6px;height:6px;border-radius:50%}
.volume-mask-segment>span{font-size:10px;line-height:1.4;color:#e2eaf6;overflow-wrap:anywhere}
.volume-mask-segment small{margin-left:auto;text-align:right;flex-shrink:0;max-width:35%;font-size:8px;line-height:1.4;color:#8596ae}
.volume-mask-help{font-size:9px;line-height:1.5;color:#94a5bd;margin:0}
.volume-mask-message{font-size:10px;line-height:1.5;color:#c3d0e4;margin:7px 0 0}
.volume-mask-message .text-button{font-size:10px;margin-left:5px}
.volume-mask-margin{display:flex;align-items:center;gap:10px;margin:8px 0 4px}
.volume-mask-margin>.volume-slider{display:grid;grid-template-columns:1fr auto;align-items:center;gap:3px 8px;flex:1;min-width:0;font-size:9px;color:#a2b2c8}
.volume-mask-margin>.button{font-size:9px;min-height:26px;padding:4px 8px}
@media(max-width:760px){.volume-appearance{grid-template-columns:minmax(130px,1fr) minmax(110px,1fr);gap:8px 12px}.volume-quality{grid-column:1 / -1;grid-template-columns:50px 90px!important;align-items:center}.volume-crop-grid{gap:10px}}
</style>

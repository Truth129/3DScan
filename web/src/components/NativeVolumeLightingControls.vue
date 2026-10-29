<script setup lang="ts">
import { computed, nextTick, watch } from 'vue';
import { freshNativeVolumeLighting, type NativeVolumeLightingSettings } from '../lib/volume-lighting';

const props = defineProps<{ settings: NativeVolumeLightingSettings; disabled?: boolean }>();
const emit = defineEmits<{ update: [settings: NativeVolumeLightingSettings]; layout: [] }>();
const manual = computed(() => props.settings.mode !== 'preset');
const materials = [
  { key: 'ambient', label: 'Ambient', aria: 'Volume ambient light', min: 0, max: 1, step: 0.05 },
  { key: 'diffuse', label: 'Diffuse', aria: 'Volume diffuse light', min: 0, max: 1, step: 0.05 },
  { key: 'specular', label: 'Specular', aria: 'Volume specular light', min: 0, max: 1, step: 0.05 },
  { key: 'specularPower', label: 'Shininess', aria: 'Volume specular power', min: 1, max: 100, step: 1 },
] as const;
function number(event: Event) {
  return Number((event.target as HTMLInputElement).value);
}
function update(change: Partial<NativeVolumeLightingSettings>) {
  emit('update', freshNativeVolumeLighting({ ...props.settings, ...change }));
}
async function layout() {
  await nextTick();
  emit('layout');
}
watch(() => props.settings.mode, layout);
</script>

<template>
  <details class="native-lighting-controls" @toggle="layout">
    <summary>
      Lighting and shadows
      <span>{{
        settings.mode === 'preset' ? 'Preset' : settings.mode === 'cinematic' ? 'Cinematic' : settings.mode === 'standard' ? 'Standard' : 'Ambient occlusion'
      }}</span>
    </summary>
    <div class="lighting-heading">
      <label
        >Rendering method
        <select
          aria-label="Volume rendering method"
          :value="settings.mode"
          :disabled="disabled"
          @change="update({ mode: ($event.target as HTMLSelectElement).value as NativeVolumeLightingSettings['mode'] })"
        >
          <option value="preset">Preset lighting</option>
          <option value="standard">Standard</option>
          <option value="cinematic">Cinematic (VolView Hybrid)</option>
          <option value="ambient-occlusion">Ambient occlusion</option>
        </select>
      </label>
      <button type="button" class="text-button" :disabled="disabled" @click="emit('update', freshNativeVolumeLighting())">Reset lighting</button>
    </div>
    <p v-if="!manual" class="lighting-note">
      Uses this appearance preset’s lighting. Choose a rendering method to adjust the light and shadows for any CT preset.
    </p>
    <template v-else>
      <div class="lighting-grid">
        <label v-for="control in materials" :key="control.key" class="lighting-slider">
          {{ control.label }} <output>{{ control.key === 'specularPower' ? settings[control.key] : `${Math.round(settings[control.key] * 100)}%` }}</output>
          <input
            type="range"
            :aria-label="control.aria"
            :min="control.min"
            :max="control.max"
            :step="control.step"
            :value="settings[control.key]"
            :disabled="disabled"
            @input="update({ [control.key]: number($event) })"
          />
        </label>
      </div>
      <div v-if="settings.mode === 'cinematic'" class="lighting-grid">
        <label class="lighting-slider"
          >Scattering <output>{{ Math.round(settings.scattering * 100) }}%</output>
          <input
            type="range"
            aria-label="Volume scattering"
            min="0"
            max="1"
            step="0.05"
            :value="settings.scattering"
            :disabled="disabled"
            @input="update({ scattering: number($event) })"
          />
        </label>
        <label class="lighting-slider"
          >Shadow reach <output>{{ Math.round(settings.reach * 100) }}%</output>
          <input
            type="range"
            aria-label="Volume shadow reach"
            min="0"
            max="1"
            step="0.05"
            :value="settings.reach"
            :disabled="disabled || settings.scattering === 0"
            @input="update({ reach: number($event) })"
          />
        </label>
      </div>
      <div v-if="settings.mode === 'ambient-occlusion'" class="lighting-grid">
        <label class="lighting-slider"
          >Occlusion samples <output>{{ settings.aoKernelSize }}</output>
          <input
            type="range"
            aria-label="Volume ambient occlusion samples"
            min="1"
            max="32"
            step="1"
            :value="settings.aoKernelSize"
            :disabled="disabled"
            @input="update({ aoKernelSize: number($event) })"
          />
        </label>
        <label class="lighting-slider"
          >Occlusion radius <output>{{ settings.aoRadius }} steps</output>
          <input
            type="range"
            aria-label="Volume ambient occlusion radius"
            min="1"
            max="20"
            step="1"
            :value="settings.aoRadius"
            :disabled="disabled"
            @input="update({ aoRadius: number($event) })"
          />
        </label>
      </div>
      <label class="lighting-follow"
        ><input
          type="checkbox"
          aria-label="Light follows camera"
          :checked="settings.lightFollowsCamera"
          :disabled="disabled"
          @change="update({ lightFollowsCamera: ($event.target as HTMLInputElement).checked })"
        />Light follows camera</label
      >
      <div class="lighting-grid">
        <label class="lighting-slider"
          >Light azimuth <output>{{ settings.azimuth }}°</output>
          <input
            type="range"
            aria-label="Volume light azimuth"
            min="-180"
            max="180"
            step="5"
            :value="settings.azimuth"
            :disabled="disabled"
            @input="update({ azimuth: number($event) })"
          />
        </label>
        <label class="lighting-slider"
          >Light elevation <output>{{ settings.elevation }}°</output>
          <input
            type="range"
            aria-label="Volume light elevation"
            min="-89"
            max="89"
            step="1"
            :value="settings.elevation"
            :disabled="disabled"
            @input="update({ elevation: number($event) })"
          />
        </label>
      </div>
      <p class="lighting-note">
        {{
          settings.lightFollowsCamera
            ? 'The light moves with your view. Turn this off to keep the light fixed while you rotate.'
            : 'The light stays fixed in the scene. Adjust either angle to position it from the current view.'
        }}
      </p>
      <p v-if="settings.mode !== 'standard'" class="lighting-note">
        Cinematic shadows and ambient occlusion use VolView’s vtk.js techniques. More scattering, reach or occlusion samples can slow interaction.
      </p>
    </template>
  </details>
</template>

<style scoped>
.native-lighting-controls {
  border-top: 1px solid var(--line);
  margin-top: 10px;
  padding-top: 8px;
  color: #bac7d9;
  font-size: 10px;
}
summary {
  cursor: pointer;
  color: #c8d4e7;
  padding: 3px 0;
}
summary span {
  color: #8395b0;
  font-size: 9px;
  margin-left: 6px;
}
.lighting-heading {
  display: flex;
  align-items: end;
  gap: 8px;
  margin: 10px 0;
}
.lighting-heading label {
  display: grid;
  gap: 5px;
  flex: 1;
  min-width: 0;
}
.lighting-heading select {
  width: 100%;
  min-width: 0;
  font-size: 10px;
}
.lighting-heading button {
  font-size: 9px;
  white-space: nowrap;
  min-height: 28px;
}
.lighting-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 12px;
  margin: 10px 0;
}
.lighting-slider {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 5px;
  align-items: center;
  min-width: 0;
  font-size: 9px;
  color: #a2b2c8;
}
.lighting-slider output {
  color: #d1dced;
  font-variant-numeric: tabular-nums;
}
.lighting-slider input {
  grid-column: 1 / -1;
  width: 100%;
  min-width: 0;
  accent-color: #8aabff;
}
.lighting-follow {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
}
.lighting-follow input {
  accent-color: #8aabff;
}
.lighting-note {
  margin: 8px 0 4px;
  font-size: 9px;
  color: #8395b0;
  line-height: 1.5;
}
</style>

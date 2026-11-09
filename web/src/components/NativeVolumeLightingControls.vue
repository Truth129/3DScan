<script setup lang="ts">
import { computed, useId } from 'vue';
import { freshNativeVolumeLighting, type NativeVolumeLightingSettings } from '../lib/volume-lighting';
import InfoTip from './InfoTip.vue';

const props = defineProps<{ settings: NativeVolumeLightingSettings; disabled?: boolean }>();
const emit = defineEmits<{ update: [settings: NativeVolumeLightingSettings] }>();
const manual = computed(() => props.settings.mode !== 'preset');
const tip = useId();
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
</script>

<template>
  <!-- The rows of the Lighting section. VolumeControls owns its header, summary and Reset lighting. -->
  <div class="native-lighting-controls">
    <label class="volume-row"
      ><span>Method</span
      ><select
        aria-label="Volume rendering method"
        :aria-describedby="manual && settings.mode !== 'standard' ? `${tip}-cost` : undefined"
        :value="settings.mode"
        :disabled="disabled"
        @change="update({ mode: ($event.target as HTMLSelectElement).value as NativeVolumeLightingSettings['mode'] })"
      >
        <option value="preset">Preset lighting</option>
        <option value="standard">Standard</option>
        <option value="cinematic">Cinematic (VolView Hybrid)</option>
        <option value="ambient-occlusion">Ambient occlusion</option>
      </select></label
    >
    <p v-if="!manual" class="volume-note">Uses the appearance preset’s own lighting. Choose a method to set light and shadows.</p>
    <template v-else>
      <label v-for="control in materials" :key="control.key" class="volume-row"
        ><span>{{ control.label }}</span
        ><input
          type="range"
          :aria-label="control.aria"
          :min="control.min"
          :max="control.max"
          :step="control.step"
          :value="settings[control.key]"
          :disabled="disabled"
          @input="update({ [control.key]: number($event) })"
        /><output>{{ control.key === 'specularPower' ? settings[control.key] : `${Math.round(settings[control.key] * 100)}%` }}</output></label
      >
      <template v-if="settings.mode === 'cinematic'">
        <label class="volume-row"
          ><span>Scattering</span
          ><input
            type="range"
            aria-label="Volume scattering"
            min="0"
            max="1"
            step="0.05"
            :value="settings.scattering"
            :disabled="disabled"
            @input="update({ scattering: number($event) })"
          /><output>{{ Math.round(settings.scattering * 100) }}%</output></label
        >
        <label class="volume-row"
          ><span>Shadow reach</span
          ><input
            type="range"
            aria-label="Volume shadow reach"
            min="0"
            max="1"
            step="0.05"
            :value="settings.reach"
            :disabled="disabled || settings.scattering === 0"
            @input="update({ reach: number($event) })"
          /><output>{{ Math.round(settings.reach * 100) }}%</output></label
        >
      </template>
      <template v-if="settings.mode === 'ambient-occlusion'">
        <label class="volume-row"
          ><span>Occlusion samples</span
          ><input
            type="range"
            aria-label="Volume ambient occlusion samples"
            min="1"
            max="32"
            step="1"
            :value="settings.aoKernelSize"
            :disabled="disabled"
            @input="update({ aoKernelSize: number($event) })"
          /><output>{{ settings.aoKernelSize }}</output></label
        >
        <label class="volume-row"
          ><span>Occlusion radius</span
          ><input
            type="range"
            aria-label="Volume ambient occlusion radius"
            min="1"
            max="20"
            step="1"
            :value="settings.aoRadius"
            :disabled="disabled"
            @input="update({ aoRadius: number($event) })"
          /><output>{{ settings.aoRadius }} steps</output></label
        >
      </template>
      <div class="volume-row volume-check-row">
        <label
          ><input
            type="checkbox"
            aria-label="Light follows camera"
            :aria-describedby="`${tip}-follow`"
            :checked="settings.lightFollowsCamera"
            :disabled="disabled"
            @change="update({ lightFollowsCamera: ($event.target as HTMLInputElement).checked })"
          />Light follows camera</label
        ><InfoTip
          :id="`${tip}-follow`"
          :text="
            settings.lightFollowsCamera
              ? 'The light moves with your view, at the angles below from it. Turn this off to keep the light fixed while you rotate.'
              : 'The light stays fixed in the scene. Either angle places it from the current view.'
          "
        />
      </div>
      <!-- The angles stay live while the light follows: they set its offset from the view. -->
      <label class="volume-row"
        ><span>Azimuth</span
        ><input
          type="range"
          aria-label="Volume light azimuth"
          min="-180"
          max="180"
          step="5"
          :value="settings.azimuth"
          :disabled="disabled"
          @input="update({ azimuth: number($event) })"
        /><output>{{ settings.azimuth }}°</output></label
      >
      <label class="volume-row"
        ><span>Elevation</span
        ><input
          type="range"
          aria-label="Volume light elevation"
          min="-89"
          max="89"
          step="1"
          :value="settings.elevation"
          :disabled="disabled"
          @input="update({ elevation: number($event) })"
        /><output>{{ settings.elevation }}°</output></label
      >
      <p v-if="settings.mode !== 'standard'" class="volume-note">
        More scattering, reach or samples can slow rotation.<InfoTip
          :id="`${tip}-cost`"
          text="Cinematic shadows and ambient occlusion use VolView’s vtk.js techniques."
        />
      </p>
    </template>
  </div>
</template>

<style scoped>
.native-lighting-controls {
  display: grid;
  gap: 6px;
}
</style>

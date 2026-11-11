<script setup lang="ts">
import { computed, useId } from 'vue';
import { acquisitionSelectionError, CARDIAC_PHASE_OPTIONS, CONTRAST_PHASE_OPTIONS, isCardiacRegion, SCAN_REGION_OPTIONS } from '../lib/acquisition';
import type { AcquisitionSelection } from '../types';

const props = defineProps<{ modelValue: AcquisitionSelection; disabled?: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: AcquisitionSelection] }>();
const rrHelpId = useId();
const cardiac = computed(() => isCardiacRegion(props.modelValue.profile));
const error = computed(() => acquisitionSelectionError(props.modelValue));
const rrInvalid = computed(
  () =>
    props.modelValue.rr_percent !== null &&
    (!Number.isFinite(props.modelValue.rr_percent) || props.modelValue.rr_percent < 0 || props.modelValue.rr_percent > 100),
);
const savedRegion = computed(() => !SCAN_REGION_OPTIONS.some(group => group.options.some(item => item.id === props.modelValue.profile)));
const savedPhase = computed(
  () => props.modelValue.phase && !CONTRAST_PHASE_OPTIONS.some(group => group.options.some(item => item.id === props.modelValue.phase)),
);
function update(change: Partial<AcquisitionSelection>) {
  emit('update:modelValue', { ...props.modelValue, ...change });
}
function region(event: Event) {
  const profile = (event.target as HTMLSelectElement).value;
  update({ profile, ...(!isCardiacRegion(profile) ? { cardiac_phase: 'unknown', rr_percent: null } : {}) });
}
function rr(event: Event) {
  const input = event.target as HTMLInputElement;
  update({ rr_percent: input.validity.badInput ? Number.NaN : input.value.trim() === '' ? null : input.valueAsNumber });
}
</script>

<template>
  <div class="acquisition-fields">
    <div class="acquisition-grid">
      <label class="field"
        >Scan region / study
        <select aria-label="Scan region" :value="modelValue.profile" :disabled="disabled" @change="region">
          <option v-if="savedRegion" :value="modelValue.profile">Saved region: {{ modelValue.profile }}</option>
          <optgroup v-for="group in SCAN_REGION_OPTIONS" :key="group.group" :label="group.group">
            <option v-for="option in group.options" :key="option.id" :value="option.id">{{ option.label }}</option>
          </optgroup>
        </select>
      </label>
      <label class="field"
        >Contrast phase
        <select
          aria-label="Contrast phase"
          :value="modelValue.phase"
          :disabled="disabled"
          @change="update({ phase: ($event.target as HTMLSelectElement).value })"
        >
          <option value="" disabled>Select phase…</option>
          <option v-if="savedPhase" :value="modelValue.phase">Saved phase: {{ modelValue.phase }}</option>
          <optgroup v-for="group in CONTRAST_PHASE_OPTIONS" :key="group.group" :label="group.group">
            <option v-for="option in group.options" :key="option.id" :value="option.id">{{ option.label }}</option>
          </optgroup>
        </select>
      </label>
      <template v-if="cardiac">
        <label class="field"
          >Cardiac reconstruction
          <select
            aria-label="Cardiac reconstruction phase"
            :value="modelValue.cardiac_phase"
            :disabled="disabled"
            @change="update({ cardiac_phase: ($event.target as HTMLSelectElement).value })"
          >
            <option v-for="option in CARDIAC_PHASE_OPTIONS" :key="option.id" :value="option.id">{{ option.label }}</option>
          </select>
        </label>
        <label class="field"
          >Reconstruction phase (% R–R) <span class="optional">Optional</span>
          <input
            aria-label="Reconstruction phase (% R–R)"
            type="number"
            min="0"
            max="100"
            step="any"
            inputmode="decimal"
            placeholder="e.g. 69"
            :value="Number.isFinite(modelValue.rr_percent) ? modelValue.rr_percent : ''"
            :disabled="disabled"
            :aria-invalid="rrInvalid"
            :aria-describedby="rrHelpId"
            @input="rr"
          />
        </label>
      </template>
    </div>
    <p v-if="cardiac" :id="rrHelpId" class="acquisition-help">
      Record the reconstruction shown in the selected series, such as Best diastolic at 69% R–R. Non-contrast includes calcium-score reconstructions. Leave
      timing unknown when it is not recorded.
    </p>
    <p class="acquisition-help">
      These labels describe the acquisition. The selected DICOM series determines the images; choose a segmentation task separately.
    </p>
    <p v-if="error && modelValue.phase" class="acquisition-error" role="alert">{{ error }}</p>
  </div>
</template>

<style scoped>
.acquisition-fields {
  margin: 20px 0;
}
.acquisition-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 7px;
  position: relative;
  font-size: 11px;
  color: var(--text-secondary);
}
.field select,
.field input {
  width: 100%;
  min-width: 0;
  padding: 9px 10px;
  border: 1px solid var(--line-strong);
  border-radius: 5px;
  background: var(--surface);
  color: var(--white);
  font-size: 11px;
  font-family: inherit;
}
.field select:focus-visible,
.field input:focus-visible {
  outline: 2px solid var(--accent, #86e6b0);
  outline-offset: 2px;
}
.field input[aria-invalid='true'] {
  border-color: #e99595;
}
.optional {
  font-size: 9px;
  color: var(--text-muted);
  position: absolute;
  right: 0;
  top: 0;
}
.acquisition-help {
  margin: 9px 0 0;
  font-size: 10px;
  line-height: 1.6;
  color: var(--text-secondary);
}
.acquisition-error {
  margin: 8px 0 0;
  font-size: 11px;
  line-height: 1.5;
  color: #f0b3b3;
}
@media (max-width: 600px) {
  .acquisition-grid {
    grid-template-columns: 1fr;
  }
}
</style>

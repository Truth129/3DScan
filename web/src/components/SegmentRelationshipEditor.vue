<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import type { RelationshipSuggestion, SegmentRelationship, Structure } from '../types';
import { normalizeRelationship, relationshipError } from '../lib/interactive-review';
import { prettyName } from '../lib/geometry';
const props = defineProps<{
  modelValue: SegmentRelationship;
  structures: Structure[];
  targetId: string;
  disabled?: boolean;
  suggestions?: RelationshipSuggestion[];
}>();
const emit = defineEmits<{ 'update:modelValue': [value: SegmentRelationship]; validity: [valid: boolean] }>();
const draft = ref(normalizeRelationship(props.modelValue));
const error = computed(() => relationshipError(draft.value, props.targetId, props.structures));
const choices = computed(() => props.structures.filter(item => item.id !== props.targetId));
const hint = computed(() =>
  draft.value.kind === 'independent'
    ? 'Overlap is allowed. Every other mask keeps its voxels.'
    : draft.value.kind === 'subregion'
      ? 'The parent mask is preserved. No other mask loses voxels.'
      : 'Only the checked sibling masks lose voxels that overlap this segment. Parent and child overlap is protected.',
);
function update(next: SegmentRelationship) {
  draft.value = normalizeRelationship(next);
  if (!relationshipError(draft.value, props.targetId, props.structures)) emit('update:modelValue', normalizeRelationship(draft.value));
}
function kind(event: Event) {
  update({ kind: (event.target as HTMLSelectElement).value as SegmentRelationship['kind'], exclusive_with: [] });
}
function parent(event: Event) {
  update({ ...draft.value, parent_id: (event.target as HTMLSelectElement).value || undefined });
}
function toggle(id: string, checked: boolean) {
  update({ ...draft.value, exclusive_with: checked ? [...draft.value.exclusive_with, id] : draft.value.exclusive_with.filter(value => value !== id) });
}
function protectedSibling(id: string) {
  return !!relationshipError({ ...draft.value, kind: 'subdivision', exclusive_with: [id] }, props.targetId, props.structures);
}
function useSuggestion(value: RelationshipSuggestion) {
  update({
    kind: 'subdivision',
    exclusive_with: value.exclusive_with.filter(id => choices.value.some(item => item.id === id) && !protectedSibling(id)),
    parent_id: draft.value.parent_id,
  });
}
watch(
  () => [props.targetId, JSON.stringify(normalizeRelationship(props.modelValue))],
  () => {
    draft.value = normalizeRelationship(props.modelValue);
  },
);
watch(error, value => emit('validity', !value), { immediate: true });
onBeforeUnmount(() => emit('validity', true));
</script>

<template>
  <div class="segment-relationship-editor" :data-relationship="draft.kind">
    <div class="relationship-fields">
      <label
        >Relationship<select aria-label="Segment relationship" :value="draft.kind" :disabled="disabled" @change="kind">
          <option value="independent">Independent</option>
          <option value="subregion">Subregion of</option>
          <option value="subdivision">Subdivision alongside</option>
        </select></label
      >
      <label v-if="draft.kind !== 'independent'"
        >Parent {{ draft.kind === 'subregion' ? '' : '(optional)'
        }}<select aria-label="Parent segment" :value="draft.parent_id || ''" :disabled="disabled" @change="parent">
          <option value="">{{ draft.kind === 'subregion' ? 'Choose parent…' : 'No parent' }}</option>
          <option v-for="item in choices" :key="item.id" :value="item.id">{{ prettyName(item.name) }}</option>
        </select></label
      >
      <p>{{ hint }}</p>
    </div>
    <div v-if="draft.kind === 'subdivision'" class="relationship-siblings" role="group" aria-label="Subdivision siblings">
      <span>Replace overlaps only in:</span>
      <label v-for="item in choices" :key="item.id" :class="{ protected: protectedSibling(item.id) }"
        ><input
          type="checkbox"
          :data-overlap-structure="item.id"
          :aria-label="`Replace overlap with ${prettyName(item.name)}`"
          :checked="draft.exclusive_with.includes(item.id)"
          :disabled="disabled || (protectedSibling(item.id) && !draft.exclusive_with.includes(item.id))"
          @change="toggle(item.id, ($event.target as HTMLInputElement).checked)"
        />{{ prettyName(item.name) }}<small v-if="protectedSibling(item.id)">parent/child protected</small></label
      >
    </div>
    <div v-if="suggestions?.length" class="relationship-suggestions">
      <span>Known anatomy suggestions:</span
      ><button v-for="item in suggestions" :key="item.id" class="text-button" type="button" :disabled="disabled" @click="useSuggestion(item)">
        Use {{ item.name }}</button
      ><small>Applied only when selected.</small>
    </div>
    <p v-if="error" class="relationship-error" role="status">{{ error }}</p>
  </div>
</template>

<style scoped>
.segment-relationship-editor {
  flex-basis: 100%;
  min-width: 0;
  border: 1px solid #39465b;
  border-radius: 5px;
  padding: 9px;
  background: #121923;
  color: #b9c9de;
  font-size: 10px;
}
.relationship-fields {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.relationship-fields > label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 150px;
  font-size: 10px;
}
.relationship-fields select {
  padding: 6px;
  font-size: 11px;
  max-width: 240px;
}
.relationship-fields p {
  flex: 1;
  min-width: 180px;
  margin: 0;
  line-height: 1.5;
  color: #9fb0c9;
}
.relationship-siblings {
  display: flex;
  flex-wrap: wrap;
  gap: 7px 13px;
  margin-top: 9px;
  padding-top: 8px;
  border-top: 1px solid #303c4f;
  max-height: 108px;
  overflow: auto;
}
.relationship-siblings > span {
  flex-basis: 100%;
  color: #c7d5e7;
}
.relationship-siblings label {
  display: flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
  font-size: 10px;
}
.relationship-siblings input {
  accent-color: #86a6eb;
  margin: 0;
}
.relationship-siblings .protected {
  opacity: 0.55;
}
.relationship-siblings small {
  font-size: 9px;
}
.relationship-suggestions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
  color: #9eb1cb;
}
.relationship-suggestions .text-button {
  font-size: 10px;
}
.relationship-suggestions small {
  font-size: 9px;
  color: #8498b6;
}
.relationship-error {
  margin: 7px 0 0;
  color: #ecc28c;
  line-height: 1.4;
}
</style>

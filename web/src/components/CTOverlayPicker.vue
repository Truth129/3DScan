<script setup lang="ts">
import { computed, ref } from 'vue';
import type { Structure } from '../types';
import { colorCss, prettyName } from '../lib/geometry';

const props = defineProps<{ structures: Structure[]; ids: string[]; selected: string; following: boolean; modality?: string }>();
const emit = defineEmits<{ change: [ids: string[]]; follow: [] }>();
const modalityLabel = computed(() => props.modality ?? 'CT');
const search = ref('');
const included = computed(() => new Set(props.ids));
const chosen = computed(() => props.ids.map(id => props.structures.find(s => s.id === id)).filter((s): s is Structure => !!s));
const matches = computed(() => {
  const query = search.value.trim().toLowerCase();
  return props.structures.filter(s => `${s.name} ${s.group}`.toLowerCase().includes(query));
});
function toggle(id: string) {
  emit('change', included.value.has(id) ? props.ids.filter(value => value !== id) : [...props.ids, id]);
}
</script>

<template>
  <section class="ct-overlay-picker" :aria-label="`${modalityLabel} overlay segments`">
    <details>
      <summary><span>Segments</span><span>{{ ids.length }} selected <i>⌄</i></span></summary>
      <div class="ct-overlay-options">
        <div class="ct-overlay-actions"><button class="text-button" :disabled="!selected" @click="emit('follow')">Use selected segment</button><button class="text-button" :disabled="!ids.length" @click="emit('change', [])">Clear overlays</button></div>
        <input v-model="search" type="search" placeholder="Find segments…" :aria-label="`Find ${modalityLabel} overlay segments`" />
        <div class="ct-overlay-list">
          <label v-for="item in matches" :key="item.id" :class="{ checked: included.has(item.id) }">
            <input type="checkbox" :checked="included.has(item.id)" :aria-label="`Overlay ${prettyName(item.name)} on ${modalityLabel}`" @change="toggle(item.id)" />
            <i :style="{ background: colorCss(item.color) }"></i><span>{{ prettyName(item.name) }}</span>
          </label>
          <p v-if="!matches.length" class="help">No matching segments.</p>
        </div>
      </div>
    </details>
    <div v-if="chosen.length" class="ct-overlay-legend" :aria-label="`Selected ${modalityLabel} overlays`">
      <span v-for="item in chosen" :key="item.id" :title="prettyName(item.name)"><i :style="{ background: colorCss(item.color) }"></i>{{ prettyName(item.name) }}</span>
    </div>
    <p class="ct-overlay-hint">{{ following ? 'Follows the selected segment. Check more to review together.' : `Checked segments stay on ${modalityLabel} when you select other anatomy.` }}</p>
  </section>
</template>

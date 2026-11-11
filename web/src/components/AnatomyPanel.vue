<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { ChevronDown, ChevronRight, CircleDot } from '@lucide/vue';
import type { Structure, StructureDisplay } from '../types';
import { colorCss, prettyName } from '../lib/geometry';
const props = defineProps<{ structures: Structure[]; display: StructureDisplay; selected: string }>();
// The workspace opens the selected row to show its colour, opacity and checks; the portable viewer does not.
defineSlots<{ selected?: (props: { structure: Structure }) => unknown }>();
const emit = defineEmits<{
  select: [id: string];
  visibility: [id: string, visible: boolean];
  groupVisibility: [group: string, visible: boolean];
  solo: [id: string];
  all: [];
  hideAll: [];
}>();
const search = ref('');
const collapsed = ref<Record<string, boolean>>({});
const groupOf = (s: Structure) => s.group || 'Anatomy';
const matches = (s: Structure) => `${s.name} ${groupOf(s)}`.toLowerCase().includes(search.value.toLowerCase());
const groups = computed(() => {
  const map = new Map<string, Structure[]>();
  props.structures.forEach(s => {
    const name = groupOf(s);
    map.set(name, [...(map.get(name) || []), s]);
  });
  return [...map.entries()]
    .map(([name, allMembers]) => ({
      name,
      members: allMembers.filter(matches),
      visible: allMembers.filter(s => props.display[s.id]?.visible).length,
      total: allMembers.length,
    }))
    .filter(group => group.members.length);
});
const visibleCount = computed(() => Object.values(props.display).filter(d => d.visible).length);
const hasSelection = computed(() => props.structures.some(structure => structure.id === props.selected));
const list = ref<HTMLElement>();
// A row clicked here stays under the pointer while the open row above it closes.
let anchor: { id: string; top: number } | undefined;
function choose(id: string, event: MouseEvent) {
  anchor = { id, top: (event.currentTarget as HTMLElement).getBoundingClientRect().top };
  emit('select', id);
}
/** Brings the selected row and its details into view, the row first when both do not fit. */
function reveal() {
  const box = list.value,
    row = box?.querySelector<HTMLElement>('.structure-row.selected');
  if (!box || !row) return;
  const details = row.nextElementSibling?.classList.contains('structure-details') ? row.nextElementSibling : row,
    view = box.getBoundingClientRect(),
    top = row.getBoundingClientRect().top,
    bottom = details.getBoundingClientRect().bottom;
  if (top < view.top) box.scrollTop -= view.top - top;
  else if (bottom > view.bottom) box.scrollTop += Math.min(bottom - view.bottom, top - view.top);
}
// A structure picked in 3D, on a slice or from another panel opens even if its group was folded or the
// search hid it, since its details and checks show only under its row.
watch(
  () => props.selected,
  async id => {
    const structure = props.structures.find(s => s.id === id);
    if (!structure) return;
    collapsed.value[groupOf(structure)] = false;
    if (!matches(structure)) search.value = '';
    await nextTick();
    const row = list.value?.querySelector('.structure-row.selected > .structure-select');
    if (list.value && row && anchor?.id === id) list.value.scrollTop += row.getBoundingClientRect().top - anchor.top;
    anchor = undefined;
    reveal();
  },
);
defineExpose({ reveal });
</script>

<template>
  <div class="anatomy-panel">
    <div class="panel-heading">
      <div>
        <span class="eyebrow">ANATOMY</span>
        <h2>
          Structure library <span>{{ structures.length }}</span>
        </h2>
      </div>
    </div>
    <div class="library-actions" role="group" aria-label="Structure visibility">
      <button type="button" class="library-action" title="Show all segmented structures" @click="emit('all')">Show all</button>
      <button type="button" class="library-action" title="Hide all segmented structures" @click="emit('hideAll')">Hide all</button>
      <button
        type="button"
        class="library-action library-selected-action"
        title="Keep only the selected structure visible"
        :disabled="!hasSelection"
        @click="emit('solo', selected)"
      >
        Show selected only
      </button>
    </div>
    <div class="structure-search">
      <span>⌕</span><input v-model="search" type="search" placeholder="Find a structure…" aria-label="Search anatomical structures" /><kbd>/</kbd>
    </div>
    <div class="library-meta">
      <span>{{ visibleCount }} visible</span><span>VOLUME · mL</span>
    </div>
    <div ref="list" class="structure-groups">
      <section v-for="group in groups" :key="group.name" class="structure-group">
        <div class="group-header">
          <button
            type="button"
            class="group-heading"
            :aria-label="`${collapsed[group.name] ? 'Expand' : 'Collapse'} ${prettyName(group.name)} group; ${group.visible} of ${group.total} visible`"
            :title="`${collapsed[group.name] ? 'Expand' : 'Collapse'} ${prettyName(group.name)}`"
            :aria-expanded="!collapsed[group.name]"
            @click="collapsed[group.name] = !collapsed[group.name]"
          >
            <span><ChevronRight v-if="collapsed[group.name]" /><ChevronDown v-else /> {{ prettyName(group.name) }}</span
            ><span :title="`${group.visible} of ${group.total} visible`">{{ group.visible }}/{{ group.total }}</span>
          </button>
          <button
            type="button"
            class="group-visibility-button"
            :aria-label="`${group.visible ? 'Hide' : 'Show'} all ${prettyName(group.name)}`"
            :title="`${group.visible ? 'Hide' : 'Show'} all ${prettyName(group.name)}`"
            :aria-pressed="group.visible === 0 ? false : group.visible === group.total ? true : 'mixed'"
            @click="emit('groupVisibility', group.name, group.visible === 0)"
          >
            <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
              <path d="M2 10s3-5 8-5 8 5 8 5-3 5-8 5-8-5-8-5Z" />
              <circle v-if="group.visible === group.total" cx="10" cy="10" r="2" />
              <path v-else-if="group.visible" d="M7 10h6" />
              <path v-else d="m3 3 14 14" />
            </svg>
          </button>
        </div>
        <div v-if="!collapsed[group.name]">
          <template v-for="structure in group.members" :key="structure.id">
            <div class="structure-row" :class="{ selected: selected === structure.id, hidden: !display[structure.id]?.visible }">
              <button
                type="button"
                class="structure-select"
                :title="`Select ${prettyName(structure.name)}`"
                :aria-label="`Select ${prettyName(structure.name)}`"
                :aria-pressed="selected === structure.id"
                @click="choose(structure.id, $event)"
              >
                <i :style="{ background: colorCss(structure.color) }"></i><span>{{ prettyName(structure.name) }}</span
                ><small>{{ Number(structure.volume_ml).toLocaleString(undefined, { maximumFractionDigits: 1 }) }}</small>
              </button>
              <button
                type="button"
                class="solo-button"
                :aria-label="`Isolate ${prettyName(structure.name)}`"
                :title="`Show only ${prettyName(structure.name)}; hide other structures`"
                @click.stop="emit('solo', structure.id)"
              >
                <CircleDot />
              </button>
              <button
                type="button"
                class="visibility-button"
                :aria-label="`${display[structure.id]?.visible ? 'Hide' : 'Show'} ${prettyName(structure.name)}`"
                :title="`${display[structure.id]?.visible ? 'Hide' : 'Show'} ${prettyName(structure.name)}`"
                :aria-pressed="!!display[structure.id]?.visible"
                @click.stop="emit('visibility', structure.id, !display[structure.id]?.visible)"
              >
                <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
                  <path d="M2 10s3-5 8-5 8 5 8 5-3 5-8 5-8-5-8-5Z" />
                  <circle cx="10" cy="10" r="2" />
                  <path v-if="!display[structure.id]?.visible" d="m3 3 14 14" />
                </svg>
              </button>
            </div>
            <div v-if="selected === structure.id && $slots.selected" class="structure-details">
              <slot name="selected" :structure="structure" />
            </div>
          </template>
        </div>
      </section>
      <p v-if="!groups.length" class="muted empty-search">No structures match “{{ search }}”.</p>
    </div>
    <div class="library-footer"><span class="live-dot"></span> Volumes measured from segmentation masks</div>
  </div>
</template>

<style scoped>
/* Selection fills the row; visibility is the stable, rightmost action. */
.structure-row {
  gap: 5px;
  padding-left: 10px;
  min-height: 38px;
}
.structure-select {
  min-height: 36px;
  padding-right: 3px;
}
.visibility-button {
  flex: 0 0 32px;
  width: 32px;
  height: 30px;
  border: 1px solid var(--line);
  border-radius: 5px;
  background: var(--surface);
  color: var(--text-secondary);
}
.visibility-button svg {
  width: 17px;
  height: 17px;
  stroke-width: 1.35;
}
.visibility-button[aria-pressed='false'] {
  color: var(--muted);
  background: transparent;
}
.visibility-button:hover {
  color: var(--white);
  background: var(--surface-active);
  border-color: var(--line-strong);
}
.visibility-button:focus-visible,
.structure-select:focus-visible,
.solo-button:focus-visible,
.group-visibility-button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.solo-button {
  flex: 0 0 24px;
  width: 24px;
  height: 30px;
  padding: 2px;
  border-radius: 4px;
}
.solo-button:hover {
  background: var(--surface-active);
  color: var(--white);
}
.library-meta > span:last-child {
  margin-right: 67px;
}
/* The selected row opens below itself on the row's background and accent edge, so the two read as one. */
.structure-details {
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding: 3px 12px 12px 18px;
  border-left: 2px solid var(--accent);
}
@media (max-width: 1000px) {
  .structure-row {
    min-height: 43px;
  }
  .visibility-button {
    flex-basis: 35px;
    width: 35px;
    height: 36px;
  }
  .solo-button {
    flex-basis: 33px;
    width: 33px;
    height: 36px;
  }
  .library-meta > span:last-child {
    margin-right: 82px;
  }
}
</style>

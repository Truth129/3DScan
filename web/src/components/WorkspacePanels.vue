<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId } from 'vue';
import {
  applyWorkspacePreset,
  canHideWorkspacePanel,
  defaultWorkspaceLayout,
  defaultWorkspacePanels,
  setWorkspacePanel,
  toggleWorkspaceSidebar,
  workspacePreset,
  type WorkspaceLayoutState,
  type WorkspacePanelId,
  type WorkspacePanelsState,
  type WorkspacePreset,
} from '../lib/workspace-panels';

const props = withDefaults(defineProps<{ modelValue: WorkspacePanelsState; layout?: WorkspaceLayoutState; reviewLabel?: string }>(), {
  layout: defaultWorkspaceLayout,
  reviewLabel: 'CT review',
});
const emit = defineEmits<{ 'update:modelValue': [value: WorkspacePanelsState]; 'update:layout': [value: WorkspaceLayoutState] }>();
const root = ref<HTMLElement>(),
  trigger = ref<HTMLButtonElement>();
const open = ref(false),
  panelId = useId(),
  helpId = useId();
const options = computed(() => [
  { id: 'anatomy' as const, label: 'Anatomy' },
  { id: 'model' as const, label: '3D view' },
  { id: 'review' as const, label: props.reviewLabel },
  { id: 'inspector' as const, label: 'Selection & history' },
]);
const presets = computed(() => [
  { id: 'columns' as const, label: `3D and ${props.reviewLabel} side by side` },
  { id: 'rows' as const, label: `3D above ${props.reviewLabel}` },
  { id: 'model' as const, label: '3D view only' },
  { id: 'review' as const, label: `${props.reviewLabel} only` },
]);
const preset = computed(() => workspacePreset(props.modelValue, props.layout));
const sidebarShown = computed(() => props.modelValue.anatomy || props.modelValue.inspector);

function disabled(id: WorkspacePanelId): boolean {
  return props.modelValue[id] && !canHideWorkspacePanel(props.modelValue, id);
}
function change(id: WorkspacePanelId, event: Event) {
  emit('update:modelValue', setWorkspacePanel(props.modelValue, id, (event.target as HTMLInputElement).checked));
}
function choose(id: WorkspacePreset) {
  const next = applyWorkspacePreset(props.modelValue, props.layout, id);
  emit('update:modelValue', next.panels);
  emit('update:layout', next.layout);
}
function reset() {
  emit('update:modelValue', defaultWorkspacePanels());
  emit('update:layout', defaultWorkspaceLayout());
}
async function close(restoreFocus: boolean) {
  if (!open.value) return;
  open.value = false;
  if (restoreFocus) {
    await nextTick();
    trigger.value?.focus({ preventScroll: true });
  }
}
function outside(event: PointerEvent) {
  if (open.value && event.target instanceof Node && !root.value?.contains(event.target)) void close(true);
}
function keyboard(event: KeyboardEvent) {
  if (open.value && event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    void close(true);
  }
}
onMounted(() => {
  document.addEventListener('pointerdown', outside);
  document.addEventListener('keydown', keyboard);
});
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', outside);
  document.removeEventListener('keydown', keyboard);
});
</script>

<template>
  <div ref="root" class="workspace-panels">
    <div class="layout-presets" role="group" aria-label="Workspace layout">
      <button
        type="button"
        class="layout-preset"
        :aria-pressed="sidebarShown"
        :aria-label="sidebarShown ? 'Hide sidebar' : 'Show sidebar'"
        :title="sidebarShown ? 'Hide sidebar' : 'Show sidebar'"
        @click="emit('update:modelValue', toggleWorkspaceSidebar(modelValue))"
      >
        <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <rect x="2.5" y="3.5" width="15" height="13" rx="1.5" />
          <path d="M7.5 3.5v13" />
          <path class="layout-fill" d="M3.5 4.5h3v11h-3z" />
        </svg></button
      ><span class="layout-divider" aria-hidden="true"></span
      ><button
        v-for="item in presets"
        :key="item.id"
        type="button"
        class="layout-preset"
        :aria-pressed="preset === item.id"
        :aria-label="item.label"
        :title="item.label"
        @click="choose(item.id)"
      >
        <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <rect x="2.5" y="3.5" width="15" height="13" rx="1.5" />
          <template v-if="item.id === 'columns'">
            <path d="M11 3.5v13" />
            <path class="layout-fill" d="M3.5 4.5h6.5v11H3.5z" />
          </template>
          <template v-else-if="item.id === 'rows'">
            <path d="M2.5 10.5h15" />
            <path class="layout-fill" d="M3.5 4.5h13v5h-13z" />
          </template>
          <path v-else-if="item.id === 'model'" class="layout-fill" d="M3.5 4.5h13v11h-13z" />
          <template v-else>
            <path d="M7.5 3.5v13m5-13v13" />
            <path class="layout-fill layout-fill-faint" d="M3.5 4.5h13v11h-13z" />
          </template>
        </svg>
      </button>
    </div>
    <button
      ref="trigger"
      type="button"
      class="button secondary compact panels-trigger"
      aria-label="Panels"
      :aria-expanded="open"
      :aria-controls="panelId"
      @click="open = !open"
    >
      <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
        <rect x="2.5" y="3.5" width="15" height="13" rx="1.5" />
        <path d="M7 4v12m6-12v12" />
      </svg>
      Panels <span aria-hidden="true">{{ open ? '⌃' : '⌄' }}</span>
    </button>
    <section v-if="open" :id="panelId" class="panels-popover" aria-label="Workspace panels" :aria-describedby="helpId">
      <div class="panels-heading">
        <strong>Workspace panels</strong><button type="button" class="panels-close" aria-label="Close panel options" @click="close(true)">×</button>
      </div>
      <p :id="helpId">Show the sections you need. Keep at least one viewer open. Drag the dividers between areas to resize them.</p>
      <label v-for="option in options" :key="option.id" class="panel-option" :class="{ disabled: disabled(option.id) }">
        <input
          type="checkbox"
          :checked="modelValue[option.id]"
          :disabled="disabled(option.id)"
          :aria-label="option.label"
          :aria-controls="`workspace-${option.id}-panel`"
          @change="change(option.id, $event)"
        />
        <span>{{ option.label }}<small v-if="option.id === 'inspector'">Below the anatomy list</small></span>
      </label>
      <button type="button" class="panel-reset" @click="reset">Reset layout</button>
    </section>
  </div>
</template>

<style scoped>
.workspace-panels {
  position: relative;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}
.layout-presets {
  display: flex;
  align-items: center;
  padding: 2px;
  border: 1px solid var(--control-border);
  border-radius: 6px;
  background: var(--surface);
}
.layout-preset {
  display: grid;
  place-items: center;
  width: 28px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}
.layout-preset svg {
  width: 17px;
  height: 17px;
  stroke: currentColor;
  stroke-width: 1.3;
}
.layout-preset .layout-fill {
  fill: currentColor;
  stroke: none;
  opacity: 0.28;
}
.layout-preset .layout-fill-faint {
  opacity: 0.14;
}
.layout-preset:hover {
  background: var(--surface-hover);
  color: var(--white);
}
.layout-preset[aria-pressed='true'] {
  background: var(--surface-active);
  color: var(--white);
}
.layout-preset[aria-pressed='true'] .layout-fill {
  opacity: 0.5;
}
.layout-preset:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.layout-divider {
  width: 1px;
  height: 16px;
  margin: 0 3px;
  background: var(--control-border);
}
/* Narrow screens show one view at a time through the bottom navigation. */
@media (max-width: 1000px) {
  .layout-presets {
    display: none;
  }
}
.panels-trigger {
  gap: 6px;
  white-space: nowrap;
}
.panels-trigger svg {
  width: 15px;
  height: 15px;
  stroke: currentColor;
  stroke-width: 1.4;
}
.panels-trigger[aria-expanded='true'] {
  border-color: var(--control-border-hover);
  background: var(--surface-hover);
  color: var(--white);
}
.panels-popover {
  position: absolute;
  top: calc(100% + 8px);
  left: 0;
  width: 248px;
  max-width: calc(100vw - 28px);
  padding: 12px;
  z-index: 65;
  border: 1px solid var(--control-border);
  border-radius: 7px;
  background: var(--panel);
  box-shadow: 0 12px 32px #0008;
  color: var(--white);
}
.panels-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.panels-heading strong {
  font-size: 12px;
  font-weight: 600;
}
.panels-close {
  display: grid;
  place-items: center;
  width: 25px;
  height: 25px;
  padding: 0;
  background: var(--surface);
  border: 1px solid var(--control-border);
  border-radius: 4px;
  color: var(--text-secondary);
  font-size: 19px;
  cursor: pointer;
}
.panels-popover > p {
  margin: 5px 0 9px;
  color: var(--text-secondary);
  font-size: 10px;
  line-height: 1.6;
}
.panel-option {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 7px;
  border-radius: 4px;
  font-size: 12px;
  cursor: pointer;
}
.panel-option:hover {
  background: var(--surface-hover);
}
.panel-option input {
  flex-shrink: 0;
  width: 15px;
  height: 15px;
  margin: 0;
  accent-color: var(--accent);
  cursor: pointer;
}
.panel-option span {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.panel-option small {
  font-size: 9px;
  color: var(--text-secondary);
}
.panel-option.disabled {
  cursor: default;
  opacity: 0.6;
}
.panel-option.disabled input {
  cursor: default;
}
.panel-reset {
  display: block;
  width: 100%;
  margin-top: 10px;
  padding: 8px;
  border: 1px solid var(--control-border);
  border-radius: 4px;
  background: var(--surface);
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
}
.panel-reset:hover,
.panels-close:hover {
  border-color: var(--control-border-hover);
  background: var(--surface-hover);
  color: var(--white);
}
</style>

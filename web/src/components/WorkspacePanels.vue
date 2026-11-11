<script setup lang="ts">
import { computed, ref, useId } from 'vue';
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
import { usePopover } from '../lib/use-popover';

/** `focus` names the slice view's modality in review, where the icons focus a tile. */
const props = withDefaults(defineProps<{ modelValue: WorkspacePanelsState; layout?: WorkspaceLayoutState; reviewLabel?: string; focus?: string }>(), {
  layout: defaultWorkspaceLayout,
  reviewLabel: 'CT review',
});
const emit = defineEmits<{ 'update:modelValue': [value: WorkspacePanelsState]; 'update:layout': [value: WorkspaceLayoutState] }>();
const root = ref<HTMLElement>(),
  trigger = ref<HTMLButtonElement>(),
  popover = ref<HTMLElement>();
const panelId = useId(),
  helpId = useId();
const { open, close, toggle } = usePopover(root, trigger, popover);
const options = computed(() => [
  { id: 'model' as const, label: '3D view' },
  { id: 'review' as const, label: props.reviewLabel },
]);
const labels = computed<Record<WorkspacePreset, string>>(() =>
  props.focus
    ? { columns: `${props.focus} + 3D`, rows: `${props.focus} + 3D`, model: 'Focus 3D', review: `Focus ${props.focus}` }
    : {
        columns: `3D and ${props.reviewLabel} side by side`,
        rows: `3D above ${props.reviewLabel}`,
        model: '3D view only',
        review: `${props.reviewLabel} only`,
      },
);
const preset = computed(() => workspacePreset(props.modelValue, props.layout));
// The first icon shows both views in the arrangement they last had; the menu switches between the two.
const quick = computed(() => [props.layout.arrangement, 'model' as const, 'review' as const]);
const arrangements = ['columns', 'rows', 'model', 'review'] as const;

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
// The side panel's tab is where the reader was, not part of the layout.
function reset() {
  emit('update:modelValue', { ...defaultWorkspacePanels(), tab: props.modelValue.tab });
  emit('update:layout', defaultWorkspaceLayout());
}
</script>

<template>
  <div ref="root" class="workspace-panels">
    <div class="layout-presets" role="group" aria-label="Workspace layout">
      <button
        v-for="item in quick"
        :key="item"
        type="button"
        class="layout-preset"
        :aria-pressed="preset === item"
        :aria-label="labels[item]"
        :title="labels[item]"
        @click="choose(item)"
      >
        <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <rect x="2.5" y="3.5" width="15" height="13" rx="1.5" />
          <template v-if="item === 'columns'">
            <path d="M11 3.5v13" />
            <path class="layout-fill" d="M3.5 4.5h6.5v11H3.5z" />
          </template>
          <template v-else-if="item === 'rows'">
            <path d="M2.5 10.5h15" />
            <path class="layout-fill" d="M3.5 4.5h13v5h-13z" />
          </template>
          <path v-else-if="item === 'model'" class="layout-fill" d="M3.5 4.5h13v11h-13z" />
          <template v-else>
            <path d="M7.5 3.5v13m5-13v13" />
            <path class="layout-fill layout-fill-faint" d="M3.5 4.5h13v11h-13z" />
          </template>
        </svg></button
      ><button
        ref="trigger"
        type="button"
        class="layout-preset panels-trigger"
        aria-label="Layout and panels"
        title="Layout and panels"
        :aria-expanded="open"
        :aria-controls="panelId"
        @click="toggle"
      >
        <span aria-hidden="true">▾</span>
      </button>
    </div>
    <section v-if="open" :id="panelId" ref="popover" class="panels-popover" aria-label="Workspace panels" :aria-describedby="helpId">
      <div class="panels-heading">
        <strong>Layout</strong><button type="button" class="panels-close" aria-label="Close panel options" @click="close()">×</button>
      </div>
      <div class="panels-arrangement" role="group" aria-label="Arrangement">
        <button v-for="item in arrangements" :key="item" type="button" class="panels-arrangement-option" :aria-pressed="preset === item" @click="choose(item)">
          {{ item === 'columns' ? 'Side by side' : item === 'rows' ? 'Stacked' : labels[item] }}
        </button>
      </div>
      <p :id="helpId">Show the sections you need. Keep at least one viewer open. Drag the dividers between areas to resize them.</p>
      <label class="panel-option side-panels-option">
        <input
          type="checkbox"
          :checked="modelValue.sidebar"
          aria-controls="workspace-sidebar"
          aria-keyshortcuts="\"
          @change="emit('update:modelValue', toggleWorkspaceSidebar(modelValue))"
        />
        <span>Side panel<small>Or press \</small></span>
      </label>
      <label v-for="option in options" :key="option.id" class="panel-option" :class="{ disabled: disabled(option.id) }">
        <input
          type="checkbox"
          :checked="modelValue[option.id]"
          :disabled="disabled(option.id)"
          :aria-label="option.label"
          :aria-controls="`workspace-${option.id}-panel`"
          @change="change(option.id, $event)"
        />
        <span>{{ option.label }}</span>
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
}
.layout-presets {
  display: flex;
  align-items: center;
  gap: 1px;
  padding: 2px;
  border: 1px solid var(--control-border);
  border-radius: 6px;
  background: var(--surface);
}
.layout-preset {
  display: grid;
  place-items: center;
  width: 28px;
  height: 24px;
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
.panels-trigger {
  width: 22px;
  margin-left: 2px;
  border-left: 1px solid var(--control-border);
  border-radius: 0 4px 4px 0;
  font-size: 11px;
}
.panels-trigger[aria-expanded='true'] {
  background: var(--surface-hover);
  color: var(--white);
}
.panels-popover {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: 264px;
  max-width: calc(100vw - 28px);
  padding: 12px;
  /* Over the error strip, like the other top-bar popovers. */
  z-index: 1010;
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
.panels-arrangement {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px;
  margin-top: 10px;
}
.panels-arrangement-option {
  min-height: 28px;
  padding: 5px 7px;
  overflow: hidden;
  border: 1px solid var(--control-border);
  border-radius: 4px;
  background: var(--surface);
  color: var(--text-secondary);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
}
.panels-arrangement-option[aria-pressed='true'] {
  border-color: var(--accent);
  background: var(--surface-active);
  color: var(--white);
}
.panels-popover > p {
  margin: 9px 0 6px;
  color: var(--text-secondary);
  font-size: 10px;
  line-height: 1.6;
}
.panel-option {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 7px;
  border-radius: 4px;
  font-size: 12px;
  cursor: pointer;
}
.panel-option:hover {
  background: var(--surface-hover);
}
.side-panels-option {
  margin-bottom: 2px;
  border-bottom: 1px solid var(--line);
  border-radius: 4px 4px 0 0;
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
.panels-close:hover,
.panels-arrangement-option:hover {
  border-color: var(--control-border-hover);
  background: var(--surface-hover);
  color: var(--white);
}
</style>

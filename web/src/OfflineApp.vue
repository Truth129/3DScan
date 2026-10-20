<script setup lang="ts">
import { computed, ref } from 'vue';
import AnatomyPanel from './components/AnatomyPanel.vue';
import ModelViewport from './components/ModelViewport.vue';
import { colorCss, initialDisplay, prettyName, setGroupVisibility } from './lib/geometry';
import type { OfflineManifest, Point } from './types';
const props = defineProps<{ manifest: OfflineManifest; data: ArrayBuffer }>();
const selected = ref(props.manifest.structures[0]?.id || ''), display = ref(initialDisplay(props.manifest.structures)), point = ref<Point>();
const model = ref<InstanceType<typeof ModelViewport>>(), mobile = ref(false);
const structure = computed(() => props.manifest.structures.find(s => s.id === selected.value));
const synthetic = computed(() => props.manifest.synthetic || /synthetic|demo/i.test(props.manifest.source || ''));
function solo(id: string) { Object.entries(display.value).forEach(([key, value]) => { value.visible = key === id; }); selected.value = id; }
function all() { Object.values(display.value).forEach(state => { state.visible = true; }); }
function hideAll() { Object.values(display.value).forEach(state => { state.visible = false; }); }
function groupVisibility(group: string, visible: boolean) {
  setGroupVisibility(props.manifest.structures, display.value, group, visible);
}
</script>

<template>
  <div class="app-shell offline-shell">
    <header class="app-header"><a class="brand" href="#"><svg viewBox="0 0 30 34" fill="none"><path d="m15 2 13 7.5v15L15 32 2 24.5v-15L15 2Z"/><path d="M15 2v15m0 0L2 9.5M15 17l13-7.5M15 17v15"/></svg><span>3D<span>Scan</span></span></a><div class="header-divider"></div><span class="header-context">PORTABLE ANATOMY</span><span class="offline-badge"><span class="live-dot"></span> Works offline · embedded model</span><button class="button secondary compact" @click="model?.screenshot()">Save PNG ↗</button></header>
    <div class="case-toolbar"><div class="case-title"><span class="eyebrow">{{ synthetic ? 'SYNTHETIC DEMONSTRATION · OFFLINE VIEWER' : 'OFFLINE VIEWER' }}</span><h1>{{ manifest.label }}</h1></div><span class="status-dot" :class="manifest.review_status">{{ manifest.review_status }}</span><div class="portable-note">{{ synthetic ? 'Synthetic geometric phantom · no patient data or inference' : 'Surface review · source CT available in the local application' }}</div></div>
    <main class="workspace offline-workspace" :class="{ 'show-anatomy': mobile }"><aside class="anatomy-rail"><AnatomyPanel :structures="manifest.structures" :display="display" :selected="selected" @select="selected = $event" @visibility="(id, visible) => { display[id].visible = visible; }" @group-visibility="groupVisibility" @solo="solo" @all="all" @hide-all="hideAll" /></aside><section class="central-workspace"><ModelViewport ref="model" :model-data="data" :structures="manifest.structures" :display="display" :selected="selected" :point="point" :label="manifest.label" @pick="(id, p) => { selected = id; point = p; }" /><section v-if="structure" class="inspector"><div class="selection-content"><div class="selection-identity"><i :style="{ background: colorCss(structure.color) }"></i><div><span class="eyebrow">{{ prettyName(structure.group) }}</span><h2>{{ prettyName(structure.name) }}</h2><span class="selection-source">Mask-derived measurement</span></div></div><div class="measurement"><span class="eyebrow">VOLUME</span><strong>{{ Number(structure.volume_ml).toLocaleString(undefined, { maximumFractionDigits: 1 }) }}<small>mL</small></strong></div><div class="selection-controls"><label>Opacity <span>{{ Math.round(display[selected].opacity * 100) }}%</span><input v-model.number="display[selected].opacity" type="range" min="0.05" max="1" step="0.01" /></label><div class="button-row"><button class="button secondary compact" @click="solo(selected)">Isolate</button><button class="button secondary compact" @click="model?.focus()">Focus ↗</button></div></div></div></section></section></main>
    <nav class="mobile-navigation"><button :class="{ active: mobile }" @click="mobile = true">☷ Anatomy</button><button :class="{ active: !mobile }" @click="mobile = false">◇ 3D model</button></nav><footer class="app-footer"><span><span class="live-dot"></span> PORTABLE VIEWER</span><span>{{ manifest.structures.length }} embedded structures</span><span>Verify anatomy on the source CT</span></footer>
  </div>
</template>

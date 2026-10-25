<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef } from 'vue';
import AnatomyPanel from './components/AnatomyPanel.vue';
import ModelViewport from './components/ModelViewport.vue';
import { colorCss, initialDisplay, prettyName, setGroupVisibility } from './lib/geometry';
import { revisionUrl } from './lib/api';
import { EmbeddedCTSession, normalizePortableState, portableDisplay, portablePoint, type PortablePayload, type PortableProgress } from './lib/portable-viewer';
import type { OfflineManifest, Point } from './types';

const props = defineProps<{ manifest: OfflineManifest; data?: ArrayBuffer; payload?: PortablePayload }>();
const state = normalizePortableState(props.payload?.state, props.manifest.structures, props.manifest.geometry);
const layers = props.payload?.layers ?? { segmentation: true, ct_volume: false, ct_plane: false };
const selected = ref(state.selected), display = ref(props.payload ? portableDisplay(state) : initialDisplay(props.manifest.structures));
const point = ref<Point | undefined>(props.manifest.geometry ? portablePoint(state, props.manifest.geometry) : undefined);
const model = ref<InstanceType<typeof ModelViewport>>(), mobile = ref(false);
const windowWidth = ref(state.plane.window), level = ref(state.plane.level), overlayIds = ref([...state.plane.overlays]);
const progress = ref<PortableProgress>(), cacheDiagnostics = ref(''), ctError = ref('');
const session = shallowRef<EmbeddedCTSession>();
if (props.payload && (layers.ct_plane || layers.ct_volume)) {
  try {
    const value = new EmbeddedCTSession(props.payload, props.manifest, revisionUrl('offline', 'offline'), update => {
      progress.value = update; cacheDiagnostics.value = JSON.stringify(session.value?.diagnostics ?? {});
    });
    session.value = value; cacheDiagnostics.value = JSON.stringify(value.diagnostics);
  } catch (reason) { ctError.value = reason instanceof Error ? reason.message : 'Embedded CT could not be prepared.'; }
}
const surfaceStructures = computed(() => layers.segmentation ? props.manifest.structures : []);
const maskStructures = computed(() => props.manifest.structures.filter(item => session.value?.maskIds.includes(item.id)));
const overlayStructures = computed(() => props.manifest.structures.filter(item => session.value?.overlayIds.includes(item.id)));
const overlays = computed(() => overlayStructures.value.filter(item => overlayIds.value.includes(item.id)));
const structure = computed(() => surfaceStructures.value.find(item => item.id === selected.value));
const synthetic = computed(() => props.manifest.synthetic || /synthetic|demo/i.test(props.manifest.source || ''));
const included = computed(() => [layers.segmentation && 'Segmentation', layers.ct_volume && 'CT volume', layers.ct_plane && 'CT plane'].filter(Boolean).join(' · '));
const preparation = computed(() => progress.value && progress.value.loadedBytes < progress.value.totalBytes
  ? `Preparing embedded ${progress.value.stage === 'ct' ? 'CT' : 'masks'} · ${(progress.value.loadedBytes / 1024 ** 2).toFixed(1)} MB read` : 'Embedded CT · local GPU rendering');
function solo(id: string) { Object.entries(display.value).forEach(([key, value]) => { value.visible = key === id; }); selected.value = id; }
function all() { Object.values(display.value).forEach(value => { value.visible = true; }); }
function hideAll() { Object.values(display.value).forEach(value => { value.visible = false; }); }
function groupVisibility(group: string, visible: boolean) { setGroupVisibility(props.manifest.structures, display.value, group, visible); }
function toggleOverlay(id: string, enabled: boolean) {
  overlayIds.value = enabled ? [...new Set([...overlayIds.value, id])] : overlayIds.value.filter(value => value !== id);
}
onBeforeUnmount(() => session.value?.dispose());
</script>

<template>
  <div class="app-shell offline-shell" :data-offline-layers="JSON.stringify(layers)" :data-offline-cache="cacheDiagnostics" :data-offline-ct-ready="!!session">
    <header class="app-header">
      <a class="brand" href="#"><svg viewBox="0 0 30 34" fill="none"><path d="m15 2 13 7.5v15L15 32 2 24.5v-15L15 2Z"/><path d="M15 2v15m0 0L2 9.5M15 17l13-7.5M15 17v15"/></svg><span>3D<span>Scan</span></span></a>
      <div class="header-divider"></div><span class="header-context">PORTABLE VIEWER</span>
      <span class="offline-badge"><span class="live-dot"></span> Works offline · embedded rendering stack</span>
      <button class="button secondary compact" @click="model?.screenshot()">Save PNG ↗</button>
    </header>
    <div class="case-toolbar"><div class="case-title"><span class="eyebrow">{{ synthetic ? 'SYNTHETIC DEMONSTRATION · OFFLINE VIEWER' : 'OFFLINE VIEWER' }}</span><h1>{{ manifest.label }}</h1></div><span class="status-dot" :class="manifest.review_status">{{ manifest.review_status }}</span><div class="portable-note">{{ included }}</div></div>
    <div v-if="session" class="offline-ct-controls">
      <label>Window <input v-model.number="windowWidth" type="number" aria-label="Offline CT window" min="1" max="20000" step="10" /></label>
      <label>Level <input v-model.number="level" type="number" aria-label="Offline CT level" min="-10000" max="20000" step="10" /></label>
      <details v-if="layers.ct_plane && overlayStructures.length" class="offline-overlays"><summary>CT overlays · {{ overlayIds.length }}</summary><div><label v-for="item in overlayStructures" :key="item.id"><input type="checkbox" :aria-label="`Offline overlay ${prettyName(item.name)}`" :checked="overlayIds.includes(item.id)" @change="toggleOverlay(item.id, ($event.target as HTMLInputElement).checked)"/><i :style="{ background: colorCss(item.color) }"></i>{{ prettyName(item.name) }}</label></div></details>
      <span role="status">{{ preparation }}</span>
    </div>
    <p v-if="ctError" class="offline-error" role="alert">{{ ctError }}</p>
    <main class="workspace offline-workspace" :class="{ 'show-anatomy': mobile, 'offline-no-anatomy': !surfaceStructures.length }">
      <aside v-if="surfaceStructures.length" class="anatomy-rail"><AnatomyPanel :structures="surfaceStructures" :display="display" :selected="selected" @select="selected = $event" @visibility="(id, visible) => { display[id].visible = visible; }" @group-visibility="groupVisibility" @solo="solo" @all="all" @hide-all="hideAll" /></aside>
      <section class="central-workspace">
        <ModelViewport ref="model" :model-data="layers.segmentation ? data : undefined" :structures="surfaceStructures" :mask-structures="maskStructures"
          :display="display" :selected="selected" :point="point" :label="manifest.label" :geometry="manifest.geometry"
          :case-id="session ? 'offline' : undefined" :revision-id="session ? 'offline' : undefined" :local-session="session" :mask-source-provider="session"
          :viewport-active="true" :local-ready="!!session" :full-resolution="!!session" :available-layers="layers" :initial-snapshot="payload ? { layers, state } : undefined"
          :window="Math.max(1, Number(windowWidth) || 400)" :level="Number(level) || 0" :overlays="overlays"
          @point="point = $event" @pick="(id, p) => { selected = id; point = p; }" />
        <section v-if="structure" class="inspector"><div class="selection-content"><div class="selection-identity"><i :style="{ background: colorCss(structure.color) }"></i><div><span class="eyebrow">{{ prettyName(structure.group) }}</span><h2>{{ prettyName(structure.name) }}</h2><span class="selection-source">Mask-derived measurement</span></div></div><div class="measurement"><span class="eyebrow">VOLUME</span><strong>{{ Number(structure.volume_ml).toLocaleString(undefined, { maximumFractionDigits: 1 }) }}<small>mL</small></strong></div><div class="selection-controls"><label>Opacity <span>{{ Math.round(display[selected].opacity * 100) }}%</span><input v-model.number="display[selected].opacity" type="range" min="0.05" max="1" step="0.01" /></label><div class="button-row"><button class="button secondary compact" @click="solo(selected)">Isolate</button><button class="button secondary compact" @click="model?.focus()">Focus ↗</button></div></div></div></section>
      </section>
    </main>
    <nav v-if="surfaceStructures.length" class="mobile-navigation"><button :class="{ active: mobile }" @click="mobile = true">☷ Anatomy</button><button :class="{ active: !mobile }" @click="mobile = false">◇ 3D model</button></nav>
    <footer class="app-footer"><span><span class="live-dot"></span> PORTABLE VIEWER</span><span>{{ included }}</span><span>{{ manifest.review_status }}</span></footer>
  </div>
</template>

<style scoped>
.offline-no-anatomy{grid-template-columns:minmax(320px,1fr)}
.offline-ct-controls{display:flex;align-items:center;flex-wrap:wrap;gap:10px;padding:7px 18px;background:#171c24;border-bottom:1px solid #354052;font-size:10px;color:#aebbd0}
.offline-ct-controls>label{display:flex;align-items:center;gap:6px}.offline-ct-controls input[type=number]{width:76px;padding:5px 7px;font-size:11px}.offline-ct-controls>span{margin-left:auto;font-size:10px}
.offline-overlays{position:relative}.offline-overlays summary{cursor:pointer;border:1px solid #485469;border-radius:4px;padding:6px 8px}.offline-overlays>div{position:absolute;top:100%;left:0;z-index:30;min-width:240px;max-height:230px;overflow:auto;padding:8px;background:#171c24;border:1px solid #485469;border-radius:5px;box-shadow:0 10px 30px #0008}.offline-overlays label{display:flex;align-items:center;gap:7px;padding:5px;white-space:nowrap}.offline-overlays i{width:8px;height:8px;border-radius:50%}
.offline-error{padding:10px 18px;color:#edb5b5;background:#291c23;margin:0;font-size:12px}
</style>

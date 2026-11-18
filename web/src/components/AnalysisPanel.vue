<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue';
import CoronaryPanel from './CoronaryPanel.vue';
import ReportPanel from './ReportPanel.vue';
import AortaReport from './AortaReport.vue';
import { hasCoronaryLumen, type CoronaryTube } from '../lib/coronary';
import { ANALYSIS_STORAGE_KEY, type AortaResult, type ReportOverlay } from '../lib/reports';
import type { ReportExportRequest } from '../lib/dicom-view';
import type { Job, Point, Revision } from '../types';

/** The side panel's Analysis tab: one chooser, then the chosen analysis. A later report adds one entry here. */
const props = defineProps<{ caseId: string; revision: Revision; point?: Point; native: boolean; busy?: boolean }>();
const emit = defineEmits<{
  select: [id: string, point: Point];
  tube: [tube: CoronaryTube | undefined];
  overlay: [overlay: ReportOverlay | undefined];
  job: [job: Job];
  dicom: [request: ReportExportRequest];
}>();

const ANALYSES = [
  { id: 'coronary', label: 'Coronary FFR' },
  { id: 'aorta', label: 'Aorta' },
] as const;
type AnalysisId = (typeof ANALYSES)[number]['id'];

function remembered(): AnalysisId | undefined {
  try {
    const stored = localStorage.getItem(ANALYSIS_STORAGE_KEY);
    return ANALYSES.find(item => item.id === stored)?.id;
  } catch {
    return undefined;
  }
}
const lumen = computed(() => hasCoronaryLumen(props.revision.structures));
// Until someone chooses, the default follows the revision: FFR where there is a lumen to analyse.
const stored = ref(remembered());
const choice = computed<AnalysisId>(() => stored.value ?? (lumen.value ? 'coronary' : 'aorta'));
function choose(event: Event) {
  const id = ANALYSES.find(item => item.id === (event.target as HTMLSelectElement).value)?.id;
  if (!id) return;
  stored.value = id;
  try {
    localStorage.setItem(ANALYSIS_STORAGE_KEY, id);
  } catch {
    /* The choice still holds for this page. */
  }
}
// Coronary FFR stays mounted once opened, so a look at another analysis keeps its inputs, the analysis
// chosen and a run being followed. Its tube draws only while it is the analysis on screen.
const coronaryOpened = ref(choice.value === 'coronary'),
  tube = shallowRef<CoronaryTube>();
// Leaving the aorta report takes its drawing with it.
watch(choice, id => {
  if (id === 'coronary') coronaryOpened.value = true;
  else emit('overlay', undefined);
});
watch([tube, choice], () => emit('tube', choice.value === 'coronary' ? tube.value : undefined));
// The panels inside clear their own drawings as they close; this covers a close that skips them.
onBeforeUnmount(() => {
  emit('tube', undefined);
  emit('overlay', undefined);
});
</script>

<template>
  <section class="analysis-panel" aria-label="Analysis">
    <label class="field analysis-choice"
      >Analysis<select :value="choice" aria-label="Choose analysis" @change="choose">
        <option v-for="item in ANALYSES" :key="item.id" :value="item.id">{{ item.label }}</option>
      </select></label
    >
    <CoronaryPanel
      v-if="lumen && coronaryOpened"
      v-show="choice === 'coronary'"
      :case-id="caseId"
      :revision="revision"
      :point="point"
      :native="native"
      @select="(id, at) => emit('select', id, at)"
      @tube="tube = $event"
    />
    <p v-else-if="choice === 'coronary'" class="help analysis-needs">Coronary FFR needs a coronary artery segment from a coronary model in the Task list.</p>
    <ReportPanel v-if="choice === 'aorta'" :case-id="caseId" :revision="revision" report="aorta" title="Aorta" :busy="busy" @job="emit('job', $event)">
      <template #default="{ result }">
        <AortaReport
          v-if="result.report === 'aorta'"
          :case-id="caseId"
          :result="result as AortaResult"
          @select="(id, at) => emit('select', id, at)"
          @overlay="emit('overlay', $event)"
          @dicom="emit('dicom', $event)"
        />
      </template>
    </ReportPanel>
  </section>
</template>

<style scoped>
.analysis-choice {
  flex-direction: row;
  align-items: center;
  gap: 8px;
  margin: 0;
  padding: 12px 14px 0;
  font-size: 10px;
}
.analysis-choice select {
  flex: 1;
  min-width: 0;
  padding: 6px;
  font-size: 11px;
}
.analysis-needs {
  margin: 10px 14px 14px;
  font-size: 10px;
  line-height: 1.5;
}
</style>

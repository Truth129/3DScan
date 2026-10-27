<script setup lang="ts">
import { computed, ref } from 'vue';
import type { Series } from '../types';
import { filterSeries, sequenceSummary, seriesIsReady, seriesIssues, seriesIssueSummary } from '../lib/image-workspace';

const props = defineProps<{ series: readonly Series[]; modelValue: string; disabled?: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();
const query = ref(''), chosenFilter = ref<'all' | 'ready' | 'unavailable'>();
const readyCount = computed(() => props.series.filter(seriesIsReady).length);
const activeFilter = computed(() => chosenFilter.value ?? (readyCount.value ? 'ready' : 'all'));
const selectedSeries = computed(() => props.series.find(item => item.uid === props.modelValue));
const shown = computed(() => filterSeries(props.series, query.value, activeFilter.value));
const filters = computed(() => [{ id: 'ready' as const, label: 'Ready', count: readyCount.value },
  { id: 'all' as const, label: 'All', count: props.series.length },
  { id: 'unavailable' as const, label: 'Unavailable', count: props.series.length - readyCount.value }]);
</script>

<template>
  <section class="series-picker" aria-label="Image series">
    <div v-if="series.length > 4" class="series-search"><label>Find a sequence<input v-model="query" type="search" aria-label="Find image series" placeholder="e.g. T1, STIR, postcontrast…" /></label></div>
    <div v-if="series.length > 1" class="series-filters" aria-label="Series availability">
      <button v-for="filter in filters" :key="filter.id" type="button" :class="{ active: activeFilter === filter.id }" :aria-pressed="activeFilter === filter.id" @click="chosenFilter = filter.id">{{ filter.label }} <span>{{ filter.count }}</span></button>
    </div>
    <p v-if="selectedSeries" class="series-selected">Selected: <strong>{{ selectedSeries.description || selectedSeries.modality }}</strong><span v-if="!seriesIsReady(selectedSeries)"> · unavailable</span></p>
    <p v-if="series.length && !readyCount" class="series-empty-note" role="status">No series is ready to open. Expand the checks below to see why.</p>
    <div class="series-list">
      <article v-for="item in shown" :key="item.uid" class="series-option" :class="{ active: modelValue === item.uid, unavailable: !seriesIsReady(item) }">
        <label class="series-choice"><input type="radio" :value="item.uid" :checked="modelValue === item.uid" :disabled="disabled || !seriesIsReady(item)" @change="emit('update:modelValue', item.uid)" /><div><strong>{{ item.description || `${item.modality} series` }}</strong><span>{{ item.slice_count }} slices <i>·</i> {{ item.spacing?.map(n => Number(n).toFixed(2)).join(' × ') || 'Spacing unavailable' }} mm</span><small v-if="sequenceSummary(item)" class="sequence-detail">{{ sequenceSummary(item) }}</small></div><span class="series-row-tags"><span class="pill">{{ item.modality }}</span><span class="series-availability" :class="{ ready: seriesIsReady(item) }">{{ seriesIsReady(item) ? 'Ready' : 'Unavailable' }}</span></span></label>
        <details v-if="!seriesIsReady(item) || seriesIssues(item).length" class="series-checks"><summary><span>{{ seriesIssueSummary(item) }}</span><small>{{ seriesIssues(item).length || 1 }} {{ seriesIssues(item).length <= 1 ? 'check' : 'checks' }}</small></summary><ul><li v-for="issue in seriesIssues(item)" :key="issue">{{ issue }}</li><li v-if="!seriesIssues(item).length">This image representation is not supported yet.</li></ul></details>
      </article>
      <p v-if="!shown.length && series.length" class="series-empty-note">No matching series. <button type="button" class="text-button" @click="query = ''; chosenFilter = 'all'">Show all series</button></p>
    </div>
  </section>
</template>

<style scoped>
.series-picker{margin:22px 0}.series-search label{display:flex;flex-direction:column;gap:7px;font-size:10px;color:var(--text-secondary)}.series-search input{width:100%;padding:9px 10px;border:1px solid var(--line-strong);border-radius:5px;background:var(--surface);color:var(--white);font-size:12px}.series-filters{display:flex;gap:6px;margin-top:11px}.series-filters button{display:flex;align-items:center;gap:6px;border:1px solid var(--line-strong);border-radius:5px;padding:7px 10px;background:var(--surface);color:var(--text-secondary);font-size:10px}.series-filters button.active{border-color:var(--accent);background:var(--surface-hover);color:var(--white)}.series-filters span{font-family:var(--mono);font-size:9px;color:var(--muted)}
.series-list{margin:12px 0 0;max-height:390px}.series-option{display:block;padding:0;cursor:default}.series-choice{display:flex;align-items:flex-start;gap:11px;padding:13px 12px;cursor:pointer}.unavailable .series-choice{cursor:default}.series-choice>input{margin:2px 0 0;flex-shrink:0}.series-choice>div{display:flex;flex-direction:column;gap:6px;min-width:0;flex:1}.series-choice strong{overflow-wrap:anywhere}.series-choice>div>span{font:10px var(--mono);color:var(--muted)}.sequence-detail{font-size:10px;line-height:1.5;color:var(--text-secondary)}.series-row-tags{display:flex;align-items:flex-end;flex-direction:column;gap:6px}.series-availability{font-size:9px;color:var(--muted)}.series-availability.ready{color:var(--text-secondary)}.unavailable{background:var(--panel)}.unavailable strong{color:var(--text-secondary)}
.series-checks{border-top:1px solid var(--line);padding:9px 12px}.series-checks summary{display:flex;align-items:flex-start;gap:10px;cursor:pointer;font-size:10px;line-height:1.5;color:var(--text-secondary)}.series-checks summary:before{content:'▸';flex-shrink:0}.series-checks[open] summary:before{content:'▾'}.series-checks summary>span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;flex:1}.series-checks summary>small{flex-shrink:0;font-size:9px;color:var(--muted);white-space:nowrap}.series-checks ul{margin:9px 0 0;padding-left:18px;font-size:10px;line-height:1.6;color:var(--text-secondary);overflow-wrap:anywhere}.series-checks li+li{margin-top:5px}.series-selected{font-size:10px;color:var(--text-secondary);line-height:1.5;margin:10px 0 0;overflow-wrap:anywhere}.series-selected strong{font-weight:500;color:var(--white)}.series-selected>span{color:var(--muted)}
.series-empty-note{font-size:11px;line-height:1.6;color:var(--text-secondary);padding:5px 0}.series-empty-note .text-button{font-size:inherit;margin-left:5px}
</style>

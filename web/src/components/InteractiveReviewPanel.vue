<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowReactive, shallowRef, watch } from 'vue';
import { api } from '../lib/api';
import { InteractiveController } from '../lib/interactive-controller';
import { newReviewPrompt, normalizeRelationship, pendingReviewTargets, reviewCandidateHasChanges, reviewMethod } from '../lib/interactive-review';
import { prettyName } from '../lib/geometry';
import { choosePath, pathOptionText, pathRun, pathStatus, readPath, REVIEW_PATH_KEY, reviewRunPath, writePath } from '../lib/inference-path';
import SegmentRelationshipEditor from './SegmentRelationshipEditor.vue';
import type {
  InteractiveMethod,
  InteractiveOptions,
  InteractivePrompt,
  InteractiveReview,
  InteractiveRunChoice,
  InteractiveStatus,
  InteractiveTool,
  Job,
  Point,
  ScissorsOperation,
  ScissorsShape,
  SegmentRelationship,
  Structure,
} from '../types';
const props = defineProps<{
  caseId: string;
  revisionId: string;
  selected: string;
  structures: Structure[];
  busy: boolean;
  active?: boolean;
  modality?: string;
  surfaceError?: string;
  surfaceLoading?: boolean;
}>();
const modalityLabel = computed(() => props.modality ?? 'CT');
const emit = defineEmits<{
  state: [review: InteractiveReview | undefined];
  prompts: [prompts: InteractivePrompt[]];
  select: [id: string];
  tool: [tool: InteractiveTool];
  polarity: [include: boolean];
  radius: [radius: number];
  scissors: [value: { shape: ScissorsShape; operation: ScissorsOperation }];
  native: [native: boolean];
  candidate: [visible: boolean];
  disabled: [disabled: boolean];
  opened: [open: boolean];
  accept: [job: Job];
  targets: [targets: Structure[]];
  'create-target': [value: { review: InteractiveReview; structure: Structure }];
  'remove-target': [id: string];
  'preview-overlaps': [ids: string[]];
  'retry-surface': [];
}>();
const runtime = ref<InteractiveStatus>(),
  tool = ref<InteractiveTool>('navigate'),
  include = ref(true),
  radius = ref(3),
  native = ref(true),
  showCandidate = ref(true),
  advanced = ref(false),
  impactOpen = ref(false),
  messages = ref<HTMLElement>();
const method = ref<InteractiveMethod>('direct'),
  methodError = ref('');
const scissorsShape = ref<ScissorsShape>('freehand'),
  scissorsOperation = ref<ScissorsOperation>('erase_inside');
const controller = shallowRef<InteractiveController>(),
  tick = ref(0);
const addOpen = ref(false),
  creating = ref(false),
  discarding = ref(false),
  createError = ref('');
const newName = ref(''),
  newGroup = ref('Custom'),
  newColor = ref('#c18dff'),
  draftTargets = ref<Structure[]>([]);
const policyOpen = ref(false),
  policyLoading = ref(false),
  policyValid = ref(true),
  newPolicyValid = ref(true),
  newRelationship = ref(normalizeRelationship());
const reviewPath = ref(''),
  reviewPathNotice = ref('');
const controllers = new Map<string, InteractiveController>();
const draftMethods = new Map<string, InteractiveMethod>();
let disposed = false,
  identityVersion = 0,
  targetVersion = 0,
  runtimeRequest = 0,
  discoveryScope = '';
const current = computed(() => {
  void tick.value;
  return controller.value;
});
const view = computed(() => {
  void tick.value;
  return current.value?.publicReview;
});
const candidate = computed(() => {
  void tick.value;
  return current.value?.candidate;
});
const disabled = computed(
  () =>
    creating.value ||
    discarding.value ||
    props.busy ||
    !!current.value?.working ||
    !current.value?.loaded ||
    !!current.value?.conflict ||
    (method.value === 'nninteractive' && !runtime.value?.available),
);
const methodDisabled = computed(
  () =>
    creating.value ||
    discarding.value ||
    props.busy ||
    !!current.value?.working ||
    !!current.value?.saving ||
    !current.value?.loaded ||
    !!current.value?.conflict,
);
const hasImpact = computed(() => !!(candidate.value?.affected_structures?.length || candidate.value?.overlaps?.length));
const groups = computed(() => [...new Set(['Custom', ...props.structures.map(item => item.group)])].sort());
const savedStructures = computed(() => props.structures.filter(item => !draftTargets.value.some(draft => draft.id === item.id)));
const relationship = computed(
  () => current.value?.relationship ?? normalizeRelationship(props.structures.find(item => item.id === props.selected)?.relationship),
);
const relationshipName = computed(() => ({ independent: 'Independent', subregion: 'Subregion', subdivision: 'Subdivision' })[relationship.value.kind]);
const pending = computed(() => {
  void tick.value;
  return current.value?.pendingCount ?? 0;
});
const pendingMarks = computed(() => {
  void tick.value;
  return current.value?.pendingMarks ?? 0;
});
const runPath = computed(() => reviewRunPath(method.value, runtime.value, reviewPath.value, view.value));
const pathLine = computed(() =>
  pathStatus(
    runPath.value.paths.find(row => row.id === reviewPath.value),
    reviewPathNotice.value,
  ),
);
// Never part of `disabled`: marking, Direct edit and relationship-only Apply need no card.
const pathStopsRun = (full: boolean) => pathRun(runPath.value, pendingMarks.value, full).stopped;
const canRerun = computed(() => runPath.value.canRerun && !disabled.value && policyValid.value);
const saveEnabled = computed(
  () =>
    policyValid.value &&
    !discarding.value &&
    reviewCandidateHasChanges(candidate.value, method.value === 'direct' && !view.value?.new_structure) &&
    !current.value?.dirty &&
    !current.value?.working &&
    !props.busy,
);
function publish() {
  if (disposed) return;
  tick.value++;
  const value = controller.value;
  emit('state', discarding.value ? undefined : value?.publicReview);
  // Applied direct strokes are already represented by the exact mask overlay.
  // Retain vectors locally for undo without covering the result with old ink.
  const visible = method.value === 'direct' && value?.candidate ? value.prompts.slice(value.appliedCount) : (value?.prompts ?? []);
  emit('prompts', discarding.value ? [] : visible);
}
const rememberedTargetKey = (caseId = props.caseId, revisionId = props.revisionId) => `scan3d-review-target:${caseId}:${revisionId}`;
async function discoverTargets() {
  if (!props.active) return;
  const scope = `${props.caseId}:${props.revisionId}`,
    identity = identityVersion,
    targetsAtRequest = targetVersion,
    selected = props.selected;
  if (discoveryScope === scope) return;
  discoveryScope = scope;
  try {
    const values = await api.interactiveReviews(props.caseId);
    await nextTick();
    if (disposed || identity !== identityVersion || targetsAtRequest !== targetVersion || scope !== `${props.caseId}:${props.revisionId}`) return;
    draftTargets.value = pendingReviewTargets(values, props.revisionId);
    emit('targets', draftTargets.value);
    for (const value of values)
      if (value.new_structure && value.revision_id === props.revisionId && !['accepted', 'discarded'].includes(value.status))
        draftMethods.set(value.structure_id, reviewMethod(value));
    if (draftMethods.has(props.selected)) void ensureController();
    const remembered = localStorage.getItem(rememberedTargetKey());
    if (remembered && props.selected === selected && draftTargets.value.some(item => item.id === remembered)) emit('select', remembered);
  } catch (reason) {
    if (identity === identityVersion) {
      discoveryScope = '';
      createError.value = reason instanceof Error ? reason.message : String(reason);
    }
  }
}
async function createTarget() {
  if (creating.value || discarding.value || props.busy || current.value?.working || !newName.value.trim() || !newPolicyValid.value) return;
  const identity = identityVersion,
    caseId = props.caseId,
    revisionId = props.revisionId;
  creating.value = true;
  createError.value = '';
  try {
    const review = await api.createInteractiveStructure(caseId, revisionId, {
      name: newName.value.trim(),
      group: newGroup.value.trim() || 'Custom',
      color: newColor.value,
      relationship: newRelationship.value,
      method: method.value,
    });
    await nextTick();
    if (disposed || identity !== identityVersion || props.caseId !== caseId || props.revisionId !== revisionId) return;
    if (!review.new_structure || !review.target || !review.preview_revision_id) throw new Error('The server did not return an empty segment preview');
    const structure = review.target,
      key = `${caseId}:${revisionId}:${structure.id}:${method.value}`;
    let value: InteractiveController;
    value = shallowReactive(
      new InteractiveController(
        caseId,
        revisionId,
        structure.id,
        () => {
          if (controller.value === value) publish();
        },
        undefined,
        undefined,
        method.value,
      ),
    );
    controllers.set(key, value);
    draftMethods.set(structure.id, method.value);
    targetVersion++;
    draftTargets.value = [...draftTargets.value.filter(item => item.id !== structure.id), structure];
    emit('create-target', { review, structure });
    emit('targets', draftTargets.value);
    emit('select', structure.id);
    controller.value = value;
    value.adopt(review);
    try {
      localStorage.setItem(rememberedTargetKey(caseId, revisionId), structure.id);
    } catch {
      /* The server still retains the draft when local storage is unavailable. */
    }
    addOpen.value = false;
    newName.value = '';
    newRelationship.value = normalizeRelationship();
    policyOpen.value = false;
    showCandidate.value = true;
    include.value = true;
    chooseTool(method.value === 'direct' ? 'scribble' : 'point');
  } catch (reason) {
    if (identity === identityVersion) createError.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    if (identity === identityVersion) creating.value = false;
  }
}
async function discardCurrent() {
  const value = current.value,
    review = value?.review;
  if (!value || (!review && !value.hasDraftChanges) || discarding.value) return;
  const identity = identityVersion;
  // Detach immutable preview readers before the server invalidates their aliases.
  // Draft state remains intact so a failed request can restore the same work.
  discarding.value = true;
  publish();
  await nextTick();
  try {
    await value.discard();
    methodError.value = '';
    if (review?.new_structure && !value.review) {
      controllers.delete(`${review.case_id}:${review.revision_id}:${review.structure_id}:${reviewMethod(review)}`);
      draftMethods.delete(review.structure_id);
      try {
        if (localStorage.getItem(rememberedTargetKey(review.case_id, review.revision_id)) === review.structure_id)
          localStorage.removeItem(rememberedTargetKey(review.case_id, review.revision_id));
      } catch {
        /* Persistence remains server-owned. */
      }
      await nextTick();
      if (disposed || identity !== identityVersion || props.caseId !== review.case_id || props.revisionId !== review.revision_id) return;
      targetVersion++;
      draftTargets.value = draftTargets.value.filter(item => item.id !== review.structure_id);
      emit('remove-target', review.structure_id);
      emit('targets', draftTargets.value);
      await nextTick();
    }
  } finally {
    if (!disposed && identity === identityVersion) {
      discarding.value = false;
      publish();
    }
  }
}
async function ensureController() {
  if (!props.active) return;
  const targetMethod = draftMethods.get(props.selected);
  if (targetMethod && targetMethod !== method.value) {
    method.value = targetMethod;
    return;
  }
  const key = `${props.caseId}:${props.revisionId}:${props.selected}:${method.value}`;
  let value = controllers.get(key);
  if (!value) {
    value = shallowReactive(
      new InteractiveController(
        props.caseId,
        props.revisionId,
        props.selected,
        () => {
          if (controller.value === value) publish();
        },
        undefined,
        props.structures.find(item => item.id === props.selected)?.relationship,
        method.value,
      ),
    );
    controllers.set(key, value);
    controller.value = value;
    publish();
    await value.load();
  } else {
    controller.value = value;
    publish();
    if (value.review && ['accepted', 'discarded'].includes(value.review.status)) await value.reload();
  }
}
async function checkRuntime() {
  if (method.value !== 'nninteractive') return;
  const request = ++runtimeRequest;
  let status: InteractiveStatus;
  try {
    status = await api.interactiveStatus();
  } catch (reason) {
    status = { installed: false, model_available: false, available: false, reason: reason instanceof Error ? reason.message : String(reason) };
  }
  // Opening review asks twice, and the chooser must not settle on the older reply.
  if (request !== runtimeRequest) return;
  runtime.value = status;
  const choice = choosePath(status.inference_paths ?? [], readPath(REVIEW_PATH_KEY) || reviewPath.value);
  reviewPath.value = choice.selected;
  reviewPathNotice.value = choice.notice;
}
function selectPath(event: Event) {
  reviewPath.value = (event.target as HTMLSelectElement).value;
  reviewPathNotice.value = '';
  writePath(REVIEW_PATH_KEY, reviewPath.value);
}
function chooseMethod(value: InteractiveMethod) {
  if (value === method.value || methodDisabled.value) return;
  if (current.value?.hasDraftChanges) {
    methodError.value = 'Save or discard this draft before switching editing methods.';
    return;
  }
  method.value = value;
  methodError.value = '';
  policyOpen.value = false;
  advanced.value = false;
  impactOpen.value = false;
  showCandidate.value = true;
  include.value = true;
  native.value = true;
  chooseTool(value === 'direct' ? 'scribble' : 'point');
}
function chooseDirect(includeStroke: boolean) {
  include.value = includeStroke;
  chooseTool('scribble');
}
function chooseTool(value: InteractiveTool) {
  tool.value = value;
  if (!['point', 'navigate', 'scissors'].includes(value)) native.value = true;
}
function addGesture(prompt: InteractivePrompt) {
  if (!disabled.value) current.value?.append(prompt);
}
async function run(full: boolean, choice?: InteractiveRunChoice) {
  const value = current.value,
    identity = identityVersion,
    caseId = props.caseId,
    revisionId = props.revisionId;
  showCandidate.value = true;
  await value?.apply(full, choice);
  if (value?.job && !disposed && identity === identityVersion && current.value === value && props.caseId === caseId && props.revisionId === revisionId)
    policyOpen.value = false;
}
async function apply(full = false) {
  const { stopped, choice } = pathRun(runPath.value, pendingMarks.value, full);
  if (!disabled.value && policyValid.value && !stopped) await run(full, choice);
}
// apply() saves unsaved marks first. reload() would clear their timer and drop them, and
// it would reset undo history, so a re-run goes through the same request as Run AI.
function rerun() {
  if (canRerun.value) void run(false, { inference_path: reviewPath.value, replay: true });
}
async function accept() {
  if (!saveEnabled.value) return;
  const identity = identityVersion,
    caseId = props.caseId,
    revisionId = props.revisionId;
  const value = await current.value?.accept();
  await nextTick();
  if (value && !disposed && identity === identityVersion && props.caseId === caseId && props.revisionId === revisionId) emit('accept', value);
}
function option(name: keyof InteractiveOptions, value: boolean) {
  if (current.value && !discarding.value) current.value.edit(current.value.prompts, { ...current.value.options, [name]: value });
}
function setRelationship(value: SegmentRelationship) {
  if (current.value && !disabled.value && !policyLoading.value) current.value.editRelationship(value);
}
async function togglePolicy() {
  policyOpen.value = !policyOpen.value;
  addOpen.value = false;
  if (!policyOpen.value) return;
  const value = current.value,
    identity = identityVersion,
    selected = props.selected;
  policyLoading.value = true;
  try {
    await value?.ensureDraft();
  } finally {
    if (!disposed && identity === identityVersion && current.value === value && props.selected === selected) policyLoading.value = false;
  }
}
function previewAffected() {
  if (candidate.value) emit('preview-overlaps', candidate.value.affected_structures?.map(item => item.id) ?? [props.selected]);
}
function changeNative(value: boolean) {
  if (method.value === 'direct') return;
  native.value = value;
  if (!value && !['point', 'navigate'].includes(tool.value)) chooseTool('point');
}
watch(
  () => [props.caseId, props.revisionId, props.selected, props.active, method.value],
  () => {
    void ensureController();
  },
);
watch(
  () => [props.caseId, props.revisionId],
  () => {
    identityVersion++;
    targetVersion++;
    discoveryScope = '';
    draftMethods.clear();
    draftTargets.value = [];
    emit('targets', []);
    addOpen.value = false;
    creating.value = false;
    discarding.value = false;
    policyOpen.value = false;
    impactOpen.value = false;
    policyLoading.value = false;
    policyValid.value = true;
    newPolicyValid.value = true;
    newRelationship.value = normalizeRelationship();
    createError.value = '';
    methodError.value = '';
    newName.value = '';
    showCandidate.value = true;
    void discoverTargets();
  },
);
watch(
  () => props.selected,
  () => {
    policyOpen.value = false;
    impactOpen.value = false;
    policyLoading.value = false;
    policyValid.value = true;
    methodError.value = '';
  },
);
watch(method, value => {
  native.value = true;
  include.value = true;
  chooseTool(value === 'direct' ? 'scribble' : 'point');
  if (value === 'nninteractive') void checkRuntime();
});
// Errors lead the message list; one that arrives while it is scrolled must not stay out of view.
watch([() => current.value?.error, createError, methodError, () => runPath.value.block, () => props.surfaceError], () => messages.value?.scrollTo({ top: 0 }), {
  flush: 'post',
});
watch(tool, value => emit('tool', value), { immediate: true });
watch(include, value => emit('polarity', value), { immediate: true });
watch(
  radius,
  value => {
    const clamped = Math.max(0.5, Math.min(30, Number(value) || 3));
    if (clamped !== value) radius.value = clamped;
    else emit('radius', value);
  },
  { immediate: true },
);
watch([scissorsShape, scissorsOperation], ([shape, operation]) => emit('scissors', { shape, operation }), { immediate: true });
watch(native, value => emit('native', value), { immediate: true });
watch(showCandidate, value => emit('candidate', value), { immediate: true });
watch(disabled, value => emit('disabled', value), { immediate: true });
watch(
  () => props.active,
  value => {
    emit('opened', !!value);
    if (value) {
      void checkRuntime();
      void ensureController();
      void discoverTargets();
    } else chooseTool('navigate');
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  disposed = true;
  identityVersion++;
  targetVersion++;
  for (const value of controllers.values()) value.dispose();
});
defineExpose({
  addGesture,
  addPrompt(ras: Point) {
    if (method.value !== 'direct') addGesture(newReviewPrompt(ras, include.value));
  },
  async show() {
    if (!disposed) {
      chooseTool(method.value === 'direct' ? 'scribble' : 'point');
      await Promise.all([checkRuntime(), ensureController()]);
    }
  },
});
</script>

<template>
  <section
    class="interactive-review review-toolbar"
    :data-review-method="method"
    :data-review-id="view?.id"
    :data-review-status="view?.status"
    :data-generation="view?.generation"
    :data-candidate-revision="candidate?.revision_id"
    :data-review-tool="tool"
    :data-scissors-shape="scissorsShape"
    :data-scissors-operation="scissorsOperation"
    :data-pending-count="pending"
    :data-new-structure="view?.new_structure || false"
    :data-policy-dirty="current?.relationshipDirty || false"
    :data-relationship="relationship.kind"
    :data-affected-count="candidate?.affected_structures?.length || 0"
    :data-inference-path="runPath.id || undefined"
  >
    <div class="review-workspace-tabs">
      <slot name="navigation" />
      <div ref="messages" class="review-messages" role="group" aria-label="Review messages" tabindex="0">
        <p v-if="current?.error" class="review-alert" role="alert">
          {{ current.error }} <button v-if="current.conflict" class="text-button" @click="current.reload()">Reload saved work</button>
        </p>
        <p v-if="createError" class="review-alert" role="alert">{{ createError }}</p>
        <p v-if="surfaceError" class="review-alert" role="alert">
          {{ surfaceError }} <button class="text-button" @click="emit('retry-surface')">Retry preview</button>
        </p>
        <p v-else-if="surfaceLoading" class="review-progress" role="status">Preparing the 3D candidate preview… Previous surfaces remain visible.</p>
        <p v-if="methodError" class="review-alert" role="status">{{ methodError }}</p>
        <p v-if="method === 'nninteractive' && runtime && !runtime.available" class="review-alert" role="status">
          {{ runtime.reason || 'nnInteractive is unavailable on the workstation.' }} Direct paint and erase remain available.
          <button class="text-button" @click="checkRuntime">Check again</button>
        </p>
        <p v-if="runPath.block" class="review-alert" role="status">
          {{ runPath.block }}
          <button class="text-button" @click="checkRuntime">Check again</button>
        </p>
        <p v-else-if="runPath.id && reviewPathNotice" class="review-progress" role="status">{{ reviewPathNotice }}</p>
        <div v-if="candidate" class="review-result-summary">
          <span v-if="candidate.relationship_changed && (candidate.total_changed_voxels ?? candidate.summary.changed_voxels) === 0"
            >Relationships updated · masks unchanged</span
          ><span v-else
            >{{ (candidate.total_changed_voxels ?? candidate.summary.changed_voxels).toLocaleString() }} voxels changed<template
              v-if="(candidate.affected_structures?.length || 0) > 1"
            >
              across affected masks</template
            ></span
          ><span>Target +{{ candidate.summary.added_voxels.toLocaleString() }} / −{{ candidate.summary.removed_voxels.toLocaleString() }}</span
          ><span>{{ candidate.summary.before_volume_ml.toFixed(1) }} → {{ candidate.summary.after_volume_ml.toFixed(1) }} mL</span
          ><span v-if="candidate.gpu_name">AI result from {{ candidate.gpu_name }}</span
          ><span v-if="candidate.summary.after_voxels === 0">{{
            method === 'direct' && !view?.new_structure
              ? 'This segment will be empty when saved.'
              : 'The candidate is empty. Paint or add include hints before saving.'
          }}</span
          ><span v-else-if="current?.dirty">Apply pending changes before saving.</span><span v-else>Inspect the result, then save a new revision.</span>
        </div>
        <p v-for="warning in candidate?.warnings || []" :key="warning" class="review-alert" role="status">{{ warning }}</p>
      </div>
      <slot name="exit" />
    </div>
    <div class="review-toolbar-rows">
      <div class="review-toolbar-main">
        <div class="review-toolset review-methods" role="group" aria-label="Editing method">
          <button :class="{ active: method === 'direct' }" :aria-pressed="method === 'direct'" :disabled="methodDisabled" @click="chooseMethod('direct')">
            Direct edit</button
          ><button
            :class="{ active: method === 'nninteractive' }"
            :aria-pressed="method === 'nninteractive'"
            :disabled="methodDisabled"
            @click="chooseMethod('nninteractive')"
          >
            AI assist
          </button>
        </div>
        <label class="review-target"
          >Structure<select
            :value="selected"
            aria-label="Review structure"
            :disabled="creating || discarding || !!current?.working"
            @change="emit('select', ($event.target as HTMLSelectElement).value)"
          >
            <option v-for="item in structures" :key="item.id" :value="item.id">
              {{ prettyName(item.name) }}{{ draftTargets.some(target => target.id === item.id) ? ' · draft' : '' }}
            </option>
          </select></label
        >
        <button
          class="button secondary compact review-add-target"
          :disabled="creating || discarding || busy || !!current?.working"
          :aria-expanded="addOpen"
          @click="
            addOpen = !addOpen;
            policyOpen = false;
          "
        >
          + Add segment
        </button>
        <div class="review-tools">
          <div v-if="method === 'direct'" class="review-toolset direct-tools" role="group" aria-label="Direct editing tools">
            <button :class="{ active: tool === 'navigate' }" :aria-pressed="tool === 'navigate'" :disabled="disabled" @click="chooseTool('navigate')">
              Navigate</button
            ><button
              :class="{ active: tool === 'scribble' && include }"
              :aria-pressed="tool === 'scribble' && include"
              :disabled="disabled"
              @click="chooseDirect(true)"
            >
              Paint</button
            ><button
              :class="{ active: tool === 'scribble' && !include }"
              :aria-pressed="tool === 'scribble' && !include"
              :disabled="disabled"
              @click="chooseDirect(false)"
            >
              Erase</button
            ><button :class="{ active: tool === 'scissors' }" :aria-pressed="tool === 'scissors'" :disabled="disabled" @click="chooseTool('scissors')">
              Scissors
            </button>
          </div>
          <template v-if="method === 'direct' && tool === 'scissors'"
            ><label class="review-scissors"
              >Shape<select v-model="scissorsShape" aria-label="Scissors shape" :disabled="disabled">
                <option value="freehand">Freehand</option>
                <option value="rectangle">Rectangle</option>
                <option value="circle">Circle</option>
              </select></label
            ><label class="review-scissors"
              >Cut<select v-model="scissorsOperation" aria-label="Scissors operation" :disabled="disabled">
                <option value="erase_inside">Erase inside</option>
                <option value="erase_outside">Erase outside</option>
              </select></label
            ></template
          >
          <template v-else-if="method === 'nninteractive'">
            <div class="review-toolset" role="group" aria-label="AI annotation tool">
              <button
                v-for="item in [
                  ['navigate', 'Navigate'],
                  ['point', 'Point'],
                  ['scribble', 'Hint brush'],
                  ['lasso', 'Lasso'],
                  ['bbox', 'Box'],
                ] as const"
                :key="item[0]"
                :class="{ active: tool === item[0] }"
                :aria-pressed="tool === item[0]"
                :disabled="disabled"
                @click="chooseTool(item[0])"
              >
                {{ item[1] }}
              </button>
            </div>
            <div class="review-toolset polarity" role="group" aria-label="AI hint polarity">
              <button :class="{ active: include }" :aria-pressed="include" :disabled="disabled" @click="include = true">+ Include</button
              ><button :class="{ active: !include }" :aria-pressed="!include" :disabled="disabled" @click="include = false">− Exclude</button>
            </div>
          </template>
          <label v-if="tool === 'scribble'" class="review-brush"
            >Radius<input v-model.number="radius" type="number" aria-label="Brush radius in millimetres" min=".5" max="30" step=".5" />mm</label
          >
        </div>
        <div class="review-history">
          <button class="button secondary compact" :disabled="discarding || !current?.canUndo" @click="current?.undo()">↶ Undo</button
          ><button class="button secondary compact" :disabled="discarding || !current?.canRedo" @click="current?.redo()">↷ Redo</button>
        </div>
        <div class="review-primary-actions">
          <button class="button primary compact review-apply" :disabled="disabled || !pending || !policyValid || pathStopsRun(false)" @click="apply()">
            {{ method === 'direct' ? 'Apply edits' : 'Run AI' }}<template v-if="pending"> · {{ pending }}</template></button
          ><button class="button primary compact review-save" :disabled="!saveEnabled" @click="accept">
            {{ view?.new_structure ? 'Save new segment' : 'Save correction' }}
          </button>
        </div>
      </div>
      <div class="review-toolbar-context">
        <span v-if="discarding">Closing draft…</span><span v-else-if="!current?.loaded">Loading saved work…</span
        ><span v-else-if="current.working" class="review-working"
          ><span class="spinner"></span>{{ current.job?.stage || 'Processing on workstation…'
          }}<button class="text-button" @click="current.cancel()">Cancel</button></span
        ><span v-else
          >{{ current.prompts.length }} {{ current.prompts.length === 1 ? 'gesture' : 'gestures' }} ·
          {{ current.saving || current.unsaved ? 'saving draft…' : view ? 'draft saved' : `mark the ${modalityLabel}, then Apply once`
          }}<template v-if="pending"> · {{ pending }} unapplied</template></span
        >
        <span v-if="method === 'direct' && tool === 'scissors'"
          >3D cut · {{ scissorsOperation === 'erase_inside' ? 'erase inside' : 'erase outside' }} through the full depth of the selected segment.
          {{ scissorsShape === 'circle' ? 'Drag from centre to edge.' : 'Draw on the 3D view.' }} Apply edits to preview.</span
        ><span v-else-if="method === 'direct'">Exact paint/erase on one native slice · no AI. Add strokes, then Apply edits.</span
        ><span v-else-if="tool !== 'navigate'">{{
          tool === 'point'
            ? 'Click to mark an AI hint.'
            : tool === 'bbox'
              ? 'Drag an AI box hint on one native slice.'
              : tool === 'lasso'
                ? 'Draw an AI lasso hint on one native slice.'
                : 'Brush hints guide a 3D AI prediction.'
        }}</span>
        <span v-if="view?.new_structure" class="review-new-target-note">New segment · starts empty</span>
        <span v-if="policyOpen && policyLoading">Loading saved relationship…</span>
        <span v-if="runPath.label" class="review-ai-path" :title="runPath.label">AI · {{ runPath.label }}</span>
        <div class="review-context-actions">
          <button
            v-if="!addOpen"
            class="text-button relationship-toggle"
            aria-label="Edit segment relationship"
            :aria-expanded="policyOpen"
            :disabled="!current?.loaded || discarding"
            @click="togglePolicy"
          >
            Relationship · {{ relationshipName }}
          </button>
          <button
            v-if="hasImpact"
            class="text-button review-impact-toggle"
            :aria-expanded="impactOpen"
            :data-stale="!!current?.dirty"
            @click="impactOpen = !impactOpen"
          >
            Impact preview · {{ candidate?.affected_structures?.length || 1 }} {{ candidate?.affected_structures?.length === 1 ? 'mask' : 'masks' }}
          </button>
          <div v-if="candidate" class="review-toolset candidate-switch" role="group" aria-label="Candidate display">
            <button :class="{ active: !showCandidate }" :aria-pressed="!showCandidate" @click="showCandidate = false">
              {{ view?.new_structure ? 'Empty baseline' : 'Original' }}</button
            ><button :class="{ active: showCandidate }" :aria-pressed="showCandidate" @click="showCandidate = true">
              Candidate<template v-if="current?.dirty"> · previous result</template>
            </button>
          </div>
          <button v-if="current?.hasDraftChanges" class="text-button review-discard" :disabled="current.working || discarding || busy" @click="discardCurrent">
            Discard draft
          </button>
          <button class="text-button review-more" :aria-expanded="advanced" @click="advanced = !advanced">Options {{ advanced ? '−' : '+' }}</button>
        </div>
      </div>
    </div>
    <div class="review-floating-panels">
      <form v-if="addOpen" class="review-add-form" @submit.prevent="createTarget">
        <label
          >Name<input
            v-model="newName"
            aria-label="New segment name"
            maxlength="80"
            placeholder="e.g. Additional vessel"
            :disabled="creating || discarding"
            required
            autofocus /></label
        ><label
          >Group<input
            v-model="newGroup"
            aria-label="New segment group"
            maxlength="60"
            list="review-segment-groups"
            :disabled="creating || discarding" /><datalist id="review-segment-groups">
            <option v-for="group in groups" :key="group" :value="group" /></datalist></label
        ><label class="review-add-color">Color<input v-model="newColor" aria-label="New segment color" type="color" :disabled="creating || discarding" /></label
        ><button class="button primary compact" type="submit" :disabled="creating || discarding || !newName.trim() || !newPolicyValid">
          {{ creating ? 'Creating…' : 'Create empty segment' }}</button
        ><button class="text-button" type="button" :disabled="creating || discarding" @click="addOpen = false">Cancel</button
        ><span>Draw the new anatomy on {{ modalityLabel }}, then Apply and save.</span
        ><SegmentRelationshipEditor
          v-model="newRelationship"
          :structures="savedStructures"
          target-id=""
          :disabled="creating || discarding"
          @validity="newPolicyValid = $event"
        />
      </form>
      <SegmentRelationshipEditor
        v-if="policyOpen && !addOpen && current?.loaded"
        :model-value="relationship"
        :structures="savedStructures"
        :target-id="selected"
        :disabled="disabled || policyLoading"
        :suggestions="view?.relationship_suggestions"
        @update:model-value="setRelationship"
        @validity="policyValid = $event"
      />
      <div v-if="hasImpact" v-show="impactOpen" class="review-impact" :data-stale="!!current?.dirty">
        <div class="review-impact-heading">
          <span
            >{{
              current?.dirty
                ? 'Previous preview. Apply the latest edits and relationship before saving.'
                : 'These mask and relationship changes will be saved together.'
            }}<template v-if="candidate?.relationship_changed"> Relationships updated.</template></span
          ><button class="text-button" @click="previewAffected">Show affected in {{ modalityLabel }}</button>
        </div>
        <table v-if="candidate?.affected_structures?.length">
          <thead>
            <tr>
              <th>Segment</th>
              <th>Before</th>
              <th>After</th>
              <th>Change</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in candidate.affected_structures" :key="item.id" :data-affected-structure="item.id">
              <td>
                {{ prettyName(item.name) }} <small>{{ item.role === 'target' ? 'target' : 'sibling' }}</small>
              </td>
              <td>{{ item.summary.before_volume_ml.toFixed(2) }} mL</td>
              <td>{{ item.summary.after_volume_ml.toFixed(2) }} mL</td>
              <td>{{ item.metadata_only ? 'Relationship only' : `${item.summary.changed_voxels.toLocaleString()} voxels` }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="candidate?.overlaps?.length" class="review-overlaps">
          <span v-for="item in candidate.overlaps" :key="item.id" :data-overlap-structure="item.id" :data-overlap-policy="item.policy" :title="item.reason"
            >{{ prettyName(item.name) }} · {{ item.volume_ml.toFixed(2) }} mL {{ item.policy === 'removed' ? 'replaced' : 'preserved' }}</span
          >
        </div>
      </div>
      <div v-if="advanced && current" class="review-advanced">
        <label
          ><input type="checkbox" :checked="native" :disabled="method === 'direct'" @change="changeNative(($event.target as HTMLInputElement).checked)" />
          Native slice planes
          <small>{{ method === 'direct' ? 'Paint and erase affect this slice only.' : 'Required for hint brush, lasso and box.' }}</small></label
        >
        <label v-if="method === 'nninteractive'"
          ><input
            type="checkbox"
            :checked="current.options.autozoom"
            :disabled="current.working || discarding"
            @change="option('autozoom', ($event.target as HTMLInputElement).checked)"
          />
          Automatically focus inference</label
        >
        <label
          ><input
            type="checkbox"
            :checked="current.options.start_empty"
            :disabled="current.working || discarding || view?.new_structure"
            @change="option('start_empty', ($event.target as HTMLInputElement).checked)"
          />
          Start from an empty mask <small>{{ view?.new_structure ? 'New segments always start empty.' : 'Applies to this target only.' }}</small></label
        >
        <button
          v-if="method === 'nninteractive'"
          class="button secondary compact"
          :disabled="disabled || !policyValid || (current.options.start_empty && !current.prompts.length) || pathStopsRun(true)"
          @click="apply(true)"
        >
          Refine whole mask
        </button>
        <template v-if="runPath.id">
          <label
            >{{ runPath.locked ? 'Re-run draft on' : 'Run on'
            }}<select aria-label="Inference path" :value="reviewPath" :disabled="current.working || discarding" @change="selectPath">
              <option v-for="row in runPath.paths" :key="row.id" :value="row.id" :disabled="!row.available">{{ pathOptionText(row) }}</option>
            </select></label
          >
          <button v-if="runPath.locked" class="button secondary compact review-rerun" :disabled="!canRerun" @click="rerun">Re-run on this GPU</button>
          <p v-if="runPath.locked">
            This draft's AI results came from {{ runPath.label }}, and Run AI stays on it so each step builds on the last. Re-running on another GPU repeats
            every Apply step from the original mask and replaces the current result when it finishes. The new result can differ, including where no marks were
            made, so inspect it before saving. If the re-run fails, the old result stays visible as the previous result but cannot be saved; re-run again or
            discard. Marks are never lost.
          </p>
          <p v-else>The first AI result keeps this draft on this GPU.</p>
          <p>One AI job runs at a time across GPUs.</p>
          <p v-if="pathLine" role="status">{{ pathLine }}</p>
        </template>
        <p v-if="method === 'direct'">
          Paint and erase affect one native slice. Scissors cuts through the selected segment in the captured 3D view direction. Edits run in gesture order.
          Save a correction before switching to optional AI assist. Apply edits previews the mask and surface; Save creates a revision.
        </p>
        <p v-else>
          AI hints can change the mask beyond your marks. The installed model supports 2D boxes. Drafts save automatically; Run AI previews a prediction and
          Save creates a revision.
        </p>
      </div>
    </div>
  </section>
</template>

<style scoped>
/*
 * The editing toolbar sits above both viewers, so its height decides theirs. No tool,
 * message or panel changes it: tools share a reserved slot, messages scroll in the
 * navigation row and panels lie over the viewers until they are folded.
 */
.review-toolbar {
  container: review-toolbar / inline-size;
  position: relative;
  color: #c8d1df;
}
/* An auto track would size to its nowrap spans, leaving them nothing to shrink against. */
.review-toolbar-rows {
  background: #171c24;
  border-block: 1px solid #354052;
  padding: 8px 12px;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 6px;
}
.review-toolbar-main,
.review-tools,
.review-context-actions,
.review-result-summary {
  display: flex;
  align-items: center;
  gap: 7px 9px;
}
.review-toolbar-main,
.review-tools,
.review-result-summary {
  flex-wrap: wrap;
}
/* The basis holds the widest tool set, so choosing a tool can never rewrap the row. */
.review-tools {
  flex: 1 1 564px;
  min-width: 0;
}
.review-messages {
  flex: 1 1 0;
  min-width: 0;
  margin-inline: 7px;
  overflow-anchor: none;
  scrollbar-width: thin;
  scrollbar-color: var(--control-border) transparent;
}
.review-messages:focus-visible {
  outline: 2px solid var(--control-focus);
  outline-offset: 2px;
}
.review-messages:empty {
  display: none;
}
.review-target {
  display: flex;
  flex: 0 0 200px;
  align-items: center;
  gap: 6px;
  font-size: 9px;
  color: #a9b6c9;
}
.review-target select {
  flex: 1 1 0;
  font-size: 11px;
  padding: 5px 7px;
  min-width: 0;
}
.review-toolset {
  display: flex;
  border: 1px solid #424e60;
  border-radius: 5px;
  overflow: hidden;
}
.review-toolset button {
  border: 0;
  background: transparent;
  color: #b7c3d5;
  padding: 6px 9px;
  font-size: 11px;
  cursor: pointer;
  white-space: nowrap;
}
.review-toolset button.active {
  background: #39465b;
  color: #fff;
}
.review-toolset button:disabled {
  opacity: 0.4;
  cursor: default;
}
.polarity button:first-child.active {
  background: #25463d;
  color: #9ae4c2;
}
.polarity button:last-child.active {
  background: #51333d;
  color: #ffb9c3;
}
.review-history,
.review-primary-actions {
  display: flex;
  gap: 5px;
}
.review-history {
  margin-left: auto;
}
.review-toolbar .button {
  font-size: 11px;
  padding: 6px 10px;
  min-height: 29px;
}
/* Labels change with the draft, so the buttons keep room for the longest one. */
.review-apply {
  min-width: 104px;
}
.review-save {
  min-width: 124px;
  background: #cfddf2;
}
.review-toolbar-context {
  display: flex;
  align-items: center;
  gap: 7px 9px;
  height: 27px;
  font-size: 10px;
  color: #9dadc4;
}
.review-toolbar-context > span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.review-toolbar-context > span:first-child {
  flex: none;
}
.review-toolbar-context > span:nth-child(2) {
  color: #c1cadd;
}
.review-context-actions {
  flex: none;
  margin-left: auto;
}
.review-context-actions .candidate-switch {
  padding: 1px;
}
.candidate-switch button {
  padding: 2px 9px;
  font-size: 10px;
}
.review-result-summary {
  font-size: 10px;
  color: #c1cfe4;
  gap: 0 15px;
}
.review-brush {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 10px;
}
.review-brush input {
  width: 55px;
  font-size: 11px;
  padding: 6px;
}
.review-scissors {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 10px;
}
.review-scissors select {
  font-size: 11px;
  padding: 6px;
  max-width: 132px;
}
.review-working {
  display: flex;
  align-items: center;
  gap: 8px;
}
.review-working .spinner {
  width: 12px;
  height: 12px;
}
.review-progress {
  margin: 0;
  font-size: 10px;
  color: #c1cfe4;
}
.review-alert {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
  font-size: 11px;
  color: #e1b6b6;
}
/* An action stays beside its message, so a wrapped line cannot push it out of view. */
.review-alert .text-button {
  flex: none;
  padding: 1px 7px;
  font-size: 10px;
}
.review-advanced {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px;
  background: #11161d;
  border: 1px solid #3b485e;
  border-radius: 5px;
  font-size: 10px;
}
.review-advanced label {
  display: flex;
  align-items: center;
  gap: 6px;
}
.review-advanced input {
  margin: 0;
}
.review-advanced select {
  min-width: 0;
  max-width: 280px;
  font-size: 11px;
  padding: 5px 7px;
}
.review-advanced small {
  color: #8696ad;
}
.review-advanced p {
  flex-basis: 100%;
  margin: 0;
  color: #91a2ba;
  line-height: 1.5;
}
.review-add-form {
  display: flex;
  align-items: flex-end;
  gap: 9px;
  flex-wrap: wrap;
  padding: 10px;
  background: #10161f;
  border: 1px solid #3b485e;
  border-radius: 5px;
}
.review-add-form label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 10px;
  color: #acbdd4;
}
.review-add-form input {
  font-size: 12px;
  padding: 7px;
  min-width: 140px;
}
.review-add-form label:first-child input {
  min-width: 220px;
}
.review-add-form .review-add-color input {
  width: 42px;
  min-width: 42px;
  height: 32px;
  padding: 2px;
}
.review-add-form > span {
  align-self: center;
  font-size: 10px;
  color: #92a5c0;
}
.review-new-target-note {
  color: #d4b4ff;
}
.relationship-toggle,
.review-impact-toggle {
  font-size: 10px;
}
.review-impact {
  padding: 3px 10px 6px;
  background: #11161d;
  border: 1px solid #3b485e;
  border-radius: 5px;
  font-size: 10px;
  color: #acbdd4;
}
.review-impact-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 0;
  color: #93a7c3;
}
.review-impact .text-button {
  font-size: 10px;
  white-space: nowrap;
}
.review-impact table {
  width: 100%;
  border-collapse: collapse;
  font-size: 10px;
}
.review-impact th,
.review-impact td {
  padding: 5px 8px;
  text-align: right;
  border-bottom: 1px solid #2b3647;
}
.review-impact th:first-child,
.review-impact td:first-child {
  text-align: left;
}
.review-impact th {
  color: #879bb7;
  font-weight: 400;
}
.review-impact td small {
  color: #8297b5;
  padding-left: 5px;
}
.review-overlaps {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
  padding: 8px;
  color: #a6b9d3;
}
.review-impact[data-stale='true'],
.review-impact-toggle[data-stale='true'] {
  border-color: #6b573c;
}
.review-floating-panels {
  display: grid;
  padding-inline: 12px;
}
.review-floating-panels > * {
  margin-block: 8px 0;
}
.review-floating-panels > :last-child {
  margin-bottom: 8px;
}
@container review-toolbar (max-width: 1423px) {
  .review-tools {
    order: 1;
    flex-basis: 100%;
  }
}
/* A narrow toolbar keeps a second context line whether or not the draft fills it. */
@container review-toolbar (max-width: 900px) {
  .review-toolbar-context {
    flex-wrap: wrap;
    align-content: center;
    height: 60px;
  }
  .review-context-actions {
    flex-basis: 100%;
    justify-content: flex-end;
  }
}
@media (min-width: 1001px) {
  /* Two lines of messages fit the navigation row; the rest scroll in place. */
  .review-messages {
    max-height: 30px;
    overflow-y: auto;
    line-height: 15px;
  }
  /* Panels open on request and fold the same way, so they lie over the viewers. */
  .review-floating-panels {
    position: absolute;
    top: 100%;
    inset-inline: 0;
    justify-items: end;
    gap: 6px;
    max-height: calc(100dvh - 280px);
    padding-block: 6px;
    overflow: auto;
    pointer-events: none;
  }
  .review-floating-panels > * {
    width: min(640px, 100%);
    margin: 0;
    box-shadow: 0 12px 32px #0008;
    pointer-events: auto;
  }
  .review-floating-panels > .review-add-form {
    justify-self: start;
    width: min(800px, 100%);
  }
}
@media (max-width: 1000px) {
  .review-messages {
    order: 1;
    flex-basis: 100%;
  }
}
</style>

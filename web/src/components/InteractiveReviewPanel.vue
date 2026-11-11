<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, shallowReactive, shallowRef, watch } from 'vue';
import { api } from '../lib/api';
import { InteractiveController } from '../lib/interactive-controller';
import { newReviewPrompt, normalizeRelationship, pendingReviewTargets, reviewCandidateHasChanges, reviewMethod } from '../lib/interactive-review';
import { prettyName } from '../lib/geometry';
import {
  otherPathReasons,
  pathInUse,
  pathOptionText,
  pathRun,
  pathStatus,
  REVIEW_PATH_KEY,
  reviewRunPath,
  staleResultHint,
  statusReader,
  usePathChoice,
} from '../lib/inference-path';
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
  alert: [shown: boolean];
}>();
const runtime = ref<InteractiveStatus>(),
  tool = ref<InteractiveTool>('navigate'),
  include = ref(true),
  radius = ref(3),
  native = ref(true),
  showCandidate = ref(true),
  advanced = ref(false),
  impactOpen = ref(false),
  messages = ref<HTMLElement>(),
  body = ref<HTMLElement>();
const method = ref<InteractiveMethod>('direct'),
  methodError = ref('');
// Include only adds starts off, and this browser remembers the reader's choice.
const ONLY_ADDS_KEY = 'scan3d-ai-include-only-adds-v1';
const onlyAdds = ref(readOnlyAdds());
function readOnlyAdds() {
  try {
    return localStorage.getItem(ONLY_ADDS_KEY) === 'true';
  } catch {
    return false;
  }
}
function setOnlyAdds(value: boolean) {
  onlyAdds.value = value;
  try {
    localStorage.setItem(ONLY_ADDS_KEY, String(value));
  } catch {
    // Without storage the choice lasts for this page.
  }
}
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
const { selected: reviewPath, notice: reviewPathNotice, settle: settleReviewPath, choose: chooseReviewPath } = usePathChoice(REVIEW_PATH_KEY);
const controllers = new Map<string, InteractiveController>();
const draftMethods = new Map<string, InteractiveMethod>();
let disposed = false,
  identityVersion = 0,
  targetVersion = 0,
  discoveryScope = '';
const discovered = ref('');
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
    // With no segment chosen there is no draft to protect, so a new segment can start in either method.
    (!!current.value && !current.value.loaded) ||
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
const runPath = computed(() => reviewRunPath(method.value, runtime.value, reviewPath.value, view.value, current.value?.working));
const pathLine = computed(() =>
  pathStatus(
    runPath.value.paths.find(row => row.id === reviewPath.value),
    reviewPathNotice.value,
  ),
);
const otherReasons = computed(() => otherPathReasons(runPath.value.paths, reviewPath.value));
const cardUnavailable = computed(() => runPath.value.paths.some(row => !row.available));
const pathLabel = computed(() => {
  void tick.value;
  return pathInUse(runPath.value.label, current.value?.working, current.value?.job);
});
// Never part of `disabled`: marking, Direct edit and relationship-only Apply need no card.
const pathStopsRun = (full: boolean) => pathRun(runPath.value, pendingMarks.value, full).stopped;
const staleHint = (apply?: string) =>
  staleResultHint(runPath.value.locked, pending.value, current.value?.working, pathStopsRun(false), current.value?.unsaved, apply);
const canRerun = computed(() => runPath.value.canRerun && !disabled.value && policyValid.value);
const runFailed = computed(() => {
  void tick.value;
  const job = current.value?.job;
  return job?.kind === 'review_predict' && job.status === 'failed';
});
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
    // After the page has taken the targets and selection, so the watcher below sees them.
    await nextTick();
    if (!disposed && identity === identityVersion && scope === `${props.caseId}:${props.revisionId}`) discovered.value = scope;
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
  // A scan with no segments has nothing to review until one is added.
  if (!props.selected) {
    if (controller.value) {
      controller.value = undefined;
      publish();
    }
    return;
  }
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
const readRuntime = statusReader(
  () => api.interactiveStatus(),
  (status, overlapped) => {
    runtime.value = status;
    settleReviewPath(status.inference_paths ?? [], overlapped);
  },
);
async function checkRuntime() {
  if (method.value === 'nninteractive') await readRuntime();
}
/**
 * A refusal or a failed job is the only news a page gets that its list may be out of date, and
 * settling on a fresh one brings the notice, or the block and its Check again. Without a card in
 * play a failure says nothing about one, so one path asks for nothing it did not before.
 */
function recheckCards() {
  if (runPath.value.id) void readRuntime();
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
  if (!['point', 'navigate', 'scissors', 'remove'].includes(value)) native.value = true;
}
function addGesture(prompt: InteractivePrompt) {
  if (!disabled.value) current.value?.append(prompt);
}
// A double-click must take one mark, and must not both ask about a mark and answer for it.
const REMOVE_PAUSE_MS = 350;
let removeClick = -Infinity;
/** The mark whose removal would discard the result on screen, after one click on it. */
const armed = ref('');
function removeMark(id: string) {
  const value = current.value,
    index = value?.prompts.findIndex(item => item.id === id) ?? -1,
    now = performance.now();
  if (!value || index < 0 || disabled.value || method.value !== 'nninteractive' || now - removeClick < REMOVE_PAUSE_MS) return;
  removeClick = now;
  // Undo brings a mark back but not a result made with it, so that one click is asked about. It is
  // asked once: the removal leaves no result for the next one to discard.
  if (value.candidate && index < value.appliedCount && armed.value !== id) {
    armed.value = id;
    return;
  }
  armed.value = '';
  value.remove(id);
}
async function run(full: boolean, choice?: InteractiveRunChoice) {
  const value = current.value,
    identity = identityVersion,
    caseId = props.caseId,
    revisionId = props.revisionId;
  showCandidate.value = true;
  // A re-run repeats each step as it first ran; the guard reaches only marks it adds as a new step.
  await value?.apply(full, choice, method.value === 'nninteractive' && onlyAdds.value);
  if (disposed || identity !== identityVersion || current.value !== value || props.caseId !== caseId || props.revisionId !== revisionId) return;
  if (value?.job) policyOpen.value = false;
  else if (value?.error) recheckCards();
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
function toggleAdd() {
  addOpen.value = !addOpen.value;
  policyOpen.value = false;
  // The form opens at the top of the tools, which may have scrolled past it.
  if (addOpen.value) body.value?.scrollTo({ top: 0 });
}
// Impact and Options open under their buttons near the end of the tools, often below a short
// column's fold, so the tools scroll until the part shows, keeping its button in view.
async function reveal(button: string, selector: string) {
  await nextTick();
  const box = body.value,
    toggle = box?.querySelector(button),
    part = box?.querySelector(selector);
  if (!box || !toggle || !part) return;
  const view = box.getBoundingClientRect(),
    below = part.getBoundingClientRect().bottom - view.bottom;
  if (below > 0) box.scrollTop += Math.min(below, toggle.getBoundingClientRect().top - view.top);
}
function toggleImpact() {
  impactOpen.value = !impactOpen.value;
  if (impactOpen.value) void reveal('.review-impact-toggle', '.review-impact');
}
function toggleAdvanced() {
  advanced.value = !advanced.value;
  if (advanced.value) void reveal('.review-more', '.review-advanced');
}
function previewAffected() {
  if (candidate.value) emit('preview-overlaps', candidate.value.affected_structures?.map(item => item.id) ?? [props.selected]);
}
function changeNative(value: boolean) {
  if (method.value === 'direct') return;
  native.value = value;
  if (!value && !['point', 'navigate', 'remove'].includes(tool.value)) chooseTool('point');
}
// The question is about one mark and one result, so it ends when either changes, or the tool or
// draft does. A result changes with the marks or through a run, which blocks editing; its name
// alone can change with any save.
watch(
  [
    tool,
    // The ref itself, being shallow, would end the question on every notify, such as a save's answer.
    () => controller.value,
    disabled,
    () => {
      void tick.value;
      return controller.value?.prompts;
    },
    () => !!candidate.value,
  ],
  () => {
    armed.value = '';
  },
);
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
    discovered.value = '';
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
// A scan with no segments has nothing selected. Review returns to its unsaved new segment, the
// remembered one first and otherwise the newest the server listed, or opens the form that adds one.
watch(
  () => [!!props.active && discovered.value === `${props.caseId}:${props.revisionId}` && !props.selected, draftTargets.value] as const,
  ([idle, drafts]) => {
    if (!idle || creating.value) return;
    let remembered: string | null = null;
    try {
      remembered = localStorage.getItem(rememberedTargetKey());
    } catch {
      /* The newest draft stands in. */
    }
    const target = drafts.find(item => item.id === remembered) ?? drafts[0];
    if (target) emit('select', target.id);
    else if (!props.structures.length) {
      addOpen.value = true;
      policyOpen.value = false;
    }
  },
);
watch(method, value => {
  native.value = true;
  include.value = true;
  chooseTool(value === 'direct' ? 'scribble' : 'point');
  if (value === 'nninteractive') void checkRuntime();
});
// A job fails long after apply() has returned, so run() never sees it. As there, only this draft counts.
watch([runFailed, controller], ([failed, value], [, before]) => {
  if (failed && value === before && value?.caseId === props.caseId && value.revisionId === props.revisionId) recheckCards();
});
// Errors lead the message list; one that arrives while it is scrolled must not stay out of view.
watch(
  [() => current.value?.error, createError, methodError, () => runPath.value.block, () => props.surfaceError, armed],
  () => messages.value?.scrollTo({ top: 0 }),
  { flush: 'post' },
);
// The page marks the hidden side panel's edge while an error, warning or question waits here.
const alerting = computed(
  () =>
    !!(
      armed.value ||
      current.value?.error ||
      createError.value ||
      props.surfaceError ||
      methodError.value ||
      runPath.value.block ||
      (method.value === 'nninteractive' && runtime.value && !runtime.value.available) ||
      candidate.value?.warnings?.length
    ),
);
watch(alerting, value => emit('alert', value), { immediate: true });
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
  removeMark,
  addPrompt(ras: Point) {
    if (method.value !== 'direct') addGesture(newReviewPrompt(ras, include.value));
  },
  async show() {
    if (!disposed) {
      chooseTool(method.value === 'direct' ? 'scribble' : 'point');
      await Promise.all([checkRuntime(), ensureController()]);
    }
  },
  /**
   * Marks are saved 250 ms after they are drawn, and a case moved to Trash must return with them.
   * False when a mark could not be saved, because the panel that shows why is about to go.
   */
  saveDrafts: async () => {
    const values = [...controllers.values()];
    await Promise.allSettled(values.map(controller => controller.flush()));
    return !values.some(controller => controller.unsaved);
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
    <header class="review-panel-header">
      <div class="review-panel-title">
        <h2>Review segmentation</h2>
        <slot name="exit" />
      </div>
      <div class="review-target-row">
        <label class="review-target"
          >Structure<select
            :value="selected"
            aria-label="Review structure"
            :disabled="creating || discarding || !!current?.working"
            @change="emit('select', ($event.target as HTMLSelectElement).value)"
          >
            <option v-if="!structures.some(item => item.id === selected)" value="" disabled>
              {{ structures.length ? 'Choose a segment' : 'No segments yet' }}
            </option>
            <option v-for="item in structures" :key="item.id" :value="item.id">
              {{ prettyName(item.name) }}{{ draftTargets.some(target => target.id === item.id) ? ' · draft' : '' }}
            </option>
          </select></label
        ><button
          class="button secondary compact review-add-target"
          :disabled="creating || discarding || busy || !!current?.working"
          :aria-expanded="addOpen"
          @click="toggleAdd"
        >
          + Add segment
        </button>
      </div>
    </header>
    <div ref="messages" class="review-messages" role="group" aria-label="Review messages" tabindex="0">
      <p v-if="armed" class="review-alert" role="status">
        This mark was already run. Removing it discards the result on screen; Run AI then makes a new one from the marks that remain. Click the mark again to
        remove it.
      </p>
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
        ><span v-else-if="current?.dirty">{{ staleHint() }}</span
        ><span v-else>Inspect the result, then save a new revision.</span>
      </div>
      <p v-else-if="method === 'nninteractive' && current?.resultDiscarded && !current.working" class="review-progress" role="status">
        The result was discarded because its marks or options changed. Run AI makes a new one from the marks that remain.
      </p>
      <p v-for="warning in candidate?.warnings || []" :key="warning" class="review-alert" role="status">{{ warning }}</p>
    </div>
    <!-- The tools scroll between the header and the footer, so Apply and Save always show. Each part opens in place. -->
    <div ref="body" class="review-toolbar-rows">
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
        ><SegmentRelationshipEditor
          v-model="newRelationship"
          :structures="savedStructures"
          target-id=""
          :disabled="creating || discarding"
          @validity="newPolicyValid = $event"
        /><span>Draw the new anatomy on {{ modalityLabel }}, then Apply and save.</span>
        <div class="review-add-actions">
          <button class="button primary compact" type="submit" :disabled="creating || discarding || !newName.trim() || !newPolicyValid">
            {{ creating ? 'Creating…' : 'Create empty segment' }}</button
          ><button class="text-button" type="button" :disabled="creating || discarding" @click="addOpen = false">Cancel</button>
        </div>
      </form>
      <div v-else class="review-relationship">
        <button
          class="text-button relationship-toggle"
          aria-label="Edit segment relationship"
          :aria-expanded="policyOpen"
          :disabled="!current?.loaded || discarding"
          @click="togglePolicy"
        >
          Relationship · {{ relationshipName }}
        </button>
        <SegmentRelationshipEditor
          v-if="policyOpen && current?.loaded"
          :model-value="relationship"
          :structures="savedStructures"
          :target-id="selected"
          :disabled="disabled || policyLoading"
          :suggestions="view?.relationship_suggestions"
          @update:model-value="setRelationship"
          @validity="policyValid = $event"
        />
      </div>
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
                ['remove', 'Remove mark'],
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
          >Radius<span><input v-model.number="radius" type="number" aria-label="Brush radius in millimetres" min=".5" max="30" step=".5" /> mm</span></label
        >
      </div>
      <div class="review-toolbar-context">
        <span v-if="discarding">Closing draft…</span><span v-else-if="!current">Add a segment to start.</span
        ><span v-else-if="!current.loaded">Loading saved work…</span
        ><span v-else-if="current.working" class="review-working"
          ><span class="spinner"></span>{{ current.accepted ? 'Opening the saved revision…' : current.job?.stage || 'Processing on workstation…'
          }}<button v-if="!current.accepted" class="text-button" @click="current.cancel()">Cancel</button></span
        ><span v-else
          >{{ current.prompts.length }} {{ current.prompts.length === 1 ? 'gesture' : 'gestures' }} ·
          {{
            current.conflict && current.unsaved
              ? 'not saved'
              : current.saving || current.unsaved
                ? 'saving draft…'
                : view
                  ? 'draft saved'
                  : `mark the ${modalityLabel}, then Apply once`
          }}<template v-if="pending"> · {{ pending }} unapplied</template></span
        >
        <span v-if="view?.started_by">Started by {{ view.started_by }}</span>
        <span v-if="method === 'direct' && tool === 'scissors'"
          >3D cut · {{ scissorsOperation === 'erase_inside' ? 'erase inside' : 'erase outside' }} through the full depth of the selected segment.
          {{ scissorsShape === 'circle' ? 'Drag from centre to edge.' : 'Draw on the 3D view.' }} Apply edits to preview.</span
        ><span v-else-if="method === 'direct'">Exact paint/erase on one native slice · no AI. Add strokes, then Apply edits.</span
        ><span v-else-if="tool !== 'navigate'">{{
          tool === 'remove'
            ? 'Click a mark to remove it; the outlined one goes. ↶ Undo puts it back.'
            : tool === 'point'
              ? 'Click to mark an AI hint.'
              : tool === 'bbox'
                ? 'Drag an AI box hint on one native slice.'
                : tool === 'lasso'
                  ? 'Draw an AI lasso hint on one native slice.'
                  : 'Brush hints guide a 3D AI prediction.'
        }}</span>
        <span v-if="view?.new_structure" class="review-new-target-note">New segment · starts empty</span>
        <span v-if="policyOpen && policyLoading">Loading saved relationship…</span>
        <span v-if="pathLabel" class="review-ai-path" :title="pathLabel">AI · {{ pathLabel }}</span>
        <div class="review-context-actions">
          <button v-if="hasImpact" class="text-button review-impact-toggle" :aria-expanded="impactOpen" :data-stale="!!current?.dirty" @click="toggleImpact">
            Impact preview · {{ candidate?.affected_structures?.length || 1 }} {{ candidate?.affected_structures?.length === 1 ? 'mask' : 'masks' }}
          </button>
        </div>
      </div>
      <div v-if="hasImpact" v-show="impactOpen" class="review-impact" :data-stale="!!current?.dirty">
        <div class="review-impact-heading">
          <span
            >{{
              current?.dirty
                ? `Previous preview. ${staleHint('Apply the latest edits and relationship before saving.')}`
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
      <button class="text-button review-more" :aria-expanded="advanced" @click="toggleAdvanced">Options {{ advanced ? '−' : '+' }}</button>
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
        <label v-if="method === 'nninteractive'"
          ><input type="checkbox" :checked="onlyAdds" @change="setOnlyAdds(($event.target as HTMLInputElement).checked)" />
          Include only adds
          <small
            >A Run AI with only Include marks never removes voxels, and one with only Exclude marks never adds. Mixed runs and Refine whole mask are not
            guarded.</small
          ></label
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
          <label class="review-run-on"
            >{{ runPath.locked ? 'Re-run draft on' : 'Run on'
            }}<select :value="reviewPath" :disabled="current.working || discarding" @change="chooseReviewPath(($event.target as HTMLSelectElement).value)">
              <option v-for="row in runPath.paths" :key="row.id" :value="row.id" :disabled="!row.available">{{ pathOptionText(row) }}</option>
            </select></label
          >
          <button v-if="cardUnavailable" class="text-button" @click="checkRuntime">Check again</button>
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
          <p v-for="(reason, index) in otherReasons" :key="index" role="status">{{ reason }}</p>
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
    <footer class="review-footer">
      <!-- Comparing the result is the step before Save, so it stays with the buttons below the scrolling tools. -->
      <div v-if="candidate || current?.hasDraftChanges" class="review-compare">
        <div v-if="candidate" class="review-toolset candidate-switch" role="group" aria-label="Candidate display">
          <button :class="{ active: !showCandidate }" :aria-pressed="!showCandidate" @click="showCandidate = false">
            {{ view?.new_structure ? 'Empty baseline' : 'Original' }}</button
          ><button :class="{ active: showCandidate }" :aria-pressed="showCandidate" @click="showCandidate = true">
            Candidate<template v-if="current?.dirty"> · previous result</template>
          </button>
        </div>
        <button v-if="current?.hasDraftChanges" class="text-button review-discard" :disabled="current.working || discarding || busy" @click="discardCurrent">
          {{ view?.started_by ? `Discard ${view.started_by}'s draft` : 'Discard draft' }}
        </button>
      </div>
      <div class="review-history">
        <button class="button secondary compact" :disabled="discarding || !current?.canUndo" @click="current?.undo()">↶ Undo</button
        ><button class="button secondary compact" :disabled="discarding || !current?.canRedo" @click="current?.redo()">↷ Redo</button>
      </div>
      <div class="review-primary-actions">
        <button class="button secondary compact review-apply" :disabled="disabled || !pending || !policyValid || pathStopsRun(false)" @click="apply()">
          {{ method === 'direct' ? 'Apply edits' : 'Run AI' }}<template v-if="pending"> · {{ pending }}</template></button
        ><button class="button primary compact review-save" :disabled="!saveEnabled" @click="accept">
          {{ view?.new_structure ? 'Save new segment' : 'Save correction' }}
        </button>
      </div>
    </footer>
  </section>
</template>

<style scoped>
/*
 * Review's tools dock in the side panel, so no tool, message or open part ever covers or resizes a
 * viewer. The header, the messages and the footer stay put; the tools between them scroll when the
 * column is short, so Apply and Save always show.
 */
.review-toolbar {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-height: 0;
  background: #171c24;
  color: #c8d1df;
}
.review-panel-header {
  display: grid;
  flex: none;
  gap: 8px;
  padding: 9px 12px 10px;
  border-bottom: 1px solid #2b3647;
}
.review-panel-title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.review-panel-title h2 {
  flex: 1 1 auto;
  min-width: 0;
  margin: 0;
  overflow: hidden;
  color: #d8e2f2;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.review-panel-title .text-button {
  flex: none;
  font-size: 10px;
}
.review-target-row {
  display: flex;
  align-items: flex-end;
  gap: 6px;
}
.review-target {
  display: grid;
  flex: 1 1 0;
  gap: 3px;
  min-width: 0;
  font-size: 9px;
  color: #a9b6c9;
}
.review-target select {
  width: 100%;
  min-width: 0;
  padding: 5px 7px;
  font-size: 11px;
}
/*
 * About four lines show; more scroll in place, and an error scrolls them back to the top. The tools
 * below take only the room left over, so in a very short panel the messages give way before the
 * bottom buttons do.
 */
.review-messages {
  display: grid;
  flex: 0 1 auto;
  min-height: 0;
  gap: 4px;
  max-height: 78px;
  padding: 7px 12px;
  overflow-y: auto;
  overflow-anchor: none;
  border-bottom: 1px solid #2b3647;
  line-height: 15px;
  scrollbar-width: thin;
  scrollbar-color: var(--control-border) transparent;
}
.review-messages:focus-visible {
  outline: 2px solid var(--control-focus);
  outline-offset: -2px;
}
.review-messages:empty {
  display: none;
}
/* A zero minimum keeps a long GPU name from widening the column; the chip shortens instead. */
.review-toolbar-rows {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  flex: 1 1 0%;
  align-content: start;
  gap: 10px;
  min-height: 0;
  padding: 10px 12px 12px;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--control-border) transparent;
}
.review-relationship {
  display: grid;
  justify-items: start;
  gap: 8px;
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
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.review-toolset button.active {
  background: #39465b;
  color: #fff;
}
.review-toolset button:disabled {
  opacity: 0.4;
  cursor: default;
}
/* Tools fill a grid of equal cells, as many to a row as the column holds. The minimum fits the
   longest label, Remove mark, whole. */
.review-methods,
.review-tools > .review-toolset {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(90px, 1fr));
}
.review-methods,
.review-tools > .polarity {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.polarity button:first-child.active {
  background: #25463d;
  color: #9ae4c2;
}
.polarity button:last-child.active {
  background: #51333d;
  color: #ffb9c3;
}
.review-tools {
  display: grid;
  gap: 7px;
}
.review-brush,
.review-scissors {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 10px;
}
.review-brush input {
  width: 58px;
  padding: 5px 6px;
  font-size: 11px;
}
.review-scissors select {
  flex: 0 1 140px;
  min-width: 0;
  padding: 5px 6px;
  font-size: 11px;
}
.review-toolbar-context {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px 8px;
  font-size: 10px;
  line-height: 1.45;
  color: #9dadc4;
}
.review-toolbar-context > span {
  flex: 1 1 100%;
  min-width: 0;
}
.review-toolbar-context > span:first-child {
  color: #c1cadd;
}
.review-toolbar-context > .review-ai-path {
  flex: 0 1 auto;
  overflow: hidden;
  padding: 1px 7px;
  border: 1px solid #424e60;
  border-radius: 9px;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.review-context-actions {
  display: flex;
  flex: 1 1 100%;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.review-context-actions:empty {
  display: none;
}
/* Original | Candidate and Discard draft share the first line, apart from Undo and Redo. */
.review-compare {
  display: flex;
  flex: 1 1 100%;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.review-compare > .candidate-switch {
  display: grid;
  flex: 1 1 150px;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  padding: 1px;
}
.candidate-switch button {
  padding: 3px 7px;
  font-size: 10px;
  white-space: normal;
}
.review-result-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 0 12px;
  font-size: 10px;
  color: #c1cfe4;
}
.review-working {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
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
  margin: 0;
  font-size: 11px;
  color: #e1b6b6;
}
/* An action follows the end of its message, so the column's full width carries the text. */
.review-alert .text-button {
  margin-left: 4px;
  padding: 1px 7px;
  font-size: 10px;
}
.review-toolbar .button {
  min-height: 29px;
  padding: 6px 10px;
  font-size: 11px;
}
.relationship-toggle,
.review-impact-toggle,
.review-more {
  justify-self: start;
  font-size: 10px;
}
.review-new-target-note {
  color: #d4b4ff;
}
.review-add-form,
.review-advanced,
.review-impact {
  border: 1px solid #3b485e;
  border-radius: 5px;
  background: #11161d;
  font-size: 10px;
}
.review-add-form {
  display: grid;
  gap: 9px;
  padding: 10px;
}
.review-add-form label {
  display: grid;
  gap: 4px;
  color: #acbdd4;
}
.review-add-form input {
  width: 100%;
  min-width: 0;
  padding: 7px;
  font-size: 12px;
}
.review-add-form .review-add-color {
  grid-template-columns: auto 42px;
  justify-content: start;
  align-items: center;
  gap: 10px;
}
.review-add-form .review-add-color input {
  height: 30px;
  padding: 2px;
}
.review-add-form > span {
  color: #92a5c0;
}
/* The form is taller than a short column, so its buttons stick to the bottom of the tools, over their 12 px padding. */
.review-add-actions {
  position: sticky;
  bottom: -12px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding-block: 6px;
  background: #11161d;
}
.review-advanced {
  display: grid;
  gap: 10px;
  padding: 10px;
}
.review-advanced label {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 6px;
}
.review-advanced small {
  flex-basis: 100%;
  color: #8696ad;
}
.review-advanced input {
  margin: 0;
}
.review-advanced .review-run-on {
  display: grid;
}
.review-advanced select {
  width: 100%;
  min-width: 0;
  padding: 5px 7px;
  font-size: 11px;
}
.review-advanced > :is(.button, .text-button) {
  justify-self: start;
  font-size: 10px;
}
.review-advanced p {
  margin: 0;
  color: #91a2ba;
  line-height: 1.5;
}
.review-impact {
  padding: 3px 10px 6px;
  color: #acbdd4;
}
.review-impact-heading {
  display: grid;
  justify-items: start;
  gap: 6px;
  padding: 7px 0;
  color: #93a7c3;
}
.review-impact .text-button {
  font-size: 10px;
  white-space: nowrap;
}
/* Each segment takes a line of its own, with its three numbers under it, so the table fits the column. */
.review-impact table,
.review-impact :is(thead, tbody) {
  display: grid;
  width: 100%;
  font-size: 10px;
}
.review-impact tr {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 2px 6px;
  padding: 5px 0;
  border-bottom: 1px solid #2b3647;
}
.review-impact :is(th, td) {
  padding: 0;
  text-align: right;
  overflow-wrap: anywhere;
}
.review-impact :is(th, td):first-child {
  grid-column: 1 / -1;
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
  padding: 8px 0;
  color: #a6b9d3;
}
.review-impact[data-stale='true'],
.review-impact-toggle[data-stale='true'] {
  border-color: #6b573c;
}
/* Undo and Redo, then Apply and Save, share a line when the column has room and stack when it has not. */
.review-footer {
  display: flex;
  flex: none;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 12px;
  border-top: 1px solid #354052;
  background: #141921;
}
.review-history,
.review-primary-actions {
  display: flex;
  flex: 1 1 auto;
  flex-wrap: wrap;
  gap: 6px;
}
.review-footer .button {
  flex: 1 1 auto;
}
/* Labels change with the draft, so the buttons keep room for the longest one. */
.review-apply {
  min-width: 104px;
}
.review-save {
  min-width: 124px;
  background: #cfddf2;
}
.review-discard {
  margin-left: auto;
  font-size: 10px;
}
</style>

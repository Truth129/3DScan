import { api, ApiError } from './api';
import type { InteractiveMethod, InteractiveOptions, InteractivePrompt, InteractiveReview, InteractiveRunChoice, Job, SegmentRelationship } from '../types';
import { normalizeRelationship, reviewMethod } from './interactive-review';

interface Snapshot {
  prompts: InteractivePrompt[];
  options: InteractiveOptions;
  relationship: SegmentRelationship;
}
const initialOptions = (): InteractiveOptions => ({ autozoom: true, start_empty: false });
const signature = (value: unknown) =>
  JSON.stringify(value, (_key, item) => {
    if (item && typeof item === 'object' && !Array.isArray(item)) {
      const normalized = 'ras' in item && 'include' in item && !item.kind ? { ...item, kind: 'point' } : item;
      return Object.fromEntries(
        Object.keys(normalized)
          .sort()
          .map(key => [key, normalized[key]]),
      );
    }
    return item;
  });
const snapshot = (prompts: InteractivePrompt[], options: InteractiveOptions, relationship?: SegmentRelationship): Snapshot => ({
  prompts: structuredClone(prompts),
  options: { ...options },
  relationship: normalizeRelationship(relationship),
});
const saved = (value: InteractiveReview) => signature(snapshot(value.prompts, value.options ?? initialOptions(), value.relationship));
type Reader = Pick<
  typeof api,
  | 'interactiveReviews'
  | 'startInteractive'
  | 'interactivePrompts'
  | 'interactiveReview'
  | 'predictInteractive'
  | 'acceptInteractive'
  | 'discardInteractive'
  | 'cancel'
  | 'job'
>;

/** Local gesture history is independent of WAN writes. One queue saves the newest snapshot. */
export class InteractiveController {
  review?: InteractiveReview;
  prompts: InteractivePrompt[] = [];
  options = initialOptions();
  relationship = normalizeRelationship();
  loaded = false;
  saving = false;
  working = false;
  conflict = false;
  error = '';
  job?: Job;
  private history: Snapshot[] = [snapshot([], initialOptions())];
  private cursor = 0;
  private persisted = signature(this.history[0]);
  private timer: ReturnType<typeof setTimeout> | undefined;
  private writing?: Promise<void>;
  private pollTimer: ReturnType<typeof setTimeout> | undefined;
  private stopped = false;
  private cancelRequested = false;
  private initialRelationship = normalizeRelationship();
  private starting?: Promise<InteractiveReview>;

  constructor(
    readonly caseId: string,
    readonly revisionId: string,
    readonly structureId: string,
    private changed: () => void,
    private reader: Reader = api,
    relationship?: SegmentRelationship,
    readonly method: InteractiveMethod = 'nninteractive',
  ) {
    this.initialRelationship = normalizeRelationship(relationship);
    this.install();
  }
  get canUndo() {
    return this.cursor > 0 && !this.working;
  }
  get canRedo() {
    return this.cursor + 1 < this.history.length && !this.working;
  }
  get unsaved() {
    return signature(snapshot(this.prompts, this.options, this.relationship)) !== this.persisted;
  }
  get appliedCount() {
    return this.review?.applied_prompt_count ?? (this.review?.candidate ? this.review.prompts.length : 0);
  }
  get candidate() {
    const value = this.review;
    if (!value?.candidate || ['accepted', 'discarded'].includes(value.status) || signature(value.options ?? initialOptions()) !== signature(this.options))
      return undefined;
    const count = this.appliedCount;
    return signature(value.prompts.slice(0, count)) === signature(this.prompts.slice(0, count)) && this.prompts.length >= count ? value.candidate : undefined;
  }
  get relationshipDirty() {
    const value = this.review;
    if (value?.candidate) return signature(this.relationship) !== signature(normalizeRelationship(value.candidate.relationship ?? value.relationship));
    return !!value?.relationship_dirty || signature(this.relationship) !== signature(normalizeRelationship(value?.relationship ?? this.initialRelationship));
  }
  /** Marks waiting for a model. A relationship edit alone needs no card, so it is counted apart. */
  get pendingMarks() {
    return this.candidate ? Math.max(0, this.prompts.length - this.appliedCount) : this.prompts.length;
  }
  get pendingCount() {
    return this.pendingMarks + Number(this.relationshipDirty);
  }
  get dirty() {
    return this.unsaved || !!this.review?.dirty || !this.candidate || this.pendingCount > 0;
  }
  get hasDraftChanges() {
    return !!(this.prompts.length || this.review?.new_structure || this.candidate || this.relationshipDirty || this.unsaved);
  }
  get publicReview(): InteractiveReview | undefined {
    return this.review && !['accepted', 'discarded'].includes(this.review.status)
      ? {
          ...this.review,
          prompts: this.prompts,
          options: this.options,
          relationship: this.relationship,
          relationship_dirty: this.relationshipDirty,
          candidate: this.candidate,
          dirty: this.dirty,
          pending_count: this.pendingCount,
        }
      : undefined;
  }
  private notify() {
    this.changed();
  }
  adopt(value: InteractiveReview) {
    if (
      value.case_id !== this.caseId ||
      value.revision_id !== this.revisionId ||
      value.structure_id !== this.structureId ||
      reviewMethod(value) !== this.method ||
      ['accepted', 'discarded'].includes(value.status)
    )
      throw new Error('This draft belongs to a different review target or editing method');
    this.install(value);
    this.loaded = true;
    this.working = ['predicting', 'accepting'].includes(value.status);
    this.error = '';
    this.notify();
    if (this.working) this.poll();
  }
  async load() {
    try {
      const records = await this.reader.interactiveReviews(this.caseId);
      const latest = records.find(
        item =>
          item.revision_id === this.revisionId &&
          item.structure_id === this.structureId &&
          reviewMethod(item) === this.method &&
          !['accepted', 'discarded'].includes(item.status),
      );
      if (this.stopped) return;
      this.install(latest);
      this.loaded = true;
      if (latest && ['predicting', 'accepting'].includes(latest.status)) {
        this.working = true;
        this.poll();
      }
    } catch (reason) {
      this.error = reason instanceof Error ? reason.message : String(reason);
      this.loaded = true;
    }
    this.notify();
  }
  private install(value?: InteractiveReview) {
    // A Discard or a Reload can land during a follow, and what was being followed belonged to the
    // record this replaces. A poll left running would stop on a missing review with the spinner on.
    clearTimeout(this.pollTimer);
    this.working = false;
    this.review = value;
    this.prompts = structuredClone(value?.prompts ?? []);
    this.options = { ...(value?.options ?? initialOptions()) };
    this.relationship = normalizeRelationship(value?.relationship ?? this.initialRelationship);
    this.history = [snapshot(this.prompts, this.options, this.relationship)];
    this.cursor = 0;
    this.persisted = signature(this.history[0]);
    this.conflict = false;
  }
  edit(prompts: InteractivePrompt[], options = this.options, relationship = this.relationship) {
    if (!this.loaded || this.working || this.conflict) return;
    const next = snapshot(prompts, options, relationship);
    if (signature(next) === signature(this.history[this.cursor])) return;
    this.history = [...this.history.slice(0, this.cursor + 1), next];
    this.cursor++;
    this.prompts = next.prompts;
    this.options = next.options;
    this.relationship = normalizeRelationship(next.relationship);
    this.error = '';
    this.schedule();
    this.notify();
  }
  editRelationship(value: SegmentRelationship) {
    this.edit(this.prompts, this.options, value);
  }
  append(prompt: InteractivePrompt) {
    if (this.method === 'direct' && prompt.kind !== 'scribble' && prompt.kind !== 'scissors') {
      this.error = 'Direct editing uses Paint and Erase strokes on CT or Scissors in 3D.';
      this.notify();
      return;
    }
    if (this.method === 'nninteractive' && prompt.kind === 'scissors') {
      this.error = '3D scissors is available in Direct edit.';
      this.notify();
      return;
    }
    if (this.prompts.length >= 128 || [...this.prompts, prompt].reduce((sum, item) => sum + ('vertices' in item ? item.vertices.length : 0), 0) > 8192) {
      this.error = 'This draft has reached its gesture limit. Remove an edit before adding more.';
      this.notify();
      return;
    }
    this.edit([...this.prompts, prompt]);
  }
  undo() {
    if (this.canUndo) {
      this.cursor--;
      this.restore();
    }
  }
  redo() {
    if (this.canRedo) {
      this.cursor++;
      this.restore();
    }
  }
  private restore() {
    const value = this.history[this.cursor];
    this.prompts = value.prompts;
    this.options = value.options;
    this.relationship = normalizeRelationship(value.relationship);
    this.schedule();
    this.notify();
  }
  async ensureDraft() {
    if (!this.loaded || this.working || this.conflict) return;
    try {
      const value = await this.createReview();
      this.notify();
      return value;
    } catch (reason) {
      this.error = reason instanceof Error ? reason.message : String(reason);
      this.notify();
    }
  }
  private async createReview(): Promise<InteractiveReview> {
    if (this.review) return this.review;
    if (this.starting) return this.starting;
    this.starting = this.startReview();
    try {
      return await this.starting;
    } finally {
      this.starting = undefined;
    }
  }
  private async startReview() {
    const requested = normalizeRelationship(this.relationship),
      explicit = signature(requested) !== signature(this.initialRelationship);
    const value = await this.reader.startInteractive(this.caseId, this.revisionId, this.structureId, explicit ? requested : undefined, this.method);
    if (reviewMethod(value) !== this.method) throw new Error('The workstation returned a different editing method. Reload before editing.');
    this.review = value;
    if (!explicit) {
      const previous = this.initialRelationship,
        effective = normalizeRelationship(value.relationship ?? previous);
      this.history = this.history.map(item =>
        signature(item.relationship) === signature(previous) ? { ...item, relationship: normalizeRelationship(effective) } : item,
      );
      this.initialRelationship = effective;
      if (signature(this.relationship) === signature(requested)) this.relationship = normalizeRelationship(effective);
      this.persisted = signature(snapshot(value.prompts, value.options ?? initialOptions(), value.relationship ?? effective));
    }
    return value;
  }
  private schedule() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      void this.flush().catch(() => {});
    }, 250);
  }
  async flush(): Promise<void> {
    clearTimeout(this.timer);
    if (this.writing) {
      await this.writing;
      if (this.unsaved && !this.conflict) return this.flush();
      return;
    }
    if (!this.unsaved || this.conflict) return;
    this.saving = true;
    this.notify();
    this.writing = (async () => {
      try {
        if (!this.review) await this.createReview();
        while (this.unsaved && !this.conflict) {
          const requested = snapshot(this.prompts, this.options, this.relationship);
          const value = await this.reader.interactivePrompts(
            this.caseId,
            this.review!.id,
            this.review!.generation,
            requested.prompts,
            requested.options,
            requested.relationship,
          );
          this.review = value;
          this.persisted = signature(requested);
          this.notify();
        }
      } catch (reason) {
        this.error = reason instanceof Error ? reason.message : String(reason);
        if (reason instanceof ApiError && reason.status === 409) this.conflict = true;
        throw reason;
      } finally {
        this.saving = false;
        this.writing = undefined;
        this.notify();
      }
    })();
    return this.writing;
  }
  async reload() {
    clearTimeout(this.timer);
    if (this.writing) await this.writing.catch(() => {});
    this.error = '';
    await this.load();
  }
  async apply(fullRefine = false, choice?: InteractiveRunChoice) {
    if (this.working || this.conflict) return;
    if (this.method === 'direct' && fullRefine) {
      this.error = 'Whole-mask refinement is available in AI assist.';
      this.notify();
      return;
    }
    this.working = true;
    this.job = undefined;
    this.cancelRequested = false;
    this.error = '';
    this.notify();
    try {
      await this.flush();
      if (fullRefine && !this.review) await this.createReview();
      // A re-run has nothing pending by design, and saving may just have freed the draft.
      const replay = !!choice?.replay && !!this.review?.batches?.length;
      if (!this.review || (this.pendingCount === 0 && !fullRefine && !replay)) {
        this.working = false;
        this.notify();
        return;
      }
      const { id, generation } = this.review;
      // One path sends no fifth argument at all, so it stays today's request.
      this.job = await (choice
        ? this.reader.predictInteractive(this.caseId, id, generation, fullRefine, choice)
        : this.reader.predictInteractive(this.caseId, id, generation, fullRefine));
      this.review = { ...this.review, status: 'predicting', job_id: this.job.id };
      this.notify();
      if (this.cancelRequested) await this.reader.cancel(this.job.id);
      if (!this.takeOn(this.review, await this.reader.interactiveReview(this.caseId, this.review.id))) return;
      this.notify();
      this.poll();
    } catch (reason) {
      this.error = reason instanceof Error ? reason.message : String(reason);
      this.working = false;
      this.notify();
      // `job` is set only once predict has answered, and a save that failed this way set `conflict`.
      if (reason instanceof ApiError && reason.status === 409 && !this.job && !this.conflict && this.review) await this.catchUp(this.review);
    }
  }
  /**
   * A 409 from predict can mean this page is behind, and no save may ever tell it: publishing a
   * result moves no generation, so another tab's lock and its Re-run would never arrive. Only
   * `review` moves, because install() and adopt() reset the marks and their history. A newer
   * generation holds another tab's marks, and this page's next save would overwrite them if it
   * took that generation on, so that asks for a reload instead.
   */
  private async catchUp(seen: InteractiveReview) {
    const refusal = this.error;
    let value: InteractiveReview;
    try {
      value = await this.reader.interactiveReview(this.caseId, seen.id);
    } catch {
      return; // The refusal stays on screen, and it is still true.
    }
    // A save in flight can already be in this reply, which would read as another tab's generation.
    if (this.stopped || this.working || this.saving || this.review !== seen) return;
    if (value.generation !== seen.generation || ['accepted', 'discarded'].includes(value.status)) {
      this.conflict = true;
      // Reload saved work sits inside the message, and an edit since the refusal has cleared it.
      this.error ||= refusal;
    } else {
      this.review = value;
      if (['predicting', 'accepting'].includes(value.status)) {
        this.working = true;
        this.poll();
      }
    }
    this.notify();
  }
  /**
   * Takes on a record read from the server, unless another tab's work is in it. A save sends this
   * page's marks, options and relationship at the generation it holds, so holding a newer one would
   * let that save past the server's check and overwrite the work. A generation can move with
   * nothing in it changed, as a re-run's does at enqueue. Both sides are the server's own records,
   * so a mark it normalised still compares equal.
   */
  private takeOn(seen: InteractiveReview, value: InteractiveReview) {
    if (value.generation === seen.generation || saved(value) === saved(seen)) {
      this.review = value;
      return true;
    }
    this.working = false;
    this.conflict = true;
    this.error = 'The correction draft changed; reload its current prompts';
    this.notify();
    return false;
  }
  async accept(): Promise<Job | undefined> {
    if (this.working || this.dirty || !this.review?.candidate) return;
    this.working = true;
    this.error = '';
    this.notify();
    try {
      await this.flush();
      const value = await this.reader.acceptInteractive(this.caseId, this.review.id, this.review.generation);
      this.job = value;
      this.review = { ...this.review, status: 'accepting', job_id: value.id };
      this.poll();
      this.notify();
      return value;
    } catch (reason) {
      this.error = reason instanceof Error ? reason.message : String(reason);
      this.working = false;
      this.notify();
    }
  }
  async cancel() {
    const id = this.job?.id ?? (this.review && ['predicting', 'accepting'].includes(this.review.status) ? this.review.job_id : undefined);
    try {
      if (id) {
        await this.reader.cancel(id);
        this.poll();
      } else if (this.working) this.cancelRequested = true;
    } catch (reason) {
      this.error = `Cancellation could not be requested. ${reason instanceof Error ? reason.message : reason}`;
      this.notify();
      if (this.working) this.poll();
    }
  }
  async discard() {
    if (this.working) return;
    try {
      await this.flush();
      if (this.review) await this.reader.discardInteractive(this.caseId, this.review.id, this.review.generation);
      this.install();
      this.notify();
    } catch (reason) {
      this.error = reason instanceof Error ? reason.message : String(reason);
      this.notify();
    }
  }
  private poll() {
    clearTimeout(this.pollTimer);
    if (!this.review || this.stopped) return;
    this.pollTimer = setTimeout(async () => {
      const seen = this.review!;
      try {
        const value = await this.reader.interactiveReview(this.caseId, seen.id);
        if (this.stopped) return;
        const job = value.job_id ? await this.reader.job(value.job_id) : this.job;
        // A save of this page's can be in the reply already, which would read as another tab's, or
        // have landed after it, which the reply would undo. The next read can tell.
        if (this.saving || this.review !== seen) {
          if (this.working) this.poll();
          return;
        }
        if (!this.takeOn(seen, value)) return;
        this.job = job;
        this.working = ['predicting', 'accepting'].includes(value.status);
        if (!this.working) {
          this.persisted = saved(value);
          this.error = value.error ?? '';
        }
        this.notify();
        if (this.working) this.poll();
      } catch (reason) {
        this.error = `Review status unavailable. ${reason instanceof Error ? reason.message : reason}`;
        this.notify();
        this.poll();
      }
    }, 900);
  }
  dispose() {
    clearTimeout(this.timer);
    clearTimeout(this.pollTimer);
    this.stopped = true;
    void this.flush().catch(() => {});
  }
}

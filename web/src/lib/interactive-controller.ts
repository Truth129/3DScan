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
/** Saves and runs move a generation, but a draft from an older build can hold a result at none. */
const inUse = (value: InteractiveReview) => value.generation > 0 || !!value.candidate || ['predicting', 'accepting'].includes(value.status);
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
  /**
   * Moves when a run or a Save starts and when a record is installed. A timer or a reply from
   * before that belongs to something that is over, and must leave what came after it alone.
   */
  private follow = 0;
  /** The last follow to have a job to read about, which a run or a Save has only once it answers. */
  private reading = 0;
  /** The last follow to have a poll, or a Cancel that ends in one, before the read after predict lands. */
  private polled = 0;
  /** What the end of a follow kept on screen for the save it is retrying, which ends with that save. */
  private retrying = '';
  private stopped = false;
  private cancelRequested = false;
  private initialRelationship = normalizeRelationship();
  private starting?: Promise<InteractiveReview>;
  /** The job of this page's own Save. App follows that job and opens what it saves. */
  private ownSave?: string;
  /** Results this draft has shown, for an Undo or Redo onto one of their steps. */
  private results: {
    prompts: InteractivePrompt[];
    options: InteractiveOptions;
    relationship: SegmentRelationship;
    candidate: NonNullable<InteractiveReview['candidate']>;
  }[] = [];

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
    if (!value || value.status === 'discarded' || (value.status === 'accepted' && !this.accepted)) return undefined;
    const count = this.appliedCount;
    if (
      value.candidate &&
      signature(value.options ?? initialOptions()) === signature(this.options) &&
      signature(value.prompts.slice(0, count)) === signature(this.prompts.slice(0, count)) &&
      this.prompts.length >= count
    )
      return value.candidate;
    // Undo onto an applied step shows that step's result while the draft save that brings it back
    // is on its way, as the server answers with the same result under a new name. Without this the
    // original mask showed in between. The server restores a result only for exactly its marks,
    // options and relationship, from steps it still holds, so an undo of marks it has, under
    // options it has, onto the end of a run it still has: nothing looser is shown. A removed mark
    // drops every run after it, and a record from a build without the run list is read as before.
    const length = this.prompts.length;
    if (
      !this.unsaved ||
      length >= value.prompts.length ||
      signature(this.prompts) !== signature(value.prompts.slice(0, length)) ||
      signature(this.options) !== signature(value.options ?? initialOptions()) ||
      (value.batches && !value.batches.some(batch => batch.end === length))
    )
      return undefined;
    const wanted = signature([this.prompts, this.options, this.relationship]);
    return [...this.results].reverse().find(item => signature([item.prompts, item.options, item.relationship]) === wanted)?.candidate;
  }
  /**
   * This page's Save has finished. Its result stays on screen, with the page busy, until App opens
   * the saved revision and replaces this controller. Dropping it at once showed the original mask,
   * with the saved marks as unapplied, for as long as that took.
   */
  get accepted() {
    const value = this.review;
    return value?.status === 'accepted' && !!value.job_id && this.ownSave === value.job_id;
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
  /** Marks with no result, on a page that has shown one for this draft. Run AI then reads as lost work unless the page says why. */
  get resultDiscarded() {
    return this.prompts.length > 0 && this.results.length > 0 && !this.candidate;
  }
  get dirty() {
    return this.unsaved || !!this.review?.dirty || !this.candidate || this.pendingCount > 0;
  }
  get hasDraftChanges() {
    return !!(this.prompts.length || this.review?.new_structure || this.candidate || this.relationshipDirty || this.unsaved);
  }
  get publicReview(): InteractiveReview | undefined {
    return this.review && this.review.status !== 'discarded' && (this.review.status !== 'accepted' || this.accepted)
      ? {
          ...this.review,
          status: this.accepted ? 'accepting' : this.review.status,
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
    const value = this.review;
    if (value?.candidate && !['accepted', 'discarded'].includes(value.status)) {
      const result = {
        prompts: value.prompts.slice(0, this.appliedCount),
        options: value.options ?? initialOptions(),
        relationship: normalizeRelationship(value.candidate.relationship ?? value.relationship),
        candidate: value.candidate,
      };
      const key = signature([result.prompts, result.options, result.relationship]);
      this.results = [...this.results.filter(item => signature([item.prompts, item.options, item.relationship]) !== key), result].slice(-50);
    }
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
    // A run, a Save or another list that has come since is newer than this list. Installing it would
    // end a follow over the very record that follow is on, and wipe any mark made since.
    const follow = this.follow;
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
      if (follow === this.follow) {
        this.install(latest);
        if (latest && ['predicting', 'accepting'].includes(latest.status)) {
          this.working = true;
          this.poll();
        }
      }
      this.loaded = true;
    } catch (reason) {
      if (follow === this.follow) this.error = reason instanceof Error ? reason.message : String(reason);
      this.loaded = true;
    }
    this.notify();
  }
  private install(value?: InteractiveReview) {
    // A Discard or a Reload can land during a follow, and what was being followed belonged to the
    // record this replaces. A poll left running would stop on a missing review with the spinner on,
    // and cancel() would reach the old job before the running one until the first poll.
    clearTimeout(this.pollTimer);
    this.follow++;
    this.working = false;
    this.job = undefined;
    if (value?.id !== this.review?.id) this.results = [];
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
  /** A step like any other, so Undo puts the mark back where it was in the list. */
  remove(id: string) {
    const prompts = this.prompts.filter(item => item.id !== id);
    if (prompts.length !== this.prompts.length) this.edit(prompts);
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
    // The server hands back a draft that is already open, and it can be someone else's or another tab's. A save
    // replaces every mark at the generation it holds, and cancels a running job, so as in takeOn()
    // this page may not hold that record while it has changes of its own to send. It stays draftless.
    if (inUse(value)) {
      if (this.unsaved) {
        this.conflict = true;
        throw new Error(
          "Someone else or another tab has a draft for this structure, so this page's changes were not saved. Reloading shows that draft and clears them.",
        );
      }
      this.adopt(value);
      return value;
    }
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
      let requested: Snapshot | undefined;
      try {
        if (!this.review) await this.createReview();
        while (this.unsaved && !this.conflict) {
          requested = snapshot(this.prompts, this.options, this.relationship);
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
          if (this.error === this.retrying) this.error = '';
          this.notify();
        }
      } catch (reason) {
        this.error = reason instanceof Error ? reason.message : String(reason);
        if (reason instanceof ApiError && reason.status === 409) this.conflict = true;
        else if (reason instanceof ApiError && reason.status === 400 && requested) this.withdraw(requested, reason.message);
        throw reason;
      } finally {
        this.saving = false;
        this.writing = undefined;
        this.notify();
      }
    })();
    return this.writing;
  }
  /**
   * Takes out the mark a refused save added, such as a drawing that covers no voxel centre. Every
   * later save would carry it and be refused too. Its steps leave the history as well, so Undo
   * cannot bring it back. Other marks in the same save stay and go out with the next one.
   */
  private withdraw(requested: Snapshot, reason: string) {
    const value = this.review;
    if (!value) return;
    const held = new Set(value.prompts.map(item => item.id)),
      added = requested.prompts.filter(item => !held.has(item.id)),
      unmarked = signature(
        snapshot(
          requested.prompts.filter(item => held.has(item.id)),
          requested.options,
          requested.relationship,
        ),
      );
    // Only a save that added nothing but marks was refused for one of them. A draft started with a
    // relationship holds it before this page's first save does, so the record counts as saved too.
    if (!added.length || (unmarked !== this.persisted && unmarked !== saved(value))) return;
    // The server names the first bad mark by its position in the request ("Lasso mark 2 covers ...").
    // A message without that names no one mark, so every added mark goes, as before.
    const number = Number(/(\d+) covers no voxel centre/.exec(reason)?.[1]),
      named = requested.prompts[number - 1],
      refused = new Set(named && !held.has(named.id) ? [named.id] : added.map(item => item.id)),
      keep = (prompts: InteractivePrompt[]) => prompts.filter(item => !refused.has(item.id));
    const history: Snapshot[] = [];
    let cursor = 0;
    this.history.forEach((item, index) => {
      const next = { ...item, prompts: keep(item.prompts) };
      if (!history.length || signature(next) !== signature(history[history.length - 1])) history.push(next);
      if (index === this.cursor) cursor = history.length - 1;
    });
    this.history = history;
    this.cursor = cursor;
    this.prompts = keep(this.prompts);
    // Marks made while the save was out are not part of the refusal, and still need saving.
    this.schedule();
  }
  async reload() {
    clearTimeout(this.timer);
    if (this.writing) await this.writing.catch(() => {});
    this.error = '';
    await this.load();
  }
  /** `polarityGuard` is Include only adds, for the step this run adds; AI assist only. */
  async apply(fullRefine = false, choice?: InteractiveRunChoice, polarityGuard = false) {
    if (this.working || this.conflict) return;
    if (this.method === 'direct' && fullRefine) {
      this.error = 'Whole-mask refinement is available in AI assist.';
      this.notify();
      return;
    }
    const follow = ++this.follow;
    this.working = true;
    this.job = undefined;
    this.cancelRequested = false;
    this.error = '';
    this.notify();
    try {
      await this.flush();
      if (fullRefine && !this.review) await this.createReview();
      // Either can have opened someone else's or another tab's draft here, whose marks a run would apply unseen.
      if (follow !== this.follow) {
        this.error = 'Someone else or another tab has already started this draft, so nothing was run. Check it here, then run again.';
        this.notify();
        return;
      }
      // A re-run has nothing pending by design, and saving may just have freed the draft.
      const replay = !!choice?.replay && !!this.review?.batches?.length;
      if (!this.review || (this.pendingCount === 0 && !fullRefine && !replay)) {
        this.working = false;
        this.notify();
        return;
      }
      const { id, generation } = this.review;
      // The server records the guard only on a new step, so a re-run's own steps keep theirs.
      const guard = polarityGuard && this.method !== 'direct';
      // One path sends no fifth argument at all, and an unguarded run no sixth, so they stay today's requests.
      const job = await (guard
        ? this.reader.predictInteractive(this.caseId, id, generation, fullRefine, choice, true)
        : choice
          ? this.reader.predictInteractive(this.caseId, id, generation, fullRefine, choice)
          : this.reader.predictInteractive(this.caseId, id, generation, fullRefine));
      // The record that was asked about has gone, and this job says nothing about what replaced it.
      if (follow !== this.follow) return;
      this.job = job;
      this.reading = follow;
      const seen: InteractiveReview = { ...this.review, status: 'predicting', job_id: job.id };
      this.review = seen;
      this.notify();
      if (this.cancelRequested) await this.reader.cancel(job.id);
      const value = await this.reader.interactiveReview(this.caseId, seen.id);
      // Cancel's poll can end this follow with this read still out, and a poll armed here would
      // belong to whatever started next, reading a job it never asked for.
      if (follow !== this.follow) return;
      // As in poll(): a save of this page's can be in the reply already, or have landed after it.
      if (this.saving || this.review !== seen) {
        if (this.working) this.poll();
        return;
      }
      if (!this.takeOn(seen, value)) return;
      this.notify();
      this.poll();
    } catch (reason) {
      if (follow !== this.follow) return;
      this.error = reason instanceof Error ? reason.message : String(reason);
      // A poll that Cancel started goes on without this read, and only it can say how the job ended.
      // It stops when the page does, so the page stays on the job until that poll has said.
      if (this.polled !== follow) this.working = false;
      this.notify();
      // `job` is set only once predict has answered, and a save that failed this way set `conflict`.
      if (reason instanceof ApiError && reason.status === 409 && !this.job && !this.conflict && this.review) await this.catchUp(this.review);
    }
  }
  /**
   * A 409 from predict can mean this page is behind with no newer generation to show for it: a Save
   * moves none, and a draft from an older build can hold a lock published at this one. Only
   * `review` moves, because install() and adopt() reset the marks and their history. A newer
   * generation holds another tab's work, and this page's next save would overwrite it if it
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
   * nothing in it changed, as every run's does at enqueue. Both sides are the server's own records,
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
    const follow = ++this.follow;
    this.working = true;
    this.error = '';
    this.notify();
    try {
      await this.flush();
      const value = await this.reader.acceptInteractive(this.caseId, this.review.id, this.review.generation);
      // As in apply(), but the server has the Save whatever became of this record, so App must hear.
      if (follow !== this.follow) return value;
      this.job = value;
      this.ownSave = value.id;
      this.review = { ...this.review, status: 'accepting', job_id: value.id };
      this.poll();
      this.notify();
      return value;
    } catch (reason) {
      if (follow !== this.follow) return;
      this.error = reason instanceof Error ? reason.message : String(reason);
      this.working = false;
      this.notify();
    }
  }
  async cancel() {
    const id = this.job?.id ?? (this.review && ['predicting', 'accepting'].includes(this.review.status) ? this.review.job_id : undefined),
      follow = this.follow;
    try {
      if (id) {
        // Either way out of this ends in restart(), so a failed read in the meantime must not free the page.
        if (this.reading === follow) this.polled = follow;
        await this.reader.cancel(id);
        this.restart();
      } else if (this.working) this.cancelRequested = true;
    } catch (reason) {
      if (follow !== this.follow) return;
      this.error = `Cancellation could not be requested. ${reason instanceof Error ? reason.message : reason}`;
      this.notify();
      this.restart();
    }
  }
  /**
   * Cancel's reply can land after the follow has ended, or before a run or a Save has a job to
   * read about. A read then would find no job and free the page under that request.
   */
  private restart() {
    if (this.working && this.reading === this.follow) this.poll();
  }
  async discard() {
    if (this.working) return;
    // A page that refused another tab's draft has only its own marks to drop, and that draft is
    // not this page's to close.
    if (this.conflict && !this.review) return this.reload();
    try {
      await this.flush();
      if (this.review) await this.reader.discardInteractive(this.caseId, this.review.id, this.review.generation);
      this.install();
      this.notify();
    } catch (reason) {
      this.error = reason instanceof Error ? reason.message : String(reason);
      // Refused for a newer generation, which only a reload can bring, so asking again cannot pass.
      if (reason instanceof ApiError && reason.status === 409) this.conflict = true;
      this.notify();
    }
  }
  private poll() {
    clearTimeout(this.pollTimer);
    if (!this.review || this.stopped) return;
    // A timer or a reply can outlast what it was following, and the page may be idle or on a new run.
    const follow = this.follow,
      over = () => this.stopped || !this.working || follow !== this.follow;
    this.reading = follow;
    this.polled = follow;
    this.pollTimer = setTimeout(async () => {
      if (over()) return;
      const seen = this.review!;
      try {
        const value = await this.reader.interactiveReview(this.caseId, seen.id);
        if (over()) return;
        // A failed job read says nothing about the record, so it waits until the record is judged.
        let job = this.job,
          failed: { reason: unknown } | undefined;
        if (value.job_id)
          try {
            job = await this.reader.job(value.job_id);
          } catch (reason) {
            failed = { reason };
          }
        if (over()) return;
        // A save of this page's can be in the reply already, which would read as another tab's, or
        // have landed after it, which the reply would undo. The next read can tell.
        if (this.saving || this.review !== seen) {
          this.poll();
          return;
        }
        if (!this.takeOn(seen, value)) return;
        if (failed) throw failed.reason;
        this.job = job;
        this.working = ['predicting', 'accepting'].includes(value.status) || this.accepted;
        if (!this.working) {
          this.persisted = saved(value);
          // Marks still unsaved with a message up mean a save failed during the follow. It keeps the
          // message, and only this can bring it back. Without a message they are marks the server
          // rewrote, which need no save. Reload saved work sits inside a conflict's message.
          const retry = this.unsaved && !!this.error;
          this.error = value.error ?? (this.conflict || retry ? this.error : '');
          if (retry) {
            this.retrying = value.error ? '' : this.error;
            this.schedule();
          }
        }
        this.notify();
        if (this.working && !this.accepted) this.poll();
      } catch (reason) {
        if (over()) return;
        this.error = `Review status unavailable. ${reason instanceof Error ? reason.message : reason}`;
        // Asking again cannot bring back a review or job that has gone.
        if (reason instanceof ApiError && reason.status === 404) this.working = false;
        this.notify();
        if (this.working) this.poll();
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

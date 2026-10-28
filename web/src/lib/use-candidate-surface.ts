import { onScopeDispose, ref, shallowRef, watch } from 'vue';
import { api } from './api';
import { reviewCandidateRevision } from './interactive-review';
import type { InteractiveReview, Revision } from '../types';

const CACHED_PREVIEWS = 8;

interface CandidateSurfaceSource {
  caseId: () => string | undefined;
  revisionId: () => string | undefined;
  review: () => InteractiveReview | undefined;
}

/** Load the 3D surface preview of the current correction candidate, newest request wins. */
export function useCandidateSurface(source: CandidateSurfaceSource) {
  const revision = shallowRef<Revision>(),
    session = ref(''),
    loading = ref(false),
    error = ref('');
  let generation = 0;
  const cache = new Map<string, Revision>();

  async function load() {
    const request = ++generation,
      caseId = source.caseId(),
      review = source.review(),
      alias = reviewCandidateRevision(review);
    if (!caseId || !review || !alias || !review.candidate?.preview_3d_ready) {
      loading.value = false;
      error.value = '';
      return;
    }
    loading.value = true;
    error.value = '';
    try {
      const next = cache.get(alias) ?? (await api.revision(caseId, alias));
      if (request !== generation || caseId !== source.caseId() || alias !== reviewCandidateRevision(source.review())) return;
      if (!next.model_manifest_url) throw new Error('The workstation did not return a 3D candidate preview.');
      cache.delete(alias);
      cache.set(alias, next);
      while (cache.size > CACHED_PREVIEWS) cache.delete(cache.keys().next().value!);
      revision.value = next;
      session.value = review.id;
    } catch (reason) {
      if (request === generation) error.value = reason instanceof Error ? reason.message : 'The 3D candidate preview could not be loaded.';
    } finally {
      if (request === generation) loading.value = false;
    }
  }
  function clear() {
    generation++;
    cache.clear();
  }

  watch(
    () => [source.caseId(), source.revisionId()],
    () => {
      clear();
      revision.value = undefined;
      session.value = '';
      loading.value = false;
      error.value = '';
    },
  );
  watch(
    () => [source.review()?.id, reviewCandidateRevision(source.review())],
    () => {
      void load();
    },
  );
  onScopeDispose(clear);
  return { revision, session, loading, error, load };
}

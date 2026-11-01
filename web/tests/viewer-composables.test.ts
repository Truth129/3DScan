import { afterEach, describe, expect, it, vi } from 'vitest';
import { effectScope, nextTick, ref, shallowRef } from 'vue';
import { api, type CinematicReference } from '../src/lib/api';
import { freshVolumeSettings } from '../src/lib/volume-presets';
import { useCinematicReference } from '../src/lib/use-cinematic-reference';
import { useExpandedViewport } from '../src/lib/use-expanded-viewport';
import { useFoldingControls, useReviewFoldingControls } from '../src/lib/use-folding-controls';
import { useStructureDisplay } from '../src/lib/use-structure-display';
import type { Structure } from '../src/types';

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('floating controls during review', () => {
  function controls(saved: string | null, reviewing = false) {
    const stored = new Map(saved === null ? [] : [['key', saved]]);
    vi.stubGlobal('localStorage', { getItem: (key: string) => stored.get(key) ?? null, setItem: (key: string, value: string) => stored.set(key, value) });
    const review = ref(reviewing);
    const folding = effectScope().run(() =>
      useReviewFoldingControls(
        useFoldingControls('key', () => true),
        () => review.value,
      ),
    )!;
    return { ...folding, review, stored };
  }

  it('starts every review folded over a saved open preference, and restores that preference after', async () => {
    const { open, setOpen, review } = controls('true');
    expect(open.value).toBe(true);
    review.value = true;
    await nextTick();
    expect(open.value).toBe(false);
    setOpen(true);
    expect(open.value).toBe(true);
    review.value = false;
    await nextTick();
    expect(open.value).toBe(true);
    review.value = true;
    await nextTick();
    expect(open.value).toBe(false);
  });

  it('starts folded when the viewer mounts during a review', () => {
    expect(controls(null, true).open.value).toBe(false);
  });

  it('never saves a choice made during review', async () => {
    const { open, setOpen, review, stored } = controls('false');
    review.value = true;
    await nextTick();
    setOpen(true);
    expect(stored.get('key')).toBe('false');
    setOpen(false);
    review.value = false;
    await nextTick();
    expect(open.value).toBe(false);
    setOpen(true);
    expect(stored.get('key')).toBe('true');
  });

  it('lets an error reveal the controls in either mode without saving a choice', async () => {
    const { open, review, stored } = controls('false');
    open.value = true;
    expect(open.value).toBe(true);
    open.value = false;
    review.value = true;
    await nextTick();
    open.value = true;
    expect(open.value).toBe(true);
    expect(stored.get('key')).toBe('false');
  });
});

describe('cinematic measurement ownership', () => {
  function fixture() {
    const scope = effectScope(),
      source = ref({ caseId: 'case-a', revisionId: 'revision-a' }),
      settings = shallowRef(freshVolumeSettings({ preset: 'CT-Cinematic-Heart' })),
      changed = vi.fn();
    const reference = scope.run(() => useCinematicReference({ source: () => source.value, settings, available: ref(true), changed }))!;
    return { scope, source, settings, changed, reference };
  }

  it('rejects late measurements after manual edits, source replacement and disposal', async () => {
    const pending: ((value: CinematicReference) => void)[] = [],
      signals: AbortSignal[] = [];
    vi.spyOn(api, 'cinematicReference').mockImplementation(
      (_case, _revision, signal) =>
        new Promise(resolve => {
          pending.push(resolve);
          signals.push(signal!);
        }),
    );
    const { scope, source, settings, reference } = fixture();
    const result = { myocardium_hu: 95, blood_hu: 430, blood_source: 'aorta' } as CinematicReference;
    expect(reference.claimAutomatic()).toBe(true);
    const first = reference.measure(true);
    reference.setSettings(freshVolumeSettings({ ...settings.value, cinematic: { ...settings.value.cinematic, myocardiumHU: 90, bloodHU: 410 } }));
    pending[0](result);
    await first;
    expect(signals[0].aborted).toBe(true);
    expect(settings.value.cinematic.myocardiumHU).toBe(90);
    expect(reference.claimAutomatic()).toBe(false);

    const second = reference.measure();
    source.value = { caseId: 'case-b', revisionId: 'revision-b' };
    reference.selectSource();
    pending[1](result);
    await second;
    expect(reference.claimAutomatic()).toBe(true);
    expect(settings.value.cinematic.bloodHU).toBe(410);
    const third = reference.measure();
    scope.stop();
    pending[2](result);
    await third;
    expect(signals.every(signal => signal.aborted)).toBe(true);
    expect(reference.loading.value).toBe(false);
    expect(settings.value.cinematic.bloodHU).toBe(410);
  });

  it('applies only valid current measurements and preserves settings on failure', async () => {
    const response = vi.spyOn(api, 'cinematicReference');
    const { scope, settings, changed, reference } = fixture();
    response.mockResolvedValueOnce({ myocardium_hu: 92, blood_hu: 420, blood_source: 'aorta' } as CinematicReference);
    await reference.measure();
    expect(settings.value.cinematic).toMatchObject({ myocardiumHU: 92, bloodHU: 420 });
    expect(changed).toHaveBeenCalledTimes(1);
    response.mockResolvedValueOnce({ myocardium_hu: NaN, blood_hu: 420 } as CinematicReference);
    await reference.measure();
    expect(reference.error.value).toBe(true);
    expect(settings.value.cinematic.myocardiumHU).toBe(92);
    expect(changed).toHaveBeenCalledTimes(1);
    scope.stop();
  });
});

describe('expanded viewport lifecycle', () => {
  it('restores prior inert state and focus without remounting; cancelled entry cannot lock the page', async () => {
    const listeners = new Map<string, EventListener>();
    class Element {
      inert = false;
      isConnected = true;
      parentElement?: Element;
      children: Element[] = [];
      focus = vi.fn(() => {
        document.activeElement = this as unknown as HTMLElement;
      });
    }
    const body = new Element(),
      viewport = new Element(),
      button = new Element(),
      sibling = new Element(),
      previous = new Element();
    body.children = [viewport, sibling];
    viewport.parentElement = sibling.parentElement = body;
    sibling.inert = true;
    const document = {
      body,
      activeElement: previous as unknown as HTMLElement,
      addEventListener: (name: string, listener: EventListener) => listeners.set(name, listener),
      removeEventListener: (name: string) => listeners.delete(name),
      querySelector: () => null,
    };
    vi.stubGlobal('document', document);
    vi.stubGlobal('HTMLElement', Element);
    const scope = effectScope();
    const expansion = scope.run(() => useExpandedViewport(shallowRef(viewport as unknown as HTMLElement), shallowRef(button as unknown as HTMLButtonElement)))!;
    await expansion.setExpanded(true);
    expect(button.focus).toHaveBeenCalledTimes(1);
    expect(listeners.has('keydown')).toBe(true);
    const escape = { key: 'Escape', isComposing: true, preventDefault: vi.fn(), stopPropagation: vi.fn() };
    listeners.get('keydown')!(escape as unknown as Event);
    expect(expansion.expanded.value).toBe(true);
    escape.isComposing = false;
    listeners.get('keydown')!(escape as unknown as Event);
    await nextTick();
    expect(expansion.expanded.value).toBe(false);
    expect(escape.preventDefault).toHaveBeenCalledTimes(1);
    expect(escape.stopPropagation).toHaveBeenCalledTimes(1);
    expect(sibling.inert).toBe(true);
    expect(previous.focus).toHaveBeenCalledTimes(1);
    const entering = expansion.setExpanded(true);
    await expansion.setExpanded(false, false);
    await entering;
    expect(listeners.size).toBe(0);
    await expansion.setExpanded(true);
    scope.stop();
    await nextTick();
    expect(listeners.size).toBe(0);
    expect(sibling.inert).toBe(true);
    expect(previous.focus).toHaveBeenCalledTimes(1);
  });

  async function expandedPage() {
    const listeners = new Map<string, EventListener>();
    class Element {
      inert = false;
      tagName = 'DIV';
      parentElement?: Element;
      children: Element[] = [];
      focus = vi.fn();
      contains = () => false;
      querySelectorAll = () => [] as Element[];
    }
    const body = new Element(),
      viewport = new Element(),
      button = new Element(),
      header = new Element(),
      held = new Element(),
      state = { modal: false };
    held.tagName = 'DIALOG';
    body.children = [viewport, header, held];
    viewport.parentElement = header.parentElement = held.parentElement = body;
    Object.assign(button, { tabIndex: 0, matches: () => false, closest: () => null, getClientRects: () => [{}] });
    viewport.querySelectorAll = () => [button];
    vi.stubGlobal('document', {
      body,
      activeElement: held,
      addEventListener: (name: string, listener: EventListener) => listeners.set(name, listener),
      removeEventListener: (name: string) => listeners.delete(name),
      querySelector: (selector: string) => (selector === 'dialog:modal' && state.modal ? held : null),
    });
    vi.stubGlobal('HTMLElement', Element);
    const expansion = effectScope().run(() =>
      useExpandedViewport(shallowRef(viewport as unknown as HTMLElement), shallowRef(button as unknown as HTMLButtonElement)),
    )!;
    await expansion.setExpanded(true);
    const press = (key: string) => {
      const event = { key, isComposing: false, shiftKey: false, preventDefault: vi.fn(), stopPropagation: vi.fn() };
      listeners.get('keydown')!(event as unknown as Event);
      return event;
    };
    return { ...expansion, button, header, held, state, press };
  }
  it('leaves a held dialog usable, since showModal cannot lift inert from it', async () => {
    const { header, held } = await expandedPage();
    expect(header.inert).toBe(true);
    expect(held.inert).toBe(false);
  });
  it('leaves Tab and Escape to a modal dialog, and takes them back when it closes', async () => {
    const page = await expandedPage();
    page.button.focus.mockClear();
    page.state.modal = true;
    expect(page.press('Tab').preventDefault).not.toHaveBeenCalled();
    expect(page.button.focus).not.toHaveBeenCalled();
    expect(page.press('Escape').preventDefault).not.toHaveBeenCalled();
    await nextTick();
    expect(page.expanded.value).toBe(true);
    page.state.modal = false;
    expect(page.press('Tab').preventDefault).toHaveBeenCalledOnce();
    expect(page.button.focus).toHaveBeenCalledOnce();
    page.press('Escape');
    await nextTick();
    expect(page.expanded.value).toBe(false);
  });
});

describe('shared anatomy presentation', () => {
  it('uses replacement state and full structure groups while keeping selection and opacity independent', () => {
    const structures = ref([
      { id: 'a', group: 'organs' },
      { id: 'b', group: 'organs' },
    ] as Structure[]);
    const display = ref({ a: { visible: true, opacity: 0.4 }, b: { visible: false, opacity: 0.7 } }),
      selected = ref('a');
    const actions = useStructureDisplay(() => structures.value, display, selected);
    actions.solo('b');
    expect(selected.value).toBe('b');
    actions.groupVisibility('organs', true);
    expect(Object.values(display.value).every(state => state.visible)).toBe(true);
    actions.allSegmentOpacity(NaN);
    expect(display.value.a.opacity).toBe(0.4);
    actions.allSegmentOpacity(2);
    expect(Object.values(display.value).map(state => state.opacity)).toEqual([1, 1]);
    const replacement = { a: { visible: true, opacity: 0.5 }, b: { visible: true, opacity: 0.5 } };
    display.value = replacement;
    actions.hideAll();
    actions.visibility('missing', true);
    expect(Object.keys(display.value)).toEqual(['a', 'b']);
    expect(Object.values(display.value).every(state => !state.visible)).toBe(true);
    actions.showAll();
    expect(Object.values(display.value).every(state => state.visible)).toBe(true);
  });
});

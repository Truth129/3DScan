import { afterEach, describe, expect, it, vi } from 'vitest';
import CaseLibrary from '../src/components/CaseLibrary.vue';
import { api, ApiError } from '../src/lib/api';
import type { Case } from '../src/types';
import { click, mount, settled, type MountedElement } from './mounted';

const NOTICE = 'Research use only · not for diagnostic use';
const scan = (id: string, label: string, values: Partial<Case> = {}): Case => ({
  id,
  label,
  created_at: '2026-11-01T12:00:00Z',
  status: 'ready',
  active_revision_id: null,
  series: [],
  ...values,
});
const liver = scan('liver', 'Liver'),
  chest = scan('chest', 'Chest');

function library(values: Record<string, unknown>) {
  const view = mount(CaseLibrary, () => ({ cases: [liver, chest], closable: true, importing: false, mrEnabled: false, notice: NOTICE, ...values }));
  return Object.assign(view, {
    search: () => view.labelled('Search cases')!,
    nameField: () => view.labelled('Case name'),
    rows: () => view.all(element => element.className.split(' ').includes('case-row')),
    section: () => view.labelled('Library')!,
    form: () => view.all(element => element.tagName === 'FORM')[0],
  });
}
function fill(element: MountedElement, value: string) {
  (element as unknown as HTMLInputElement).value = value;
  element.dispatchEvent(new Event('input'));
}
const press = (element: MountedElement, key: string) => element.dispatchEvent(Object.assign(new Event('keydown', { cancelable: true }), { key }));
const submit = (element: MountedElement) => element.dispatchEvent(new Event('submit', { cancelable: true }));

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('finding a case', () => {
  it('opens a row', () => {
    const open = vi.fn(),
      view = library({ onOpen: open });
    click(view.rows()[1].children[0]);
    expect(open).toHaveBeenCalledExactlyOnceWith('chest');
  });
  it('narrows the list as you type, and says when nothing matches', async () => {
    const view = library({});
    fill(view.search(), 'che');
    await settled();
    expect(view.rows().map(row => row.getAttribute('data-case-id'))).toEqual(['chest']);
    fill(view.search(), 'knee');
    await settled();
    expect(view.text()).toContain('No cases match.');
    expect(view.text()).not.toContain('No cases yet');
  });
  it('opens the first match on Enter', async () => {
    const open = vi.fn(),
      view = library({ onOpen: open });
    fill(view.search(), 'che');
    await settled();
    press(view.search(), 'Enter');
    expect(open).toHaveBeenCalledExactlyOnceWith('chest');
  });
  it('marks the open case', () => {
    const view = library({ openId: 'chest' });
    expect(view.rows().map(row => row.className.includes('active'))).toEqual([false, true]);
  });
  it('gives each row its date, status and whether it is synthetic', () => {
    const when = new Date('2026-11-02T12:00:00Z').toLocaleDateString();
    const view = library({ cases: [scan('demo', 'Demo', { opened_at: '2026-11-02T12:00:00Z', synthetic: true })] });
    expect(view.text()).toContain(`${when} · Ready · Synthetic`);
  });
});

describe('where the cursor goes', () => {
  it('starts in the search field', () => {
    const view = library({});
    expect(document.activeElement).toBe(view.search());
  });
  it('leaves the search field alone on a touch screen, where a keyboard would cover the list', () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true }));
    const before = document.activeElement;
    library({});
    expect(document.activeElement).toBe(before);
  });
  it('moves to the name when a rename opens', async () => {
    const view = library({});
    click(view.button('Rename'));
    await settled();
    expect(document.activeElement).toBe(view.nameField());
  });
});

describe('Escape', () => {
  it('cancels a rename first, then closes', async () => {
    const close = vi.fn(),
      view = library({ onClose: close });
    click(view.button('Rename'));
    await settled();
    press(view.section(), 'Escape');
    await settled();
    expect(view.form()).toBeUndefined();
    expect(close).not.toHaveBeenCalled();
    press(view.section(), 'Escape');
    expect(close).toHaveBeenCalledOnce();
  });
  it('leaves a Library that is all there is', () => {
    const close = vi.fn(),
      view = library({ closable: false, onClose: close });
    press(view.section(), 'Escape');
    expect(close).not.toHaveBeenCalled();
    expect(view.labelled('Close Library')).toBeUndefined();
  });
});

describe('renaming', () => {
  it('renames a case and shows a refusal', async () => {
    const refusal = 'This case is still importing. Rename it when the import finishes.';
    const next = { ...liver, label: 'Liver, week 12' };
    const rename = vi.spyOn(api, 'rename').mockRejectedValueOnce(new ApiError(409, refusal)).mockResolvedValueOnce(next);
    const renamed = vi.fn(),
      view = library({ onRenamed: renamed });
    click(view.button('Rename'));
    await settled();
    expect(view.text()).toContain('Do not put patient identifiers in a case name.');
    expect(view.nameField()?.getAttribute('maxlength')).toBe('128');
    fill(view.nameField()!, '  Liver, week 12  ');
    await settled();
    submit(view.form());
    await settled();
    expect(rename).toHaveBeenLastCalledWith('liver', 'Liver, week 12');
    expect(view.all(element => element.getAttribute('role') === 'alert')[0].textContent).toBe(refusal);
    expect(renamed).not.toHaveBeenCalled();
    submit(view.form());
    await settled();
    expect(renamed).toHaveBeenCalledExactlyOnceWith(next);
    expect(view.form()).toBeUndefined();
    expect(view.text()).not.toContain(refusal);
  });
  it('asks for a name', async () => {
    const rename = vi.spyOn(api, 'rename'),
      view = library({});
    click(view.button('Rename'));
    await settled();
    fill(view.nameField()!, '   ');
    await settled();
    expect((view.button('Save') as unknown as HTMLButtonElement).disabled).toBe(true);
    submit(view.form());
    await settled();
    expect(rename).not.toHaveBeenCalled();
  });
  it('offers nothing on a case that is still importing', () => {
    const view = library({ cases: [scan('new', 'New', { status: 'importing' })] });
    expect(view.button('Rename')).toBeUndefined();
  });
});

describe('an empty library', () => {
  it('shows the import buttons and the research notice on an empty library', () => {
    const importScan = vi.fn(),
      demo = vi.fn(),
      view = library({ cases: [], onImportScan: importScan, onDemo: demo });
    expect(view.text()).toContain('No cases yet');
    expect(view.text()).toContain('Explore CT scans with linked slices and 3D views.');
    expect(view.text()).toContain(NOTICE);
    click(view.button('Import your first scan →'));
    click(view.button('Explore synthetic demo'));
    expect(importScan).toHaveBeenCalledOnce();
    expect(demo).toHaveBeenCalledOnce();
  });
  it('names MR only where it is offered', () => {
    expect(library({ cases: [], mrEnabled: true }).text()).toContain('Explore CT and static 3D MR scans');
  });
  it('holds the demo back while an import runs', () => {
    const view = library({ cases: [], importing: true });
    expect((view.button('Explore synthetic demo') as unknown as HTMLButtonElement).disabled).toBe(true);
  });
});

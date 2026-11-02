import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ref } from 'vue';
import CaseLibrary from '../src/components/CaseLibrary.vue';
import { api, ApiError } from '../src/lib/api';
import type { Case } from '../src/types';
import { click, deferred, mount, settled, type MountedElement } from './mounted';

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
  const view = mount(CaseLibrary, () => ({ cases: [liver, chest], stamp: 0, closable: true, importing: false, mrEnabled: false, notice: NOTICE, ...values }));
  return Object.assign(view, {
    search: () => view.labelled('Search cases')!,
    nameField: () => view.labelled('Case name'),
    rows: () => view.all(element => element.className.split(' ').includes('case-row')),
    section: () => view.labelled('Library')!,
    form: () => view.all(element => element.tagName === 'FORM')[0],
    tab: (name: string) => view.all(element => element.tagName === 'BUTTON' && element.textContent.trim().startsWith(name))[0],
  });
}
function fill(element: MountedElement, value: string) {
  (element as unknown as HTMLInputElement).value = value;
  element.dispatchEvent(new Event('input'));
}
const press = (element: MountedElement, key: string) => element.dispatchEvent(Object.assign(new Event('keydown', { cancelable: true }), { key }));
const submit = (element: MountedElement) => element.dispatchEvent(new Event('submit', { cancelable: true }));
const pressNumber = (element: MountedElement | undefined, detail: number) => element!.dispatchEvent(Object.assign(new Event('click'), { detail }));

beforeEach(() => {
  vi.spyOn(api, 'trashed').mockImplementation(async () => []);
});
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

describe('moving a case to Trash', () => {
  it('ignores the second press of a double-click', () => {
    const trash = vi.fn(),
      view = library({ onTrash: trash });
    pressNumber(view.button('Move to Trash'), 1);
    pressNumber(view.button('Move to Trash'), 2);
    expect(trash).toHaveBeenCalledExactlyOnceWith('liver');
  });
  it('returns the cursor to search, even from the press it ignores', () => {
    const view = library({}),
      button = view.button('Move to Trash');
    button!.focus();
    pressNumber(button, 2);
    expect(document.activeElement).toBe(view.search());
  });
  it('takes a press from the keyboard, which counts no clicks', () => {
    const trash = vi.fn(),
      view = library({ onTrash: trash });
    pressNumber(view.button('Move to Trash'), 0);
    expect(trash).toHaveBeenCalledExactlyOnceWith('liver');
  });
  it('offers it on a case that is still importing', () => {
    const view = library({ cases: [scan('new', 'New', { status: 'importing' })] });
    expect(view.button('Move to Trash')).toBeDefined();
  });
});

describe('Trash', () => {
  const old = scan('old', 'Old', { trashed_at: '2026-11-02T12:00:00Z' });

  it('says Trash still uses disk space and offers only Restore', async () => {
    vi.mocked(api.trashed).mockResolvedValue([old]);
    const restore = vi.fn(),
      view = library({ onRestore: restore });
    await settled();
    expect(view.tab('Trash').textContent.trim()).toBe('Trash1');
    click(view.tab('Trash'));
    await settled();
    expect(view.text()).toContain('Cases in Trash still use disk space. Deleting them for good is not available yet.');
    const when = new Date(old.trashed_at!).toLocaleDateString();
    expect(view.rows()[0].textContent).toContain(`Old${when} · Ready`);
    expect(
      view
        .rows()[0]
        .descendants()
        .filter(element => element.tagName === 'BUTTON')
        .map(button => button.textContent),
    ).toEqual(['Restore']);
    expect(view.text()).not.toMatch(/delete forever|empty trash/i);
    click(view.button('Restore'));
    expect(restore).toHaveBeenCalledExactlyOnceWith('old');
    expect(document.activeElement).toBe(view.search());
  });
  it('says so when it is empty, and still says what Trash does', async () => {
    const view = library({});
    await settled();
    click(view.tab('Trash'));
    await settled();
    expect(view.text()).toContain('Trash is empty.');
    expect(view.text()).toContain('still use disk space');
    expect(view.text()).toContain(NOTICE);
  });
  it('searches Trash, where Enter opens nothing', async () => {
    vi.mocked(api.trashed).mockResolvedValue([old]);
    const open = vi.fn(),
      view = library({ onOpen: open });
    await settled();
    click(view.tab('Trash'));
    await settled();
    fill(view.search(), 'knee');
    await settled();
    expect(view.text()).toContain('No cases match.');
    fill(view.search(), 'old');
    await settled();
    press(view.search(), 'Enter');
    expect(view.rows()).toHaveLength(1);
    expect(open).not.toHaveBeenCalled();
  });
  it('reads Trash again on a new stamp, and only the newest answer counts', async () => {
    const stamp = ref(0),
      slow = deferred<Case[]>(),
      view = library({
        get stamp() {
          return stamp.value;
        },
      });
    await settled();
    click(view.tab('Trash'));
    vi.mocked(api.trashed).mockReturnValueOnce(slow.promise).mockResolvedValueOnce([old]);
    stamp.value = 1;
    await settled();
    stamp.value = 2;
    await settled();
    slow.resolve([]);
    await settled();
    expect(view.rows()).toHaveLength(1);
  });
  it('keeps the last list when a read fails', async () => {
    vi.mocked(api.trashed).mockResolvedValueOnce([old]);
    const stamp = ref(0),
      view = library({
        get stamp() {
          return stamp.value;
        },
      });
    await settled();
    click(view.tab('Trash'));
    vi.mocked(api.trashed).mockRejectedValueOnce(new Error('No answer'));
    stamp.value = 1;
    await settled();
    expect(view.rows()).toHaveLength(1);
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
  it('says where the cases went when Trash holds every one', async () => {
    vi.mocked(api.trashed).mockResolvedValue([scan('old', 'Old')]);
    const view = library({ cases: [] });
    await settled();
    expect(view.text()).toContain('All your cases are in Trash');
    expect(view.text()).not.toContain('No cases yet');
    expect(view.button('Explore synthetic demo')).toBeDefined();
  });
  it('names MR only where it is offered', () => {
    expect(library({ cases: [], mrEnabled: true }).text()).toContain('Explore CT and static 3D MR scans');
  });
  it('holds the demo back while an import runs', () => {
    const view = library({ cases: [], importing: true });
    expect((view.button('Explore synthetic demo') as unknown as HTMLButtonElement).disabled).toBe(true);
  });
});

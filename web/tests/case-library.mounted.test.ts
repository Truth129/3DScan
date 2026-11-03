import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ref } from 'vue';
import CaseLibrary from '../src/components/CaseLibrary.vue';
import { api, ApiError } from '../src/lib/api';
import { session } from '../src/lib/session';
import type { Case, StorageReport } from '../src/types';
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
const report: StorageReport = {
  cases: [
    { id: 'liver', label: 'Liver', owner: 'Ada', created_at: '2026-11-01T12:00:00Z', trashed_at: null, size: 7 * 1024 ** 2 },
    { id: 'old', label: 'Old', owner: null, created_at: '2026-11-01T12:00:00Z', trashed_at: '2026-11-02T12:00:00Z', size: 1024 },
    { label: null, owner: 'Grace', created_at: '2026-10-01T12:00:00Z', size: 3 * 1024 ** 2 },
  ],
  totals: { cases: 10 * 1024 ** 2, trash: 1024, shared_caches: 20 * 1024 ** 2, other: 30 * 1024 ** 2, rebuildable: 15 * 1024 ** 2 },
  disk: { total: 100 * 1024 ** 2, free: 40 * 1024 ** 2 },
};

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
  vi.spyOn(api, 'sizes').mockImplementation(async () => ({ sizes: {} }));
});
afterEach(() => {
  session.value = undefined;
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
  it('gives each row its date, status, size and whether it is synthetic', async () => {
    const when = new Date('2026-11-02T12:00:00Z').toLocaleDateString();
    const view = library({ cases: [scan('demo', 'Demo', { opened_at: '2026-11-02T12:00:00Z', synthetic: true })] });
    await settled();
    expect(view.text()).toContain(`Opened ${when} · Ready · — · Synthetic`);
  });
  it('says Added on a case nobody has opened', async () => {
    const when = new Date(liver.created_at).toLocaleDateString();
    const view = library({});
    await settled();
    expect(view.rows()[0].textContent).toContain(`Added ${when} · Ready`);
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
  it('still closes with the focus on the page, unless a dialog owns the key', () => {
    const stray = () =>
      document.dispatchEvent(Object.defineProperty(Object.assign(new Event('keydown'), { key: 'Escape' }), 'target', { value: document.body }));
    const close = vi.fn(),
      view = library({ onClose: close });
    stray();
    expect(close).toHaveBeenCalledOnce();
    vi.spyOn(document, 'querySelector').mockReturnValueOnce(view.root);
    stray();
    expect(close, 'the dialog answers this one').toHaveBeenCalledOnce();
    view.unmount();
    stray();
    expect(close, 'nothing listens once the Library has gone').toHaveBeenCalledOnce();
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
  it('returns the cursor to the name after a refusal, so Esc and Enter still work', async () => {
    vi.spyOn(api, 'rename').mockRejectedValue(new ApiError(409, 'Refused'));
    const view = library({});
    click(view.button('Rename'));
    await settled();
    view.button('Save')!.focus();
    submit(view.form());
    await settled();
    expect(document.activeElement).toBe(view.nameField());
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

  it('says what Delete forever does, with Restore first', async () => {
    vi.mocked(api.trashed).mockResolvedValue([old]);
    const restore = vi.fn(),
      view = library({ onRestore: restore });
    await settled();
    expect(view.tab('Trash').textContent.trim()).toBe('Trash1');
    click(view.tab('Trash'));
    await settled();
    expect(view.text()).toContain('Cases in Trash still use disk space. Delete forever frees it and cannot be undone.');
    const when = new Date(old.trashed_at!).toLocaleDateString();
    expect(view.rows()[0].textContent).toContain(`OldIn Trash since ${when} · Ready`);
    expect(
      view
        .rows()[0]
        .descendants()
        .filter(element => element.tagName === 'BUTTON')
        .map(button => button.textContent),
    ).toEqual(['Restore', 'Delete forever']);
    expect(view.button('Empty Trash')).toBeDefined();
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
    fill(view.search(), 'l');
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

describe('Delete forever', () => {
  const old = scan('old', 'Old', { trashed_at: '2026-11-02T12:00:00Z' }),
    PASSWORD = `test-${crypto.randomUUID()}`;
  const signedIn = () => (session.value = { mode: 'password', setup_required: false, user: { id: 'u', username: 'ada', display_name: 'Ada', role: 'admin' } });
  /** The first read only, so the read after the dialog closes finds Trash as the server would leave it. */
  async function trashTab(cases: Case[], values: Record<string, unknown> = {}) {
    vi.mocked(api.trashed).mockResolvedValueOnce(cases);
    const view = library(values);
    await settled();
    click(view.tab('Trash'));
    await settled();
    return Object.assign(view, {
      dialog: () => view.all(element => element.tagName === 'DIALOG')[0],
      rowDelete: (index = 0) => view.all(element => element.className.includes('case-row-delete'))[index],
      password: () => view.labelled('Your password'),
      alerts: () => view.all(element => element.getAttribute('role') === 'alert').map(element => element.textContent),
      confirm: () =>
        submit(
          view
            .all(element => element.tagName === 'DIALOG')[0]
            .descendants()
            .find(element => element.tagName === 'FORM')!,
        ),
      submitButton: () =>
        view
          .all(element => element.tagName === 'DIALOG')[0]
          .descendants()
          .find(element => element.getAttribute('type') === 'submit'),
    });
  }
  async function opened(cases: Case[], values: Record<string, unknown> = {}) {
    const view = await trashTab(cases, values);
    click(view.rowDelete());
    await settled();
    return view;
  }
  const stays = (view: Awaited<ReturnType<typeof trashTab>>) =>
    view
      .dialog()
      .descendants()
      .filter(element => element.tagName === 'LI')
      .map(element => element.textContent);

  it('asks for your password and deletes the case', async () => {
    signedIn();
    // The read after the purge never answers, so the tab can show only what it kept.
    vi.spyOn(api, 'storage')
      .mockResolvedValueOnce(report)
      .mockReturnValue(new Promise(() => {}));
    const purge = vi.spyOn(api, 'purge').mockResolvedValue({ id: 'old' }),
      purged = vi.fn(),
      view = await trashTab([old], { onPurged: purged });
    click(view.tab('Storage'));
    await settled();
    expect(view.text()).toContain('Old · In Trash');
    click(view.tab('Trash'));
    await settled();
    click(view.rowDelete());
    await settled();
    expect(view.dialog().open).toBe(true);
    expect(view.dialog().textContent).toContain('Old and everything made from it will leave this workstation. This cannot be undone.');
    expect(stays(view)).toContain('Images held in web browser caches, until the browser clears them or you sign out');
    expect(document.activeElement).toBe(view.password());
    fill(view.password()!, PASSWORD);
    await settled();
    view.confirm();
    await settled();
    expect(purge).toHaveBeenCalledExactlyOnceWith('old', { password: PASSWORD });
    expect(purged).toHaveBeenCalledExactlyOnceWith('old');
    expect(view.rows()).toHaveLength(0);
    expect(view.dialog()).toBeUndefined();
    expect(view.text()).toContain('Deleted forever.');
    expect(document.activeElement).toBe(view.search());
    click(view.tab('Storage'));
    await settled();
    expect(view.text()).toContain('Measuring storage…');
    expect(view.text(), 'the deleted case').not.toContain('Old · In Trash');
  });
  it('asks only for a confirmation with sign-in off', async () => {
    const purge = vi.spyOn(api, 'purge').mockResolvedValue({ id: 'old' }),
      view = await opened([old]);
    expect(view.password()).toBeUndefined();
    expect(view.dialog().textContent).toContain('Images held in web browser caches, until the browser clears them');
    expect(view.dialog().textContent).not.toContain('sign out');
    expect(document.activeElement?.textContent).toBe('Cancel');
    view.confirm();
    await settled();
    expect(purge).toHaveBeenCalledExactlyOnceWith('old', { confirm: true });
  });
  it('keeps the dialog open after a wrong password, with the field empty', async () => {
    signedIn();
    const purge = vi.spyOn(api, 'purge').mockRejectedValue(new ApiError(403, 'Wrong password.')),
      purged = vi.fn(),
      view = await opened([old], { onPurged: purged });
    fill(view.password()!, PASSWORD);
    await settled();
    view.confirm();
    await settled();
    expect(purge, 'sent once, and never again on its own').toHaveBeenCalledOnce();
    expect(view.alerts()).toEqual(['Wrong password.']);
    expect((view.password() as unknown as HTMLInputElement).value).toBe('');
    expect(document.activeElement).toBe(view.password());
    expect(view.dialog()).toBeDefined();
    expect(view.rows()).toHaveLength(1);
    expect(purged).not.toHaveBeenCalled();
  });
  it('empties Trash one case at a time and lists the cases it could not delete', async () => {
    const first = deferred<{ id: string }>(),
      busy = 'This case is still in use. Try again in a minute.';
    const purge = vi.spyOn(api, 'purge').mockReturnValueOnce(first.promise).mockRejectedValueOnce(new ApiError(409, busy)).mockResolvedValue({ id: '' });
    const purged = vi.fn(),
      view = await trashTab([old, scan('busy', 'Busy', { trashed_at: old.trashed_at }), scan('new', 'New', { trashed_at: old.trashed_at })], {
        onPurged: purged,
      });
    // Empty Trash means every case in it, not only the rows a search shows.
    fill(view.search(), 'Busy');
    await settled();
    expect(view.rows().map(row => row.getAttribute('data-case-id'))).toEqual(['busy']);
    click(view.button('Empty Trash'));
    await settled();
    expect(view.dialog().textContent).toContain('3 cases and everything made from them will leave this workstation.');
    view.confirm();
    await settled();
    expect(view.dialog().textContent).toContain('Deleting 1 of 3…');
    expect(purge).toHaveBeenCalledOnce();
    first.resolve({ id: 'old' });
    await settled();
    expect(purge.mock.calls.map(([id]) => id)).toEqual(['old', 'busy', 'new']);
    expect(purged.mock.calls).toEqual([['old'], ['new']]);
    expect(view.alerts()).toEqual([`Not deleted:Busy: ${busy}`]);
    expect(view.dialog().textContent).toContain('Busy and everything made from it will leave this workstation.');
    fill(view.search(), '');
    await settled();
    expect(view.rows().map(row => row.getAttribute('data-case-id'))).toEqual(['busy']);
    view.confirm();
    await settled();
    expect(purge.mock.calls.map(([id]) => id)).toEqual(['old', 'busy', 'new', 'busy']);
    expect(view.dialog()).toBeUndefined();
    expect(view.text()).toContain('3 cases deleted forever.');
  });
  it('takes a case already deleted elsewhere off the list', async () => {
    vi.spyOn(api, 'purge').mockRejectedValue(new ApiError(404, 'Case not found'));
    const purged = vi.fn(),
      view = await opened([old], { onPurged: purged });
    view.confirm();
    await settled();
    expect(purged).not.toHaveBeenCalled();
    expect(view.rows()).toHaveLength(0);
    expect(view.dialog()).toBeUndefined();
    expect(view.text()).toContain('1 case had already left Trash.');
  });
  it('ignores the second press of a double-click', async () => {
    const purge = vi.spyOn(api, 'purge').mockResolvedValue({ id: 'old' }),
      view = await trashTab([old]);
    pressNumber(view.rowDelete(), 2);
    await settled();
    expect(view.dialog()).toBeUndefined();
    pressNumber(view.rowDelete(), 1);
    await settled();
    /** As a browser does: a press on a submit button submits its form unless a handler stopped it. */
    const pressSubmit = (detail: number) => {
      const event = Object.assign(new Event('click', { cancelable: true }), { detail });
      view.submitButton()!.dispatchEvent(event);
      if (!event.defaultPrevented) view.confirm();
    };
    pressSubmit(2);
    await settled();
    expect(purge).not.toHaveBeenCalled();
    pressSubmit(1);
    await settled();
    expect(purge).toHaveBeenCalledOnce();
  });
  it('focuses the title, not the password, on a touch screen', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true }));
    signedIn();
    const view = await opened([old]);
    expect(document.activeElement).toBe(view.all(element => element.tagName === 'H2')[0]);
    expect(document.activeElement?.textContent).toBe('Delete forever?');
  });
  it('stays open while deleting', async () => {
    const answer = deferred<{ id: string }>();
    vi.spyOn(api, 'purge').mockReturnValue(answer.promise);
    const close = vi.fn(),
      view = await opened([old], { onClose: close });
    view.confirm();
    await settled();
    // This DOM passes an event to no one else, so its own flags say where a browser would let it go.
    const escape = Object.assign(new Event('keydown', { cancelable: true }), { key: 'Escape' });
    view.dialog().dispatchEvent(escape);
    await settled();
    expect(escape.defaultPrevented && escape.cancelBubble, "refused, and kept from the Library's own Escape").toBe(true);
    expect(view.dialog()).toBeDefined();
    expect((view.button('Cancel') as unknown as HTMLButtonElement).disabled).toBe(true);
    expect(close).not.toHaveBeenCalled();
    answer.resolve({ id: 'old' });
    await settled();
    expect(view.dialog()).toBeUndefined();
  });
});

describe('the 30-day reminder', () => {
  // From the clock, never the suite's fixed dates, which pass 30 days on their own.
  const inTrashFor = (id: string, days: number) => scan(id, id, { trashed_at: new Date(Date.now() - days * 86_400_000).toISOString() });

  it('names the cases in Trash for over 30 days and opens Trash', async () => {
    vi.mocked(api.trashed).mockResolvedValue([inTrashFor('old', 31), inTrashFor('older', 45), inTrashFor('new', 1)]);
    const view = library({});
    await settled();
    expect(view.text()).toContain('2 cases have been in Trash for over 30 days.');
    click(view.button('Open Trash'));
    await settled();
    expect(view.text()).toContain('Cases in Trash still use disk space.');
    expect(view.rows()).toHaveLength(3);
  });
  it('says nothing while every case in Trash is newer', async () => {
    vi.mocked(api.trashed).mockResolvedValue([inTrashFor('new', 1)]);
    const view = library({});
    await settled();
    expect(view.text()).not.toContain('30 days');
    expect(view.button('Open Trash')).toBeUndefined();
  });
});

describe('sizes', () => {
  it('fills sizes in after the list', async () => {
    const answer = deferred<{ sizes: Record<string, number> }>();
    vi.mocked(api.sizes).mockReturnValue(answer.promise);
    const view = library({});
    await settled();
    expect(view.rows()[0].textContent).toContain('Ready · …');
    answer.resolve({ sizes: { liver: 5 * 1024 ** 2 } });
    await settled();
    expect(view.rows()[0].textContent).toContain('Ready · 5 MB');
    expect(view.rows()[1].textContent, 'a case with no size').toContain('Ready · —');
  });
  it('leaves a dash and no banner when they cannot be read', async () => {
    vi.mocked(api.sizes).mockRejectedValue(new Error('No answer'));
    const view = library({});
    await settled();
    expect(view.rows()[0].textContent).toContain('Ready · —');
    expect(view.text()).not.toContain('No answer');
  });
  it('reads them once, whatever a heartbeat does', async () => {
    const stamp = ref(0);
    library({
      get stamp() {
        return stamp.value;
      },
    });
    await settled();
    stamp.value = 1;
    await settled();
    expect(api.sizes).toHaveBeenCalledOnce();
    expect(api.trashed).toHaveBeenCalledTimes(2);
  });
  it('shows the size of a case in Trash', async () => {
    vi.mocked(api.trashed).mockResolvedValue([scan('old', 'Old', { trashed_at: '2026-11-02T12:00:00Z' })]);
    vi.mocked(api.sizes).mockResolvedValue({ sizes: { old: 2 * 1024 ** 3 } });
    const view = library({});
    await settled();
    click(view.tab('Trash'));
    await settled();
    expect(view.rows()[0].textContent).toContain(`Ready · ${(2).toLocaleString(undefined, { minimumFractionDigits: 1 })} GB`);
  });
});

describe('Storage', () => {
  async function storageTab() {
    const read = vi.spyOn(api, 'storage').mockResolvedValue(report),
      view = library({});
    click(view.tab('Storage'));
    await settled();
    return Object.assign(view, {
      read,
      cells: (index: number) => view.all(element => element.tagName === 'TBODY')[0].children[index].children.map(cell => cell.textContent),
    });
  }

  it('gives the totals and the free space', async () => {
    const view = await storageTab();
    expect(view.all(element => element.tagName === 'DL')[0].children.map(item => item.textContent)).toEqual([
      'Cases10 MB',
      'Trash< 1 MB',
      'Shared caches20 MB',
      'Other30 MB',
      'Free space40 MB',
    ]);
    expect(view.text()).toContain('Case sizes leave out shared caches, which several cases can use at once.');
    expect(view.text()).toContain(NOTICE);
  });
  it('lists your own case with its name and owner', async () => {
    const when = new Date('2026-11-01T12:00:00Z').toLocaleDateString();
    expect((await storageTab()).cells(0)).toEqual(['Liver', 'Ada', when, '7 MB']);
  });
  it('says which of your cases are in Trash, and which have no owner', async () => {
    const [name, owner] = (await storageTab()).cells(1);
    expect([name, owner]).toEqual(['Old · In Trash', 'No owner']);
  });
  it('shows no label for a case that is not yours', async () => {
    const view = await storageTab(),
      when = new Date('2026-10-01T12:00:00Z').toLocaleDateString();
    expect(view.cells(2)).toEqual(['Not your case', 'Grace', when, '3 MB']);
    expect(view.all(element => element.tagName === 'TBODY')[0].children[2].getAttribute('data-case-id')).toBeNull();
  });
  it('hides the search, where most rows may have no name', async () => {
    expect((await storageTab()).labelled('Search cases')).toBeUndefined();
  });
  it('reads on each visit, one at a time', async () => {
    const view = await storageTab(),
      slow = deferred<StorageReport>();
    view.read.mockReturnValue(slow.promise);
    click(view.tab('Cases'));
    click(view.tab('Storage'));
    click(view.tab('Cases'));
    click(view.tab('Storage'));
    expect(view.read).toHaveBeenCalledTimes(2);
    slow.resolve(report);
    await settled();
    click(view.tab('Cases'));
    click(view.tab('Storage'));
    expect(view.read).toHaveBeenCalledTimes(3);
  });
  it('clears caches and shows what was freed', async () => {
    const view = await storageTab(),
      fresh = { ...report, totals: { ...report.totals, shared_caches: 5 * 1024 ** 2, rebuildable: 0 } },
      answer = deferred<{ freed: number; storage: StorageReport }>(),
      clear = vi.spyOn(api, 'clearCaches').mockReturnValue(answer.promise);
    expect(view.text()).toContain('15 MB rebuilds when needed. Saved surfaces and cases stay.');
    click(view.button('Clear caches'));
    await settled();
    expect((view.button('Clearing…') as unknown as HTMLButtonElement).disabled).toBe(true);
    answer.resolve({ freed: 15 * 1024 ** 2, storage: fresh });
    await settled();
    expect(clear).toHaveBeenCalledOnce();
    expect(view.all(element => element.getAttribute('role') === 'status').map(element => element.textContent)).toEqual(['Freed 15 MB.']);
    expect(view.all(element => element.tagName === 'DL')[0].children[2].textContent).toBe('Shared caches5 MB');
    expect(view.text()).toContain('< 1 MB rebuilds when needed.');
    expect(view.read, 'the answer carries the fresh report').toHaveBeenCalledOnce();
  });
  it('shows a refusal to clear caches', async () => {
    const refusal = 'Jobs are running. Try again when they finish.';
    vi.spyOn(api, 'clearCaches').mockRejectedValue(new ApiError(409, refusal));
    const view = await storageTab();
    click(view.button('Clear caches'));
    await settled();
    expect(view.all(element => element.getAttribute('role') === 'alert').map(element => element.textContent)).toEqual([refusal]);
    expect(view.button('Clear caches')).toBeDefined();
    expect(view.text()).not.toContain('Freed');
  });
  it('shows a failure in the tab', async () => {
    vi.spyOn(api, 'storage').mockRejectedValue(new ApiError(403, 'Administrator access is required'));
    const view = library({});
    click(view.tab('Storage'));
    await settled();
    expect(view.all(element => element.getAttribute('role') === 'alert')[0].textContent).toBe('Administrator access is required');
  });
  it('hides Storage from a member', () => {
    session.value = { mode: 'password', setup_required: false, user: { id: 'u', username: 'u', display_name: 'U', role: 'member' } };
    expect(library({}).button('Storage')).toBeUndefined();
  });
  it('offers Storage to an admin', () => {
    session.value = { mode: 'password', setup_required: false, user: { id: 'u', username: 'u', display_name: 'U', role: 'admin' } };
    expect(library({}).button('Storage')).toBeDefined();
  });
});

describe('an empty library', () => {
  it('shows the import buttons and the research notice on an empty library', async () => {
    const importScan = vi.fn(),
      demo = vi.fn(),
      view = library({ cases: [], onImportScan: importScan, onDemo: demo });
    await settled();
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
    expect(view.button('Import a scan →')).toBeDefined();
    expect(view.button('Import your first scan →')).toBeUndefined();
    expect(view.button('Explore synthetic demo')).toBeDefined();
  });
  it('says nothing about being empty until Trash has answered', async () => {
    const answer = deferred<Case[]>();
    vi.mocked(api.trashed).mockReturnValue(answer.promise);
    const view = library({ cases: [] });
    await settled();
    expect(view.all(element => element.tagName === 'H2')).toHaveLength(0);
    expect(view.text()).not.toContain('No cases match.');
    answer.resolve([]);
    await settled();
    expect(view.text()).toContain('No cases yet');
  });
  it('is still empty when Trash cannot be read', async () => {
    vi.mocked(api.trashed).mockRejectedValue(new Error('No answer'));
    const view = library({ cases: [] });
    await settled();
    expect(view.text()).toContain('No cases yet');
  });
  it('names MR only where it is offered', async () => {
    const view = library({ cases: [], mrEnabled: true });
    await settled();
    expect(view.text()).toContain('Explore CT and static 3D MR scans');
  });
  it('holds the demo back while an import runs', async () => {
    const view = library({ cases: [], importing: true });
    await settled();
    expect((view.button('Explore synthetic demo') as unknown as HTMLButtonElement).disabled).toBe(true);
  });
});

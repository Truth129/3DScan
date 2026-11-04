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
  chest = scan('chest', 'Chest'),
  old = scan('old', 'Old', { trashed_at: '2026-11-02T12:00:00Z' });
const report: StorageReport = {
  cases: [
    { id: 'liver', label: 'Liver', owner: 'Ada', created_at: '2026-11-01T12:00:00Z', trashed_at: null, size: 7 * 1024 ** 2 },
    { id: 'old', label: 'Old', owner: null, created_at: '2026-11-01T12:00:00Z', trashed_at: '2026-11-02T12:00:00Z', size: 1024 },
    { label: null, owner: 'Grace', created_at: '2026-10-01T12:00:00Z', size: 3 * 1024 ** 2 },
  ],
  totals: { cases: 10 * 1024 ** 2, trash: 1024, shared_caches: 20 * 1024 ** 2, other: 30 * 1024 ** 2, rebuildable: 15 * 1024 ** 2 },
  disk: { total: 100 * 1024 ** 2, free: 40 * 1024 ** 2 },
};
const signedIn = (role: 'admin' | 'member', mode: 'off' | 'password' = 'password') =>
  (session.value = { mode, setup_required: false, user: { id: 'u', username: 'u', display_name: 'U', role } });

function library(values: Record<string, unknown>) {
  const view = mount(CaseLibrary, () => ({ cases: [liver, chest], stamp: 0, closable: true, importing: false, mrEnabled: false, notice: NOTICE, ...values }));
  return Object.assign(view, {
    search: () => view.labelled('Search cases')!,
    nameField: () => view.labelled('Case name'),
    rows: () => view.all(element => element.className.split(' ').includes('case-row')),
    ids: () => view.all(element => element.className.split(' ').includes('case-row')).map(row => row.getAttribute('data-case-id')),
    section: () => view.labelled('Library')!,
    form: () => view.all(element => element.tagName === 'FORM')[0],
    tab: (name: string) => view.all(element => element.tagName === 'BUTTON' && element.textContent.trim().startsWith(name))[0],
    alerts: () => view.all(element => element.getAttribute('role') === 'alert').map(element => element.textContent),
  });
}
/** A Library whose stamp the test moves, as the workspace heartbeat does. */
function stamped() {
  const stamp = ref(0),
    view = library({
      get stamp() {
        return stamp.value;
      },
    });
  return Object.assign(view, { stamp });
}
async function trashTab(cases: Case[], values: Record<string, unknown> = {}) {
  vi.mocked(api.trashed).mockResolvedValueOnce(cases);
  const view = library(values);
  await settled();
  click(view.tab('Trash'));
  await settled();
  return view;
}
type Library = ReturnType<typeof library>;
const rowButtons = (view: Library) =>
  view
    .rows()[0]
    .descendants()
    .filter(element => element.tagName === 'BUTTON')
    .map(button => button.textContent);
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
  it('starts in the search, narrows the list as you type, and opens the first match on Enter', async () => {
    const open = vi.fn(),
      view = library({ onOpen: open, openId: 'chest' });
    expect(document.activeElement).toBe(view.search());
    expect(view.rows().map(row => row.className.includes('active'))).toEqual([false, true]);
    click(view.rows()[0].children[0]);
    expect(open).toHaveBeenLastCalledWith('liver');
    fill(view.search(), 'knee');
    await settled();
    expect(view.text()).toContain('No cases match.');
    expect(view.text()).not.toContain('No cases yet');
    fill(view.search(), 'che');
    await settled();
    expect(view.ids()).toEqual(['chest']);
    press(view.search(), 'Enter');
    expect(open).toHaveBeenLastCalledWith('chest');
  });
  it('leaves the search field alone on a touch screen, where a keyboard would cover the list', () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true }));
    const before = document.activeElement;
    library({});
    expect(document.activeElement).toBe(before);
  });
  it('gives each row its date, status, size, who added it and whether it is synthetic', async () => {
    const opened = new Date('2026-11-02T12:00:00Z').toLocaleDateString(),
      added = new Date(liver.created_at).toLocaleDateString();
    const view = library({ cases: [liver, scan('demo', 'Demo', { opened_at: '2026-11-02T12:00:00Z', synthetic: true, added_by: 'Meera P' })] });
    await settled();
    expect(view.rows()[0].textContent).toContain(`Added ${added} · Ready`);
    expect(view.rows()[0].textContent).not.toContain('Added by');
    expect(view.rows()[1].textContent).toContain(`Opened ${opened} · Ready · — · Added by Meera P · Synthetic`);
    fill(view.search(), 'meera');
    await settled();
    expect(view.ids()).toEqual(['demo']);
  });
});

describe('Escape', () => {
  it('cancels a rename first, then closes, unless the Library is all there is', async () => {
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
    const kept = vi.fn(),
      only = library({ closable: false, onClose: kept });
    press(only.section(), 'Escape');
    expect(kept).not.toHaveBeenCalled();
    expect(only.labelled('Close Library')).toBeUndefined();
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
  it('renames a case and shows a refusal, with the cursor back in the name', async () => {
    const refusal = 'This case is still importing. Rename it when the import finishes.';
    const next = { ...liver, label: 'Liver, week 12' };
    const rename = vi.spyOn(api, 'rename').mockRejectedValueOnce(new ApiError(409, refusal)).mockResolvedValueOnce(next);
    const renamed = vi.fn(),
      view = library({ onRenamed: renamed });
    click(view.button('Rename'));
    await settled();
    expect(document.activeElement).toBe(view.nameField());
    expect(view.text()).toContain('Do not put patient identifiers in a case name.');
    expect(view.nameField()?.getAttribute('maxlength')).toBe('128');
    fill(view.nameField()!, '  Liver, week 12  ');
    await settled();
    view.button('Save')!.focus();
    submit(view.form());
    await settled();
    expect(rename).toHaveBeenLastCalledWith('liver', 'Liver, week 12');
    expect(view.alerts()[0]).toBe(refusal);
    expect(renamed).not.toHaveBeenCalled();
    expect(document.activeElement, 'so Esc and Enter still work').toBe(view.nameField());
    submit(view.form());
    await settled();
    expect(renamed).toHaveBeenCalledExactlyOnceWith(next);
    expect(view.form()).toBeUndefined();
    expect(view.text()).not.toContain(refusal);
  });
  it('asks for a name, and offers nothing on a case that is still importing', async () => {
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
    const importing = library({ cases: [scan('new', 'New', { status: 'importing' })] });
    expect(importing.button('Rename')).toBeUndefined();
    expect(importing.button('Move to Trash')).toBeDefined();
  });
});

describe('moving a case to Trash', () => {
  it('ignores the second press of a double-click, takes a keyboard press, and returns the cursor to search', () => {
    const trash = vi.fn(),
      view = library({ onTrash: trash }),
      button = view.button('Move to Trash');
    button!.focus();
    pressNumber(button, 2);
    expect(document.activeElement, 'even from the press it ignores').toBe(view.search());
    expect(trash).not.toHaveBeenCalled();
    pressNumber(button, 1);
    pressNumber(button, 0);
    expect(trash.mock.calls).toEqual([['liver'], ['liver']]);
  });
});

describe('Trash', () => {
  it('says what Delete forever does, with Restore first', async () => {
    const restore = vi.fn(),
      view = await trashTab([old], { onRestore: restore });
    expect(view.tab('Trash').textContent.trim()).toBe('Trash1');
    expect(view.text()).toContain('Cases in Trash still use disk space. Delete forever frees it and cannot be undone.');
    expect(view.rows()[0].textContent).toContain(`OldIn Trash since ${new Date(old.trashed_at!).toLocaleDateString()} · Ready`);
    expect(rowButtons(view)).toEqual(['Restore', 'Delete forever']);
    expect(view.button('Empty Trash')).toBeDefined();
    click(view.button('Restore'));
    expect(restore).toHaveBeenCalledExactlyOnceWith('old');
    expect(document.activeElement).toBe(view.search());
  });
  it('offers a member only Restore, and says who can delete forever', async () => {
    signedIn('member');
    const view = await trashTab([old]);
    expect(view.text()).toContain('Cases in Trash still use disk space. An administrator can delete them forever.');
    expect(view.text()).not.toContain('Delete forever frees it');
    expect(rowButtons(view)).toEqual(['Restore']);
    expect(view.button('Empty Trash')).toBeUndefined();
  });
  it('says so when it is empty, and still says what Trash does', async () => {
    const view = await trashTab([]);
    expect(view.text()).toContain('Trash is empty.');
    expect(view.text()).toContain('still use disk space');
    expect(view.text()).toContain(NOTICE);
  });
  it('searches Trash, where Enter opens nothing', async () => {
    const open = vi.fn(),
      view = await trashTab([old], { onOpen: open });
    fill(view.search(), 'knee');
    await settled();
    expect(view.text()).toContain('No cases match.');
    fill(view.search(), 'l');
    await settled();
    press(view.search(), 'Enter');
    expect(view.rows()).toHaveLength(1);
    expect(open).not.toHaveBeenCalled();
  });
  it('reads Trash again on a new stamp, where only the newest answer counts and a failed read keeps the list', async () => {
    const slow = deferred<Case[]>(),
      view = stamped();
    await settled();
    click(view.tab('Trash'));
    vi.mocked(api.trashed).mockReturnValueOnce(slow.promise).mockResolvedValueOnce([old]).mockRejectedValueOnce(new Error('No answer'));
    view.stamp.value = 1;
    await settled();
    view.stamp.value = 2;
    await settled();
    slow.resolve([]);
    await settled();
    expect(view.rows()).toHaveLength(1);
    view.stamp.value = 3;
    await settled();
    expect(view.rows()).toHaveLength(1);
  });
});

describe('Delete forever', () => {
  const PASSWORD = `test-${crypto.randomUUID()}`;
  async function opened(cases: Case[], values: Record<string, unknown> = {}) {
    const view = await trashTab(cases, values);
    return Object.assign(view, {
      dialog: () => view.all(element => element.tagName === 'DIALOG')[0],
      rowDelete: () => view.all(element => element.className.includes('case-row-delete'))[0],
      password: () => view.labelled('Your password'),
      confirm: () =>
        submit(
          view
            .all(element => element.tagName === 'DIALOG')[0]
            .descendants()
            .find(element => element.tagName === 'FORM')!,
        ),
      async open() {
        click(view.all(element => element.className.includes('case-row-delete'))[0]);
        await settled();
      },
    });
  }

  it('asks for your password and deletes the case, and from Storage too', async () => {
    signedIn('admin');
    // The read after the purge never answers, so the tab can show only what it kept.
    vi.spyOn(api, 'storage')
      .mockResolvedValueOnce(report)
      .mockReturnValue(new Promise(() => {}));
    const purge = vi.spyOn(api, 'purge').mockResolvedValue({ id: 'old' }),
      purged = vi.fn(),
      view = await opened([old], { onPurged: purged });
    click(view.tab('Storage'));
    await settled();
    expect(view.text()).toContain('Old · In Trash');
    click(view.tab('Trash'));
    await settled();
    await view.open();
    expect(view.dialog().open).toBe(true);
    expect(view.dialog().textContent).toContain('Old and everything made from it will leave this workstation. This cannot be undone.');
    expect(view.dialog().textContent).toContain('Images held in web browser caches, until the browser clears them or you sign out');
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
    expect(view.text(), 'the deleted case').not.toContain('Old · In Trash');
  });
  it('asks only for a confirmation with sign-in off', async () => {
    const purge = vi.spyOn(api, 'purge').mockResolvedValue({ id: 'old' }),
      view = await opened([old]);
    await view.open();
    expect(view.password()).toBeUndefined();
    expect(view.dialog().textContent).not.toContain('sign out');
    expect(document.activeElement?.textContent).toBe('Cancel');
    view.confirm();
    await settled();
    expect(purge).toHaveBeenCalledExactlyOnceWith('old', { confirm: true });
  });
  it('keeps the dialog open after a wrong password, with the field empty', async () => {
    signedIn('admin');
    const purge = vi.spyOn(api, 'purge').mockRejectedValue(new ApiError(403, 'Wrong password.')),
      purged = vi.fn(),
      view = await opened([old], { onPurged: purged });
    await view.open();
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
      view = await opened([old, scan('busy', 'Busy', { trashed_at: old.trashed_at }), scan('new', 'New', { trashed_at: old.trashed_at })], {
        onPurged: purged,
      });
    // Empty Trash means every case in it, not only the rows a search shows.
    fill(view.search(), 'Busy');
    await settled();
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
    expect(view.ids()).toEqual(['busy']);
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
    await view.open();
    view.confirm();
    await settled();
    expect(purged).not.toHaveBeenCalled();
    expect(view.rows()).toHaveLength(0);
    expect(view.dialog()).toBeUndefined();
    expect(view.text()).toContain('1 case had already left Trash.');
  });
  it('ignores the second press of a double-click', async () => {
    const purge = vi.spyOn(api, 'purge').mockResolvedValue({ id: 'old' }),
      view = await opened([old]);
    pressNumber(view.rowDelete(), 2);
    await settled();
    expect(view.dialog()).toBeUndefined();
    pressNumber(view.rowDelete(), 1);
    await settled();
    /** As a browser does: a press on a submit button submits its form unless a handler stopped it. */
    const pressSubmit = (detail: number) => {
      const event = Object.assign(new Event('click', { cancelable: true }), { detail });
      view
        .dialog()
        .descendants()
        .find(element => element.getAttribute('type') === 'submit')!
        .dispatchEvent(event);
      if (!event.defaultPrevented) view.confirm();
    };
    pressSubmit(2);
    await settled();
    expect(purge).not.toHaveBeenCalled();
    pressSubmit(1);
    await settled();
    expect(purge).toHaveBeenCalledOnce();
  });
  it('stays open while deleting', async () => {
    const answer = deferred<{ id: string }>();
    vi.spyOn(api, 'purge').mockReturnValue(answer.promise);
    const close = vi.fn(),
      view = await opened([old], { onClose: close });
    await view.open();
    view.confirm();
    await settled();
    // This DOM passes an event to no one else, so its own flags say where a browser would let it go.
    const escape = Object.assign(new Event('keydown', { cancelable: true }), { key: 'Escape' });
    view.dialog().dispatchEvent(escape);
    await settled();
    expect(escape.defaultPrevented && escape.cancelBubble, "refused, and kept from the Library's own Escape").toBe(true);
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
    expect(view.rows()).toHaveLength(3);
  });
  it.each<[string, Case[], 'admin' | 'member']>([
    ['while every case in Trash is newer', [inTrashFor('new', 1)], 'admin'],
    ['to a member', [inTrashFor('old', 31)], 'member'],
  ])('says nothing %s', async (_name, cases, role) => {
    signedIn(role);
    vi.mocked(api.trashed).mockResolvedValue(cases);
    const view = library({});
    await settled();
    expect(view.text()).not.toContain('30 days');
    expect(view.button('Open Trash')).toBeUndefined();
  });
});

describe('sizes', () => {
  it('fills sizes in after the list, once whatever a heartbeat does, and for Trash too', async () => {
    const answer = deferred<{ sizes: Record<string, number> }>();
    vi.mocked(api.sizes).mockReturnValue(answer.promise);
    vi.mocked(api.trashed).mockResolvedValue([old]);
    const view = stamped();
    await settled();
    expect(view.rows()[0].textContent).toContain('Ready · …');
    answer.resolve({ sizes: { liver: 5 * 1024 ** 2, old: 2 * 1024 ** 3 } });
    await settled();
    expect(view.rows()[0].textContent).toContain('Ready · 5 MB');
    expect(view.rows()[1].textContent, 'a case with no size').toContain('Ready · —');
    view.stamp.value = 1;
    await settled();
    expect(api.sizes).toHaveBeenCalledOnce();
    expect(api.trashed).toHaveBeenCalledTimes(2);
    click(view.tab('Trash'));
    await settled();
    expect(view.rows()[0].textContent).toContain(`Ready · ${(2).toLocaleString(undefined, { minimumFractionDigits: 1 })} GB`);
  });
  it('leaves a dash and no banner when they cannot be read', async () => {
    vi.mocked(api.sizes).mockRejectedValue(new Error('No answer'));
    const view = library({});
    await settled();
    expect(view.rows()[0].textContent).toContain('Ready · —');
    expect(view.text()).not.toContain('No answer');
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
      totals: () => view.all(element => element.tagName === 'DL')[0].children.map(item => item.textContent),
    });
  }

  it('gives the totals, the free space and each case, with no search', async () => {
    const view = await storageTab(),
      day = (when: string) => new Date(when).toLocaleDateString();
    expect(view.totals()).toEqual(['Cases10 MB', 'Trash< 1 MB', 'Shared caches20 MB', 'Other30 MB', 'Free space40 MB']);
    expect(view.text()).toContain('Case sizes leave out shared caches, which several cases can use at once.');
    expect(view.text()).toContain(NOTICE);
    expect(view.cells(0)).toEqual(['Liver', 'Ada', day('2026-11-01T12:00:00Z'), '7 MB']);
    expect(view.cells(1).slice(0, 2), 'in Trash, and added by nobody').toEqual(['Old · In Trash', '—']);
    expect(view.cells(2)).toEqual(['Not your case', 'Grace', day('2026-10-01T12:00:00Z'), '3 MB']);
    expect(view.all(element => element.tagName === 'TBODY')[0].children[2].getAttribute('data-case-id')).toBeNull();
    expect(view.labelled('Search cases'), 'most rows may have no name').toBeUndefined();
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
    expect(view.totals()[2]).toBe('Shared caches5 MB');
    expect(view.read, 'the answer carries the fresh report').toHaveBeenCalledOnce();
  });
  it('shows a refusal to clear caches, and a failure to read', async () => {
    const refusal = 'Jobs are running. Try again when they finish.';
    vi.spyOn(api, 'clearCaches').mockRejectedValue(new ApiError(409, refusal));
    const view = await storageTab();
    click(view.button('Clear caches'));
    await settled();
    expect(view.alerts()).toEqual([refusal]);
    expect(view.text()).not.toContain('Freed');
    vi.mocked(api.storage).mockRejectedValue(new ApiError(403, 'Administrator access is required'));
    const failed = library({});
    click(failed.tab('Storage'));
    await settled();
    expect(failed.alerts()).toEqual(['Administrator access is required']);
  });
});

describe('admin tabs', () => {
  it('offers Storage to an admin and People to an admin with sign-in on, and neither to a member', () => {
    signedIn('admin');
    expect(library({}).button('Storage')).toBeDefined();
    expect(library({}).button('People')).toBeDefined();
    signedIn('member');
    expect(library({}).button('Storage')).toBeUndefined();
    expect(library({}).button('People')).toBeUndefined();
    signedIn('admin', 'off');
    expect(library({}).button('People')).toBeUndefined();
  });
  it('moves a demoted admin off People and Storage', async () => {
    vi.spyOn(api, 'people').mockResolvedValue([]);
    vi.spyOn(api, 'storage').mockResolvedValue(report);
    signedIn('admin');
    const view = library({});
    for (const name of ['People', 'Storage']) {
      click(view.tab(name));
      await settled();
      expect(view.search()).toBeUndefined();
      signedIn('member');
      await settled();
      expect(view.tab(name)).toBeUndefined();
      expect(view.ids()).toEqual(['liver', 'chest']);
      signedIn('admin');
      await settled();
    }
    expect(api.people).toHaveBeenCalledOnce();
  });
});

describe('an empty library', () => {
  it('shows the import buttons and the research notice, once Trash has answered', async () => {
    const answer = deferred<Case[]>();
    vi.mocked(api.trashed).mockReturnValue(answer.promise);
    const importScan = vi.fn(),
      demo = vi.fn(),
      view = library({ cases: [], onImportScan: importScan, onDemo: demo });
    await settled();
    expect(view.all(element => element.tagName === 'H2')).toHaveLength(0);
    expect(view.text()).not.toContain('No cases match.');
    answer.resolve([]);
    await settled();
    expect(view.text()).toContain('No cases yet');
    expect(view.text()).toContain('Explore CT scans with linked slices and 3D views.');
    expect(view.text()).toContain(NOTICE);
    click(view.button('Import your first scan →'));
    click(view.button('Explore synthetic demo'));
    expect(importScan).toHaveBeenCalledOnce();
    expect(demo).toHaveBeenCalledOnce();
  });
  it('says where the cases went when Trash holds every one, and is still empty when Trash cannot be read', async () => {
    vi.mocked(api.trashed).mockResolvedValue([scan('old', 'Old')]);
    const view = library({ cases: [] });
    await settled();
    expect(view.text()).toContain('Every case is in Trash');
    expect(view.text()).not.toContain('No cases yet');
    expect(view.button('Import a scan →')).toBeDefined();
    expect(view.button('Explore synthetic demo')).toBeDefined();
    vi.mocked(api.trashed).mockRejectedValue(new Error('No answer'));
    const unread = library({ cases: [] });
    await settled();
    expect(unread.text()).toContain('No cases yet');
  });
  it('names MR only where it is offered, and holds the demo back while an import runs', async () => {
    const view = library({ cases: [], mrEnabled: true, importing: true });
    await settled();
    expect(view.text()).toContain('Explore CT and static 3D MR scans');
    expect((view.button('Explore synthetic demo') as unknown as HTMLButtonElement).disabled).toBe(true);
  });
});

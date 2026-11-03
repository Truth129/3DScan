import { afterEach, expect, it, vi } from 'vitest';
import ImportDialog from '../src/components/ImportDialog.vue';
import { api } from '../src/lib/api';
import { ARCHIVE_OFF, leavePage, session, type SessionView } from '../src/lib/session';
import { click, mount, settled } from './mounted';

// The session has ended, so the upload is waiting for a sign-in that here never comes.
vi.mock('../src/lib/import-upload', () => ({ uploadImport: () => new Promise(() => {}) }));

const member = (archive_access: boolean): SessionView => ({
  mode: 'password',
  setup_required: false,
  user: { id: 'u', username: 'u', display_name: 'U', role: 'member', archive_access },
});

afterEach(() => {
  session.value = undefined;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it('asks before a reload during an upload, unless a session change requires the reload', async () => {
  vi.stubGlobal('location', { ...location, reload: () => {} });
  const listen = vi.spyOn(window, 'addEventListener'),
    view = mount(ImportDialog, () => ({ busy: false })),
    input = view.all(element => element.tagName === 'INPUT' && element.getAttribute('type') === 'file')[0];
  (input as unknown as { files: File[] }).files = [new File(['x'], 'one.dcm')];
  input.dispatchEvent(new Event('change'));
  await settled();
  click(view.all(element => element.tagName === 'BUTTON' && element.textContent.includes('Import scan →'))[0]);
  await settled();
  const keepPage = listen.mock.calls.find(([type]) => type === 'beforeunload')![1] as (event: BeforeUnloadEvent) => void;
  const asked = () => {
    const event = new Event('beforeunload', { cancelable: true }) as BeforeUnloadEvent;
    keepPage(event);
    return event.defaultPrevented;
  };
  expect(asked()).toBe(true);
  leavePage();
  expect(asked()).toBe(false);
});

it('offers no Orthanc tab to an account without the archive', () => {
  session.value = member(false);
  const view = mount(ImportDialog, () => ({ busy: false }));
  expect(view.button('Folder / ZIP')).toBeDefined();
  expect(view.button('Orthanc archive')).toBeUndefined();
  expect(view.text()).toContain('Import DICOM files. Select a CT reconstruction before processing.');
});

it('goes back to files and says why when the archive is turned off', async () => {
  session.value = member(true);
  vi.spyOn(api, 'orthancStatus').mockImplementation(() => new Promise(() => {}));
  const view = mount(ImportDialog, () => ({ busy: false }));
  click(view.button('Orthanc archive'));
  await settled();
  expect(view.text()).toContain('Connecting to Orthanc…');
  session.value = member(false);
  await settled();
  expect(view.button('Orthanc archive')).toBeUndefined();
  expect(view.button('Choose folder')).toBeDefined();
  expect(view.all(element => element.getAttribute('role') === 'alert').map(element => element.textContent)).toEqual([
    `${ARCHIVE_OFF} An import already started still finishes and appears in the Library.`,
  ]);
});

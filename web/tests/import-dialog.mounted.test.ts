import { afterEach, expect, it, vi } from 'vitest';
import ImportDialog from '../src/components/ImportDialog.vue';
import { leavePage } from '../src/lib/session';
import { click, mount, settled } from './mounted';

// The session has ended, so the upload is waiting for a sign-in that here never comes.
vi.mock('../src/lib/import-upload', () => ({ uploadImport: () => new Promise(() => {}) }));

afterEach(() => {
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

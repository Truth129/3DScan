import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PeoplePanel from '../src/components/PeoplePanel.vue';
import { api, ApiError } from '../src/lib/api';
import { redeemLink, session } from '../src/lib/session';
import type { OneTimeLink, Person } from '../src/types';
import { click, mount, settled, type MountedElement } from './mounted';

const person = (id: string, values: Partial<Person> = {}): Person => ({
  id,
  username: id,
  display_name: id[0].toUpperCase() + id.slice(1),
  role: 'member',
  archive_access: true,
  state: 'active',
  created_at: '2026-11-01T12:00:00Z',
  last_seen_at: null,
  link_expires_at: null,
  ...values,
});
const asha = person('asha', { role: 'admin', last_seen_at: '2026-11-03T12:00:00Z' }),
  ravi = person('ravi', { archive_access: false });
const made: OneTimeLink = { code: 'made-up-code', purpose: 'invite', expires_at: '2026-11-05T12:00:00Z' };

function panel() {
  const view = mount(PeoplePanel, () => ({}));
  return Object.assign(view, {
    row: (id: string) => view.all(element => element.getAttribute('data-person-id') === id)[0],
    named: (name: string) => view.all(element => element.getAttribute('name') === name)[0],
    linkField: () => view.labelled('Link') as unknown as HTMLInputElement | undefined,
    action: (id: string, text: string) =>
      view
        .all(element => element.getAttribute('data-person-id') === id)[0]
        .descendants()
        .find(element => element.tagName === 'BUTTON' && element.textContent.trim() === text),
  });
}
const buttons = (row: MountedElement) =>
  row
    .descendants()
    .filter(element => element.tagName === 'BUTTON')
    .map(element => element.textContent.trim());
function fill(element: MountedElement, value: string) {
  (element as unknown as HTMLInputElement).value = value;
  element.dispatchEvent(new Event('input'));
}
/** Ravi's link, which the list then shows as the server does, until it is used. */
function linkForRavi(link: OneTimeLink = made) {
  vi.mocked(api.people).mockResolvedValue([asha, { ...ravi, link_expires_at: link.expires_at }]);
  return vi.spyOn(api, 'personLink').mockResolvedValue(link);
}

let tab: EventTarget;
beforeEach(() => {
  session.value = { mode: 'password', setup_required: false, user: { id: 'asha', username: 'asha', display_name: 'Asha', role: 'admin' } };
  vi.spyOn(api, 'people').mockResolvedValue([asha, ravi]);
  vi.stubGlobal('location', { ...location, hostname: 'review.example', origin: 'https://review.example' });
  // A window of its own, so a test can dispatch focus.
  tab = new EventTarget();
  vi.stubGlobal('addEventListener', tab.addEventListener.bind(tab));
  vi.stubGlobal('removeEventListener', tab.removeEventListener.bind(tab));
});
afterEach(() => {
  session.value = undefined;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('People', () => {
  it('lists each person with role, archive and when last seen', async () => {
    const view = panel(),
      when = new Date(asha.last_seen_at!).toLocaleDateString();
    await settled();
    expect(view.row('asha').textContent).toContain(`Asha (you)asha · Administrator · Archive · Last seen ${when}`);
    expect(view.row('ravi').textContent).toContain('Raviravi · Member · No archive · Not seen yet');
  });
  it('adds a person and shows a link to copy', async () => {
    const newcomer = person('newcomer', { display_name: 'New Comer', state: 'invited', link_expires_at: made.expires_at }),
      added = vi.spyOn(api, 'addPerson').mockResolvedValue({ person: newcomer, link: made }),
      writeText = vi.fn(async () => {});
    vi.stubGlobal('navigator', { clipboard: { writeText } });
    const view = panel();
    await settled();
    fill(view.named('new-username'), ' newcomer ');
    fill(view.named('new-display-name'), 'New Comer');
    view.all(element => element.tagName === 'FORM')[0].dispatchEvent(new Event('submit', { cancelable: true }));
    await settled();
    expect(added).toHaveBeenCalledExactlyOnceWith({ username: 'newcomer', display_name: 'New Comer', role: 'member' });
    expect(view.row('newcomer').textContent).toContain('newcomer · Member · Archive · Invited · link works until');
    expect(view.linkField()!.value).toBe('https://review.example/#set-password=made-up-code&username=newcomer');
    expect(view.text()).toContain('Send this link to New Comer. It works once, until');
    expect(view.text()).not.toContain('only on this computer');
    expect(document.activeElement).toBe(view.button('Copy link'));
    expect((view.named('new-username') as unknown as HTMLInputElement).value).toBe('');
    click(view.button('Copy link'));
    await settled();
    expect(writeText).toHaveBeenCalledExactlyOnceWith('https://review.example/#set-password=made-up-code&username=newcomer');
    expect(view.text()).toContain('Copied.');
  });
  it('selects the link when the clipboard refuses', async () => {
    linkForRavi({ ...made, purpose: 'reset' });
    vi.stubGlobal('navigator', {});
    const view = panel();
    await settled();
    click(view.action('ravi', 'Reset link'));
    await settled();
    expect(view.text()).toContain('Any earlier link no longer works.');
    const field = view.linkField()!,
      selected = vi.spyOn(field, 'select');
    click(view.button('Copy link'));
    await settled();
    expect(document.activeElement).toBe(field);
    expect(selected).toHaveBeenCalled();
    expect(view.text()).toContain('Copy the selected link.');
  });
  it('says a link from a loopback address works only on this computer', async () => {
    vi.stubGlobal('location', { ...location, hostname: '127.0.0.1', origin: 'http://127.0.0.1:9010' });
    linkForRavi();
    const view = panel();
    await settled();
    click(view.action('ravi', 'Reset link'));
    await settled();
    expect(view.linkField()!.value).toBe('http://127.0.0.1:9010/#set-password=made-up-code&username=ravi');
    expect(view.text()).toContain('This link works only on this computer. Open 3DScan through its HTTPS address to make one for someone else.');
  });
  it('reads again when a link is used in this tab or the window regains focus, and drops a link used up', async () => {
    linkForRavi();
    const view = panel();
    await settled();
    click(view.action('ravi', 'Reset link'));
    await settled();
    vi.mocked(api.people).mockClear();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ mode: 'password', user: null, setup_required: false })),
    );
    await redeemLink(made.code, 'made up for this test', false);
    await settled();
    expect(view.linkField(), 'used in this tab').toBeUndefined();
    expect(api.people).toHaveBeenCalledOnce();
    click(view.action('ravi', 'Reset link'));
    await settled();
    vi.mocked(api.people).mockResolvedValue([asha, { ...ravi, last_seen_at: '2026-11-04T12:00:00Z' }]);
    tab.dispatchEvent(new Event('focus'));
    await settled();
    expect(view.linkField(), 'used in another browser').toBeUndefined();
    expect(view.row('ravi').textContent).toContain('Last seen');
  });
  it('drops the link of someone it disables, and keeps the focus on the button', async () => {
    linkForRavi();
    vi.spyOn(api, 'disablePerson').mockResolvedValue({ ...ravi, state: 'disabled', link_expires_at: made.expires_at });
    const view = panel();
    await settled();
    click(view.action('ravi', 'Reset link'));
    await settled();
    const disable = view.action('ravi', 'Disable')!;
    disable.focus();
    click(disable);
    // As a browser does when the focused button is disabled.
    document.body.focus();
    await settled();
    expect(view.linkField()).toBeUndefined();
    expect(document.activeElement).toBe(disable);
  });
  it('offers no role change, sign-out or disable on your own row', async () => {
    const view = panel();
    await settled();
    expect(buttons(view.row('asha'))).toEqual(['Turn archive off', 'Reset link']);
    expect(buttons(view.row('ravi'))).toEqual(['Make administrator', 'Turn archive on', 'Reset link', 'Sign out everywhere', 'Disable']);
  });
  it('shows a refusal and keeps the list', async () => {
    vi.spyOn(api, 'disablePerson').mockRejectedValue(new ApiError(409, 'Keep at least one administrator who can sign in'));
    const view = panel();
    await settled();
    const disable = view.action('ravi', 'Disable')!;
    disable.focus();
    click(disable);
    // As a browser does when the focused button is disabled.
    document.body.focus();
    await settled();
    expect(view.all(element => element.getAttribute('role') === 'alert').map(element => element.textContent)).toEqual([
      'Keep at least one administrator who can sign in',
    ]);
    expect(view.row('ravi').textContent).toContain('Not seen yet');
    expect(document.activeElement, 'the focus returns to the button pressed').toBe(disable);
  });
});

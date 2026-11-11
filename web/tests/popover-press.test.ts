import { describe, expect, it } from 'vitest';
import { moveMenuFocus, swallowPress } from '../src/lib/use-popover';

/** A page whose popover just took a press, and a viewer that hears what the page's capture listeners let through. */
function closedByPress() {
  const page = new EventTarget(),
    seen: string[] = [],
    closing = new Event('pointerdown', { cancelable: true });
  swallowPress(closing, page);
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) page.addEventListener(type, () => seen.push(type));
  return { closing, seen, press: (type: string) => page.dispatchEvent(new Event(type, { cancelable: true })) };
}

describe('a press that only closes a popover', () => {
  it('reaches no viewer through its click, and the next click does', () => {
    const { closing, seen, press } = closedByPress();
    ['mousedown', 'pointerup', 'mouseup', 'click'].forEach(press);
    expect(closing.defaultPrevented).toBe(true);
    expect(seen).toEqual([]);
    press('click');
    expect(seen).toEqual(['click']);
  });
  it('ends at the next press when a drag sends no click', () => {
    const { seen, press } = closedByPress();
    ['pointerup', 'mouseup'].forEach(press);
    expect(seen).toEqual([]);
    ['pointerdown', 'mousedown', 'click'].forEach(press);
    expect(seen).toEqual(['pointerdown', 'mousedown', 'click']);
  });
});

describe('arrow keys in a menu', () => {
  /** Two items around one hidden in a closed section, and the one that has focus. */
  function menu() {
    const focused: string[] = [],
      item = (name: string, closed = false) => ({ name, getClientRects: () => [{}], closest: () => (closed ? {} : null), focus: () => focused.push(name) });
    const items = [item('first'), item('closed', true), item('last')];
    const panel = { querySelectorAll: () => items } as unknown as ParentNode;
    const key = (key: string, active: unknown = null, target: unknown = null) => {
      const event = { key, target, defaultPrevented: false, preventDefault: () => void (event.defaultPrevented = true) };
      return { moved: moveMenuFocus(event as unknown as KeyboardEvent, panel, active as Element), event };
    };
    return { items, focused, key };
  }
  it('move down and up with wrapping, past a closed section, and jump with Home and End', () => {
    const { items, focused, key } = menu();
    key('ArrowDown');
    key('ArrowDown', items[0]);
    key('ArrowDown', items[2]);
    key('ArrowUp', items[0]);
    key('End', items[0]);
    key('Home', items[2]);
    expect(focused).toEqual(['first', 'last', 'first', 'last', 'last', 'first']);
  });
  it('leave the arrows to a field, and other keys alone', () => {
    const { focused, key } = menu();
    const field = { closest: (selector: string) => (selector.includes('input') ? {} : null) };
    expect(key('ArrowDown', null, field).moved).toBe(false);
    expect(key('Escape').moved).toBe(false);
    expect(key('ArrowDown').event.defaultPrevented).toBe(true);
    expect(focused).toEqual(['first']);
  });
});

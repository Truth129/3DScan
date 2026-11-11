import { describe, expect, it } from 'vitest';
import { swallowPress } from '../src/lib/use-popover';

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

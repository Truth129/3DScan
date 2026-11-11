import { describe, expect, it } from 'vitest';
import { railViewForKey } from '../src/lib/view-rail';

const key = (value: string, modifiers: Partial<KeyboardEvent> = {}) => ({
  key: value,
  altKey: false,
  ctrlKey: false,
  metaKey: false,
  isComposing: false,
  ...modifiers,
});
const element = (tagName: string, inForm = false) => ({ tagName, closest: (selector: string) => (selector === 'form' && inForm ? {} : null) });

describe('3D view rail keys', () => {
  it('pick the six views in rail order', () => {
    expect(['1', '2', '3', '4', '5', '6'].map(value => railViewForKey(key(value), element('BODY')))).toEqual([
      'anterior',
      'posterior',
      'right',
      'left',
      'superior',
      'oblique',
    ]);
    expect(railViewForKey(key('7'), element('BODY'))).toBeUndefined();
    expect(railViewForKey(key('1', { ctrlKey: true }), element('BODY'))).toBeUndefined();
  });
  it('leave a digit to a text field, a list or a form such as review’s', () => {
    for (const tag of ['INPUT', 'SELECT', 'TEXTAREA']) expect(railViewForKey(key('1'), element(tag))).toBeUndefined();
    // A checkbox or slider takes no digit, so one just clicked keeps the keys working.
    expect(railViewForKey(key('1'), { ...element('INPUT'), type: 'checkbox' })).toBe('anterior');
    expect(railViewForKey(key('1'), { ...element('INPUT'), type: 'range' })).toBe('anterior');
    expect(railViewForKey(key('1'), { tagName: 'DIV', isContentEditable: true })).toBeUndefined();
    expect(railViewForKey(key('1'), element('BUTTON', true))).toBeUndefined();
    expect(railViewForKey(key('1'), element('BUTTON'))).toBe('anterior');
  });
});

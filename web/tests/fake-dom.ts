import { expect } from 'vitest';
import { createRenderer } from 'vue';

/**
 * Just enough of an element for Vue's renderer and directives, plus the queries a suite makes.
 * SSR shows only the state a component opens in, so a suite that has to press its buttons
 * drives the real component here, with the browser build that tests/client-sfc-environment.ts gives.
 */
export class FakeElement {
  children: FakeElement[] = [];
  parent: FakeElement | null = null;
  props: Record<string, unknown> = {};
  style = {};
  options = [];
  text = '';
  constructor(readonly tag: string) {}
  addEventListener() {}
  removeEventListener() {}
  scrollTo() {}
  focus() {}
  querySelector() {
    return null;
  }
  showModal() {}
  close() {}
  getRootNode() {
    return this;
  }
  get content(): string {
    return this.text + this.children.map(child => child.content).join('');
  }
  get all(): FakeElement[] {
    return [this, ...this.children.flatMap(child => child.all)];
  }
  named(name: string) {
    return this.all.find(
      node =>
        node.tag !== '#text' &&
        String(node.props.class ?? '')
          .split(' ')
          .includes(name),
    );
  }
  /** An aria-label replaces the label around a control, so a select answers to one or the other. */
  private get name() {
    const around = this.parent?.tag === 'label' ? this.parent.children.filter(child => child !== this) : [];
    return this.props['aria-label'] ?? around.map(child => child.content).join('');
  }
  select(label: string) {
    return this.all.find(node => node.tag === 'select' && node.name === label);
  }
  button(start: string) {
    return this.all.find(node => node.tag === 'button' && node.content.trim().startsWith(start));
  }
  optionTexts() {
    return this.all.filter(node => node.tag === 'option').map(node => node.content.trim());
  }
}

const detach = (node: FakeElement) => {
  if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
  node.parent = null;
};
const insert = (child: FakeElement, parent: FakeElement, anchor?: FakeElement | null) => {
  detach(child);
  parent.children.splice(anchor ? parent.children.indexOf(anchor) : parent.children.length, 0, child);
  child.parent = parent;
};

export const { createApp } = createRenderer<FakeElement, FakeElement>({
  createElement: tag => new FakeElement(tag),
  createText: text => Object.assign(new FakeElement('#text'), { text }),
  createComment: () => new FakeElement('#text'),
  setText: (node, text) => void (node.text = text),
  setElementText(node, text) {
    node.children = [];
    node.text = text;
  },
  insert,
  // Vue stringifies a long static tree. Nothing queries inside one, so it stays one text node.
  insertStaticContent(content, parent, anchor) {
    const node = Object.assign(new FakeElement('#text'), { text: content });
    insert(node, parent, anchor);
    return [node, node];
  },
  remove: detach,
  parentNode: node => node.parent,
  nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
  patchProp: (node, key, _previous, next) => void (node.props[key] = next),
});

/** Long enough for every promise a click started that does not wait on a timer. */
export const settled = () => new Promise(resolve => setTimeout(resolve));

/** Press as a person can: a disabled button fails the test instead of firing. */
export async function press(button: FakeElement | undefined) {
  expect(button, 'button rendered').toBeDefined();
  expect(button!.props.disabled, 'button enabled').toBeFalsy();
  (button!.props.onClick as () => void)();
  await settled();
}

export async function choose(select: FakeElement | undefined, value: string) {
  expect(select, 'select rendered').toBeDefined();
  (select!.props.onChange as (event: unknown) => void)({ target: { value } });
  await settled();
}

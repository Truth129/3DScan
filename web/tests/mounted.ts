import { afterEach } from 'vitest';
import { createApp, h, nextTick, shallowRef, type App, type Component } from 'vue';

export type MountedElement = HTMLElement & { children: MountedElement[]; descendants(): MountedElement[]; open: boolean };

const mounted = new Set<App>();
// A workspace left mounted would keep polling into the next test.
afterEach(() => {
  mounted.forEach(app => app.unmount());
  mounted.clear();
});
export function setDocumentVisibility(state: DocumentVisibilityState) {
  (document as { visibilityState: DocumentVisibilityState }).visibilityState = state;
}
/** Run the queued updates and whatever they start, including a response body or a frame that needs a task. */
export async function settled() {
  for (let index = 0; index < 6; index++) {
    await nextTick();
    await new Promise(resolve => setTimeout(resolve, 0));
  }
}
/** Mount a real component into the memory DOM. Reading `props` inside the render keeps a reactive object live. */
export function mount<T = any>(component: Component, props: () => Record<string, unknown>) {
  const root = document.createElement('div') as unknown as MountedElement,
    instance = shallowRef<T>();
  const app = createApp({ render: () => h(component, { ...props(), ref: instance }) });
  app.mount(root);
  mounted.add(app);
  return {
    root,
    unmount() {
      mounted.delete(app);
      app.unmount();
    },
    get exposed() {
      return instance.value!;
    },
    all: (test: (element: MountedElement) => boolean) => root.descendants().filter(test),
    button(text: string) {
      return root.descendants().find(element => element.tagName === 'BUTTON' && element.textContent.trim() === text);
    },
    labelled(label: string) {
      return root.descendants().find(element => element.getAttribute('aria-label') === label);
    },
    text: () => root.textContent!,
  };
}
export function click(element: MountedElement | undefined) {
  if (!element) throw new Error('Nothing to click.');
  if ((element as unknown as { disabled: boolean }).disabled) throw new Error('A disabled control cannot be clicked.');
  element.dispatchEvent(new Event('click'));
}
export interface Deferred<T> {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(reason: unknown): void;
}
export function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void, reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

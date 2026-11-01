import type { Environment } from 'vitest/environments';

/**
 * Just enough DOM for Vue's own renderer, in the one environment that compiles SFCs for the
 * client. These tests mount the real components, because what they check is the wiring between
 * a template, its refs and its watchers, which a string render never runs.
 */
class MemoryNode extends EventTarget {
  parentNode: MemoryElement | null = null;
  nodeValue: string | null = null;
  get nextSibling(): MemoryNode | null {
    const siblings = this.parentNode?.childNodes;
    return siblings?.[siblings.indexOf(this) + 1] ?? null;
  }
  get previousSibling(): MemoryNode | null {
    const siblings = this.parentNode?.childNodes;
    return siblings?.[siblings.indexOf(this) - 1] ?? null;
  }
  get isConnected(): boolean {
    return this.parentNode?.isConnected ?? false;
  }
  get textContent(): string {
    return this.nodeValue ?? '';
  }
}
class MemoryElement extends MemoryNode {
  childNodes: MemoryNode[] = [];
  attributes = new Map<string, string>();
  className = '';
  value = '';
  checked = false;
  selected = false;
  disabled = false;
  open = false;
  multiple = false;
  selectedIndex = -1;
  clientWidth = 800;
  clientHeight = 600;
  connected = false;
  style = { cssText: '', setProperty() {}, removeProperty() {} };
  readonly tagName: string;
  constructor(tag: string) {
    super();
    this.tagName = tag.toUpperCase();
  }
  get isConnected(): boolean {
    return this.connected || super.isConnected;
  }
  get children() {
    return this.childNodes.filter((node): node is MemoryElement => node instanceof MemoryElement);
  }
  get firstChild() {
    return this.childNodes[0] ?? null;
  }
  get lastChild() {
    return this.childNodes.at(-1) ?? null;
  }
  /** Vue inserts static markup through a template element, which this DOM keeps as plain text. */
  get content() {
    return this;
  }
  get options() {
    return this.descendants().filter(node => node.tagName === 'OPTION');
  }
  get textContent(): string {
    return this.childNodes.map(node => node.textContent).join('');
  }
  set textContent(value: string) {
    this.childNodes = [];
    if (value) this.insertBefore(new MemoryNode(), null).nodeValue = value;
  }
  set innerHTML(value: string) {
    this.textContent = value;
  }
  insertBefore<T extends MemoryNode>(node: T, anchor: MemoryNode | null) {
    if (node instanceof MemoryElement && node.tagName === 'TEMPLATE') {
      for (const child of [...node.childNodes]) this.insertBefore(child, anchor);
      return node;
    }
    node.parentNode?.removeChild(node);
    const index = anchor ? this.childNodes.indexOf(anchor) : -1;
    this.childNodes.splice(index < 0 ? this.childNodes.length : index, 0, node);
    node.parentNode = this;
    return node;
  }
  removeChild(node: MemoryNode) {
    this.childNodes.splice(this.childNodes.indexOf(node), 1);
    node.parentNode = null;
  }
  setAttribute(name: string, value: string) {
    this.attributes.set(name, String(value));
  }
  removeAttribute(name: string) {
    this.attributes.delete(name);
  }
  getAttribute(name: string) {
    return this.attributes.get(name) ?? null;
  }
  hasAttribute(name: string) {
    return this.attributes.has(name);
  }
  descendants(): MemoryElement[] {
    return this.children.flatMap(child => [child, ...child.descendants()]);
  }
  /** Only the `[aria-label="..."]` form the components use. */
  querySelector(selector: string) {
    const label = /^\[aria-label="(.*)"\]$/.exec(selector)?.[1];
    return this.descendants().find(node => node.getAttribute('aria-label') === label) ?? null;
  }
  querySelectorAll() {
    return [];
  }
  getBoundingClientRect() {
    return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight };
  }
  getClientRects() {
    return [this.getBoundingClientRect()];
  }
  focus() {
    memoryDocument.activeElement = this;
  }
  showModal() {
    this.open = true;
  }
  close() {
    this.open = false;
  }
}
class MemoryDocument extends EventTarget {
  visibilityState = 'visible';
  activeElement: MemoryElement | null = null;
  head = new MemoryElement('head');
  body = new MemoryElement('body');
  get hidden() {
    return this.visibilityState !== 'visible';
  }
  createElement = (tag: string) => new MemoryElement(tag);
  createElementNS = (_namespace: string, tag: string) => new MemoryElement(tag);
  createTextNode(text: string) {
    const node = new MemoryNode();
    node.nodeValue = text;
    return node;
  }
  createComment = this.createTextNode;
  querySelector = () => null;
  querySelectorAll = () => [];
}
class MemoryResizeObserver {
  observe() {}
  disconnect() {}
}
const memoryDocument = new MemoryDocument(),
  storage = new Map<string, string>(),
  windowEvents = new EventTarget();

export default <Environment>{
  name: 'client-sfc',
  viteEnvironment: 'client',
  setup(global) {
    Object.assign(global, {
      document: memoryDocument,
      window: global,
      HTMLElement: MemoryElement,
      Element: MemoryElement,
      SVGElement: class {},
      ResizeObserver: MemoryResizeObserver,
      requestAnimationFrame: (callback: FrameRequestCallback) => setTimeout(() => callback(performance.now()), 0),
      cancelAnimationFrame: clearTimeout,
      addEventListener: windowEvents.addEventListener.bind(windowEvents),
      removeEventListener: windowEvents.removeEventListener.bind(windowEvents),
      location: { hash: '', pathname: '/', search: '', origin: 'http://localhost' },
      history: { state: null, replaceState() {} },
      localStorage: {
        getItem: (key: string) => storage.get(key) ?? null,
        setItem: (key: string, value: string) => storage.set(key, value),
        removeItem: (key: string) => storage.delete(key),
      },
    });
    return {
      teardown() {
        memoryDocument.visibilityState = 'visible';
        storage.clear();
      },
    };
  },
};

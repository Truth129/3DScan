import { describe, expect, it, vi } from 'vitest';
import type * as Core from '@cornerstonejs/core';
import { destroyRenderingEngine } from '../src/lib/cornerstone-runtime';

function fakeContext() {
  let lost = false;
  const loseContext = vi.fn(() => {
    lost = true;
  });
  return { gl: { isContextLost: () => lost, getExtension: (name: string) => (name === 'WEBGL_lose_context' ? { loseContext } : null) }, loseContext };
}
const window = (gl: unknown) => ({ getOpenGLRenderWindow: () => ({ getContext: () => gl }) });

describe('destroyRenderingEngine', () => {
  it('frees every pooled WebGL context after the engine is destroyed', () => {
    const first = fakeContext(),
      second = fakeContext(),
      order: string[] = [];
    first.loseContext.mockImplementation(() => order.push('lose'));
    // The public class is a facade, so the pool sits on its implementation.
    const engine = {
      _implementation: { contextPool: { getAllContexts: () => [window(first.gl), window(second.gl)] } },
      destroy: vi.fn(() => order.push('destroy')),
    };
    destroyRenderingEngine(engine as unknown as Core.RenderingEngine);
    expect(engine.destroy).toHaveBeenCalledOnce();
    expect(first.loseContext).toHaveBeenCalledOnce();
    expect(second.loseContext).toHaveBeenCalledOnce();
    expect(order).toEqual(['destroy', 'lose']);
  });

  it('ignores destroyed, missing and contextless engines', () => {
    destroyRenderingEngine(undefined);
    const destroyed = { hasBeenDestroyed: true, destroy: vi.fn() };
    destroyRenderingEngine(destroyed as unknown as Core.RenderingEngine);
    expect(destroyed.destroy).not.toHaveBeenCalled();
    const bare = { hasBeenDestroyed: false, destroy: vi.fn() };
    destroyRenderingEngine(bare as unknown as Core.RenderingEngine);
    expect(bare.destroy).toHaveBeenCalledOnce();
  });
});

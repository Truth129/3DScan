import { createApp } from 'vue';
import OfflineApp from './OfflineApp.vue';
import type { OfflineManifest } from './types';
import { presentStructures } from './lib/appearance';
import { normalizePortableState, validatePortablePayload, type PortablePayload } from './lib/portable-viewer';
import './style.css';
import './theme.css';

/** One IIFE/CSS and embedded assets; legacy surface-only exports retain their signature. */
export function initOffline(manifest: OfflineManifest, modelBase64: string | null, payloadOrMount?: PortablePayload | string, mountId = 'app') {
  if (typeof payloadOrMount === 'string') mountId = payloadOrMount;
  const shown = { ...manifest, structures: presentStructures(manifest.structures) };
  const payload = typeof payloadOrMount === 'object' && payloadOrMount ? validatePortablePayload(payloadOrMount, shown) : undefined;
  if (payload) {
    const state = normalizePortableState(payload.state, shown.structures, shown.geometry);
    shown.structures = shown.structures.map(item => ({ ...item, color: state.display[item.id]?.color ?? item.color }));
    if (!payload.layers.segmentation && modelBase64) throw new Error('Excluded segmentation geometry must not be embedded');
    if (payload.layers.segmentation && !modelBase64) throw new Error('The embedded anatomical model is missing');
  }
  let data: ArrayBuffer | undefined;
  if (modelBase64) {
    const binary = atob(modelBase64),
      bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    data = bytes.buffer;
  }
  return createApp(OfflineApp, { manifest: shown, data, payload }).mount(`#${mountId}`);
}

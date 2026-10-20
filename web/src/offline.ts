import { createApp } from 'vue';
import OfflineApp from './OfflineApp.vue';
import type { OfflineManifest } from './types';
import { presentStructures } from './lib/appearance';
import './style.css';
import './theme.css';

/** Backend embeds this IIFE, offline-viewer.css, manifest JSON and base64 GLB. No fetch or external assets. */
export function initOffline(manifest: OfflineManifest, modelBase64: string, mountId = 'app') {
  const binary = atob(modelBase64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return createApp(OfflineApp, { manifest: { ...manifest, structures: presentStructures(manifest.structures) }, data: bytes.buffer }).mount(`#${mountId}`);
}

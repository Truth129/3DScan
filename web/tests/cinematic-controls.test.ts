import { describe, expect, it } from 'vitest';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import VolumeControls from '../src/components/VolumeControls.vue';
import { CINEMATIC_HEART_PRESET, freshVolumeSettings } from '../src/lib/volume-presets';
import { freshVolumeMaskSettings } from '../src/lib/volume-mask';

describe('cinematic appearance controls', () => {
  it('keeps an exported volume mask margin visible and fixed offline', async () => {
    const html = await renderToString(
      createSSRApp(VolumeControls, {
        enabled: true,
        settings: freshVolumeSettings(),
        mask: freshVolumeMaskSettings({ enabled: true, ids: ['heart'], margin_mm: 2.5 }),
        maskMarginFixed: true,
      }),
    );
    expect(html).toMatch(/aria-label="CT volume mask margin"[^>]*value="2.5"[^>]*disabled/);
    expect(html).toContain('This offline export retains its prepared display margin.');
    expect(html).not.toContain('aria-label="Use 2.5 mm volume mask margin"');
  });

  it('offers explicit manual references and shadows offline without a measurement action', async () => {
    const settings = freshVolumeSettings({ preset: CINEMATIC_HEART_PRESET, cinematic: { myocardiumHU: 110, bloodHU: 800, shadows: 0, shadowReach: 0.3 } });
    const html = await renderToString(createSSRApp(VolumeControls, { enabled: true, settings, referenceAvailable: false }));
    expect(html).toContain('aria-label="Myocardium reference HU"');
    expect(html).toContain('value="110"');
    expect(html).toContain('value="800"');
    expect(html).toContain('aria-label="Cinematic shadows"');
    expect(html).not.toContain('>Measure from segments</button>');
    const ordinary = await renderToString(createSSRApp(VolumeControls, { enabled: true, settings: freshVolumeSettings() }));
    expect(ordinary).not.toContain('aria-label="Myocardium reference HU"');
  });

  it('shows measured-reference failures without hiding manual correction', async () => {
    const html = await renderToString(
      createSSRApp(VolumeControls, {
        enabled: true,
        settings: freshVolumeSettings({ preset: CINEMATIC_HEART_PRESET }),
        referenceAvailable: true,
        referenceMessage: 'Myocardium segment missing. Set both reference values manually.',
        referenceError: true,
      }),
    );
    expect(html).toContain('>Measure from segments</button>');
    expect(html).toMatch(/role="alert"[^>]*>Myocardium segment missing\./);
    expect(html).toContain('Apply reference HU');
  });
});

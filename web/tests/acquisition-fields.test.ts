import { describe, expect, it } from 'vitest';
import { createSSRApp } from 'vue';
import { renderToString } from '@vue/server-renderer';
import AcquisitionFields from '../src/components/AcquisitionFields.vue';
import { acquisitionSelection } from '../src/lib/acquisition';

describe('acquisition selection form', () => {
  it('shows distinct contrast and cardiac reconstruction fields for CorCTA', async () => {
    const html = await renderToString(
      createSSRApp(AcquisitionFields, {
        modelValue: acquisitionSelection({ profile: 'corcta', phase: 'coronary_arterial', cardiac_phase: 'best_diastolic', rr_percent: 69 }),
      }),
    );
    for (const label of ['Scan region', 'Contrast phase', 'Cardiac reconstruction phase', 'Reconstruction phase (% R–R)'])
      expect(html).toContain(`aria-label="${label}"`);
    expect(html).toContain('value="69"');
    expect(html).toContain('Best diastolic reconstruction');
    expect(html).toContain('calcium-score');
    expect(html).toContain('The selected DICOM series determines the images');
    expect(html).not.toContain('dual_phase');
  });
  it('keeps timing hidden for noncardiac scans', async () => {
    const html = await renderToString(createSSRApp(AcquisitionFields, { modelValue: acquisitionSelection({ profile: 'abdomen', phase: 'portal_venous' }) }));
    expect(html).not.toContain('aria-label="Cardiac reconstruction phase"');
    expect(html).not.toContain('aria-label="Reconstruction phase (% R–R)"');
  });
  it.each([
    [101, 'value="101"'],
    // An incomplete entry stays invalid without assigning NaN to the native input.
    [NaN, 'value=""'],
  ])('reports the invalid percentage %s visibly and keeps what was typed for correction', async (rr_percent, kept) => {
    const html = await renderToString(
      createSSRApp(AcquisitionFields, { modelValue: acquisitionSelection({ profile: 'corcta', phase: 'arterial', rr_percent }) }),
    );
    expect(html).toContain('role="alert"');
    expect(html).toContain('between 0 and 100% R–R');
    expect(html).toContain('aria-invalid="true"');
    expect(html).toMatch(new RegExp(`aria-label="Reconstruction phase \\(% R–R\\)"[^>]*${kept}`));
  });
  it('escapes saved custom phase labels and respects the busy state', async () => {
    const html = await renderToString(
      createSSRApp(AcquisitionFields, { disabled: true, modelValue: acquisitionSelection({ profile: 'corcta', phase: '<img src=x>' }) }),
    );
    expect(html).toContain('Saved phase: &lt;img src=x&gt;');
    expect(html).not.toContain('<img src=x>');
    expect((html.match(/ disabled/g) ?? []).length).toBeGreaterThanOrEqual(4);
  });
});

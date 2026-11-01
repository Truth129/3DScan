# Development

## Set up

Install as in [INSTALL.md](INSTALL.md), then add the test and lint tools:

```bash
.tools/bin/uv pip install --python .venv/bin/python --editable ".[test,lint]"
```

Tests never need a GPU, because they use fakes for the AI engines. They need only the
`--torch none` install plus TotalSegmentator's task registry, which CI installs without
its dependencies. See `.github/workflows/ci.yml`.

## Checks

These are the same checks CI runs.

```bash
# Python
.venv/bin/pytest -q
.venv/bin/ruff check
.venv/bin/ruff format --check

# Web
cd web
npm ci
npm run lint
npm run format:check
npx vue-tsc --noEmit
npx vitest run
```

`npm run format` and `ruff format` apply the formatting. `pre-commit install` runs the
same tools on each commit.

Files matching `web/tests/dicom-view-*.test.ts`, and
`web/tests/dicom-seg-file-dialog.test.ts`, run in Vitest's `mounted` project, whose environment
compiles Vue components for the client. Vitest otherwise compiles them for the
server, which leaves nothing to mount, and these tests check the real wiring between
templates, refs and watchers.

`scan3d serve` serves the built files in `web/dist`. While working on the viewer, run the
backend and then `npm run dev` in `web/`. Vite proxies `/api` to the port in
`SCAN3D_PORT`.

Use a throwaway library so development never touches real cases:

```bash
SCAN3D_DATA_DIR=/tmp/scan3d-dev SCAN3D_PORT=9011 .venv/bin/scan3d serve
```

## Browser gates

`web/tests/*_smoke.py` drive the built app in a real browser over WebDriver, using
synthetic data and temporary directories. They are not part of CI.

They need Chrome or Chromium and a matching chromedriver, found on `PATH` or through
`SCAN3D_BROWSER` and `SCAN3D_CHROMEDRIVER`. Each script also takes `--binary` and
`--driver`. They need a built copy of the viewer:

```bash
cd web
npx vite build --outDir /tmp/scan3d-stage/dist --emptyOutDir
```

Read each script's `--help` before running it. Some use software rendering by default,
and pass `--native` to use a GPU. Others need a Vulkan-capable GPU unless they offer
`--software`. `dicom_view_export_smoke.py` also starts two scratch Orthancs, so it needs
`--orthanc-binary`, a `--scratch` directory and `dcm2niix` on `PATH`.

A watcher closes each session's browser and removes its temporary profile when the gate
exits, even if the gate is killed. After SIGKILL, a chromedriver or server that the gate
started can still be running and holding its port, and the next run refuses that port.

A gate that starts its own server exits if a port it needs is already in use, and one
that starts the API also checks the API echoes that run's ID. This way a shared machine
cannot test another run's server by accident. Gates that take `--base-url` test whatever
server you point them at.

## Project rules

- **Patient data never enters the repository.** That includes tests, logs, screenshots,
  issues and pull requests. Use the synthetic demo case or the DICOM writers in
  `tests/synthetic_dicom.py`.
- **Geometry.** Work in patient RAS millimetres with the full affine. Never assume
  axis-aligned or orthogonal grids: oblique and reflected series are normal, and the viewer
  handles sheared stacks, although DICOM import refuses gantry-tilted CT. Masks are
  authoritative; meshes and display unions never feed measurements.
- **Shaders.** Software rendering cannot prove a shader works on a real GPU. GLSL leaves
  some results undefined, such as division by zero, and drivers differ. Axis-aligned rays
  reach those cases in every slice view, so check any shader or vtk.js change in an ordinary
  browser on real hardware. A gate's `--native` uses Vulkan, which is a different driver path
  from a browser's default on Linux. `web/vite-vtk-ray-guard.ts` patches one such line in
  vtk.js in every build, and it fails the build if that line moves or an unguarded copy
  ships. Keep the guard until vtk.js handles the case itself.
- **Errors.** `errors.NotFound` maps to 404, `ValueError` to 400, `ReviewConflict` and
  `ImportConflict` to 409, and schema errors to 422. Any other exception is a logged 500,
  so do not raise a bare `KeyError` for "not found".
- **Jobs.** Work that never touches the GPU runs on the CPU lane. TotalSegmentator and
  nnInteractive share the serialized GPU lane. Both run their models outside the API
  process: TotalSegmentator as a subprocess, and nnInteractive in the supervised child
  `scan3d/interactive_worker.py`. The API process never imports PyTorch, so a segmentation
  job resolves its device in a short-lived child, `segmentation._resolve_device`.
- **Vue templates.** Vue drops a space between two inline elements once Prettier breaks
  them onto separate lines. Keep such markup behind `<!-- prettier-ignore -->`.
- **Docs.** Describe behaviour changes in [API.md](API.md) and
  [ARCHITECTURE.md](ARCHITECTURE.md).

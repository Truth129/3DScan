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
`--software`.

A watcher closes each session's browser and removes its temporary profile when the gate
exits, even if the gate is killed. After SIGKILL, a chromedriver or server that the gate
started can still be running and holding its port, and the next run refuses that port.

A gate that starts its own server exits if a port it needs is already in use, and one
that starts the API also checks the API echoes that run's ID. This way a shared machine
cannot test another run's server by accident. Gates that take `--base-url` test whatever
server you point them at.

## Testing inference paths

No test needs a second GPU or a second real stack, and nothing here has run on a real
NVIDIA card or CUDA build. These facilities show what the code does, not what a card does.

- **`tests/conftest.py`** points `SCAN3D_INFERENCE_PATH_DIR` at an empty temporary folder
  and `SCAN3D_GPU_PROOF` at a file that does not exist for the whole run, so a checkout
  with an added path or its own GPU check still sees one unproven row, in the test process
  and in every child it spawns. Neither variable is a `Settings` field, so a temporary data
  directory isolates neither. A test that wants added paths or a default proof sets the
  variable to its own.
- **Browser gates** run outside pytest, so that fixture does not reach them, whether a gate
  starts its own server or takes `--base-url`. `web/tests/interactive_test_server.py`
  defaults `SCAN3D_INFERENCE_PATH_DIR` to a folder inside its temporary data that need not
  exist, and `web/tests/dicom_seg_import_smoke.py` sets the same, so a gate on either server
  lists one path whatever this checkout has added. `web/tests/inference_path_smoke.py` sets
  its own. They cover only that folder: a gate that does not set `SCAN3D_DEVICE=cpu` still
  shows this checkout's default card, and a plain `scan3d serve` behind `--base-url` lists
  whatever the checkout has.
- **`tests/inference_path_stub.py`** builds a real second interpreter with no second
  PyTorch. `stand_in()` makes a venv with package metadata and a passing proof, `activate()`
  makes it discoverable, and `shadow()` gives its children a module of your choice. A
  child's own `sys.prefix` then shows where it ran, which its argv cannot.
- **`tests/fake_torch/torch.py`** stands in for PyTorch in those children.
  `SCAN3D_FAKE_GPUS` is a JSON list of `[name, total_memory, is_integrated]`,
  `SCAN3D_FAKE_TORCH` selects a failure (`missing`, `crash` or `hang`),
  `SCAN3D_FAKE_TORCH_BUILD=cuda` reports a CUDA build instead of a ROCm one, and
  `SCAN3D_FAKE_TORCH_PREFIXES` names a file that collects each child's `sys.prefix`.
- **`tests/test_inference_lock.py`** keeps `requirements/cuda-py314.lock` honest without
  installing it. If a child entry point gains an import the lock lacks,
  `test_everything_a_child_imports_is_in_the_lock` fails. Add the package to
  `requirements/cuda-py314.in` and recompile with the command at the top of the lock. Pins
  outside the GPU family must also equal the AMD lock.
- **`web/vitest.config.ts`** splits `npx vitest run` into two projects. `mounted` takes
  `tests/**/*.mounted.test.ts`, which compile for the browser and run on
  `tests/fake-dom.ts`, because server rendering cannot exercise a component's clicks.
  `unit` takes the rest.
- **`web/tests/inference_path_smoke.py`** is a browser gate that always renders with
  SwiftShader, so it needs no flag. It starts its own server, chromedriver and temporary
  data, by default on ports 9035 and 9535, and it refuses `web/dist`, so give it a scratch
  build as above. Real routes list stand-in proofs, `SCAN3D_DEVICE=cpu` leaves the default
  path no card, the stand-in's children get fake PyTorch, and review predictions use a
  synthetic engine. It proves the viewer and its requests, not a card or a model. Run it
  from the repository root with the backend's Python:

  ```bash
  .venv/bin/python web/tests/inference_path_smoke.py --stage /tmp/scan3d-stage/dist
  ```

## Project rules

- **Patient data never enters the repository.** That includes tests, logs, screenshots,
  issues and pull requests. Use the synthetic demo case or the DICOM writers in
  `tests/synthetic_dicom.py`.
- **Geometry.** Work in patient RAS millimetres with the full affine. Never assume
  axis-aligned or orthogonal grids: oblique and reflected series are normal, and the viewer
  handles sheared stacks, although DICOM import refuses gantry-tilted CT. Masks are
  authoritative; meshes and display unions never feed measurements.
- **Errors.** `errors.NotFound` maps to 404, `ValueError` to 400, `ReviewConflict` and
  `ImportConflict` to 409, and schema errors to 422. Any other exception is a logged 500,
  so do not raise a bare `KeyError` for "not found".
- **Jobs.** Work that never touches the GPU runs on the CPU lane. TotalSegmentator and
  nnInteractive share the serialized GPU lane. Both run their models outside the API
  process: TotalSegmentator as a subprocess, and nnInteractive in the supervised child
  `scan3d/interactive_worker.py`. The API process never imports PyTorch, so a segmentation
  job resolves its device in a short-lived child, `segmentation._resolve_device`.
- **Inference paths.** Spell the name `inference_path` everywhere, because `path`, `runtime`
  and `target` collide with locals. A child takes its environment only from
  `InferencePath.environment()`, so no spawn site can be missed. The default path passes no
  new keyword, so its command, environment and fingerprint stay exactly what they were. A
  job carries its path at the top level, never in `payload`, because `public_job` strips the
  payload and the label has to outlive a failure that overwrites the stage. An added path
  is never re-routed: a job runs on its card or fails naming it, with no fallback to another
  card or the CPU. Never set `CUDA_VISIBLE_DEVICES` or any other visibility variable: each
  stack sees only its own vendor's cards, and HIP reads `CUDA_VISIBLE_DEVICES` too. See
  [ARCHITECTURE.md](ARCHITECTURE.md#inference-paths).
- **Vue templates.** Vue drops a space between two inline elements once Prettier breaks
  them onto separate lines. Keep such markup behind `<!-- prettier-ignore -->`.
- **Docs.** Describe behaviour changes in [API.md](API.md) and
  [ARCHITECTURE.md](ARCHITECTURE.md).

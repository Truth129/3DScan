# Development

## Set up

Install as in [INSTALL.md](INSTALL.md), then add the test and lint tools:

```bash
.tools/bin/uv pip install --python .venv/bin/python --editable ".[test,lint]"
```

Tests fake the AI engines, so they need no GPU. They need only the `--torch none` install plus
TotalSegmentator's task registry. See `.github/workflows/ci.yml`.

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
npx vite build --outDir /tmp/scan3d-check/dist --emptyOutDir
npx vite build --config vite.offline.config.ts --outDir /tmp/scan3d-check/dist
```

Check-only builds go outside `web/dist`, which `scan3d serve` serves.

`npm run format` and `ruff format` apply the formatting. `pre-commit install` runs the same tools
on each commit.

Vitest's `mounted` project compiles Vue components for the client, so tests can mount them:
`web/tests/dicom-view-*.test.ts`, `web/tests/dicom-seg-file-dialog.test.ts` and
`web/tests/**/*.mounted.test.ts`.

`scan3d serve` serves the built files in `web/dist`. For viewer work, run the backend, then
`npm run dev` in `web/`. Vite proxies `/api` to the port in `SCAN3D_PORT`.

Use a throwaway library so development never touches real cases:

```bash
SCAN3D_DATA_DIR=/tmp/scan3d-dev SCAN3D_PORT=9011 .venv/bin/scan3d serve
```

## Testing inference paths

No test needs a second GPU or a second real stack, and none runs on a real NVIDIA card or CUDA
build.

- **`tests/conftest.py`** points `SCAN3D_INFERENCE_PATH_DIR` at an empty folder and
  `SCAN3D_GPU_PROOF` at a missing file, so every test and child sees one unproven row. A temporary
  data directory isolates neither. A test that wants added paths or a default proof sets its own.
- **`tests/inference_path_stub.py`** builds a real second interpreter with no second PyTorch.
  `stand_in()` makes a venv with package metadata and a passing proof, `activate()` makes it
  discoverable, and `shadow()` gives its children a module of your choice.
- **`tests/fake_torch/torch.py`** stands in for PyTorch in those children:
  - `SCAN3D_FAKE_GPUS` is a JSON list of `[name, total_memory, is_integrated]`.
  - `SCAN3D_FAKE_TORCH` selects a failure: `missing`, `crash` or `hang`.
  - `SCAN3D_FAKE_TORCH_BUILD=cuda` reports a CUDA build instead of a ROCm one.
  - `SCAN3D_FAKE_TORCH_PREFIXES` names a file that collects each child's `sys.prefix`.
- **`tests/test_inference_lock.py`** checks `requirements/cuda-py314.lock` without installing it. If
  a child entry point gains an import the lock lacks, add the package to
  `requirements/cuda-py314.in` and recompile with the command at the top of the lock. Pins outside
  the GPU family must equal the AMD lock.
- **`web/tests/fake-dom.ts`** is the small DOM that the two inference-path mounted suites render into.
  The other mounted suites use `web/tests/mounted.ts`.

## Project rules

- **Patient data never enters the repository.** That includes tests, logs, screenshots, issues and
  pull requests. Use the synthetic demo case or the DICOM writers in `tests/synthetic_dicom.py`.
- **Geometry.** Work in patient RAS millimetres with the full affine. Never assume axis-aligned or
  orthogonal grids: oblique and reflected series are normal, and the viewer handles sheared stacks,
  although DICOM import refuses gantry-tilted CT. Masks are authoritative; meshes and display
  unions never feed measurements.
- **Shaders.** Software rendering cannot prove a shader works on a real GPU.
  - Check any shader or vtk.js change in an ordinary browser on real hardware.
  - `web/vite-vtk-ray-guard.ts` patches the vtk.js ray division and ambient-occlusion kernel in every
    build. The build fails if either line moves or an unguarded copy ships. Keep each patch until
    vtk.js handles the case itself.
- **Third-party code.** `web/vite-third-party-notices.ts` writes the licence of every bundled
  package to `licenses/THIRD-PARTY-NOTICES.txt` in the build, and into the portable viewer file.
  Code adapted from a dependency keeps its copyright in a comment and its licence in
  `web/public/licenses/`.
- **Routes.** Every new route needs a rule in `RULES` in `scan3d/access.py`, and `check_policy`
  stops the app from starting without one, in both modes.
  - A route with `{case_id}` gets the case rule on its own, which also hides a case in Trash, and
    needs an entry only for an audit action, or for the `trashed`, `trashed_admin` or
    `case_archive` kind.
    `{job_id}` and `{upload_id}` need those rule kinds.
  - People routes exist only in `password` mode, so their rules sit in `_CONDITIONAL`. They audit
    inside their own transactions, with a target, so their rules carry no audit action.
  - Read cases through `case_summaries(scope, opener)` or `visible_cases(scope)`. Every account's
    scope is `EVERY_CASE`, and a user ID scope still works. `Store.list` refuses `"cases"`.
    `storage_rows` reads every case. The only other reader of every case is the janitor's
    `existing_case_ids`, which gives IDs only.
- **Errors.** Raise the exceptions that [API.md](API.md#errors) maps to each status, never a bare
  `KeyError` for "not found". An `IntegrityError` reading `purged` is 404 `Case not found`, and any
  other is a logged 500.
- **Jobs.**
  - Work that never touches the GPU runs on the CPU lane.
  - TotalSegmentator, pinned nnU-Net models and nnInteractive share the serialized GPU lane.
  - All run outside the API process: TotalSegmentator and `scan3d/nnunet_worker.py` as
    subprocesses, and nnInteractive in the supervised child `scan3d/interactive_worker.py`.
  - The API process never imports PyTorch, so a segmentation job resolves its device in a
    short-lived child, `segmentation._resolve_device`.
- **Shared caches.** A review-cache writer passes `case_id`, so Delete forever can find its entries.
  Work that writes case files or a shared cache with no request or job holds
  `maintenance_lock(..., exclusive=False)`. Never call `Store.case_dir` where a case may be deleted,
  because it makes the folder.
- **Inference paths.** See [ARCHITECTURE.md](ARCHITECTURE.md#inference-paths).
  - Spell the name `inference_path` everywhere. `path`, `runtime` and `target` collide with locals.
  - The default path passes no new keyword, so its command, environment and fingerprint never change.
  - A job carries its path at the top level, never in `payload`, which `public_job` strips.
  - Never set `CUDA_VISIBLE_DEVICES` or any other visibility variable. HIP reads
    `CUDA_VISIBLE_DEVICES` too.
- **Vue templates.** Vue drops a space between two inline elements once Prettier breaks them onto
  separate lines. Keep such markup behind `<!-- prettier-ignore -->`.
- **Docs.** Describe behaviour changes in [API.md](API.md) and [ARCHITECTURE.md](ARCHITECTURE.md).

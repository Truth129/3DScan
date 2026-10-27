# Working on 3DScan

3DScan is a local, AI-assisted case-analysis workbench for radiologists: DICOM import,
TotalSegmentator segmentation, nnInteractive and direct-edit corrections with exact
revision history, a Cornerstone/Three.js viewer, and AI-agreement metrics. Backend:
Python/FastAPI in `scan3d/`. Frontend: Vue 3/TypeScript in `web/`.

Read before changing anything:

| Topic | Document |
|---|---|
| System overview and diagrams | `docs/ARCHITECTURE.md` |
| HTTP API, data model, geometry rules | `docs/CONTRACT.md` |
| Production runtime, releases, rollback | `docs/PRODUCTION_RUNTIME.md` |
| Setup and development checks | `docs/SETUP.md` |
| Evidence, newest first | `docs/VALIDATION.md` |
| Next viewer work | `docs/CORNERSTONE_PLAN.md` |
| Clinic integration direction | `docs/CLINIC_INTEGRATION.md` |

## Ground rules

- **`data/` is the live case library and holds patient scans.** Never read case contents,
  copy them into the repository, tests, logs or reports, or point experiments at it. Use a
  temporary data directory (`Settings(<tmp>/data, ...)` or `SCAN3D_DATA_DIR`).
- **The live service is a frozen release.** `scan3d.service` is a systemd user unit on
  `127.0.0.1:9010`, published on the tailnet at
  `https://scan3d-host.example.ts.net:8443`. It runs the release named in
  `.runtime/rocm10/code-dir`, under `.runtime/releases/<name>/`. Editing the checkout does
  not change it. The exceptions are `scripts/run-production.sh` and
  `scripts/production-env.sh`, which it sources whenever it starts.
- **Ask the user first** before stopping or restarting the service, switching releases,
  using the R9700 GPU, pushing, or anything outward-facing. Run test servers on other
  ports (9011, 9031–9034 were used) against temporary data.
- **Production is CT-only:** `production-env.sh` sets `SCAN3D_MODALITIES=CT`. MRI is
  implemented on `expansion` but stays off in production until the user qualifies it.
- **Never delete user data.** To remove it, move it to `.runtime/archive/` (git-ignored)
  and let the user delete the archive.
- **Git:** commit locally in focused commits on `expansion`. Ask before pushing, and never
  force-push.

## Commands

The project's shell scripts are bash. `scripts/production-env.sh` refuses zsh and fish,
which would silently resolve the wrong project root. In zsh, never use `path` as a variable
name, and write `pkill -f '[s]erve ...'` so the pattern cannot match your own command.

```bash
# Python 3.14 + ROCm runtime; tests use fakes and never touch the GPU (~30 s)
PYTHONPATH=$PWD PYTHONSAFEPATH=1 .venv-rocm10-py314/bin/python -m pytest -q -p no:cacheprovider

# Web (Node 26)
cd web && npm ci
npm run lint && npm run format:check && npx vue-tsc --noEmit && npx vitest run
npx vite build --outDir /tmp/<scratch>/dist --emptyOutDir   # never build into a release
npx vite build --config vite.offline.config.ts --outDir /tmp/<scratch>/dist
```

Lint and formatting: ruff (`pip install -e '.[lint]'` gives the pinned version) and
Prettier, both enforced by CI (`.github/workflows/ci.yml`) and `.pre-commit-config.yaml`.
`npm run format` and `ruff format` apply them. The one-time reformat is listed in
`.git-blame-ignore-revs`.

## Conventions

- **Style:** match the surrounding code. Docstrings and comments explain *why*. Keep
  changes surgical.
- **Geometry:**
  - Patient RAS millimetres everywhere, with the full affine.
  - Never assume axis-aligned or orthogonal grids: oblique, reflected and sheared
    (tilted-gantry) series exist.
  - Masks are authoritative; meshes and display unions never feed measurements.
- **Errors:**
  - `errors.NotFound` means 404, `ValueError` 400, `ReviewConflict` or `ImportConflict`
    409, schema errors 422.
  - Any other exception is a logged 500. Do not raise a bare `KeyError` for "not found".
- **Jobs:**
  - Work that never touches the GPU runs on the CPU lane.
  - TotalSegmentator and nnInteractive share the serialized GPU lane.
  - nnInteractive runs in the supervised child process `scan3d/interactive_worker.py`;
    the API process never imports torch.
- **Vue templates:** Vue drops a space between two inline elements once Prettier breaks
  it onto a new line. Keep such markup behind `<!-- prettier-ignore -->`.
- **Docs:** describe behaviour changes in `CONTRACT.md`, `ARCHITECTURE.md` and the README,
  and record evidence as a new dated section at the top of `docs/VALIDATION.md`.
- **Tests:** synthetic data only. Use `POST /api/cases/demo` for a phantom with masks; the
  tests contain synthetic CT and MR DICOM writers.

## Releasing

`docs/PRODUCTION_RUNTIME.md` holds the full procedure. In short:

1. From a clean tree, run `git archive <commit> scan3d | tar -x -C .runtime/releases/<name>`,
   then build both viewers into `<release>/web/dist`.
2. Write `release.json`: every file's SHA-256, the commit and the baseline release. Check
   that every `scan3d/` file matches the commit exactly.
3. Qualify the release with the production environment on scratch data and another port:
   - GPU ready and inference verified, both lanes alive, the expected modalities
   - the UI loads with no console errors
4. With the user's OK, and no queued or running jobs or accepting reviews: atomically
   replace `.runtime/rocm10/code-dir` and restart. Then verify the health check, that the
   served `index.html` matches the release, that record counts are unchanged, and that
   the journal is clean.
5. To roll back, write the previous release path back and restart.

Real-GPU check for the nnInteractive worker (stop the service first, with the user's OK):
`scripts/stack_validation.py interactive-worker --ct CT.nii.gz --mask MASK.nii.gz --output DIR`.
The demo case's CT and a demo mask are suitable synthetic inputs.

## Agent notes

- Claude Code subagent worktrees have started from `master` instead of the current
  branch. Check `git merge-base HEAD expansion` and reset onto the intended commit before
  working.
- `/tmp` is RAM-backed on this workstation. Delete large scratch builds and venvs when
  done.

## State on 27 October 2026

- **Branch:** `expansion` is pushed, and CI is green on Python 3.14 and the web job.
- **Live release:** `ct-hardening-20261027` (commit `971b05c`, CT-only).
- **Library:** reset to empty at the user's request. The previous library is at
  `.runtime/archive/data-20261027T143742Z`, waiting for the user to delete it.
  `test_data/` and `.runtime/migrations/` (the 25 October cutover audit, which includes a
  database backup) still hold case-derived data and were left untouched.
- **Shipped today:**
  - staged, resumable browser import
  - HTTP compression
  - explicit 404 mapping
  - research-use labels
  - the `scan3d backup` command
  - AI-agreement metrics
  - the CPU job lane
  - the supervised nnInteractive process, validated on the R9700
  - quantized display meshes
  - the modality setting
  - lint, format and CI
  - the one-time reformat

## Next steps

1. **Cornerstone refactor**, phase 2 (native volume MPR). Phase 1 is complete
   in the checkout: Cornerstone-only slices, explicit Retry and no PNG routes.
   Production remains the frozen CT release. Details and design constraints are
   in `docs/CORNERSTONE_PLAN.md`.
2. **Decisions waiting on the user:**
   - sign-in and an audit trail, needed before a second user
   - DICOM SEG/SR export, which adds a dependency to the pinned runtime
   - per-structure review status for the agreement metrics
   - frontend restructuring: stores, a router, typed API models
3. **Smaller follow-ups:**
   - precompress GLBs instead of gzipping per request
   - warm the agreement cache when a revision is committed
   - age review priority so segmentations cannot starve
   - fix the workspace overflowing at 768 px tall
   - surface Dice or the maximum surface distance for small local edits

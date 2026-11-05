# Working on 3DScan

3DScan is a local, AI-assisted case-analysis workbench for radiology research: DICOM
import, TotalSegmentator segmentation, nnInteractive and direct-edit corrections with exact
revision history, a Cornerstone/Three.js viewer, and AI-agreement metrics. The backend is
Python/FastAPI in `scan3d/`. The frontend is Vue 3/TypeScript in `web/`.

Read before changing anything:

| Topic                                | Document               |
| ------------------------------------ | ---------------------- |
| Install and supported platforms      | `docs/INSTALL.md`      |
| Settings                             | `docs/CONFIGURATION.md`|
| System overview                      | `docs/ARCHITECTURE.md` |
| HTTP API, data model, error mapping  | `docs/API.md`          |
| Using the app                        | `docs/USER_GUIDE.md`   |
| Orthanc integration                  | `docs/ORTHANC.md`      |
| DICOMweb archives                    | `docs/ARCHIVES.md`     |
| Checks and project rules             | `docs/DEVELOPMENT.md`  |

## Ground rules

- **Patient data never enters the repository**, including tests, logs, reports and
  fixtures. The configured data directory holds patient scans, so never read case contents
  or point experiments at it. Use a temporary one (`Settings(<tmp>/data, ...)` or
  `SCAN3D_DATA_DIR`) and a port other than the running service's.
- **Tests use synthetic data only.** `POST /api/cases/demo` gives a phantom with masks, and
  `tests/synthetic_dicom.py` writes synthetic DICOM.
- **Local settings stay local.** `scan3d.env` and `AGENTS.local.md` are git-ignored. Never
  commit host names, addresses, credentials or machine-specific paths.
- **Never delete user data.** Move it aside and let the user delete it.
- **Ask first** before stopping a running service, using a GPU for a long job, pushing, or
  anything outward-facing.

## Commands

The project's shell scripts are bash. Install with `scripts/bootstrap.sh`, then:

```bash
.venv/bin/pytest -q                        # backend tests; fakes, no GPU
.venv/bin/ruff check && .venv/bin/ruff format --check

cd web
npm ci
npm run lint && npm run format:check && npx vue-tsc --noEmit && npx vitest run
npx vite build --outDir /tmp/<scratch>/dist --emptyOutDir   # check-only builds go outside web/dist
```

## Conventions

- Match the surrounding code. Docstrings and comments explain _why_. Keep changes surgical.
- `docs/DEVELOPMENT.md` has the rules for geometry, errors, jobs and Vue templates. The
  geometry rules are the ones that hurt most when broken: patient RAS millimetres with the
  full affine, never assume an orthogonal grid, and masks are authoritative.
- Commit in focused commits. Describe behaviour changes in the docs in the same change.
- Git worktrees can start from the wrong base. Check `git status --short --branch` and
  `git merge-base HEAD <base>` before working in one.

# Working on 3DScan

3DScan is a local, AI-assisted case-analysis workbench for radiologists: DICOM import,
TotalSegmentator segmentation, nnInteractive and direct-edit corrections with exact
revision history, a Cornerstone/Three.js viewer, and AI-agreement metrics. Backend:
Python/FastAPI in `scan3d/`. Frontend: Vue 3/TypeScript in `web/`.

Read before changing anything:

| Topic                                  | Document                     |
| -------------------------------------- | ---------------------------- |
| System overview and diagrams           | `docs/ARCHITECTURE.md`       |
| HTTP API, data model, geometry rules   | `docs/CONTRACT.md`           |
| Production runtime, releases, rollback | `docs/PRODUCTION_RUNTIME.md` |
| Setup and development checks           | `docs/SETUP.md`              |
| Evidence, newest first                 | `docs/VALIDATION.md`         |
| Next viewer work                       | `docs/CORNERSTONE_PLAN.md`   |
| Clinic integration direction           | `docs/CLINIC_INTEGRATION.md` |

## Ground rules

- **`data/` is the live case library and holds patient scans.** Never read case contents,
  copy them into the repository, tests, logs or reports, or point experiments at it. Use a
  temporary data directory (`Settings(<tmp>/data, ...)` or `SCAN3D_DATA_DIR`).
- **`.runtime/orthanc-incoming/` is the incoming archive**, and may hold patient DICOM
  and its index after OsiriX sends studies. Apply the same protection as `data/`;
  never seed it, inspect clinical contents for experiments or remove it as a cache.
  `.runtime/orthanc-dev/` is the separate synthetic-only fixture server.
- **The live service now uses this development checkout**, at the user's explicit
  request on 28 October 2026. `scan3d.service` is a systemd user unit on
  `127.0.0.1:9010`, published on the tailnet at
  `https://scan3d-host.example.ts.net:8443`. `.runtime/rocm10/code-dir`
  selects `/home/user/Projects/3DScan`. Python changes apply on restart, but some
  modules load on first use, so restart promptly after they land and stop the
  service first when code moves between modules. Frontend changes become live
  when built assets replace `web/dist`. Do not treat this checkout
  as isolated from the live app. Test builds must still go to scratch directories.
  The previous frozen `ct-hardening-20261027` release is retained for rollback.
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

- **Style:** match the surrounding code. Docstrings and comments explain _why_. Keep
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

## State on 28 October 2026

- **Branch:** `expansion`; the Cornerstone phase 1/2 changes are local and unpushed.
  The earlier CI baseline is green on Python 3.14 and the web job.
- **Live code:** development checkout, initially activated from `09ff189`, CT-only.
  The frontend now serves native Cornerstone3D/vtk.js with bounded masking and
  the native correction reviewer from `54c23ec` (installation through `42d7a78`),
  without restarting the backend. Frontend audit/rollback:
  `.runtime/native-review-frontend-20261028T051359Z/`. The prior masking audit is
  `.runtime/native-mask-frontend-20261027T224248Z/`. The earlier native-port audit
  remains at `.runtime/cornerstone-primary-frontend-20261027T204309Z/`. The
  previous frozen `ct-hardening-20261027` release (`971b05c`) remains available.
  Audit and guarded rollback: `.runtime/development-cutover-20261027T185251Z/`
  (directory timestamp is UTC). The existing service, runtime and library were
  retained; aggregate record counts were unchanged. 884 backend tests and both
  viewer builds passed before switching. The modular refactor merge (`34e25db`)
  is active (PID558151); audit and prior backend snapshot:
  `.runtime/modular-refactor-merge-20261028T190109Z/`.
- **Library:** the user has added cases since the 27 October reset; preserve them.
  The library archived at that reset is at
  `.runtime/archive/data-20261027T143742Z`, waiting for the user to delete it.
  `test_data/` and `.runtime/migrations/` (the 25 October cutover audit, which includes a
  database backup) still hold case-derived data and were left untouched.
- **Shipped on 27 October:**
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

1. **Cornerstone refactor**, phases 1 and 2 are implemented in the checkout:
   Cornerstone-only slices, native volume MPR, slabs/crosshairs/labelmaps, shared
   memory budgeting and exact sheared-stack review. The user approved a synthetic
   R9700 comparison of Three.js, direct vtk.js and shared Cornerstone3D volume
   rendering; it passed, including a 400 MiB source and scattering. That comparison
   does not qualify native editing. See
   `docs/VOLUME_RENDERING_COMPARISON.md` and `docs/CORNERSTONE_PLAN.md` before
   further viewer work. Clinical accuracy review remains user-only. The user has now selected
   the development checkout for the primary service; do not assume it is frozen.
   The live CT 3D view now defaults to **Cornerstone3D/vtk.js**, with session-owned
   source sharing independent of MPR panel visibility, native CT plane and enabled
   quantized surfaces. CT-visible surfaces are solid. Three.js loads lazily for
   legacy mode, shear/MR and portable HTML. Orthogonal CT review now stays native,
   including target picking, exact native-plane drawing, scissors, markers and
   PNG composition; candidate/save/undo contracts are unchanged. Native masking now supports inside/outside, explicit
   segment selection and physical display margin with one <=8 MiB crop texture,
   retaining source/MPR independence and hiding CT for empty/pending/error masks.
   The pinned streaming-mapper bridge uses actor-local factories and needs
   qualification on Core/vtk dependency upgrades. 463 web tests, all three native
   reviewer suites on software/approved R9700, 22 shared-component gates, 12 App
   gates and standalone offline qualification pass. Shared/Trackball resizes
   retain the intentional physical camera through the public reset event. The user authorized one read-only actual
   CT check; it passed without retaining clinical artifacts. This does not grant
   broader patient-data access. Large combined MPR/all-surface views can exceed
   the conservative memory budget; closing MPR or hiding surfaces frees capacity.
   Completed CT transfer staging is now released; mesh memory charges only one
   largest-part upload scratch and disables unused picking maps. The 2 GiB cap
   is unchanged. Masking/memory qualification used synthetic data only.
   See the newest `docs/VALIDATION.md` section for evidence and browser-startup notes.
2. **Orthanc incoming integration:** the adapter, CPU import/recovery/provenance and
   browser workflow are implemented. A separate private receiver targets OsiriX
   MD 14.3.0 at `10.20.0.107`, calling AE `OsiriX-Mac`, sending to
   `10.20.0.3:4243` / called AE `SCAN3D`. Main-app/receiver activation is complete:
   `scan3d-orthanc.service` is enabled, its authenticated API is port8044, and
   the main app has its private EnvironmentFile drop-in. The user explicitly
   approved the existing any-address UFW4243 rule; Orthanc still enforces the
   exact sender IP/AE. App restart was approved and aggregate library counts
   stayed unchanged. Audit: `.runtime/orthanc-integration-20261028T075156Z/`.
   Rejected-association metadata corrected the originally supplied Mac IP/AE;
   the exact sender was persisted and hot-updated without restarting either
   service. Audit: `.runtime/orthanc-sender-correction-20261028T125441Z/`.
   The user subsequently enabled HTTP bind `["0.0.0.0"]`, remote access and a
   UFW8044 rule, retaining authentication, and confirmed Explorer works from
   the Mac. The main 3DScan HTTP listener remains loopback9010.
   Receiving through Tailscale is deferred. The user requested segmentation
   return qualification: listener `osirix-mac.local:11112`, AE
   `OsiriX-Mac`. Twelve synthetic CTs plus one two-mask binary SEG passed
   C-STORE from the separate 8043 archive. After an accidental deletion/resend,
   the user confirms SEG appears as usable ROIs, with different database
   presentation from native OsiriX ROIs. Basic receive/display compatibility
   passes; exact clinical alignment and editing/resave remain user qualification.
   Prototype audit:
   `.runtime/validation/osirix-seg-return-20261028T133800Z/`. See
   `docs/ORTHANC.md` and the newest validation section before operating it.
   Saved-revision export is now implemented and qualified in the checkout:
   per-structure binary SEG, exact full-affine/source/mask binding, archive/send,
   explicit same-SOP resend and restart-safe delivery receipts. The UI is native
   renderer independent. 1,071 backend and 495 web tests, both scratch builds,
   six compiled browser gates and real synthetic API→Orthanc→OsiriX delivery pass.
   highdicom 0.28.2 / pyjpegls 1.5.1 are hash-pinned and installed with explicit
   activation approval. The app now enables existing Orthanc modality `osirix`;
   frontend assets are live and idle 3DScan restarted (PID516779). Both lanes,
   CT-only policy, all 19 assets/private HTTPS index and prior GPU proof pass;
   aggregate counts remain 4 cases/18 revisions/25 jobs/6 reviews/4 image assets.
   Orthanc PID436143/configuration are unchanged, and journal priorities show
   no warnings/errors. No GPU or patient records were used for qualification.
   Audit/guarded rollback: `.runtime/dicom-seg-integration-20261028T150235Z/`.
   A source-grid rounding fix is now active: real synthetic dcm2niix stacks
   reproduce false export refusals from accumulated spacing rounding. All 1,077
   backend tests pass; integer mask gathering and strict voxel/mm limits remain.
   With explicit approval, idle 3DScan restarted (PID543504); both lanes and
   CT-only policy pass, with aggregate library counts unchanged. Orthanc
   PID436143 and frontend are unchanged. See the newest validation record and
   `.runtime/dicom-seg-grid-fix-20261028T184308Z/`.
   The user subsequently reports OsiriX cannot discover masks in the app SEG.
   A per-frame identification compatibility fix and encoding-version cache
   migration are active after explicit approval (backend `448009b`, PID572157).
   1,083 backend / 504 web tests and seven browser gates pass. Both lanes,
   CT-only policy, all 19 assets and private HTTPS index pass; aggregate counts
   remain 4 cases/22 revisions/39 jobs/10 reviews/4 image assets. Orthanc PID436143
   is unchanged, with zero warning/error journal priorities. No DICOM was sent
   during activation. Actual OsiriX display is still unconfirmed; refresh and
   explicitly export to generate the updated SOPs. Audit/guarded rollback:
   `.runtime/dicom-seg-per-frame-20261028T191145Z/`.
3. **Decisions waiting on the user:**
   - sign-in and an audit trail, needed before a second user
   - DICOM SR export and further export/viewer qualification
   - per-structure review status for the agreement metrics
   - frontend restructuring: stores, a router, typed API models
4. **Smaller follow-ups:**
   - precompress GLBs instead of gzipping per request
   - warm the agreement cache when a revision is committed
   - age review priority so segmentations cannot starve
   - finish 768 px workspace sizing outside the native 3D pane
   - surface Dice or the maximum surface distance for small local edits

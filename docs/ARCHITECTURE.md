# Architecture

3DScan is a local workbench for reviewing AI segmentation of CT scans. It imports DICOM, runs
TotalSegmentator on request, lets a reader correct masks by hand or with nnInteractive, keeps an
exact history of saved changes, and shows the result in a browser viewer. It is for research use
and complements a diagnostic viewer rather than replacing one.

[API.md](API.md) is the HTTP contract and data model. To run the software, see
[INSTALL.md](INSTALL.md), [CONFIGURATION.md](CONFIGURATION.md), [USER_GUIDE.md](USER_GUIDE.md),
[ORTHANC.md](ORTHANC.md), [ARCHIVES.md](ARCHIVES.md) and [DEVELOPMENT.md](DEVELOPMENT.md).

![3DScan system architecture](diagrams/architecture.svg)

`docs/diagrams/render_architecture.py` generates the diagram.

## Design rules

- **Masks are authoritative.** Binary masks on the source voxel grid are the only input to
  measurements, edits and exports.
- **Saved work is immutable.** Every change creates a new revision. Undo activates an earlier one.
- **Geometry is physical.** Positions are patient RAS millimetres with the full affine.
- **The browser renders, the server computes.** The server parses DICOM, runs inference and edits
  masks.
- **Expensive work is explicit.** Opening a scan does not segment it, and viewing never starts
  inference.

## Components

| Where    | Component            | Role                                                                                  |
| -------- | -------------------- | ------------------------------------------------------------------------------------- |
| Server   | API process          | `scan3d serve`: FastAPI, two lane threads, the janitor, CPU pools, built viewer files |
| Server   | TotalSegmentator     | A subprocess per task, started by the GPU lane in the job's interpreter               |
| Server   | nnU-Net worker       | The same kind of subprocess for a pinned nnU-Net model                                |
| Server   | nnInteractive worker | A long-lived supervised child with the warm model for one inference path              |
| Browser  | Main thread          | Vue 3 workspace, Cornerstone3D, vtk.js and Three.js rendering                         |
| Browser  | CT worker            | Web Worker that decodes slabs, holds the source and reslices frames                   |
| Optional | Orthanc              | A separate DICOM server, reached only over its REST API                               |
| Optional | DICOMweb archive     | Any archive reached over QIDO-RS, WADO-RS and STOW-RS                                 |
| Optional | DICOM nodes          | Receivers of direct sends by C-STORE                                                  |

### Code map

| Path                                                                    | Contents                                                |
| ----------------------------------------------------------------------- | ------------------------------------------------------- |
| `scan3d/api.py`, `api_*.py`, `api_models.py`                            | HTTP app, error mapping, routes, request bodies         |
| `scan3d/access.py`, `accounts.py`, `identity.py`                        | Route rules, accounts, sessions, audit, password hash   |
| `scan3d/service.py`, `store.py`, `config.py`, `__main__.py`             | Lanes, revision building, SQLite, settings, the CLI     |
| `scan3d/janitor.py`, `activity.py`, `disk_usage.py`                     | Delete forever, Clear caches, requests in flight, sizes |
| `scan3d/dicom.py`, `enhanced_mr.py`, `modalities.py`, `image_assets.py` | Series validation, conversion, image identity           |
| `scan3d/segmentation*.py`                                               | TotalSegmentator runs, task catalog, result merging     |
| `scan3d/nnunet_models.py`, `nnunet_worker.py`, `cas_net.py`             | Pinned nnU-Net models, CAS-Net and their worker         |
| `scan3d/sc_crop.py`                                                     | Spinal cord box for the Spinal Cord Toolbox models      |
| `scan3d/review.py`, `relationships.py`, `direct_edit.py`, `scissors.py` | Correction sessions and deterministic edits             |
| `scan3d/interactive*.py`                                                | nnInteractive engine, worker, batching, prompts         |
| `scan3d/inference_paths.py`                                             | Inference paths: discovery, binding, child environment  |
| `scan3d/imaging.py`, `surfaces.py`, `surface_glb.py`                    | Masks, measurements, slice geometry, GLB surfaces       |
| `scan3d/review_volume.py`, `volume_mask_union.py`                       | Volume and frame transport, display unions              |
| `scan3d/agreement.py`, `portable_export.py`, `slicer_io.py`             | Agreement metrics, offline HTML, Slicer exchange        |
| `scan3d/coronary.py`, `vessel_geometry.py`, `ffr_physics.py`            | Research CT-FFR job, centrelines and areas, pressure    |
| `scan3d/orthanc*.py`, `dicom_*.py`, `osirix_roi*.py`                    | Orthanc connector, DICOM SEG and view, OsiriX ROIs      |
| `scan3d/archive/`                                                       | DICOMweb client, imports, STOW exports, C-STORE sends   |
| `web/src/App.vue`, `web/src/components/`, `web/src/lib/use-*.ts`        | Workspace shell, panels and composables                 |
| `web/src/components/AuthGate.vue`, `web/src/lib/session.ts`             | Sign-in form, overlay and session state                 |
| `web/src/components/CaseLibrary.vue`, `web/src/lib/library.ts`          | The Library: search, rename, Trash and storage          |
| `web/src/lib/ct-volume.ts`, `ct-worker.ts`                              | Source session and worker                               |
| `web/src/lib/cornerstone-*.ts`, `vtk-*.ts`                              | Slice review, MPR and the native 3D view                |
| `web/src/lib/scene.ts`, `ct-volume-renderer.ts`, `gpu-ct-plane.ts`      | The Three.js renderer                                   |
| `web/src/lib/volume-memory-budget.ts`, `portable-viewer.ts`             | Client memory ledger, offline viewer                    |

## Data flow

```mermaid
flowchart TD
  INPUT["DICOM folder, ZIP or local path"] --> IMPORT["Copy originals and index series"]
  ARCHIVE["Orthanc or DICOMweb series snapshot"] --> COPY["CPU import job<br/>Verify identity, publish originals"]
  COPY --> SELECT
  IMPORT --> SELECT["Select one series"]
  SELECT --> OPEN["Queue process job"]
  OPEN --> CONVERT["Validate and convert to a reference NIfTI"]
  CONVERT --> IMAGE["Image-only revision<br/>No segmentation starts"]
  IMAGE --> VIEW["Review images and render CT"]
  IMAGE --> SEGIMPORT["Upload DICOM SEG<br/>Source and exact-grid checks"]
  IMAGE --> REQUEST["Request segmentation<br/>Task, options, license and inference path checks"]
  REQUEST --> INFER["Reuse cached inference or run TotalSegmentator"]
  SEGIMPORT --> MASKS["Native binary masks"]
  INFER --> MASKS
  MASKS --> ARTIFACTS["QC, voxel measurements and surfaces<br/>Unchanged results come from cache"]
  ARTIFACTS --> SAVE["Commit a new revision"]
  SAVE --> VIEW
  VIEW --> METHOD{"Correction method"}
  METHOD --> DIRECT["Direct paint, erase, 3D scissors<br/>CPU lane"]
  METHOD --> AI["nnInteractive prompts<br/>GPU lane, supervised worker"]
  DIRECT --> CANDIDATE["Candidate masks<br/>Overlap policy and change summary"]
  AI --> CANDIDATE
  CANDIDATE --> PREVIEW["Linked candidate preview"]
  PREVIEW -->|"Keep editing"| METHOD
  PREVIEW -->|"Discard"| VIEW
  PREVIEW -->|"Save"| ARTIFACTS
  SAVE -->|"Undo: activate an earlier revision"| VIEW
```

- **Import** copies originals into the case, and pydicom indexes every series. Unsupported series
  stay visible with the reason.
- **Prepare** is a `process` job. It runs dcm2niix on exactly one series, unless
  `converted/<sha256>/` already holds it, then checks the geometry and creates an image-only
  revision. Scan-region and timing labels never select frames, change intensities or choose a task.
- **Segment** is a `segment` job. It runs one TotalSegmentator task on the active revision at full
  resolution and keeps every nonempty output. Its cache key covers the image hash, options, package
  versions, wrapper code and device request.

## Store and revision model

All state lives under `SCAN3D_DATA_DIR`:

```text
scan3d.sqlite3           Records, in WAL mode
worker.lock              Held by the process that runs jobs
maintenance.lock         Shared by backup, demo, import and optimize; exclusive to delete files
cases/<case>/
  dicom/                 Originals from uploads and local imports
  sources/<job>/         Originals from Orthanc or DICOMweb, with source-import.json
  converted/<sha256>/    Reference NIfTI and conversion log
  inference/<key>/       TotalSegmentator runs and their manifests
  revisions/<id>/        revision.json, model.glb and on-demand exports
  refinements/, corrections/, reviews/, seg-imports/, dicom-exports/
  dicom-view-captures/   Retained 3D view captures, each named by its SHA-256
  coronary/<key>/        Coronary analysis: result.json, then complete.json
  uploads/               Slicer corrections as received
  synthetic/             The demo phantom
surface-cache/, review-cache/, analysis-cache/, agreement-cache/, cinematic-cache/
orthanc-staging/         Archive downloads in progress; leftovers go at each start
import-uploads/          Staged browser uploads, removed after a day of inactivity
```

SQLite holds JSON records in `cases`, `image_assets`, `revisions`, `jobs` and `review_sessions`. A
record is committed only after its files are complete. Records store absolute paths.

- **Image assets** describe one source image and never change. Child revisions must keep the same
  source image.
- **Added series.** A series added from Orthanc or a DICOMweb archive keeps its own source record in
  `added_sources`, so an export binds to that series' files. It must come from the case's own study.
- **Revisions** form a tree through `parent_id`.
- **Structures** are a revision's segments, each with a stable ID and a private mask file. Masks are
  independent, so overlaps are preserved.
- **Activation.** A case has one active revision. `Store.commit_revision` publishes a revision,
  activates it and completes its job in one transaction, guarded by the expected active revision.
  Activate and prepare send the active revision the page shows, so a stale page cannot replace a
  newer save.
- **Replacing results.** A segmentation run replaces every structure from its task, plus any other
  structure, reviewer-added or not, whose anatomical name matches ignoring case, underscores and
  whitespace, so `total_v3:liver` replaces `total:liver`. Voxels are never subtracted. A
  conflicting result fails before anything is published.
- **Comparing models.** A pinned nnU-Net model's structures match only that model's, so
  `mrsegmentator:liver` and `total:liver` stay side by side. Its run has the source `nnunet_model`.
  A pinned model can run another first in the same worker: FLAMeS runs HD-BET and sets every voxel
  outside its brain mask to zero. Both models' weights are checked before the run.
  A pinned model can also crop to a helper's box first and let its folds vote: the Spinal Cord
  Toolbox lesion model runs on the box the sc-crop detector finds around the cord, keeps the voxels
  at least three of its five folds mark, and puts the mask back into the full image. The detector
  runs in the worker on the CPU through onnxruntime.

Other tables:

- **`case_summaries`** holds each case without its file paths, for the case list. Triggers drop a
  summary on any write to `cases`, so a writer that skips it makes the list read the full record,
  never a stale one.
- **Accounts** live in `users`, `sessions`, `one_time_codes`, `last_seen`, `case_access` and
  `audit_events`, with no foreign keys and no owner column in `cases`.
- **`case_opened`** orders each account's case list. A **`trash`** row hides its case, and nothing
  on disk moves until Delete forever.

Each `Store` keeps one idle connection open, because closing the last connection to a WAL database
checkpoints it and slows every write.

A **backup** leaves out sessions, links, the folder of a case deleted forever, unnamed surface
assets, the caches that rebuild (review, analysis, agreement, cinematic and contour), staged
uploads and archive staging. [USER_GUIDE.md](USER_GUIDE.md#back-up-the-case-library) says how to
restore one.

**Delete forever** (`scan3d/janitor.py`) runs in steps that can each run again, so a purge resumes
at the next start:

1. `Store.purge_case` deletes every row of the case in one transaction and marks its `trash` row
   `purging`. Triggers then refuse any insert for a `purging` or `purged` ID, from any build.
2. The janitor, a thread of the API server once it holds `worker.lock`, takes an exclusive
   `maintenance.lock`, removes the case's folder, uploads and review-cache entries, and empties the
   write log.
3. When no job runs, no request is in flight and no review volume is being written, it removes
   orphaned review entries, the caches rebuilt on a miss and unnamed surface assets. It empties the
   log again and marks the case `purged`, and the idle GPU lane drops its warm engine.

What the janitor cannot remove keeps its cases `purging`, tried again each minute. Each start also
removes any folder of a purged case that came back.

## Jobs and lanes

Jobs are SQLite rows. `Service.start` locks `worker.lock` and runs one thread per lane, so one
process per data directory executes jobs. [API.md](API.md#job) lists the job kinds.

| Lane | Job kinds                                                                                |
| ---- | ---------------------------------------------------------------------------------------- |
| GPU  | `segment`; `review_predict` that runs nnInteractive                                      |
| CPU  | Every other kind, including `review_predict` for direct edits or relationship-only Apply |

- GPU jobs never overlap, whichever GPU they use, which keeps TotalSegmentator's shared settings and
  first-time weight downloads safe. The lane runs even when inference uses the CPU device.
- CPU work never waits behind a segmentation. Review jobs run before queued batch work in each lane.
- Exports and `coronary_analysis` complete their own jobs and commit no revision.
- A correction saved during a segmentation stays active. The segmentation fails at commit, and a
  retry from the current revision reuses its cached inference.

**Restart.** Running jobs fail as `Interrupted`, and draft sessions return to an editable state.
Queued jobs stay queued, except DICOM exports, file exports included, which fail locally because a
send must be requested explicitly.

**Cancellation** stops a queued job at once and a running job at its next check, but a send already
started finishes. Trash cancels a case's jobs this way, and a job that starts afterwards stops at
its first check.

**Requests in flight** are counted in memory (`scan3d/activity.py`) by `SessionGate`, for each
request it admits, in all and per case, so a purge waits for its case and the shared cache steps
for everything. Public routes and the API docs are not counted, and Restore and Delete forever
count in all only.

A request that makes a case counts it before its row exists, and a volume build counts its case
until its thread ends.

## Inference

`SCAN3D_DEVICE` selects the device on the default [inference path](#inference-paths), as
[INSTALL.md](INSTALL.md#choosing-how-ai-runs) describes. A GPU request never falls back, and a
TotalSegmentator report that shows CPU execution after one is rejected.

`scripts/check_gpu.py` writes a proof file, which [`/api/health`](API.md#system-routes) reports
without loading PyTorch.

The API process never imports PyTorch. Before it runs the model, a `segment` job checks its device
in a short-lived child, `python -P -m scan3d.segmentation`, from the job's interpreter, and no
answer within 5 minutes fails the job. A server without AI packages still serves the viewer and
direct editing.

### The nnInteractive worker

The first prediction starts `python -P -m scan3d.interactive_worker` from the job's interpreter. It
keeps the model and image warm between Apply actions and exchanges newline-delimited JSON over an
inherited socket pair. It is a separate process so a GPU driver fault fails one job, not the server,
and because only process exit returns all GPU memory.

- **Cancellation** is cooperative first. After 10 seconds its process group is killed.
- **Failure.** A crash, a GPU or model failure, or 600 seconds without progress
  (`SCAN3D_NNINTERACTIVE_STALL_SECONDS`) fails that job only. The next prediction starts a fresh
  worker.
- **Restore.** A request may name the stored result of its first Apply steps. If its hash and the
  worker's model, card and build match, a cold worker loads it and registers those marks with no
  forward pass; otherwise it replays from the original mask
  ([API.md](API.md#what-a-run-records)). An older worker ignores the field, so the protocol stays 1.
- **Exit.** The worker exits before a `segment` job, at shutdown, if the API process dies, and when
  a request names another [inference path](#inference-paths). The next worker starts only after
  that exit, so one inference process exists across all GPUs. The job reports
  `Switching the AI worker to <label>`.

The model is pinned, and its checkpoint hash is verified on load. Requests never download it.

### Inference paths

A PyTorch build drives one vendor's GPUs, so with two vendors' cards a choice of card is a choice
of interpreter. An inference path is that pair: an interpreter and a device.
`scan3d/inference_paths.py` holds the rules, [API.md](API.md#inference-paths) has the rows, requests
and refusals, and [INSTALL.md](INSTALL.md#add-a-second-gpu) adds a path.

- **`default`** is the API's own interpreter with `SCAN3D_DEVICE` and the proof in
  `SCAN3D_GPU_PROOF`. It is implicit and always available: a stale proof earns a caution and blocks
  nothing.
- **An added path** is one proof file, `<SCAN3D_INFERENCE_PATH_DIR>/<id>.json`, written by
  `scripts/check_gpu.py --path ID` from inside that interpreter. The file is the whole
  registration, so adding one needs no restart, and moving it out removes the path.

**Discovery.** `rows()` reads the folder on every call, with no PyTorch and no child process, so
opening a panel never touches a busy card. `available` never means a scan will fit: the device
check at job start is the live authority.

**Frozen at enqueue, checked at execution.**

- `binding()` refuses an unknown or unavailable path with no job row. The job stores
  `{id, label, identity}` at its top level, and `require()` compares that identity with the current
  proof as the job starts.
- An added path never re-routes: a job runs on its card or fails naming it.
- `InferencePath.confirm` must see the proven card and build, before TotalSegmentator starts and
  after an nnInteractive prediction, where a mismatch keeps no result.

**Cache identity.** An added path's `device_request` becomes `<id>|<device>|<gpu_name>`, so a run on
another card never reuses a saved result. The default path passes nothing new, so adding or
removing a path leaves its key alone.

**Environment.** A child of an added path takes its environment only from
`InferencePath.environment()`, which withholds the variables listed in
[CONFIGURATION.md](CONFIGURATION.md#ai-compute). Children of `default` inherit the API's untouched.
Discovery requires the API's engine versions on every path, so weights, the task catalog and the
version recorded in a DICOM SEG stay true.

## Corrections

A review session binds a case, a base revision and one target structure. Its method is fixed when
it starts:

- **Direct** edits (paint, erase, 3D scissors) are deterministic, run on the CPU lane, and replay in
  order from the immutable source mask.
- **nnInteractive** takes include and exclude prompts on the GPU lane: points in RAS, and boxes,
  scribbles and lassos on native slices. It can also refine a whole mask.

Upstream's refinement resets every prompt channel, so the adapter registers the draft's marks again
after it, and they keep guiding later runs. It can also sample at random, so each Apply step seeds
NumPy's generator from the draft and the step's place, then restores the generator.

Prompts autosave and never run inference. **Apply** queues one job for all pending marks and
produces an immutable candidate, `review-<session>-<generation>`. It works on the read-only image,
mask and frame routes and never appears in revision history.

**One open draft per target.** The create route returns an open draft for the same revision,
structure and method rather than start a second. A save replaces the whole prompt list, so a page
with unsaved changes refuses a draft that holds work, and a run that opened a draft stops there,
because its marks have not been seen.

Generation checks never read the account, so a stale page gets 409 across accounts as across tabs.
A draft stays on the card of its first published result unless re-run with `replay: true`, so a
saved correction ran on one card. [API.md](API.md#choosing-a-path) has the rules.

**Relationships** define who owns overlapping voxels. Display groups, names and geometric
intersection never authorize a removal. [API.md](API.md#segment-relationships) has the rules.

**Save** needs a current candidate that changes masks or relationships, with the base revision
still active. It rechecks the hashes of the image and every affected mask, then commits one
`corrected` revision with the session in one transaction. The operation record keeps the ordered
prompts, hashes, model identity and per-mask changes.

## Measurements and surfaces

Each new revision measures its masks, builds missing surfaces and commits. Results are cached, so a
correction rebuilds only what changed.

- **Volume** is the nonzero voxel count times `|det(affine[:3,:3])|`, divided by 1000 for mL.
- **QC** flags disconnected components and masks that touch the scan boundary.
- **Surfaces.** `surfaces.py` contours masks with VTK through the full affine into quantized GLBs
  with gzip siblings.

## Coronary analysis

Research use only. A `coronary_analysis` job (`scan3d/coronary.py`) on the CPU lane:

1. Loads the lumen, aorta and myocardium masks of a saved revision on its CT grid, never resampled.
2. Finds one root per patch where the lumen meets the aorta, plus any clicked ostium points. A
   clicked point more than 5 mm from the aorta, partway along lumen an aortic root already reaches,
   fails the job: it would split that vessel. A point where a vessel starts is kept, since lumen
   that only touches an aortic tree is in the same component.
3. Traces centrelines by TEASAR on the lumen's voxel graph in RAS mm (`vessel_geometry.py`). Lumen
   within 1.5 mm of the aorta never starts a trace, and a path's root end is covered like a blunt
   tip, so the rim of the face where the aorta cuts a coronary is not traced as stub branches. The
   trade-off: a side branch about 1 mm across leaving within about 1.5 mm of the aortic face can be
   lost (9 of 288 synthetic cases).
4. Measures area every 0.5 mm from the voxels each station owns, averaged over 1 mm.
5. Fits a reference diameter, finds lesions and solves steady hyperaemic pressure in a reduced-order
   model (`ffr_physics.py`). Ostium, tip and junction zones are held at the nearest trusted area,
   with spur runs that touch them; an isolated spur run is a throat and is kept. A branch with no
   trusted station takes its reference area. Runs under 0.7 of the reference less than 1 mm apart
   are one lesion. Solves 16 more times, each corner of lumen ± half a voxel, resting flow ±25%,
   TCRI and flow-split exponent, for each FFR's band.
6. Applies the reading rules and writes `result.json`, then `complete.json`. Lesions whose reference
   diameter at the throat is under 1.8 mm are not listed, but their loss stays in the solve.

The folder's key covers the code, image, masks and inputs, so a revision with the same masks reuses
it, and each read checks it again. It sits in the case folder, so backups keep it and Clear caches
does not remove it. [API.md](API.md#coronary-analysis) has the routes and the result.

## Geometry rules

- **Coordinates.** Every API point, seed and bound is patient RAS millimetres. Arrays keep
  nibabel's i, j, k order, and transport slabs are little-endian and i-fastest.
- **Affine.** Code uses the complete 4×4 matrix. Oblique and reflected grids are normal inputs.
  Brushes, display margins and surface distances use the affine metric.
- **Native drawings.** Prompts use a native axis, a slice index and continuous u, v voxel
  coordinates, whichever renderer produced them. A drawing gets that authority only when the whole
  viewport lies on one integer source plane. A patient-aligned rectangle on an oblique plane is
  never reinterpreted as a box.
- **Gestures.** A change of source, target, tool, camera or slice cancels an active gesture.
- **Authority.** No display raster, resampled preview or pooled union becomes a source mask or a
  measurement grid.
- **Other frames.** GLB world coordinates are metres, with scene = (−R, S, A) / 1000. Cornerstone
  and Slicer use LPS.
- **DICOM exchange.** SEG import and export prove that every source pixel maps to one integer
  native voxel, within 0.05 voxel per axis and 0.05 mm at every slice corner, and never resample.
- **Shear.** The viewer handles sheared affines, but the DICOM importer refuses a stack whose
  slices step off the slice normal by more than 0.05 mm. Gantry-tilted CT is rejected unless
  reconstructed onto a corrected grid.

## Viewer

Two viewers share one source and linked crosshairs: slice review (`CTReviewRail.vue`,
`CornerstoneCTView.vue`, `CornerstoneMPRView.vue`) and 3D (`ModelViewport.vue`,
`CornerstoneVolumeView.vue`).

### Source transport

The browser never parses DICOM. `LocalCTSession` hands `image-volume` gzip chunks to the CT worker,
which holds one CPU copy. A preview loads first, and the validated full volume replaces it.

Until then the viewer requests an exact frame through `review-frame` after 120 ms of idle. Preview
samples never supply the exact HU readout.

### Slice review and MPR

Cornerstone3D is the only 2D renderer, and it requires WebGL. An orthogonal source loads once into
a shared ImageVolume serving three orthographic viewports. Preview, sheared and over-budget sources
use stack viewports fed by worker-resliced frames.

### Native 3D: Cornerstone3D and vtk.js

Full orthogonal CT defaults to a `VOLUME_3D` viewport in Cornerstone's rendering engine, using
vtk.js ray casting. `acquireCornerstoneSource` gives each CT session one image volume, texture,
context and memory reservation, shared by MPR and 3D, because a separate renderer would hold a
second GPU copy of the CT.

- **Surfaces** load from quantized GLBs into vtk actors. Over a drawn CT, see-through surfaces are
  peeled into four depth-ordered layers, or two while the camera moves or memory is short, plus one
  weighted layer for faces beyond. The CT ray merges them by depth (`vtk-surface-layers.ts`), and
  opaque surfaces and the CT plane end it.
- **Solid fallback.** Without WebGL 2 float targets, 13 texture units, the memory or a working
  merge, surfaces stay solid and the view says why. A failed merge holds until the viewer is
  rebuilt, and a tile size the memory refuses until the size changes.
- **Limit to segments** (masking) uses one cropped OR union of the chosen structures, built on the
  server, with an optional margin of 0–5 mm. Crops over the limit are pooled, which can grow a
  boundary by a grid cell. An empty, loading or failed mask hides CT rather than showing the wrong
  tissue.
- **Color segments** tints the CT before lighting from a label union, largest structure first.
- **Review** can show the CT volume. Masking there uses the saved revision, not the candidate;
  colouring and Cinematic heart stay off because they would follow it. While the reader
  marks on the CT plane, the crop plane on its axis moves to the slice to cut away the camera's
  side, because vtk.js volume shaders take six clipping planes.
- **All three** planes add a plane layer for each other native axis through the same point, in
  review too. Each is made on first use and keeps its raster and memory charge until the viewer is
  rebuilt, as the main plane does. One refused by memory or its slice limit hides only itself.
  While the reader marks on the CT plane, a clipping plane keeps only their side beyond it, as the
  cut does for the volume, and review clicks treat them as opaque.
- **Volume point** marches the click's ray on the CPU through the shared ImageVolume
  (`volume-ray.ts`), because vtk.js cannot pick a shared GPU volume. It samples trilinearly every
  half smallest spacing and takes the first sample where opacity, accumulated as vtk.js does,
  reaches 0.5, within the clipping, near and far planes and before opaque surfaces and CT planes.
  A mask hides samples by the shader's own nearest-voxel test on a one-bit CPU copy of its crop.
- **Volume point limits.** See-through surfaces neither stop nor dim it, and gradient opacity and
  lighting are not modelled.
- **Surface point** is refused when visible volume or a CT plane lies in front of it by more than
  the coarsest voxel spacing or 1 mm, whichever is larger.

Masking and colouring patch exact shader anchors through a factory local to one actor. That bridge
checks the installed versions (Cornerstone3D 5.13.1, vtk.js 36.4.1) and throws on a mismatch.
[DEVELOPMENT.md](DEVELOPMENT.md) has the rules for shader changes.

### Three.js renderer

Three.js loads lazily, for sheared grids, MRI and
[offline HTML](USER_GUIDE.md#portable-offline-html), which embeds only the chosen layers and makes
no network requests. `AnatomyScene` owns one WebGL context, the surfaces, a native CT plane and the
prompt markers.

Both renderers use the 31 default 3D Slicer presets plus a Cinematic heart preset, and they differ
from each other and from 3D Slicer. vtk.js draws one linear gradient-opacity ramp, so `uCT-Skull`,
whose curve falls and rises again, is not offered, exports refuse it, and Cornerstone refuses a
restored view that names it.

### Memory budget

`volume-memory-budget.ts` keeps one reservation ledger for all viewers and reserves before
allocating. Its figures are estimates, not measurements of driver memory. A request over the limit
fails before allocating, and the viewer explains it and offers Retry.

| Limit                             | Value                                         |
| --------------------------------- | --------------------------------------------- |
| All viewers, on the ledger        | 3 GiB, or 256 MiB with 4 GiB of RAM or less   |
| CT worker's copy                  | 512 MiB, or 128 MiB with 2 GiB of RAM or less |
| Source preview                    | 192 voxels per dimension                      |
| Selected masks in the worker      | 64 MiB for preview, 192 MiB for full          |
| Native CT plane, mask, label crop | 8 MiB each; a mask adds 1 MiB of bits           |
| Three.js CT and mask atlas        | 512 MiB                                       |
| MIP and MinIP slab                | 200 mm                                        |
| Native **Color segments**         | 64 structures                                 |

## Archive connectors

Both are optional. Orthanc is off unless `SCAN3D_ORTHANC_URL` is set and is reached only over its
REST API ([ORTHANC.md](ORTHANC.md)). A DICOMweb archive is off unless `SCAN3D_DICOMWEB_URL` is set
([ARCHIVES.md](ARCHIVES.md)).

- The backend holds the credentials. Identity fields travel in POST bodies, and connector responses
  use `Cache-Control: no-store`.
- An import binds an immutable snapshot of one series. A CPU job downloads it into staging and
  checks its DICOM identities, and Orthanc's file hashes, before publishing the originals.
  Interrupted transfers never resume automatically.
- The launch route finds the library's matching import, whoever made it, but creates nothing.

## DICOM SEG and other exchange

Each DICOM export is a CPU job whose kind names its delivery: through Orthanc (`orthanc_*`), to the
DICOMweb archive by STOW-RS (`archive_*`) or to a DICOM node by C-STORE (`direct_*`). A SEG file is
an `orthanc_export` and a view file a `view_file_export`. A file, an archived and a sent SEG of the
same masks are one instance.

**SEG export** turns chosen structures of a saved revision into one binary SEG instance. It never
changes the revision, and it reads source headers, never source pixels. A manifest pins the hashes
before any upload, and a ranked export resolves overlaps in that SEG only. A segment is automatic
only while its mask is the model's own write-once output file, the evidence the agreement report uses.

Orthanc keeps and sends the same bytes to allowlisted modality IDs, and a DICOMweb archive must
return every stored instance on read-back ([ORTHANC.md](ORTHANC.md#send-a-saved-segmentation-back),
[ARCHIVES.md](ARCHIVES.md#archive-results)).

**View export** turns one capture of the 3D view into a Secondary Capture series in the source
study. `orthanc_view_exports.py` subclasses the SEG manager. The instance is a picture with no frame
of reference, and nothing measures from it.

**OsiriX ROI export** is a second format of SEG export, through Orthanc only. `osirix_roi.py` writes
one native brush report per source slice that a selected mask touches, and `osirix_roi_exports.py`
checks that they all land in the source study.

**SEG import** takes a local upload and needs no archive. It accepts only binary, uncompressed
segmentations on the exact native grid, after source-identity checks, and appends them as a new
unreviewed revision. Anything that would need thresholding or resampling is refused.

**Slicer exchange** exports a ZIP with the reference image, `segmentation.seg.nrrd` and a stable ID
mapping, and imports corrected segmentations after geometry and identity checks. A segment with
unchanged voxels keeps its saved mask file.

## Modalities

`SCAN3D_MODALITIES` lists what an installation offers, and the default is CT. `scan3d/modalities.py`
holds the adapters. Other modalities have none.

- **CT** means classic single-frame CT storage with one acquisition, phase and reconstruction per
  series. Enhanced or multi-frame and localizer series are refused.
- **MR** (`SCAN3D_MODALITIES=CT,MR`) is experimental and off by default, for one homogeneous static
  anatomical MR series or one complete static Enhanced MR instance. Values are `signal` units,
  never HU, and its task catalog is separate. Conversion uses `dcm2niix -m n -p n -l o -x i`, so
  sequences are never force-merged.

## Security boundaries

`SCAN3D_ALLOWED_HOSTS` restricts `Host` headers, and mutations from another browser origin are
refused. Files are written with umask 077, and public records omit filesystem paths. An inference
path's proof names an interpreter that the app will run, so protect its folder as the checkout.

**Sign-in.** `off` is allowed only on loopback, and the app refuses to start otherwise
([CONFIGURATION.md](CONFIGURATION.md#sign-in)). `SessionGate` in `scan3d/access.py` finds the route
the router will pick and applies its default-deny rule before the route reads a body, in both
modes. [API.md](API.md#access-rules) lists the rules, and
[DEVELOPMENT.md](DEVELOPMENT.md#project-rules) says how to add one.

- **One shared library.** Any account opens any case that exists and is not in Trash. A case route
  needs the case to exist, so no handler makes a folder for an ID that names none.
- **Admin rights are checked again under the write lock**, by `Store.purge_case` and by each People
  change in `Accounts`. The last active admin cannot be demoted or disabled, and nobody changes
  their own role or disables themselves.
- **Owners.** An owner row records who added a case, shown as "Added by". Work with nobody signed
  in goes to the oldest admin not disabled, as does a case with no owner when an account is added
  or at startup in password mode.

**Sessions and passwords.** Only the SHA-256 of a session token is stored. Passwords use scrypt,
with n=2^17, r=8 and p=1. `last_seen` records each sign-in, set-password and session slide, because
session rows go at sign-out.

**One-time links** ([API.md](API.md#people-routes)). `one_time_codes` keeps only the SHA-256 of each
code, at most one per person. Disabling or demoting the maker deletes their links in the same
transaction. Redeeming uses sign-in's hashing slots under a throttle key of its own, and checks the
link again under the write lock.

The server knows no public address, so the admin's page builds the link from its own, with the code
in the fragment, which no request, log or Referer carries. The page that opens it removes the code
from the address before any request and sends it only in the redeem body.

**Open tabs.** Each session read, every 60 seconds and on focus, also reads the open case. A tab
closes the case only on `Case not found`, never on a lost connection or a server error.

**Audit.** `audit_events` is append-only, enforced by triggers. Rows hold IDs and counts, never a
label, search term or typed username. A row means a request was allowed to start, and a failed write
fails the request. The actions are:

- Sessions: `session.sign_in`, `session.sign_in_failed`, `session.sign_out`,
  `session.sign_out_others`, and `user.link_used` with `session.sign_in` when a link sets a password
- People, each written with the change and the person as target: `user.invite`, `user.invite_link`,
  `user.reset_link`, `user.make_admin`, `user.make_member`, `user.archive_on`, `user.archive_off`,
  `user.disable`, `user.enable`, `user.sign_out_everywhere`
- Administration: `user.add`, `user.password`, `case.adopt`, `license.change`, `cache.clear`
- Cases: `case.import`, `case.open` (once per session and case), `case.rename`,
  `case.purge_password_failed`, and `case.trash`, `case.restore` and `case.purge`, each written with
  the change
- Archive and export: `archive.query`, `archive.launch`, `export.slicer`, `export.html`,
  `export.seg`, `export.seg_file`, `export.view`, `export.view_file`

**Network.** 3DScan runs no DICOM listener and sends no scan to a model or license host. It makes
only these outbound connections:

- Orthanc's REST API, when `SCAN3D_ORTHANC_URL` is set
- the DICOMweb archive by QIDO-RS and WADO-RS, and STOW-RS unless `SCAN3D_DICOMWEB_STORE=false`,
  when `SCAN3D_DICOMWEB_URL` is set
- DICOM nodes by C-STORE, and C-ECHO from `scan3d archive-check`, when
  `SCAN3D_DICOM_DESTINATIONS_FILE` is set
- TotalSegmentator's license check when a key is saved, and its weight download the first time a
  task runs
- the nnInteractive and nnU-Net model downloads, only when `scripts/download_interactive_model.py`
  or `scripts/download_segmentation_model.py` runs

DICOM sends are unencrypted unless a destination sets `tls`, and Orthanc and DICOMweb traffic is
unencrypted on an http URL. DICOMweb credentials need https unless the archive is on this machine,
and search terms reach the archive's own logs.

## Known limitations

- A case in Trash keeps its disk space until Delete forever, and importing it again from an archive
  makes a second copy.
- Delete forever is an ordinary file delete, not secure erasure. Backups, archives, nodes sent to
  and browser caches keep their copies, and restoring an older backup brings the case back.
- A revision records no reviewer and a review status no author, and the audit log has no viewer.
- The service does not scale out. [INSTALL.md](INSTALL.md#what-is-not-supported) lists unsupported
  platforms and setups, and [Choosing how AI runs](INSTALL.md#choosing-how-ai-runs) the untested
  GPU routes.
- Native 3D surfaces cast no shadows into the volume.

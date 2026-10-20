# 3DScan

Local chest–abdomen CT reconstruction with TotalSegmentator, linked CT review,
in-browser nnInteractive corrections, revisioned mask refinement, optional Slicer
exchange, and offline 3D HTML export.

Patient data, model weights, environments and generated artifacts are excluded
from version control. The synthetic demo is labelled throughout and does not run
or validate TotalSegmentator.

## Run

Install the native prerequisites on CachyOS:

```bash
sudo pacman -S --needed rocm-hip-runtime rocminfo rocm-ml-libraries hipsparselt rocm-smi-lib
paru -S dcm2niix
```

Provision the isolated environment and frontend:

```bash
scripts/bootstrap.sh full
cd web
npm ci
npm run build
cd ..
scripts/check_gpu.sh
scripts/run.sh
```

Open **http://127.0.0.1:9010**. The production build and API share one origin.
See [setup and GPU diagnostics](docs/SETUP.md) for native ROCm details, dependency
pins, allowed hostnames and private access. `scripts/bootstrap.sh core` installs
basic CT review/demo tools without the inference packages.

### Enable assisted correction

The full environment pins `nnInteractive==2.6.0` and
`nninteractive-client==2.6.0` alongside the existing ROCm stack. Download the
official model separately on the workstation:

```bash
source scripts/env.sh
python scripts/download_interactive_model.py
```

The downloader retrieves only model files; it does not upload scans. It pins
`MIC-DKFZ/nnInteractive`, model `nnInteractive_v1.0`, at revision
`3f308d751c00644e4fde6f09c600264b393b21b5` and verifies checkpoint SHA-256
`b3ac4421f85457bbd1aa0d87f5e67bcb7bc8e2ce6b824b6ac45077cc5d630ea9`.
The default folder is `.runtime/nninteractive/nnInteractive_v1.0`;
`SCAN3D_NNINTERACTIVE_MODEL` can point to another local copy of that pinned model.
The worker checks the checkpoint again before loading it.

The [engine code](https://github.com/MIC-DKFZ/nnInteractive) is Apache-2.0; the
[pretrained checkpoint](https://huggingface.co/MIC-DKFZ/nnInteractive/tree/3f308d751c00644e4fde6f09c600264b393b21b5/nnInteractive_v1.0)
uses **CC BY-NC-SA 4.0 (noncommercial)**. Review the checkpoint's license terms
when choosing it for a deployment. This adapter requires the selected R9700 and
native ROCm PyTorch; GPU failures are explicit. `GET /api/interactive/status`
reports package/model availability without initializing the GPU. Actual native
inference evidence is recorded separately in [validation](docs/VALIDATION.md).
Earlier point-at-a-time validation on the representative CT took 74 seconds for
the first candidate and 50 seconds for a warm incremental correction. Those
measurements precede batched Apply; current evidence is in the validation report.
Correction latency is not assumed to be near-instant.

## Review a case

1. Import a DICOM folder or ZIP, or create the clearly labelled synthetic demo.
2. Select one reconstruction, processing profile and contrast phase. Different
   phases remain separate; series warnings explain unsupported geometry.
3. Start processing. The single worker converts the selected DICOM series and
   runs standard-resolution `total`, plus `lung_vessels` for chest profiles.
   Every nonempty output is included; profiles select tasks without filtering anatomy.
4. Select anatomy in the list or model. Inspect its overlay in the three patient
   planes, adjust window/level and inspect HU. Review QC messages against CT.
   Open **Segments** in CT review to check multiple colored overlays. Checked
   segments remain on CT while selecting other anatomy; **Use selected segment**
   returns to following the active segment, and **Clear overlays** shows CT alone.
   Use the eye beside a section heading (for example, Bones) to hide/show the
   entire section; the arrow separately collapses its list. Section controls
   include all members even when searching and also work in portable viewers.
   The anatomy toolbar also offers Show all, Hide all and Show selected only.
   These apply to the complete case even while the structure list is filtered.
5. Open **Review segmentation** and select the target anatomy in CT review.
   To add anatomy absent from the initial result, choose **+ Add segment**, enter
   a name/group/color and **Create empty segment**. Mark its extent on CT, Apply
   and inspect, then **Save new segment**. This adds one structure in a new
   revision; its parent provides exact undo. Unsaved drafts recover on reload
   and can be discarded without adding an empty structure.
   Choose its relationship: **Independent** preserves overlaps, **Subregion**
   records a parent, and **Subdivision** explicitly selects sibling segments
   whose overlapping voxels may be transferred to the new segment.
   Use Include or Exclude with a point, box, scribble or lasso. The first mark
   starts or resumes a draft; add several marks before running the model.
   Drawings use the CT's native planes, preserving their true geometry even for
   oblique scans. Point prompts also work in patient-aligned views. Mark only a
   settled, full-resolution view; streamed exact detail is sufficient while the
   complete CT downloads. Undo/redo edits pending marks locally and autosave
   preserves the draft without running inference.
   You can also mark in the 3D view: **Surface point** locks picking to the
   review target, while **CT plane** supports points, brush, lasso and 2D boxes.
   Move its native slice slider, use **Face plane** for a straight-on view, and
   rotate with right drag while marking. A 3D point moves the linked CT crosshair
   for confirmation. New segments without a surface open the CT plane directly.
   Both views use the same pending prompts, Apply, comparison and Save controls.
6. Choose **Apply** once for the pending marks. Nearby marks share a planned
   model region; distant marks retain separate regions so none are omitted.
   Compare the original and working candidate and inspect voxel/volume changes.
   Add more marks and Apply again as needed. The last applied candidate remains
   visible while new marks are pending; saving waits for those marks to be
   applied. Applying does not change accepted masks or rebuild the 3D model.
7. Choose **Save correction** only after inspecting the candidate. This
   updates that structure and any explicitly selected exclusive siblings,
   including their measurements and surfaces. Inspect every affected volume
   and CT mask before saving; unrelated masks remain unchanged. The previous
   revision remains available in history for exact undo of all affected labels.
   Empty candidates cannot be accepted; a nonempty candidate must change masks
   or relationship metadata. **Discard draft**
   closes a proposal; saved prompts and completed candidates otherwise survive
   reloads and workstation restarts. Interrupted predictions can be retried.
8. The existing component/box operations remain available under **Refinement**.
   For external painting or contouring, expand **External correction with
   Slicer**, download its bundle and upload the edited `.seg.nrrd`. Preserve its
   segment tags: import checks segment identities, source CT hash and geometry.
9. Set the revision's review status and export PNG, Slicer files or a portable
   HTML viewer. HTML contains anatomy and voxel-derived measurements; CT review
   remains in this application. Export labels use pseudonymous case IDs.

nnInteractive uses a pretrained model for interactive inference; it does not
train or fine-tune model weights. The initial segmentation is loaded unchanged
until Apply requests a prediction. AutoZoom is enabled by default. **Start empty**
initializes a fresh proposal for the same anatomy without changing the saved
mask; changing this option resets the draft's applied history. Whole-mask
refinement is an explicit additional model stage and can take longer. The pinned
model supports native 2D boxes, rather than 3D bounding boxes.

Overlap replacement uses explicit anatomical relationships rather than display
groups or inferred names. Parent/subregion overlap is preserved; known lung-lobe
IDs provide suggestions that require selection. Policy-only changes reuse the
predicted target without rerunning nnInteractive. See
[segment relationships](docs/SEGMENT_RELATIONSHIPS.md).

An Apply batch is not identical to predicting after every individual gesture.
The pipeline records the exact Apply boundaries and settings for replay and
undo. One Apply action can require several model regions/internal forwards,
especially for distant marks, large drawings or whole-mask refinement.

Mask cleanup is anatomical and label-specific. Mesh simplification changes the
display surface; it does not change the voxel mask used for volumes. Named
bronchopulmonary territories and automated lesion characterization are outside
this version's scope.

The main 3D viewer also has a **CT plane** toggle. Choose a native i/j/k plane,
move its slice slider, or use **Face plane** to align the camera. It follows the
linked CT position, window/level and overlay selection while retaining normal
orbit and anatomy picking. Local overview frames appear during full CT preload;
once cached, moving the plane needs no workstation slice requests. Review uses
the same plane and requires an exact full-detail frame before accepting marks.

**Cornerstone3D is the default CT viewer.** It reuses the resident CT worker
through a custom image loader. The viewer dropdown is removed; rendering
failures provide fallback navigation and a retry action. The current bridge
provides GPU slice viewing and review integration; direct browser DICOM loading
and a full Cornerstone volume/MPR migration remain later stages. See
[the Cornerstone plan](docs/CORNERSTONE_PLAN.md) and
[the new-segment workflow](docs/NNINTERACTIVE_PLAN.md).

## Local scripts and recipes

```bash
source scripts/env.sh
python -m scan3d doctor
python -m scan3d import /absolute/path/to/dicom.zip --label "Teaching case"
python -m scan3d process CASE_ID --series SERIES_UID --profile chest_abdomen --phase venous
python -m scan3d include-all CASE_ID
python -m scan3d refine CASE_ID --revision REVISION_ID --structure total:liver --recipe recipe.json
python -m scan3d job JOB_ID
```

Local path imports are restricted to `SCAN3D_IMPORT_ROOTS` (colon-separated
absolute directories; the project directory by default). Set this explicitly
before importing scans elsewhere. File uploads need no local import root.

`include-all` restores previously filtered labels from a case's saved native
outputs without running inference. It creates a new revision, preserves current
corrections and leaves the prior revision available for undo. Missing original
outputs cause an explicit error.

Example recipe, in patient **RAS millimetres**:

```json
{"type":"erase_region","bounds_ras":[[-80,-60,-40],[-40,20,30]]}
```

Supported operations are `keep_component` and `remove_component` with
`seed_ras: [R,A,S]`, and `keep_region` and `erase_region` with `bounds_ras`.
Operations reject no-effect and whole-structure deletion results. Each successful
revision records its parent, operation, code version and voxel/volume changes.
Source hashes record the exact processing code and downloaded model checkpoints.

## Architecture and validation

- FastAPI + SQLite persists case, job, revision and assisted-review draft records. A filesystem lock
  enforces one worker across API and CLI processes; jobs interrupted by restart
  remain retryable and completed conversion/inference stages are reused.
- Assisted review shares that queue with TotalSegmentator and keeps one model
  and CT warm for successive Apply batches. Undo replays from the saved original
  mask using recorded batch boundaries when needed. Warm interactive GPU state is released before TotalSegmentator
  starts. Draft generations and active-revision checks prevent stale results
  from replacing another correction; accepted revisions record point history,
  model/checkpoint identity, device, timings and exact voxel/volume changes.
- nibabel/SimpleITK and full affine transforms preserve patient geometry. Meshes
  use glTF metres; API coordinates and measurements use millimetres/millilitres.
- Three.js owns one 3D scene with on-demand rendering. CT review starts with a
  small local navigation preview and selected overlay preloaded on case open.
  The full CT caches automatically in the background for fully local,
  full-resolution slicing and HU inspection. Until it is ready, navigation stays
  local and exact detail follows after movement stops. Downloads can be cancelled
  or retried. WebGL handles window/level locally.
  Segmentation, corrections and mesh generation remain on the workstation.
- Mask preparation shares one decode between measurements/QC and surface
  extraction, with memory-aware CPU concurrency (up to four structures by
  default). Content-addressed per-structure surfaces and measurements are reused.
  Detailed meshes are generated only for an explicit detailed export request.
- The live viewer keeps unchanged GPU buffers across revisions and fetches only
  replacement surfaces. Measurements are available while preparation continues;
  the current result remains visible. Offline HTML still embeds a complete model.
- Raw task outputs, reference CT, revisions and provenance are stored under
  `SCAN3D_DATA_DIR` (`data/` by default).

Existing cases can enable incremental assets without resegmentation:
`python -m scan3d optimize CASE_ID`. This preserves masks, measurements, review
status and revision identity. `SCAN3D_POSTPROCESS_WORKERS` defaults to 4, reduced
using a conservative available-memory estimate; `SCAN3D_MESH_THREADS` defaults
to 16 for VTK's available native CPU backend. Inference and browser drawing use
the GPU; exact voxel counting remains on the CPU after an end-to-end benchmark.

```bash
source scripts/env.sh
python -m pytest -q
cd web
npm run typecheck
npm test
npm run build
```

The tests cover oblique/reflected geometry, voxel-derived measurements, mask
operations, draft isolation/acceptance, cancellation and restart recovery,
revision undo, Slicer round trips, API jobs and browser interaction. Real-scan
inference and anatomical review are reported separately
from synthetic workflow tests. The implementation contract is in
[docs/CONTRACT.md](docs/CONTRACT.md); current evidence and remaining validation
limits are in [docs/VALIDATION.md](docs/VALIDATION.md).

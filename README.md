# 3DScan

An AI-assisted case-analysis workbench for radiologists, designed to complement
established viewers such as OsiriX. Easy access to segmentation and other
analysis tools, reproducible review, and a high-quality, responsive viewer are
core product goals.

Local CT and static MRI reconstruction with TotalSegmentator, linked image review,
in-browser nnInteractive corrections, revisioned mask refinement, optional Slicer
exchange, and offline 3D HTML export.

The [image foundation](docs/EXPANSION_FOUNDATION.md) separates immutable source
images, segmentation revisions and modality policies. The expansion branch adds
classic and single-instance static Enhanced MRI volumes; 2D and cine modalities
need their own adapters. On 28 October 2026 the user selected the development
checkout for the primary service; the live modality policy remains CT-only.

Patient data, model weights, environments and generated artifacts are excluded
from version control. The synthetic demo is labelled throughout and does not run
or validate TotalSegmentator. The bundled models are research tools, so real
cases and portable exports are labelled **Research use only · not for
diagnostic use**.

## Run

The configured workstation now runs **Python 3.14.8, PyTorch 2.14.0 and ROCm
Core SDK 10.1.0** through `scan3d.service`. Open **http://127.0.0.1:9010** or
the existing approved private Tailscale address. The service uses the original
cases, weights and license configuration. See [production operation and
rollback](docs/PRODUCTION_RUNTIME.md).

The usual address now serves the current development checkout, including native
Cornerstone MPR and the default Cornerstone3D/vtk.js CT renderer. It serves built frontend assets;
frontend changes require a build and backend changes require a restart. The
previous frozen release is retained for rollback. Test servers must use scratch
data and separate ports.

Normal workstation commands select that same runtime. Run them in bash: zsh and
fish cannot source `scripts/production-env.sh`, and it refuses them.

```bash
source scripts/production-env.sh
python -m scan3d doctor
systemctl --user status scan3d.service
journalctl --user -u scan3d.service -f
```

For a foreground server, stop the user service first, then run
`scripts/run-production.sh`. Both use `data/`; do not run two workers against
that directory.

### Receive studies from OsiriX

The incoming workflow targets **OsiriX MD 14.3.0 → Orthanc → 3DScan**. Once
activated, add the private receiver as an OsiriX DICOM Send destination with
AE title **SCAN3D** and port **4243**. In 3DScan choose **Import → Orthanc**,
search the archive, select one series and choose **Import selected series**.
Then select its acquisition labels and choose **Open CT**. Both steps run on
the CPU; receiving or opening a study never starts segmentation automatically.

The connector and incoming receiver are **active**. The destination address is
`10.20.0.3`; the allowed sender is `10.20.0.107` with calling AE
`OsiriX-Mac`. The separate synthetic archive remains available for
development. See the
[Orthanc setup and transfer guide](docs/ORTHANC.md) for current status and
connection settings. A synthetic binary DICOM SEG return test passes transport;
OsiriX overlay qualification and general segmentation export remain pending.

### Back up the case library

```bash
source scripts/production-env.sh
python -m scan3d backup /path/on/another/disk/scan3d-backups
```

Each run writes `scan3d-<UTC time>/` with a consistent database snapshot, the
case folders (original DICOM, reference images, model outputs and accepted
masks), the surface cache and a `manifest.json`. It only reads live data, so the
service can keep running. Later runs hard-link files unchanged since the previous
snapshot and copy only new data. Review, analysis and cinematic caches are
skipped because they rebuild on demand. Records store absolute paths: to
restore, stop the service and copy a snapshot's contents back into the same data
directory.

### Fresh checkout and retained legacy environment

The original bootstrap remains a complete Python 3.12 / ROCm 7.2.4 setup and
the runtime rollback path. On a fresh checkout, install its native prerequisites
on CachyOS:

```bash
sudo pacman -S --needed rocm-hip-runtime rocminfo rocm-ml-libraries hipsparselt rocm-smi-lib base-devel cmake pkgconf openjpeg2 zlib
```

Provision that retained environment and the frontend:

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
To provision the promoted Python 3.14 family on another checkout, first provide
the project-local uv and a Python 3.14 interpreter, then use the separate
hash-locked bootstrap described in [setup](docs/SETUP.md). The current
workstation is already provisioned. `scripts/bootstrap-rocm10.sh` does not
install uv or the interpreter itself. The [original experiment](docs/ROCM10_PY314.md)
records how this runtime was qualified before promotion.
Full setup builds a pinned native dcm2niix in `.tools/bin/` with JPEG 2000 and
JPEG-LS decoding. Some AUR builds omit these decoders and cannot import compressed
CT scans. To upgrade an existing checkout's converter, run
`scripts/build_dcm2niix.sh`; the system package stays available independently.
Known compressed-frame parsing failures use GDCM to create temporary uncompressed
copies before retrying conversion, preserving original scans and voxel values.

### Enable assisted correction

The full environment pins `nnInteractive==2.6.0` and
`nninteractive-client==2.6.0` alongside the existing ROCm stack. Download the
official model separately on the workstation:

```bash
source scripts/production-env.sh
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

### TotalSegmentator license

Open **Segment CT → TotalSegmentator license**, enter your key and choose
**Validate and save**. This checks it with TotalSegmentator and stores it on the
processing workstation for all cases. The task list refreshes automatically;
licensed models still check task access when their weights are downloaded.
The panel also supports updating and removing a key. Existing segmentations
remain available after removal.

Status reads stay local. The key is never returned to the browser or saved in
case/job records. It is stored in TotalSegmentator's existing
`TOTALSEG_HOME_DIR/config.json` with private file permissions. Failed validation
keeps the previous key and configuration unchanged. License changes wait until
queued/running jobs finish. **Validated when saved** records that earlier check;
it does not claim a fresh online check or entitlement to every task.
The [provider's license page](https://backend.totalsegmentator.com/license-academic/)
explains its noncommercial license terms.

## Review a case

1. Import a DICOM folder or ZIP, select a series from a configured Orthanc
   archive, or create the clearly labelled synthetic demo.
   Browser imports upload in bounded batches with a progress bar, so folders of
   any size up to the import limit work. Interrupted batches are resent
   automatically, **Cancel upload** discards the staged files, and the case
   appears only after every file has arrived.
2. Select one reconstruction, scan region/study and contrast phase. The study
   list includes **Coronary CTA (CorCTA)** and **Cardiac CT**, along with chest,
   abdomen, pelvis, head/neck and whole-body coverage. Contrast labels include
   coronary/pulmonary arterial, non-contrast, venous and delayed acquisitions.
   Cardiac studies also accept a separate reconstruction label (for example,
   **Best diastolic**) and optional **% R–R** (for example, **69**). Record the
   reconstruction supplied with the chosen series; unknown timing can stay
   unspecified. These are acquisition labels, so they do not change pixels,
   rendering or segmentation tasks. Different phases remain separate; series
   warnings explain unsupported geometry.
3. Choose **Open CT**. The worker converts the selected series for CT review and
   volume rendering without running a segmentation model. When ready, open
   **Segment CT**, select a TotalSegmentator task and choose **Run segmentation**.
   **Find a task or structure** searches task names and their output anatomy;
   **Included structures** previews the selected task's outputs. Search preserves
   your current task and options until you explicitly select another task.
   The catalog uses all 39 current CT tasks in the installed 2.18.0 engine;
   Tasks are scoped to the selected image modality; MRI uses `total_mr` by default.
   Legacy and internal test tasks are excluded.
   Optional settings include robust organ cropping (`-rc`), initial body cropping
   (`-bs`) and removing small disconnected components below 200 mm³ (`-rmb`).
   Task defaults and unsupported options are explained in the panel. All nonempty
   outputs of the chosen task are included at standard resolution. Each run
   replaces that task's results and existing segments with matching anatomical
   names, including manual corrections. For example, **Total v3** replaces the
   matching organs from **Total**. Unmatched segments are kept, and the previous
   revision remains available in History. Matching normalizes capitalization,
   underscores and whitespace; distinct anatomical names remain separate.
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
   **Direct edit** is the default: choose **Paint** or **Erase**, set a physical
   brush radius and draw on a native CT slice. Only the brushed voxel centres on
   that slice change, in stroke order. Direct edits run on the CPU and do not
   load or require an AI model. Several strokes can share one **Apply edits**.
   For a Slicer-style 3D cut, choose **Scissors**, select **Freehand**,
   **Rectangle** or **Circle**, then choose **Erase inside** or **Erase outside**.
   Draw directly over the 3D view; cuts extend through the full depth of the
   selected segment from that camera angle. Circle draws from centre to edge.
   Right drag rotates the camera; **Escape** cancels an in-progress cut. The
   original camera is recorded with each completed cut, so later rotation does
   not move a queued selection. Scissors does not require a CT plane.
   For larger corrections, choose optional **AI assist** and use Include or
   Exclude with a point, hint brush, box or lasso. **Run AI** proposes changes
   beyond the marked voxels. Save or discard the current draft before switching
   editing methods; saved corrections become the starting mask for further work.
   The first mark starts or resumes a draft.
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
6. Choose **Apply edits** or **Run AI** once for the pending marks. Direct edits
   replay the exact strokes. For AI, nearby marks share a planned model region;
   distant marks retain separate regions so none are omitted.
   Compare the original and working candidate and inspect voxel/volume changes.
   Add more marks and Apply again as needed. The last applied candidate remains
   visible while new marks are pending; saving waits for those marks to be
   applied. Applying does not change accepted masks. Direct edits prepare the
   changed surface for a 3D candidate preview; unchanged surfaces remain cached.
   **Original** and **Candidate** compare both the CT mask and the 3D surface.
   AI candidates remain proposals on CT until saved.
7. Choose **Save correction** only after inspecting the candidate. This
   updates that structure and any explicitly selected exclusive siblings,
   including their measurements and surfaces. Inspect every affected volume
   and CT mask before saving; unrelated masks remain unchanged. The previous
   revision remains available in history for exact undo of all affected labels.
   Direct erasing can leave an existing named segment empty, with 0 mL volume.
   Empty new segments and empty AI candidates cannot be accepted. A candidate
   must change masks or relationship metadata. **Discard draft**
   closes a proposal; saved prompts and completed candidates otherwise survive
   reloads and workstation restarts. Interrupted predictions can be retried.
8. The existing component/box operations remain available under **Refinement**.
   For external painting or contouring, expand **External correction with
   Slicer**, download its bundle and upload the edited `.seg.nrrd`. Preserve its
   segment tags: import checks segment identities, source CT hash and geometry.
9. Set the revision's review status and export PNG, Slicer files or a portable
   HTML viewer. **Export viewer** offers **Segmentation**, **CT volume** and
   **CT plane**, defaulting to the currently visible rendering layers. Selected
   layers retain the camera, appearances, structure visibility/opacity, volume
   crop/mask and plane position/window. CT layers share one embedded CT, and
   required overlay/masking labels travel with it. Unselected geometry and CT
   data are omitted. The file works offline in a desktop browser; segmentation
   editing and three-view CT review remain in the local app. Save or discard a
   correction draft before exporting. Export labels use pseudonymous case IDs.

**Model agreement** at the top of **Revision history** shows how far the
accepted masks moved from the model. Each structure is compared with its saved
TotalSegmentator output, traced through the revision history; a later run that
replaced a structure becomes its new original. **Dice** is voxel overlap (1 is
identical), **HD95** is the larger 95th-percentile distance between the two
surfaces in millimetres, exact on oblique and sheared scans, and **ΔV** is the
accepted volume minus the original. **All cases ↗** pools the active revision of
each case marked Reviewed or Corrected, by task and structure: how often the
model was accepted unchanged, mean and median Dice and mean |ΔV|. Synthetic demo
cases compare with their phantom, are labelled and form a separate pool.
These numbers measure reviewer edits, not ground truth: an uncorrected mask can
still be wrong. Review status covers a whole revision, so a structure nobody
inspected counts as accepted. HD95 ignores the farthest 5% of the surface, so a
small local fix can show 0 mm while Dice is below 1; the voxel counts show it.
Reviewer-added segments have no model original and are not compared.

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

An AI Apply batch is not identical to predicting after every individual gesture.
The pipeline records the exact Apply boundaries and settings for replay and
undo. One Apply action can require several model regions/internal forwards,
especially for distant marks, large drawings or whole-mask refinement.

Mask cleanup is anatomical and label-specific. Mesh simplification changes the
display surface; it does not change the voxel mask used for volumes. Named
bronchopulmonary territories and automated lesion characterization are outside
this version's scope.

The 3D view defaults to **Cornerstone3D**, using vtk.js ray casting. Its
**Rendering stack** shows **Segmentation**, **CT volume** and **CT plane** together
or independently. With CT visible, surfaces are solid for correct depth;
turn CT off to adjust their transparency. **CT volume settings** provides
presets, native lighting/scattering, opacity, masking and cropping. Enable
**Mask to segmentation**, choose segments and select **Inside** or **Outside**;
**Display margin** grows only that display region. Empty/loading/failed masks
keep the CT volume hidden while slices and surfaces remain available.
**Three.js (legacy)** retains the previous renderer and HTML
export. The following cinematic-heart recipe describes that legacy renderer.

Choose **Cinematic heart** under Appearance for the supplied Slicer script's
crimson myocardium and ivory blood colors, camera-following lighting and
adjustable volume shadows on black. When suitable labels exist, selecting this
appearance automatically measures median HU from myocardium and aorta (or
left-heart) masks. **Measure from segments** repeats that measurement. When
those labels are absent,
enter both reference values and click **Apply reference HU**; the initial
80 / 400 HU pair is an explicit display default. For an isolated heart, enable
**Mask to segmentation** and choose its masks. Navigation temporarily drops
shadow rays and restores them when the view settles. This browser approximation
also travels with CT-volume HTML exports; it does not modify source scans or
segmentation measurements.
**Display margin** under the mask controls can retain surrounding thin walls;
the **2.5 mm** shortcut matches the margin size used by the supplied Slicer
heart-isolation script. It affects only the volume view, including its offline
export. CT overlays remain exact. For the supplied saved CorCTA appearance,
the measured reference pair is **86/483 HU** with **Intensity +20 HU**.
The first cinematic selection in a cardiac case also opens **Heart view**, using
the supplied Slicer camera's orientation and fitting the current heart anatomy.
The **Heart view** button returns to it after rotating. General **Oblique** views
face a different side of the heart, which can expose brighter blood and chamber
interiors even in native Slicer. Saved offline cameras are retained on opening.

The **CT plane** toggle shows a native i/j/k plane. Choose a plane,
move its slice slider, or use **Face plane** to align the camera. It follows the
linked CT position and window/level while retaining normal orbit. The native
plane shows scalar CT; the legacy plane also supports overlays and anatomy
picking. Local overview frames appear during full CT preload in the legacy
renderer; once cached, moving the plane needs no workstation slice requests.
Review requires exact full detail before accepting marks; orthogonal CT uses the
native plane, while sheared scans and MRI use the legacy renderer.

Use **Expand ↗** in the top-right corner of the 3D view to fill the screen.
**Restore ↙** or **Escape** returns to the workspace. The camera, loaded CT,
volume mask and anatomy controls stay in place. This also works in review and
the portable offline viewer.

**Cornerstone3D is the only 2D CT viewer.** Once the full CT is cached, one
native Cornerstone volume supplies all three MPR views without another CT
download. Rotatable crosshairs link the planes. **Projection** selects MPR,
MIP or MinIP, with a slab thickness in millimetres; overlays show the centre
slice during slab projection and drawing is paused. **Fill**, **Outline**,
segment colours and the existing overlay picker control independent labels.

Native review locks the planes to the source grid for point, box, scribble and
lasso corrections. Preview, sheared and over-budget scans use Cornerstone stack
views from the shared worker. Sheared native planes are orthogonalized for
display only; gestures still map to original source voxels. WebGL errors show
**Retry**, which rebuilds rendering from the resident CT session.

Native 2D and 3D share one CT source, texture, context and memory reservation.
Either panel can open first or close independently. Closing both releases their
native resources; the worker keeps the source for local reuse. Plane and surface
allocations also join the budget. If native MPR cannot fit, frame navigation
remains available. Three.js and its portable export remain available. See
[the Cornerstone plan](docs/CORNERSTONE_PLAN.md) and
[the new-segment workflow](docs/NNINTERACTIVE_PLAN.md).

An isolated [Three.js / vtk.js / Cornerstone3D comparison](docs/VOLUME_RENDERING_COMPARISON.md)
records synthetic R9700 rendering and the memory-sharing basis for this change.
The default native view adds quantized segmentation surfaces and a linked CT
plane without another full CT copy. Only enabled surfaces are loaded; use
**Show selected only** to reduce mesh memory. Rendering starts after full preload and does
not require the CT review panel to be open. Opening that panel attaches MPR to
the same source. PNG captures the current view. Plane/surface failures keep CT
available; a renderer failure offers **Retry 3D view**.

Native masking reuses the CT texture and adds one cropped mask texture of at
most 8 MiB. Camera, crop and inside/outside changes reuse resident data. The
shared 2 GiB desktop budget includes current/staged resources; completed CT
upload staging is released and mesh uploads share one bounded scratch allowance.
Views that still exceed it show an error; hiding surfaces or CT review frees capacity.

Orthogonal CT corrections now use the same Cornerstone3D/vtk.js view for surface
points, native-plane brush/box/scribble/lasso, full-depth scissors and prompt
markers. Right drag rotates while marking; middle drag pans and the wheel zooms.
Plane drawings show the exact selected/candidate masks and parent comparison.
Input waits for the current geometry to render; changing the camera, slice or
editing context cancels an unfinished stroke. PNG includes review marks.

Use **Three.js (legacy)** for HTML export. Tilted scans and MRI retain that
existing renderer automatically. The ordinary linked native plane shows scalar
CT; correction mode adds exact segmentation coloring.
This port changes display, not
source masks, measurements, revision history or inference. Native scattering has
a different appearance from the earlier renderer and requires its own visual
comparison before claiming Slicer equivalence.

## Local scripts and recipes

```bash
source scripts/production-env.sh
python -m scan3d doctor
python -m scan3d import /absolute/path/to/dicom.zip --label "Teaching case"
python -m scan3d process CASE_ID --series SERIES_UID --profile chest_abdomen --phase venous
python -m scan3d segment CASE_ID --task total --body-seg
python -m scan3d segment CASE_ID --task lung_vessels --remove-small-blobs
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
{
  "type": "erase_region",
  "bounds_ras": [
    [-80, -60, -40],
    [-40, 20, 30]
  ]
}
```

Supported operations are `keep_component` and `remove_component` with
`seed_ras: [R,A,S]`, and `keep_region` and `erase_region` with `bounds_ras`.
Operations reject no-effect and whole-structure deletion results. Each successful
revision records its parent, operation, code version and voxel/volume changes.
Source hashes record the exact processing code and downloaded model checkpoints.

## Architecture and validation

- FastAPI + SQLite persists case, job, revision and assisted-review draft records. A filesystem lock
  enforces one worker set across API and CLI processes. Its GPU lane runs
  TotalSegmentator and nnInteractive one job at a time. Its CPU lane runs
  archive import, preparation, direct edits, saves, refinement, Slicer imports and include-all,
  which therefore do not wait behind a segmentation. Each lane takes review work
  before queued batch work. Jobs interrupted by restart remain retryable, and completed
  conversion/inference stages are reused.
- Assisted review keeps one model and CT warm for successive Apply batches in a
  supervised worker process; the API process never imports torch. A crash or
  hang fails only that job, and the next Apply starts a fresh worker. Cancelled
  inference stops cooperatively, or the worker is killed after 10 s. Undo
  replays from the saved original mask using recorded batch boundaries when
  needed. The worker exits, returning its VRAM, before TotalSegmentator starts.
  Draft generations and active-revision checks prevent stale results from
  replacing another correction; accepted revisions record point history,
  model/checkpoint identity, device, timings and exact voxel/volume changes.
- nibabel and full affine transforms preserve patient geometry. Meshes
  use glTF metres; API coordinates and measurements use millimetres/millilitres.
  Display meshes are quantized (KHR_mesh_quantization, within 0.03 mm) to halve
  transfer size; detailed exports remain float32. Masks stay authoritative.
- Cornerstone3D/vtk.js shares the CT source, GPU texture and context between
  native MPR and 3D. A local worker supplies preview and exact slice navigation;
  warm navigation and windowing need no workstation requests. Three.js remains
  available for sheared scans, MRI and portable HTML. The
  [architecture](docs/ARCHITECTURE.md) and
  [volume-rendering contract](docs/CT_VOLUME_RENDERING.md) describe ownership,
  budgets and renderer differences. Segmentation, corrections and mesh generation
  remain on the workstation.
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
status and revision identity, and re-encodes older float32 display meshes as
quantized assets. `SCAN3D_POSTPROCESS_WORKERS` defaults to 4, reduced
using a conservative available-memory estimate; `SCAN3D_MESH_THREADS` defaults
to 16 for VTK's available native CPU backend. Inference and browser drawing use
the GPU; exact voxel counting remains on the CPU after an end-to-end benchmark.

Run the [development checks](docs/SETUP.md#development-checks) for backend tests,
web checks, scratch builds and synthetic browser workflows. Test builds stay
outside the frontend served by the live app.

The tests cover oblique/reflected geometry, voxel-derived measurements, mask
operations, draft isolation/acceptance, cancellation and restart recovery,
revision undo, Slicer round trips, API jobs and browser interaction. Real-scan
inference and anatomical review are reported separately
from synthetic workflow tests. The implementation contract is in
[docs/CONTRACT.md](docs/CONTRACT.md); current evidence and remaining validation
limits are in [docs/VALIDATION.md](docs/VALIDATION.md).
The system architecture and scan/correction flowcharts are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

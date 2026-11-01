# HTTP API and data model

This is the contract between the 3DScan server and its clients. All routes live under `/api` on the server
(`http://127.0.0.1:9010` by default). FastAPI also serves generated OpenAPI docs at `/docs`.
[ARCHITECTURE.md](ARCHITECTURE.md) explains how the system works behind these routes.

## Conventions

- **Authentication.** There is none. See [Transport rules](#transport-rules).
- **Identifiers.** Case, revision, job, session and upload IDs are 32 lowercase hex characters. A malformed
  ID is a 400; a well-formed unknown ID is a 404.
- **Coordinates.** Every point, seed and bound is patient RAS millimetres. Voxel arrays use nibabel order
  (i, j, k) with the full affine.
- **Long work.** Actions that take time return a [Job](#job). Poll `GET /api/jobs/{id}` until its `status`
  is final, then read the result.
- **Bodies.** JSON bodies reject unknown fields and non-finite numbers. Times are UTC ISO 8601.
- **Legacy names.** `ct-volume`, `hu`, `kind: "ct"` and `X-CT-Frame` predate MRI support. They remain wire
  names for any scalar image, and do not mean the values are Hounsfield units. Read `scalar.units` and
  `modality` for the real meaning.

## Errors

Errors return `{"detail": string}`. Schema errors use FastAPI's validation list, and a rejected `Host` or an
unexpected 500 is plain text.

| Status | Meaning                                                 | Source                                    |
| ------ | ------------------------------------------------------- | ----------------------------------------- |
| 400    | Invalid input, or a `Host` outside the allowlist        | `ValueError`, `TrustedHostMiddleware`     |
| 403    | Mutation from another browser origin                    | Same-origin middleware                    |
| 404    | Record is absent or belongs to another case             | `errors.NotFound`                         |
| 406    | No acceptable GLB encoding                              | `Accept-Encoding` negotiation             |
| 409    | Stale state; reload before retrying                     | `ReviewConflict`, `ImportConflict`, `OrthancChanged`, a license change while jobs are active |
| 413    | Offline HTML export is over a size limit                | `ExportTooLarge`                          |
| 422    | Schema validation failed                                | FastAPI                                   |
| 499    | Client left while a mask or label union was being built | `volume-mask`, `volume-labels`            |
| 500    | Unexpected exception, logged without internal detail    | Anything else, including a bare `KeyError` |
| 502    | The configured Orthanc failed                           | `OrthancError`                            |
| 503    | A cached surface or viewer bundle is missing            | Surface and export routes                 |

The license route replaces validation detail with a fixed message, because Pydantic's errors would echo the
submitted key. [Inference path](#refusals-and-failures) refusals use fixed sentences too, which never hold
proof text or local paths.

## Transport rules

- **Origin.** A non-GET request whose `Origin` differs from `Host` gets 403. Hosts must appear in
  `SCAN3D_ALLOWED_HOSTS`.
- **Private responses.** `/api/orthanc/*`, the license route and `.../dicom-seg` use `Cache-Control:
  no-store`, including their errors. Candidate records, descriptors, agreement reports and frames use
  `private, no-store`.
- **Chunks.** Volume chunks are gzip with `Cache-Control: private, max-age=31536000, immutable`, keyed by
  content.
- **Compression.** JSON and viewer assets of at least 1 KiB use dynamic gzip. GLB routes negotiate stored
  gzip files. `slicer.zip` and `export.html` are left alone, so downloads keep `Content-Length`.

## Data model

Public records never contain filesystem paths or archive credentials.

### Case

```text
{id, label, created_at, status, active_revision_id, selected_series_uid, phase,
 series: Series[], error?, import_source?, synthetic?}
```

`status` is `importing`, `imported`, `ready`, `import_failed` or `import_cancelled`. `import_source` is set
for Orthanc imports: `{kind, source_key, study_uid, series_uid, frame_of_reference_uid, snapshot,
instance_count, content_digest, manifest_sha256, completeness, warnings}`.

### Series

```text
{uid, description, modality, study_uid, frame_of_reference_uid, sop_class_uids, slice_count,
 spacing, warnings, supported, unsupported_reason}
```

MR series add acquisition metadata such as `sequence_name` and `echo_time_ms`. A series of a disabled
modality reads `supported: false`, and enabling the modality restores it.

### ImageAsset

An immutable source image.

```text
{schema_version: 1, id, case_id, kind: "volume3d", modality, content_sha256,
 source: {series_uid, study_uid?, frame_of_reference_uid?, sop_class_uids?,
          source_archive_key?, source_content_sha256?, source_manifest_sha256?},
 frame_selector, geometry: Geometry, scalar: {dtype, slope, intercept, units},
 presentation: {units, background_value, default_window, default_level,
                photometric_interpretation, preset_group, preview_quantization_step},
 capabilities: ("mpr" | "volume_rendering" | "segmentation" | "voxel_measurements")[]}
```

`Geometry` is `{shape, affine_ras, spacing, bounds_ras, center_ras}`.

### Revision

```text
{id, case_id, parent_id, kind: "ct" | "image" | "segmentation", created_at, review_status, source,
 operation, geometry, reference_image_id, image_asset, structures: Structure[], qc: string[],
 acquisition, synthetic, model_url, model_manifest_url, surface_assets, mesh_statistics,
 processing_timings, pipeline_version, pipeline_code_sha256,
 model_provenance?, segmentation_settings?, segmentation_runs?}
```

- `review_status` is `unreviewed`, `reviewed` or `corrected`.
- `source` is `dicom`, `totalsegmentator`, `refinement`, `slicer_correction`, `include_all_outputs`,
  `dicom_seg_import`, `synthetic_demo`, `direct_edit_correction`, `direct_edit_new_structure`,
  `nninteractive_correction` or `nninteractive_new_structure`.
- `acquisition` is `{series_uid, profile, phase, cardiac_phase?, rr_percent?}`.

### Structure

```text
{id, name, task, group, color, volume_ml, voxel_count, centroid_ras, bounds_ras, qc: string[],
 reference_image_id, relationship?}
```

Volumes are `nonzero voxels × |det(affine[:3,:3])| / 1000` mL. `centroid_ras` and `bounds_ras` are null for
an empty mask. Structures from DICOM SEG also carry a `dicom_seg` provenance object.

### Job

```text
{id, case_id, kind, status, stage, error, revision_id, created_at, started_at?, completed_at?,
 progress?: {phase, completed, total, timings_seconds?, bytes?},
 result?, review_session_id?, source_revision_id?, inference_path?: {id, label, identity}}
```

`status` is `queued`, `running`, `completed`, `failed` or `cancelled`. `stage` is display text.
`revision_id` is the new revision once committed. For `orthanc_export` it is the exported revision.

`kind` is `process`, `segment`, `refine`, `correction`, `include_all` (from the `scan3d include-all` command
only), `review_predict`, `review_accept`, `orthanc_import`, `orthanc_export` or `dicom_seg_import`.
[ARCHITECTURE.md](ARCHITECTURE.md#jobs-and-lanes) lists which lane runs each. A restart marks running jobs
failed with stage `Interrupted`.

`inference_path` is the [inference path](#inference-paths) frozen when the job was queued: its `id`, a `label`
to show, and an `identity` that clients compare whole and never parse. `identity` is `default` for the default
path and `<id>|<device>|<gpu_name>` for an added one. The key is present on a `segment` job that named a path,
`default` included, and on a `review_predict` that runs the model and either named a path or belongs to a
draft bound to one. It is absent on every other job, including direct and relationship-only `review_predict`.

### ReviewSession

```text
{id, case_id, revision_id, structure_id, method, status, generation, prompts, options, batches,
 relationship, relationship_suggestions, relationship_dirty, dirty, pending_count,
 applied_prompt_count, job_id, candidate: Candidate | null, error, accepted_revision_id,
 created_at, updated_at, new_structure, inference_path, target?, preview_revision_id?}
```

`status` is `draft`, `predicting`, `ready`, `accepting`, `accepted` or `discarded`. A failed or cancelled
job returns the session to `draft`, or to `ready` if a candidate remains, with `error` set and its prompts
kept. `accepted` and `discarded` are closed. `target` and `preview_revision_id` appear when `new_structure`
is true.

`inference_path` is the path that made the current result, as `{id, label, identity}`. It is null while
`batches` is empty, and when the job that published the result named no path. It changes only when a model
result publishes, so a queued, failed or cancelled re-run leaves it as it was, and a relationship-only Apply
never changes it.

### Candidate

```text
{generation, revision_id, summary, created_at, relationship, overlaps, warnings,
 total_changed_voxels, relationship_changed, affected_structures, preview_3d_ready?,
 model_manifest_url?, gpu_name?}
```

- `summary`: `before_voxels`, `after_voxels`, `changed_voxels`, `added_voxels`, `removed_voxels`,
  `voxel_volume_ml`, `before_volume_ml`, `after_volume_ml`, `volume_delta_ml`.
- `affected_structures`: `{id, name, role: "target" | "exclusive_sibling", summary, metadata_only,
  relationship}`.
- `overlaps`: `{id, name, voxel_count, volume_ml, policy: "removed" | "preserved", reason}`.
- `gpu_name`: the GPU that made this result, as the run measured it. It is present only when the job named a
  path and the run reported a GPU, and a relationship-only Apply keeps it from the result it reuses.

## Route index

| Method and path                                      | Purpose                                 |
| ---------------------------------------------------- | --------------------------------------- |
| `GET /api/health`, `GET /api/imaging-capabilities`   | Liveness, installed modality adapters   |
| `GET /api/segmentation-options`                      | Task catalog and inference paths        |
| `GET`, `PUT`, `DELETE /api/totalsegmentator/license` | License status and changes              |
| `GET /api/interactive/status`                        | nnInteractive state and inference paths |
| `GET /api/agreement`                                 | Pooled AI-agreement report              |
| `GET /api/cases`, `GET /api/cases/{id}`              | Case records                            |
| `POST /api/cases/demo`, `.../import`, `.../import-local` | Phantom, multipart or local import  |
| `POST /api/imports`, `.../{id}/parts`, `.../{id}/finish`, `DELETE .../{id}` | Staged upload            |
| `GET /api/orthanc/status`, `POST .../studies/query`  | Connector state, study search           |
| `GET /api/orthanc/studies/{id}/series`               | List series with snapshots              |
| `POST /api/orthanc/imports`, `POST .../launch`       | Import a snapshot, resolve a launch     |
| `POST /api/cases/{id}/process`                       | Prepare a series                        |
| `POST /api/cases/{id}/segment`                       | Run TotalSegmentator                    |
| `POST /api/cases/{id}/refinements`, `.../corrections` | Mask operation, Slicer correction      |
| `POST /api/cases/{id}/dicom-seg/imports`             | Import DICOM SEG                        |
| `POST /api/cases/{id}/activate`                      | Undo or redo                            |
| `GET /api/cases/{id}/images`, `.../images/{image}`   | Image assets                            |
| `GET /api/cases/{id}/jobs`                           | A case's jobs, newest first             |
| `GET /api/jobs/{id}`, `POST .../cancel`, `GET .../measurements` | Poll, cancel, partial results |
| `GET /api/cases/{id}/revisions`                      | Revision history                        |
| `GET`, `PATCH /api/cases/{id}/revisions/{rid}`       | Read a revision, set review status      |
| `GET`, `POST /api/cases/{id}/reviews`, `POST .../new-structure` | List or start sessions       |
| `GET`, `PATCH /api/cases/{id}/reviews/{sid}`         | Read a session, replace its prompts     |
| `POST .../reviews/{sid}/predict`, `accept`, `discard` | Apply marks, save, close the draft     |

Revision routes below are relative to `/api/cases/{id}/revisions/{rid}`:

| Method and path                                    | Purpose                          | Candidate alias |
| -------------------------------------------------- | -------------------------------- | --------------- |
| `GET .../image-volume`, `.../ct-volume`            | Scalar descriptor                | Yes             |
| `GET .../image-volume/{key}/chunks/{index}`        | Scalar chunk (also `ct-volume`)  | Yes             |
| `GET .../mask-volume`, `.../{key}/chunks/{index}`  | Cropped mask and parent          | Yes             |
| `GET .../volume-mask`, `.../{key}/chunks/{index}`  | Display union                    | Yes             |
| `GET .../volume-labels`, `.../{key}/chunks/{index}` | Display label union             | Yes             |
| `GET .../review-frame`                             | Exact slice with binary overlays | Yes             |
| `GET .../probe`                                    | Voxel value at a RAS point       | Yes             |
| `GET .../model-manifest`                           | Per-structure surface URLs       | If prepared     |
| `GET .../surfaces/{key}.glb`                       | One structure's surface          | If prepared     |
| `GET .../model.glb`                                | Combined surface model           | No              |
| `GET .../agreement`                                | Agreement with model outputs     | No              |
| `GET .../cinematic-reference`                      | Cardiac HU references            | No              |
| `GET .../slicer.zip`                               | Slicer bundle                    | No              |
| `GET`, `POST .../export.html`                      | Offline HTML viewer              | No              |
| `GET`, `POST .../dicom-seg`                        | SEG export status and start      | No              |

## System routes

**`GET /api/health`** returns `{status, worker, lanes, modalities, gpu, version}`. `lanes` maps `gpu` and
`cpu` to whether each thread is alive, and `worker` is true only when every lane is. Without the worker
lock, `lanes` is empty and `worker` is false. `modalities` lists what this installation offers.

`gpu` has `ready`, `device` (the `SCAN3D_DEVICE` request), `dcm2niix` and `reason`. `ready` comes from the
proof written by `scripts/check_gpu.py` and means a verified GPU, so a CPU installation reports `false` by
design. With a proof present, `gpu` adds `torch_version`, and with a matching one `hip_version`,
`cuda_version`, `devices` and `checked_at`. This route never loads PyTorch. `gpu` describes the default path
alone, and added [inference paths](#inference-paths) never appear here.

**`GET /api/imaging-capabilities`** returns `{schema_version, modalities: [{adapter, modality, kind,
capabilities, presentation, enabled}]}`.

**`GET /api/segmentation-options?modality=CT`** returns `{engine_version, tasks, license_configured,
defaults, remove_small_blobs_mm3, inference_paths}`. Each task has `id`, `label`, `modality`, `classes`,
`class_count`, `model_class_count`, `description`, `license_required`, `license_available`, `available`,
`uses_organ_crop`, `robust_crop_default`, `robust_crop_supported` and `body_seg_supported`. The default task
is `total` for CT and `total_mr` for MR. [Listing paths](#listing-paths) describes `inference_paths`.

**License.** `GET /api/totalsegmentator/license` returns `{configured, validated, validated_at}` without
contacting the network. `PUT` takes `{license_key}`, checks its format, validates it with the
TotalSegmentator service and stores it. `DELETE` removes it. Changes return 409 while any job is queued or
running.

**`GET /api/interactive/status`** returns `{installed, version, model_available, available, reason,
model_id, model_revision, license, capabilities, inference_paths}`. It checks packages and files only, with no
GPU probe. `capabilities` are `point`, `bbox2d`, `scribble`, `lasso` and `initial_label`, with `bbox3d` false.
The worker verifies checkpoint integrity and the device before predicting. [Listing paths](#listing-paths)
describes `inference_paths`.

## Imports

**`POST /api/cases/import`** takes multipart `files` (folders and ZIPs accepted) and optional `label`, and
returns a Case. The multipart parser accepts at most 1,000 files.

**`POST /api/cases/import-local`** takes `{path, label?}` for a directory or ZIP under
`SCAN3D_IMPORT_ROOTS`. Symlinks are refused, and files are copied so that later changes cannot alter the
case.

**Staged uploads** are how the browser imports. The total limit is 16 GiB and 100,000 files, and abandoned
uploads are removed after a day without activity.

1. `POST /api/imports` with `{label?, total_files, total_bytes}` returns `{id, max_parts, received_files,
   total_files, received_bytes, total_bytes}`. `max_parts` is 256.
2. `POST /api/imports/{id}/parts` with multipart `manifest` (JSON `[{path, offset, size}]`) and one `files`
   part per entry returns progress. Small files arrive whole, and larger ones as ordered byte ranges. Bytes
   already received must match exactly, so resending a batch is idempotent. A gap is 409.
3. `POST /api/imports/{id}/finish` returns a Case once every declared file and byte has arrived, and 409
   otherwise. Repeating it returns the same case.
4. `DELETE /api/imports/{id}` discards an unfinished upload and returns `{id, status: "cancelled"}`.

**`POST /api/cases/demo`** creates a synthetic phantom with masks, for tests. `synthetic: true` keeps it out
of real agreement statistics.

## Preparation and segmentation

**`POST /api/cases/{id}/process`** takes `{series_uid, profile, phase, cardiac_phase?, rr_percent?}` and
returns a `process` job, which creates an image-only revision. It runs no inference.

- `profile` is `chest`, `abdomen`, `chest_abdomen` (the default), `corcta`, `cardiac`,
  `chest_abdomen_pelvis`, `abdomen_pelvis`, `pelvis`, `head_neck`, `breast`, `whole_body`, `other` or
  `unknown`.
- `phase` is required free text of up to 80 characters, describing contrast timing.
- `cardiac_phase` (`unknown`, `best_diastolic`, `best_systolic`, `diastolic`, `systolic`, `other`) and
  `rr_percent` (0–100) are accepted only for `cardiac` and `corcta`.

These fields are saved metadata. They do not select frames, change intensities, crop anatomy or choose a
task. Unsupported series and disabled modalities are refused.

**`POST /api/cases/{id}/segment`** takes `{revision_id, task?, robust_crop?, body_seg?,
remove_small_blobs?, inference_path?}` and returns a `segment` job. `revision_id` must be the active revision.
`task` must be in the catalog for the image's modality, and licensed tasks need a configured key. MR refuses
`robust_crop` and `body_seg`. Body cropping cannot combine with a task that already crops to an organ.
`inference_path` is covered under [Choosing a path](#choosing-a-path).

The run replaces results as described in [ARCHITECTURE.md](ARCHITECTURE.md#store-and-revision-model): every
structure of its task, plus same-named structures of other tasks. Ambiguous incoming names and conflicting
relationships fail before publishing. `operation` records `replacement_policy`, `replaced_structure_ids` and
`replacement_id_map`, and a run that named a path adds [`runtime`](#what-a-run-records).

**`POST /api/cases/{id}/refinements`** takes `{revision_id, structure_id, operation}` and returns a `refine`
job. `operation` is `{type, seed_ras?, bounds_ras?}`, where `keep_component` and `remove_component` need a
seed inside the mask, and `keep_region` and `erase_region` need bounds with positive extent. Components are
six-connected. Regions select voxel centres inside world-axis-aligned bounds. An operation that changes
nothing or empties the mask is refused.

**`POST /api/cases/{id}/activate`** takes `{revision_id}` and returns the Case. This is undo and redo:
masks, volumes and relationships return exactly.

**`PATCH /api/cases/{id}/revisions/{rid}`** takes `{review_status}` and returns the Revision. An image-only
revision has nothing to review, so it is refused.

## Jobs

**`POST /api/jobs/{id}/cancel`** returns the Job. A queued job is cancelled at once. A running job is marked
and stops at its next check. Once a DICOM SEG send has started, cancelling stops only the local wait; see
[DICOM SEG export](#dicom-seg-export).

**`GET /api/jobs/{id}/measurements`** returns `{structures, completed, total}`, so a client can show
measurements while surfaces are still building. They are kept out of ordinary polls.

## Corrections

### Session routes

**`POST /api/cases/{id}/reviews`** takes `{revision_id, structure_id, relationship?, method?}`. `method` is
`direct` or `nninteractive`, the API default; the browser starts with `direct`. The revision must be active
and contain the structure. Starting a review changes no mask.

**`POST /api/cases/{id}/reviews/new-structure`** takes `{revision_id, name, group?, color?, relationship?,
method?}` and creates an empty draft target on the reference grid with a server-generated `custom:<id>`. No
revision or surface exists yet. `review-<sid>-0` exposes the empty target on the read-only routes. Duplicate
names keep distinct IDs, and discarding adds nothing.

**`PATCH /api/cases/{id}/reviews/{sid}`** takes `{expected_generation, prompts, options?, relationship?}`
and replaces the complete ordered prompt list. Dropping the last prompt is undo, and an empty list resets.
Autosave never runs inference. Appending keeps the last candidate for preview and marks the draft dirty.
Editing an applied prompt drops later boundaries and invalidates the candidate. Undoing back to an exact
applied snapshot restores identical bytes under a fresh alias with no work.

**`POST .../reviews/{sid}/predict`** takes `{generation, full_refine?, inference_path?, replay?}` and returns
a `review_predict` job. It applies every pending mark. With none pending, it needs `full_refine`, a changed
relationship or `replay`. A policy-only Apply reuses the previous prediction with no inference. `full_refine`
is nnInteractive only: it copies the current candidate, resets prompt channels and refines the whole mask, and
can take longer than a local correction. Completion publishes a candidate and no revision. `inference_path` and
`replay` are covered under [Choosing a path](#choosing-a-path).

**`POST .../reviews/{sid}/accept`** takes `{generation}` and returns a `review_accept` job. The candidate
must belong to the current generation and change masks or relationships, and the base must still be active.
A candidate can be empty only for a direct edit of an existing segment, which saves it at 0 mL. A new
segment must be nonempty. The revision is marked `corrected`, and revision, active case, job and session
commit in one transaction.

**`POST .../reviews/{sid}/discard`** takes `{expected_generation}`, closes the draft, invalidates its
aliases and cancels pending work.

Stale generations, closed or busy sessions and a changed active base return 409. Prompts and completed
candidates survive a restart.

### Prompts

`id` is unique within a draft: 1–80 letters, digits, underscores or hyphens. `include` is a boolean.

| Kind       | Fields                                                                | Method        |
| ---------- | --------------------------------------------------------------------- | ------------- |
| `point`    | `ras: [R, A, S]`                                                      | nnInteractive |
| `bbox`     | `axis`, `index`, `vertices: [[u0, v0], [u1, v1]]`                     | nnInteractive |
| `lasso`    | `axis`, `index`, `vertices`, at least 3 with nonzero area             | nnInteractive |
| `scribble` | `axis`, `index`, `vertices`, at least 2; `radius_mm` 0.5–30, default 3 | Both         |
| `scissors` | `operation`, `shape`, `vertices`, `ras_to_clip`; `include: false`     | Direct        |

- **Limits.** 128 prompts, 2048 vertices per gesture, 8192 vertices per draft.
- **Points** must lie inside the reference grid. An omitted `kind` means `point`.
- **Drawings.** `axis` is native i, j or k (0, 1, 2), `index` is an integer slice, and u, v are the two
  remaining axes in increasing order, as continuous voxel-centre coordinates within `[0, shape-1]`. Brush
  distance uses the affine's in-plane metric. Boxes span exactly one slice. 3D boxes are not supported, and
  a patient-aligned rectangle on an oblique plane is never converted into a native box.
- **Direct edits** accept `scribble` (`include: true` paints, `false` erases) and `scissors` only.
- **Scissors.** `operation` is `erase_inside` or `erase_outside`, and `shape` is `freehand`, `rectangle` or
  `circle`. `vertices` are 3–2048 screen points within [−1, 1], and `ras_to_clip` is an invertible
  orthographic 4×4 matrix; perspective matrices are refused. A rectangle needs four ordered axis-aligned
  corners. A cut covers the selected segment's whole depth.

`options` is `{autozoom: true, start_empty: false}` by default. `start_empty` changes the proposal's
initialization and leaves the saved mask untouched. Changing options resets applied boundaries.

`batches` records each Apply as `{end, autozoom, full_refine, legacy?}`, where `end` is the cumulative
prompt count and `legacy` marks older pointwise sessions.

### Candidate aliases

`revision_id` on a candidate is `review-<session>-<generation>`. It works on the routes marked in the [route
index](#route-index), with case membership checked on every request. It never appears in history and cannot
supply exports. `GET .../revisions/{alias}` returns a read-only view whose `source` is
`direct_edit_candidate` or `nninteractive_candidate`. Direct candidates set `preview_3d_ready` and
`model_manifest_url`, reusing surface keys for unchanged structures. nnInteractive candidates have no
prepared surfaces.

## Segment relationships

Relationships describe voxel ownership. A draft stores `{kind, parent_id?, exclusive_with: string[]}`.

| Kind          | Requires                     | Effect                                                |
| ------------- | ---------------------------- | ----------------------------------------------------- |
| `independent` | No parent or peers           | Keeps every other mask; overlap is reported           |
| `subregion`   | `parent_id`, no peers        | Preserves the parent; does not crop or prove containment |
| `subdivision` | At least one `exclusive_with` | Named peers lose overlapping voxels; a parent is optional |

- For each selected peer B and target A, `B_candidate = B_original & ~A_candidate`.
- Parents, ancestors and descendants are protected from replacement. Peers with declared parents must share
  the target's parent.
- Exclusivity applies only to selected peers, never transitively. Cycles, self-references and foreign IDs
  fail.
- Names, colours, display groups and geometric intersection never authorize a removal. Exact
  TotalSegmentator lung-lobe IDs produce a `relationship_suggestions` entry, which still needs selecting.
- Changing the policy invalidates acceptance of an older candidate. Masks and hashes are rechecked on Save.
- Save updates reciprocal peer links in the same revision. Removing an exclusive link does not restore
  voxels removed in an earlier revision, and activating the parent revision restores everything exactly.
- An existing nonempty segment can save a relationship-only change with no voxel edit.

## Inference paths

An inference path is an interpreter plus a device that AI jobs run on. `default` is the API's own interpreter
with `SCAN3D_DEVICE`, and an added path is one proof file. [INSTALL.md](INSTALL.md#add-a-second-gpu) adds one,
and [ARCHITECTURE.md](ARCHITECTURE.md#inference-paths) explains how they work. A request that names no path
runs on the default path, or on a draft's own path once it has one.

### Listing paths

`GET /api/segmentation-options` and `GET /api/interactive/status` return the same `inference_paths` rows:
`default` first, then added paths by `id`, so there is always at least one row. Neither route starts a process,
loads PyTorch or touches a GPU, and no row holds a local path or proof text. `GET /api/health` is unchanged.

| Field            | Meaning                                                                                  |
| ---------------- | ---------------------------------------------------------------------------------------- |
| `id`             | The value to send as `inference_path`                                                    |
| `label`          | The GPU's name. `default` falls back to `Default device`, and shows `CPU` when `SCAN3D_DEVICE` is `cpu`. An added path whose proof names no GPU shows its `id` |
| `memory_gib`     | The GPU's memory to the nearest GiB, or null if unknown                                  |
| `shares_display` | True when a GPU of this vendor has a connected display now, so its free memory depends on what is on screen. False when that cannot be read |
| `available`      | Whether a request may name this path: what its proof records is still installed, and its last check passed. It never means a scan will fit. Always true for `default` |
| `reason`         | A fixed sentence, or null                                                                |

An added path with `available: false` gives the first of these that applies:

- `Its GPU check record cannot be read.`
- `Its last GPU check failed.`
- `Its last install did not finish.`
- `Its Python environment is missing.`
- `Its PyTorch or GPU runtime packages changed after the last GPU check.`
- `Its TotalSegmentator, nnU-Net or nnInteractive version differs from this workstation's.`
- `It has cupy or cucim installed, which would change resampling.`
- `The GPUs in this workstation changed after the last GPU check.`

A proof records no other package, and the version sentence also covers `nninteractive-client`.

`default` gives null or the caution `Its last GPU check failed or is out of date.`, which blocks nothing. A proof
taken before its vendor's cards changed earns that caution and loses its card: `label` is `Default device` and
`memory_gib` is null, so a job queued then freezes that label, not a card that has left. `identity` stays
`default`. A proof with no record of the cards is not compared.

### Choosing a path

`inference_path` is optional on `POST .../segment` and `POST .../reviews/{sid}/predict`. It must match
`^[a-z0-9][a-z0-9-]{0,31}$`, and it never goes inside `options` or `batches`. On a segmentation, leaving it out
and sending `default` run the same way and share saved results. `scan3d segment --inference-path ID` takes the
same IDs and gets the same refusals, and `scan3d doctor` lists the rows.

A job's path is frozen when it is queued, and an added path is checked again as the job starts. A job on an
added path never moves to another path or to the CPU. `default` follows `SCAN3D_DEVICE`, so under `auto` it can
still run on the CPU. A draft is free until a model result publishes, then stays on that result's path while
`batches` is non-empty:

| Draft           | Request                                 | Result                                               |
| --------------- | --------------------------------------- | ---------------------------------------------------- |
| `batches` empty | `inference_path`, or none for `default` | Runs there, and a published result binds the draft   |
| `batches` set   | None, or the draft's own path           | Runs on the draft's path                             |
| `batches` set   | Another path, with `replay` false       | 409                                                  |
| `batches` set   | `replay: true`                          | Repeats the draft's steps on the named path, or on its own path if none is named |

**`replay`** repeats every recorded batch exactly, from the original mask, and adds any pending marks as one
more batch. Nothing need be pending, and a pending relationship goes with it as a model run. The re-run takes a
new generation, so as it is queued the old candidate turns `dirty` and can no longer be accepted. That candidate
stays in `candidate` until the new one publishes, which is also when the draft's `inference_path` changes. After
a failed or cancelled re-run it stays there, still `dirty`, and the draft can re-run on any available path, its
own included. On its own path, a worker that still holds those batches reuses them and runs only pending marks,
if any. With `batches` empty, `replay` adds nothing, though `replay` with `full_refine` is still refused.

Direct Apply and a relationship-only Apply run no model. They ignore `inference_path`, their jobs name no path,
and they stay on the CPU lane. A direct draft ignores `replay` too.

### What a run records

A segment revision from a job that named a path, `default` included, has `operation.runtime`. One from a job
that named none does not.

| Key                    | Meaning                                                                          |
| ---------------------- | -------------------------------------------------------------------------------- |
| `inference_path`       | The path's `id`                                                                  |
| `gpu_name`             | The GPU the child measured, or null on the CPU                                   |
| `device`               | The device it selected, such as `gpu:0` or `cpu`                                 |
| `torch_version`        | The PyTorch version it measured                                                  |
| `hip_version`          | The build's HIP version, or null                                                 |
| `cuda_version`         | The build's CUDA version, or null                                                |
| `reused`               | True when this revision reused a saved result, so every value above is that original run's |
| `results_moved_to_cpu` | True when nnU-Net re-ran part of a task with its results in system memory, usually because GPU memory ran out |

Only runs on an added path are checked for that last key, so on a job that named `default`, false means not
detected. A true value also adds `Part of <task> re-ran with results in system memory on <label>, usually because
GPU memory ran out. The run took longer.` to the revision's `qc`.

A saved correction's `operation.runtime` records what made its candidate. nnInteractive's provenance holds the
same `device`, `gpu_name`, `torch_version`, `hip_version` and `cuda_version`, plus `inference_path` when the job
named a path, and its `miopen_find_mode` is null unless the build is HIP. A direct edit records its own
provenance, with `device: "cpu"` and no GPU or PyTorch keys. A relationship-only result reuses its prediction's
provenance, or has `engine: "mask-relationship-processing"` and none of these keys when there is no prediction
to reuse.

### Refusals and failures

A bad request is refused before any job exists:

| Status | When                                     | `detail`                                                                        |
| ------ | ---------------------------------------- | ------------------------------------------------------------------------------- |
| 422    | A malformed ID, or `replay` on a segment | FastAPI's validation list                                                       |
| 400    | The path is not set up                   | `This GPU is not set up on this workstation.`                                   |
| 400    | The path is unavailable                  | `<label> is unavailable: <reason>`                                              |
| 400    | `replay` with `full_refine`              | `A re-run repeats the draft's own steps, so it cannot add a whole-object refinement` |
| 409    | Another path on a draft with `batches`   | `This draft's AI results came from <label>. Re-run its steps to use another GPU.` |

Once queued, a problem fails the job and never the route. The job has `status: "failed"` and keeps its
`inference_path`, and on a review job its `error` also reaches the draft's `error`. On an added path, these
errors name the cause:

- A path that was removed: `<label> is no longer available: This GPU is not set up on this workstation. Nothing
  ran and no other GPU was tried.`
- A path that became unavailable: `<label> is no longer available: <reason> Nothing ran and no other GPU was
  tried.`
- A different card or device under that ID: `<label> changed after this job was queued. Nothing ran and no other
  GPU was tried. Start it again.`
- A device check where PyTorch reports no GPU: `GPU inference was requested but PyTorch cannot access a GPU; check
  the driver, then repeat this GPU's check (see Add a second GPU in docs/INSTALL.md). No other GPU was tried.` Both
  engines give this. `SCAN3D_DEVICE=cpu`, which the default path's sentence offers, cannot change an added path.
- A device check that measures anything else: `Expected <label>, but the GPU check found <name> (<device>,
  <build>). No result was kept and no other GPU was tried.` `<build>` is `CUDA` or `no CUDA`.
- A crop-model task such as `teeth` on a GPU index above 0: `TotalSegmentator would run part of <task> on the
  first GPU, so it cannot run on <label>. Nothing ran and no other GPU was tried.`
- A task that stopped: `TotalSegmentator <task> failed on <label>: <cause>. No other GPU was tried.` `<cause>` is
  `it ran out of GPU memory`, `the process was killed, usually because system memory ran out` or `exit <code>`.
- A report from another PyTorch: `TotalSegmentator <task> ran on PyTorch <version>, not the <version> that <label>
  was checked on. No result was kept and no other GPU was tried.`

None of these apply on `default`, whose errors keep their existing text. On a job that named a path, `default`
included, a child that will not stop gives `The AI process did not stop in time.`, and an absolute path in any
other error becomes `<local path>`. A web address with a host stays, such as the link that ends PyTorch's
out-of-memory error. The service log keeps the detail.

## Viewer data

### Volume descriptors

`image-volume` and `ct-volume` are the same handler. `quality` is `full` (default) or `preview`.

```text
{schema_version: 1, key, kind: "ct", dtype, quality, approximate, preview_quantization_hu,
 reference_geometry, shape, affine_ras, geometry, slope, intercept, byte_length, chunk_depth: 16,
 chunks: [{index, start_k, depth, byte_offset, byte_length, url}],
 reference_image_id, modality, scalar: {units}, presentation, source}
```

Samples are little-endian in i-fastest order, in slabs of 16 k-slices. Chunks are gzip, so a browser decodes
them transparently. `key` is a content-derived SHA-256.

- **Full** keeps exact int16 or uint16 samples, with `slope` and `intercept` for scaling. Other source types
  use float32 with scaling applied.
- **Preview** fits a grid of at most 192 per dimension, with `approximate: true`. Voxel faces match the
  reference field of view, so `shape` and `affine_ras` differ from `reference_geometry`. CT uses int16 at a
  1 HU step. A zero `preview_quantization_step` keeps float32 for MRI signal.

**`mask-volume`** takes `structure_id`, `compare`, `quality` and `margin_mm` (0–5), and returns
`{schema_version, current, parent}`. Each entry is a cropped binary descriptor with `kind: "mask"`, `dtype:
"uint8"`, `origin_ijk` and `empty`, or null. `compare` adds the same structure from the parent revision.
Chunk URLs carry `structure_id`, `parent`, `quality` and `margin_mm`, and the route checks the key against
the requested case, revision and structure.

A nonzero `margin_mm` grows a display copy by physical distance through the affine metric. The source mask,
measurements and history do not change.

**`volume-mask`** takes repeated `structure_ids` and `margin_mm`, and returns `{schema_version, source_id:
"volume-mask-union:<key>", current}`. `current` is one OR union with `presentation_only: true`,
`volume_mask_union: true`, `pooling: "foreground-centre-occupancy"`, `approximate`,
`selected_structure_ids`, `native_crop_origin_ijk` and `native_crop_shape`. It is cropped to the selection's
bounds and capped at 8 MiB. An all-empty selection is a valid zero mask, and an empty selection is a 400.
One native mask is decoded at a time. A cancelled build publishes nothing.

**`volume-labels`** takes repeated `structure_ids` in painting order, and returns the same shape with
`source_id: "volume-label-union:<key>"`, `volume_label_union: true`, `volume_mask_union: false` and
`labels`. Value _n_ marks the _n_-th listed structure, 0 is background, and later structures win overlaps.
It holds at most 255 structures and takes no margin. Another order is another resource.

Both unions are presentation artifacts. Never measure them or use them as masks.

### Review frames

**`review-frame`** returns one exact reference-grid slice, as gzip bytes with its metadata in the
`X-CT-Frame` header.

| Parameter                     | Meaning                                                          |
| ----------------------------- | ---------------------------------------------------------------- |
| `plane`, `position_mm`        | `axial`, `coronal` or `sagittal`, at a patient position          |
| `native_axis`, `native_index` | A source-index plane instead of a patient plane; send both       |
| `structure_id`                | One overlay, using the original response schema                  |
| `structure_ids` (repeated)    | Up to 128 independent overlays                                   |
| `compare`                     | Adds each structure from the parent revision                     |

The header holds `{meta, dtype: "float32", hu_encoding: "byte-shuffle", pixels, current_offset,
parent_offset, overlays?}` plus the image context. `meta` has `width`, `height`, `pixel_to_ras`,
`position_mm`, `plane`, `extent_mm`, `overlay_colors` and, for native planes, `native: {axis, index}`.

The decompressed body starts with `pixels × 4` bytes of unwindowed float32 values, with their four byte
planes grouped before compression. Reconstruct by interleaving those planes. Binary overlays follow at the
listed byte offsets, one byte per pixel. With `structure_ids`, `overlays` lists `{id, current_offset,
parent_offset}`, and a missing parent label has a null offset.

Window and level are never part of this request. In native mode the index is authoritative, and drawing
capture maps pixels back through `pixel_to_ras`. Native frames keep one pixel per voxel. A frame above 8 Mi
pixels, or 256 MiB decoded with its overlays, fails rather than being reduced.

**`probe?x=&y=&z=`** returns `{hu, value, units, voxel, reference_image_id}` at a RAS point. `hu` and
`value` hold the same number.

### Surfaces

**`model-manifest`** returns `{schema_version: 1, structures: [{id, key, url}]}`, and
**`surfaces/{key}.glb`** serves one structure with immutable caching. Key membership is checked against the
revision. Clients should prefer the manifest, reusing unchanged objects by `key`.

**`model.glb?detail=false`** serves the combined model. `detail=true` builds a detailed export on first
request, one at a time, and never activates a revision or changes review status.

**Coordinates.** World coordinates, after node transforms, are metres: scene = (−R, S, A) / 1000, and RAS =
(−x, z, y) × 1000. GLB nodes are named by stable structure ID.

**Display encoding.** Display GLBs need `KHR_mesh_quantization`.

- Positions are uint16 with byte stride 8 on one absolute 2⁻¹⁵ m grid shared by all structures, dequantized
  by the node's uniform `scale` and `translation`.
- Normals are normalized int8 with byte stride 4, and indices are uint16.
- A mesh above 65,535 vertices becomes consecutive primitives, repeating only seam vertices.
- Position error is at most 0.0153 mm per axis (0.0264 mm in 3D), and normals are within 0.4°.

Clients must use world-space results, never raw position attributes, which hold grid offsets. Detailed GLBs
are float32 and uint32 with untransformed nodes. Revisions saved before quantization keep their float32
display assets until `scan3d optimize` re-imports them.

**Negotiation.** These routes serve stored gzip files through `Accept-Encoding`, honouring quality weights
and explicit refusal. Responses vary on `Accept-Encoding`, use representation-specific ETags, and support
`If-None-Match`, which takes precedence over `If-Modified-Since`. Byte ranges use the original GLB bytes and
keep `If-Range` behaviour. A request that forbids every representation gets 406. If compression fails, the
raw GLB stays available.

### Cinematic references

**`cinematic-reference`** measures median HU for a saved revision, for the Cinematic heart preset. It
returns `{schema_version, myocardium_hu, blood_hu, myocardium_structure_ids, blood_structure_ids,
myocardium_voxels, blood_voxels, blood_source, method}`. It pools native samples from myocardium-named and
aorta-named masks, falling back to left heart, and excludes values at or below −900 HU. Masks without 100 HU
of separation return 400, with guidance to enter values manually.

## Agreement

Reports compare each saved structure with its model original: the nearest mask in the parent lineage that is
a saved inference output with a matching ID. A later run that replaced the structure becomes the new
original. Reports are read-only.

**`GET .../revisions/{rid}/agreement`** returns:

```text
{schema_version: 1, case_id, revision_id, review_status, synthetic, active,
 aggregate_pool: "real" | "synthetic" | null, implementation, hd95_exact,
 grid: {voxel_volume_ml, spacing_mm, sheared},
 summary: {structures, compared, unchanged, changed, excluded, dice_mean, dice_median,
           abs_volume_change_ml_mean},
 structures, excluded, cache: {hits, computed}}
```

Each row in `structures` has `id`, `name`, `task`, `group`, `origin: {kind, revision_id, run_id?, task?,
engine_version?}`, `basis` (`same_file`, `same_digest` or `voxels`), `unchanged`, `dice`, `hd95_mm`,
original and current voxels and volumes, `volume_change_ml` (accepted minus original), `added_voxels` and
`removed_voxels`. Synthetic demo cases use their phantom as the origin.

- Dice is `2|A∩B| / (|A| + |B|)`.
- HD95 is the larger of the two directed 95th percentiles of distances between face-exposed surface voxel
  centres, in millimetres through the full affine. It is null when either mask is empty.
- Identical files or bytes count as unchanged without decoding. Other pairs are cached by both mask digests,
  grid and implementation.

`excluded` rows have `{id, name, task, group, code, reason}`, where `code` is `reviewer_created`,
`no_model_output`, `model_output_unavailable`, `different_image` or `unreadable`.

**`GET /api/agreement`** pools each case's active revision when its status is `reviewed` or `corrected`:

```text
{schema_version: 1, implementation, selection,
 cases: {total, included, skipped: {no_active_revision, no_segmentation, unreviewed, unavailable}},
 pools: {real: Pool, synthetic: Pool},
 revisions: [{case_id, revision_id, review_status, pool}]}
```

A pool is `{cases, excluded, summary, tasks: [{task, ...stats, structures: [{id, name, task, ...stats}]}]}`.
Stats are `reviewed`, `unchanged`, `unchanged_percent`, `dice_mean`, `dice_median` and
`abs_volume_change_ml_mean`. Synthetic cases never enter the real pool.

## Exports

### Slicer

**`GET .../slicer.zip`** returns `reference.nii.gz`, `segmentation.seg.nrrd` and `mapping.json`. Overlapping
masks go to separate layers, and stable IDs are preserved. **`POST /api/cases/{id}/corrections`** takes
multipart `revision_id` and `segmentation` and returns a `correction` job. It verifies geometry, segment
identity and the source hash, handles cropped labelmaps, and marks the new revision `corrected`.

### Offline HTML

**`POST .../export.html`** takes `{layers, state}` and streams a self-contained HTML file. **`GET
.../export.html`** returns a segmentation-only viewer with default settings.

`layers` is `{segmentation, ct_volume, ct_plane}`, and at least one must be true. Excluding segmentation
omits its GLB, and excluding both CT layers omits all voxel data. Masks are embedded only when plane
overlays or volume masking need them.

`state` carries the saved display snapshot:

- `display`: per-structure `{visible, opacity, color?}`, plus `selected` and `segmentation_opacity`.
- `volume`: `{preset, shift, density, quality, crop, cinematic, signal_window?}`. `preset` is one of the
  bundled Slicer appearance IDs. MR exports must use `MR-SignalWindow`.
- `volume_mask`: `{enabled, mode: "inside" | "outside", ids, margin_mm}`.
- `plane`: `{plane, axis, index, window, level, overlays}`, where `axis` must match `plane`.
- `camera`: `{position_ras, target_ras, up_ras, zoom, height_mm, name?}`.

Structure references must belong to the saved revision. Limits are 512 MiB of CT samples, 192 MiB of
selected masks, 128 MiB of surfaces, 512 MiB of HTML and 256 embedded masks, and oversized exports return
413 before streaming. The manifest uses a field allowlist, so paths and acquisition identifiers stay out.

## DICOM SEG

### DICOM SEG import

**`POST /api/cases/{id}/dicom-seg/imports`** takes multipart `revision_id` and `files` (1–64 local SEG
objects, not ZIPs) and returns a `dicom_seg_import` job. Total bytes are bounded by 512 MiB and the upload
limit. The revision must be active with a prepared classic CT and its complete original series. It needs no
prior AI segmentation or Orthanc connection, and it sends nothing.

**Accepted.** Part 10 Segmentation Storage with `BINARY` pixels in uncompressed Explicit or Implicit VR
Little Endian. Refused: fractional, labelmap, concatenated, compressed, deflated and resampled SEG. Values
are never thresholded or interpolated. Limits are 1000 segments, 100,000 frames, a 512 Mi voxel grid, 16 Mi
pixels per frame and 64 MiB of metadata per object.

**Matching.** Patient, study, frame-of-reference and referenced series and SOP identities must match the CT.
Geometry is checked independently: patient functional groups map each frame through the full affine to
native integer voxels, and every frame corner must agree within both 0.05 voxel per axis and 0.05 mm.
Cropped, reordered and reflected frames and sparse planes are accepted when the mapping is proven. Repeated
assignments, unknown segments and foreign references are refused.

**Result.** Each segment gets a `dicom_seg:<SOP hash>:<SegmentNumber>` identity, keeping its label, colour
and coding provenance. Names never replace existing anatomy, and duplicate identities are refused. Existing
masks stay in the new revision, which has `source: "dicom_seg_import"` and is unreviewed. Inputs are
rechecked before publication, and activation commits only while the original parent remains active. Matching
duplicate requests reuse the queued or running job.

### DICOM SEG export

Export needs a configured Orthanc and a saved revision whose reference image is an original classic CT
imported from that archive. `SCAN3D_ORTHANC_EXPORT_DESTINATIONS` is an allowlist of configured Orthanc
modality IDs; leaving it empty allows archive-only export.

**`GET .../dicom-seg`** returns `{available, reason?, destinations: [{id, name, aet}], exports: Job[],
encoding_version}`. `exports` is scoped to the revision, newest first, and addresses stay server-side. An
unavailable revision has a `reason`, for example a local-only source, an MR image or an image-only revision.

**`POST .../dicom-seg`** takes `{structure_ids?, destination?, resend?, overlap_priority?}` and returns an
`orthanc_export` job. Omitted IDs select all nonempty masks. A null destination archives only. Export never changes review
status, adds a revision or activates one.

**Encoding.** All selected structures become numbered segments of one binary SEG instance in a new series,
keeping original patient, study, frame and source references, names and colours. Overlaps are preserved, and
segment identification is written on each frame. The grid is never resampled. Source pixels must map to
unique integer native voxels within the tolerances above, and dimensions, axis correspondence and complete
slice coverage are mandatory. A combined export is capped at 512 MiB and 100,000 frames, and exceeding that
fails before upload. Corrected masks are described as semiautomatic. The private coding scheme `99SCAN3D`
defines `REGION` and `SEGMENTATION`, which can produce unknown-vocabulary validator warnings.

**Overlap resolution.** `overlap_priority` ranks every selected structure once, highest first, and needs at
least two. Each voxel then stays only in the highest-ranked structure that holds it, in this SEG only. Saved
masks, measurements and review status do not change. Segment numbers follow the rank, `SegmentsOverlap` is
`NO`, the series description ends `overlaps resolved`, and a segment that lost voxels is described as
semiautomatic. A structure that would lose every voxel stops the export, and the error names its rank. Each
ranking is a separate artifact of the same saved inputs, so **Send again** needs the exact ranking.
`export.json` adds `overlap_priority`, and `removed_voxel_count` on each segment that lost voxels. Its
`voxel_count` is the exported count, and `mask_sha256` still names the saved mask. The job's `dicom_seg` result adds
`overlap_priority` and `removed_voxels`, keyed by structure ID. An export without a ranking is unchanged.

**Identity.** Enqueue freezes mask file identities, and later changes are refused. `export.json` pins the
source, reference, mask and SEG hashes before any network write, and `delivery.json` holds transfer
receipts.

**Result.** `result.dicom_seg` has `export_id`, `encoding_version`, `archived`, `sent`, `segments_count`,
`instance_count`, `structure_ids` and optional `destination` and `destination_id`. It has no patient fields,
paths or credentials. `sent` is true only after an exact successful C-STORE count.

**Retries.** A duplicate request reuses the job and artifact. An explicit retry reuses the exact SOP bytes
and checks any known remote job. `resend: true` needs a destination and a known successful send, and creates
a new delivery attempt with the same SOPs. A new encoder version creates fresh SOPs on explicit export, and
mask checks still apply.

Polling stops after 120 seconds with an unconfirmed outcome. Startup marks interrupted and queued exports
failed locally and never contacts Orthanc. Cancelling before delivery stops the export. After C-STORE has
started, stopping the local wait cannot cancel the remote transfer, and the job says so.

## Orthanc routes

Set `SCAN3D_ORTHANC_URL`, `SCAN3D_ORTHANC_USERNAME` and `SCAN3D_ORTHANC_PASSWORD` to enable these. See
[ORTHANC.md](ORTHANC.md).

**`GET /api/orthanc/status`** returns `{configured, connected, name, error, version?, api_version?}`. An
unconfigured or unreachable archive is reported here, without upstream detail.

**`POST /api/orthanc/studies/query`** takes `{patient_id?, patient_name?, accession_number?, study_date?,
offset?, limit?}` and returns `{studies, offset, limit, has_more}`. `limit` is 1–50. Values use Orthanc's
DICOM query syntax. Identity fields go in the body, never the URL. A study is `{id, study_uid, description,
study_date, patient_id, patient_name, accession_number, series_count, modalities, stable}`.

**`GET /api/orthanc/studies/{id}/series`** returns `{study, series}`. A series is `{id, description,
modality, series_uid, instance_count, expected_instances, stable, completeness, snapshot, warnings,
supported, unsupported_reason}`. `completeness` is `receiving`, `incomplete`, `complete` or `unknown`.

**`POST /api/orthanc/imports`** takes `{series_id, snapshot, label?}` and returns an `orthanc_import` job.
The snapshot is checked again before a case is created. Receiving and incomplete series, and any change
since selection, return 409. A stable series with an unknown expected count imports with a warning, because
stability only means no recent arrivals.

The job checks the archive before and after transfer, and independently verifies bytes, lengths, membership
and DICOM identities. A completed case holds the originals with no revision yet, and `process` prepares it
next. Requests for the same snapshot return the same job. Failed or cancelled imports can be retried, never
overwriting earlier bytes.

**`POST /api/orthanc/launch`** takes `{series_id, source_key}` and returns `{study, series, case_id, job}`.
It reads the current series and finds a matching import, creating nothing and transferring no DICOM.
`case_id` identifies a valid completed import, `job` an existing pending one, and otherwise both are null.
`source_key` is the SHA-256 of the normalized archive URL: an identity check, not a credential. A different
archive is refused before any network access, and an import that fails verification returns 409.

The viewer accepts launch links as `#orthanc-series=<id>&orthanc-source=<key>`. New imports still need an
explicit action.

# HTTP API and data model

The contract between the 3DScan server and its clients. Routes live under `/api`, on
`http://127.0.0.1:9010` by default, with generated OpenAPI docs at `/docs` while sign-in is off.
[ARCHITECTURE.md](ARCHITECTURE.md) explains the system behind them.

## Conventions

- **Authentication.** A session cookie while sign-in is on, none while it is off. See
  [Sign-in](#sign-in).
- **Identifiers.** Case, revision, job, session and upload IDs are 32 lowercase hex characters.
  Malformed is 400. Well-formed but unknown is 404.
- **Coordinates.** Patient RAS millimetres. Voxel arrays use nibabel order (i, j, k) with the full
  affine.
- **Long work** returns a [Job](#job). Poll `GET /api/jobs/{id}` until `status` is final.
- **Bodies.** JSON bodies reject unknown fields and non-finite numbers. Times are UTC ISO 8601.
- **Legacy names.** The `ct-volume` routes repeat `image-volume` for older clients. `hu`,
  `kind: "ct"` and `X-CT-Frame` cover any scalar image, not only Hounsfield units: read
  `scalar.units` and `modality`.

## Errors

Errors return `{"detail": string}`. Schema errors use FastAPI's validation list. A rejected `Host`
or an unexpected 500 is plain text.

| Status   | Meaning                                                                                                                                                                                                                                                        |
| -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 400      | `ValueError`: invalid input. Also a `Host` outside the allowlist                                                                                                                                                                                               |
| 401      | No valid session, or a failed sign-in                                                                                                                                                                                                                          |
| 403      | A cross-origin mutation, access this account lacks (`SessionGate`, `errors.Forbidden`), or a wrong password on Delete forever                                                                                                                                  |
| 404      | Absent (`errors.NotFound`), in another case, in Trash, deleted forever, or an unfinished upload another account started                                                                                                                                        |
| 406      | No acceptable GLB encoding                                                                                                                                                                                                                                     |
| 409      | Stale state, so reload before retrying; work under way must finish first; or a [People](#people-routes) change that would leave no admin or link a disabled account: `ReviewConflict`, `ImportConflict`, `OrthancChanged`, `ArchiveChanged`, `errors.Conflict` |
| 410      | A set-password link no longer works                                                                                                                                                                                                                            |
| 411, 413 | A sign-in, set-password or captured-view body without a plain `Content-Length`, or over its limit. 413 also means an offline HTML export is too large (`ExportTooLarge`)                                                                                       |
| 422      | Schema validation failed                                                                                                                                                                                                                                       |
| 429      | Too many password attempts, or no hashing slot within a second                                                                                                                                                                                                 |
| 499      | Client left while a `volume-mask` or `volume-labels` union was being built                                                                                                                                                                                     |
| 500      | Any other exception, including a bare `KeyError`, logged without internal detail                                                                                                                                                                               |
| 502      | The configured Orthanc or DICOMweb archive failed (`OrthancError`, `ArchiveError`)                                                                                                                                                                             |
| 503      | A cached surface or viewer bundle is missing                                                                                                                                                                                                                   |

Schema errors on the license, sign-in, set-password and Delete forever routes use fixed messages,
so a key, password or link code is never echoed.

## Transport rules

- **Origin.** A non-GET request whose `Origin` differs from `Host` gets 403. Hosts must appear in
  `SCAN3D_ALLOWED_HOSTS`.
- **Private responses.** `/api/session`, `/api/session/redeem`, `/api/people`, `/api/people/*`,
  `/api/orthanc/*`, `/api/archive/*`, the license route, Delete forever, `.../dicom-seg`,
  `.../dicom-view`, `.../dicom-view-file` and a prepared `segment.dcm` or `view.dcm` use
  `Cache-Control: no-store`, errors included. Any other `/api/` response without its own header
  uses `private, no-store`.
- **Viewer data.** Volume chunks are gzip, keyed by content. Chunks, surfaces and `model.glb` use
  `Cache-Control: private, no-cache` with an `ETag`, and a matching `If-None-Match` gets a 304.
  `slicer.zip` has the same policy but never answers 304.
- **Compression.** JSON and viewer assets of at least 1 KiB use dynamic gzip. GLB routes negotiate
  stored gzip files. `slicer.zip`, `export.html`, `segment.dcm` and `view.dcm` are left alone and
  keep `Content-Length`.

## Sign-in

With sign-in [off](CONFIGURATION.md#sign-in), every request acts as one built-in local user, an
admin with archive access, and every rule below passes, except that a case in Trash still reads as
absent. In `password` mode `/docs`, `/redoc` and `/openapi.json` are gone, and a request is refused
before its route reads a body.

### Sign-in routes

| Route                               | Result                                                                                                                                            |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/session`                  | Always 200 with the view                                                                                                                          |
| `POST /api/session`                 | Takes `{username, password, shared_device?}`. Returns the view and `Set-Cookie`                                                                   |
| `DELETE /api/session`               | Ends any session, clears its cookie, sends `Clear-Site-Data: "cache"` and returns the view                                                        |
| `POST /api/session/sign-out-others` | Ends this account's other sessions. Returns `{signed_out}`                                                                                        |
| `POST /api/session/redeem`          | Takes `{code, password, shared_device?}` from a [People](#people-routes) link, sets that person's password, and returns the view and `Set-Cookie` |

The view is `{mode, user, setup_required}`. `mode` is `off` or `password`. `user` is null or
`{id, username, display_name, role, archive_access}`, and always the local user with sign-in off.
`setup_required` is true in password mode with no accounts.

Refusals of sign-in and redeem:

| Status | `detail`                                                                                        | When                                                                                                                                |
| ------ | ----------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| 400    | `Sign-in is off on this workstation`                                                            | The mode is `off`                                                                                                                   |
| 400    | `A password needs 10 to 1,024 characters`                                                       | Redeem with a live link, which still works afterwards                                                                               |
| 401    | `Wrong username or password`                                                                    | Every failed sign-in, known name or not                                                                                             |
| 403    | `Sign in through the HTTPS address`, or on redeem `Set your password through the HTTPS address` | `Origin` is not https and `Host` is not loopback                                                                                    |
| 410    | `This link has expired or was already used. Ask an administrator for a new one.`                | Redeem with a link that is unknown, expired, used or replaced, for a disabled account, or made by someone no longer an active admin |
| 411    | `A sign-in request must declare its Content-Length.`                                            | No plain declared length, or chunked framing                                                                                        |
| 413    | `The sign-in request is too large.`                                                             | Over 4 KiB                                                                                                                          |
| 422    | `Invalid sign-in request.`, or on redeem `Invalid set-password request.`                        | Schema failure                                                                                                                      |
| 429    | `Too many attempts. Try again in N seconds.`                                                    | A throttled sign-in, or either route waiting over a second for a hashing slot (N is 1), with `Retry-After`                          |

- Each username gets three free failures, then waits of 1, 2, 4, 8 and 16 seconds, then 30 seconds.
  A wait refuses without checking the password and never locks the account. At most two password
  hashes run at once, shared by sign-in, redeem and Delete forever.
- Redeem refuses a dead link before hashing. A link works once, and of two uses at once the first to
  write wins. It signs the browser out of any other person's session.
- A new password, from a link or `scan3d users passwd`, ends that person's sessions and every link
  for or by them.

### Access rules

Every refusal uses `Cache-Control: no-store`. Where two are shown, a request with no session gets
the 401, and a signed-in one the other.

| Rule             | Routes                                                                                                                                                                   | Refusal                                                              |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------- |
| Public           | `GET /api/health`, `GET`, `POST`, `DELETE /api/session`, `POST /api/session/redeem`, the viewer files                                                                    | None                                                                 |
| Signed in        | Every other `/api/` route with no case, job or upload ID, unless listed below                                                                                            | 401 `Sign in to continue`                                            |
| Admin            | `POST /api/cases/import-local`, `GET /api/storage`, `POST /api/storage/clear-caches`, `PUT`, `DELETE /api/totalsegmentator/license`, the [People routes](#people-routes) | 401, 403 `Administrator access is required`                          |
| Archive          | `/api/orthanc/*` and `/api/archive/*`                                                                                                                                    | 401, 403 `This account cannot use the archive`                       |
| Case             | Every route with a case ID in its path, for a case not in Trash                                                                                                          | 401, 404 `Case not found`                                            |
| Case and archive | `GET`, `POST` of `/api/cases/{id}/orthanc-series`, `.../archive-series` and `.../dicom-view`                                                                             | 401, 404 `Case not found`, 403 `This account cannot use the archive` |
| Trashed          | `POST /api/trash/{id}/restore`                                                                                                                                           | 401, 404 `Case not found`                                            |
| Admin, in Trash  | `POST /api/trash/{id}/purge`                                                                                                                                             | 401, 403 `Administrator access is required`, 404 `Case not found`    |
| Job              | `GET /api/jobs/{id}`, `GET .../measurements`, `POST .../cancel`                                                                                                          | 401, 404 `Job not found`                                             |
| Upload           | `POST /api/imports/{id}/parts`, `.../finish`, `DELETE /api/imports/{id}`                                                                                                 | 401, 404 `Upload not found`                                          |

- One shared library: every account opens every case, and case lists, sizes and agreement cover
  every case.
- A case in Trash reads as absent in both modes, with the route's own 404: `Case not found`,
  `Job not found` or `Upload not found`. With sign-in on, a malformed case, job or upload ID is also 404.
- `POST .../dicom-seg` also needs archive access unless `delivery` is `file`, or it is 403
  `This account cannot use the archive`.
- A job follows its case, and so does a finished upload. An unfinished upload belongs to whoever
  started it, or, with no owner or started with sign-in off, to any admin.
- A session cookie on a request other than GET, HEAD or OPTIONS with no `Origin` gets 403
  `Cross-origin mutations are not allowed`.
- A route with no rule gets 403 `This route has no access rule`, in either mode.

### People routes

These exist only in `password` mode, for an admin. Each change is audited and returns the `Person`
unless noted. A change to what the person already has succeeds and writes nothing.

| Route                            | Change                                                                                                                                            |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/people`                | None. Every account, oldest first, as `Person[]`                                                                                                  |
| `POST /api/people`               | Takes `{username, display_name?, role?}` and adds a person, a member by default, with no password and archive access on. Returns `{person, link}` |
| `POST /api/people/{id}/link`     | Returns a new link, `invite` before the person has a password and `reset` after, ending any earlier one                                           |
| `POST /api/people/{id}/role`     | Takes `{role}`, `admin` or `member`                                                                                                               |
| `POST /api/people/{id}/archive`  | Takes `{archive_access}`                                                                                                                          |
| `POST /api/people/{id}/disable`  | Stops sign-in and ends the person's sessions and links                                                                                            |
| `POST /api/people/{id}/enable`   | Allows sign-in again, bringing back no session or link                                                                                            |
| `POST /api/people/{id}/sign-out` | Ends the person's sessions. Returns `{signed_out}`                                                                                                |

- A `Person` is `{id, username, display_name, role, archive_access, state, created_at,
last_seen_at, link_expires_at}`. `state` is `active`, `invited` before a password is set, or
  `disabled`. `last_seen_at` is the last sign-in, set-password or session slide, and
  `link_expires_at` is when the person's working link expires, or null if none.
- A link is `{code, purpose, expires_at}` and lasts 24 hours. The answer is the only place `code`
  appears. [Redeem](#sign-in-routes) takes it.
- A link works only while its maker is an active admin: the `admin` role, not disabled, with a
  password. Disabling or demoting the maker ends their links, and undoing that brings none back.
- Work already queued or running finishes when archive access goes off or a person is disabled.

| Status | `detail`                                                      | When                                                                |
| ------ | ------------------------------------------------------------- | ------------------------------------------------------------------- |
| 400    | `A username needs 1 to 64 letters, digits or . _ @ -`         | Add                                                                 |
| 400    | `A display name needs 1 to 128 printable characters`          | Add                                                                 |
| 400    | `That username is already in use`                             | Add, with a name taken in any case                                  |
| 400    | `You cannot change your own role or disable your own account` | Role or disable on your own account                                 |
| 403    | `Administrator access is required`                            | A member, or an admin demoted or disabled since the request started |
| 404    | `Person not found`                                            | No account has that ID                                              |
| 409    | `Keep at least one administrator who can sign in`             | Demoting or disabling the last active admin                         |
| 409    | `Enable this account before making a link`                    | A link for a disabled account                                       |

### Session cookie

- The name is `scan3d_session`, or `__Host-scan3d_session` with `Secure` on an https `Origin`.
  Attributes are `HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000`, with no `Domain`.
- A session lasts 30 days and slides. Its expiry moves at most every 10 minutes, with a fresh
  `Set-Cookie` under the same name.
- With `shared_device`, the cookie has no `Max-Age`, the session never slides and it ends after 12
  hours. Responses on non-public routes then use `Cache-Control: no-store`.

## Data model

Public records never contain filesystem paths or archive credentials. `added_by` and `started_by`
give the display name of whoever added the case or started the job or draft when that is someone
else, else `null`. With sign-in off they are always `null`.

### Case

```text
{id, label, created_at, status, active_revision_id, selected_series_uid, phase,
 series: Series[], error?, import_source?, synthetic?}
```

`status` is `importing`, `imported`, `ready`, `import_failed` or `import_cancelled`. Orthanc imports
set `import_source`: `{kind, source_key, study_uid, series_uid, frame_of_reference_uid, snapshot,
instance_count, content_digest, manifest_sha256, completeness, warnings}`.

Only `GET /api/cases` adds `opened_at`, when this account last opened the case, or `null`. It and
`GET /api/trash` add `added_by`.

### Series

```text
{uid, description, modality, study_uid, frame_of_reference_uid, sop_class_uids, slice_count,
 spacing, warnings, supported, unsupported_reason}
```

MR series add acquisition metadata such as `sequence_name` and `echo_time_ms`. A disabled modality's
series reads `supported: false` until it is enabled.

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

- `review_status`: `unreviewed`, `reviewed` or `corrected`.
- `source`: `dicom`, `totalsegmentator`, `nnunet_model`, `refinement`, `slicer_correction`,
  `include_all_outputs`, `dicom_seg_import`, `synthetic_demo`, `direct_edit_correction`,
  `direct_edit_new_structure`, `nninteractive_correction` or `nninteractive_new_structure`.
- `acquisition`: `{series_uid, profile, phase, cardiac_phase?, rr_percent?}`.

### Structure

```text
{id, name, task, group, color, volume_ml, voxel_count, centroid_ras, bounds_ras, qc: string[],
 reference_image_id, relationship?}
```

Volumes are `nonzero voxels × |det(affine[:3,:3])| / 1000` mL. `centroid_ras` and `bounds_ras` are
null for an empty mask. DICOM SEG structures also carry a `dicom_seg` provenance object.

### Job

```text
{id, case_id, kind, status, stage, error, revision_id, created_at, started_at?, completed_at?,
 progress?: {phase, completed, total, timings_seconds?, bytes?},
 result?, review_session_id?, source_revision_id?, inference_path?: {id, label, identity},
 delivery_started?}
```

- `status`: `queued`, `running`, `completed`, `failed` or `cancelled`. A restart fails running jobs
  with stage `Interrupted`. `stage` is display text.
- `kind`: `process`, `segment`, `refine`, `correction`, `include_all` (only from
  `scan3d include-all`), `review_predict`, `review_accept`, `orthanc_import`, `archive_import`
  (DICOMweb), `orthanc_export` (Orthanc SEG exports and every SEG file), `orthanc_roi_export`
  (OsiriX ROIs), `orthanc_view_export`, `view_file_export`, `archive_seg_export`,
  `archive_view_export` (DICOMweb), `direct_seg_export`, `direct_view_export` (C-STORE),
  `dicom_seg_import` or `coronary_analysis`. [ARCHITECTURE.md](ARCHITECTURE.md#jobs-and-lanes)
  lists the lanes.
- `revision_id`: the new revision once committed, the exported one for an export, or the analysed
  one for a [coronary analysis](#coronary-analysis), which commits none.
- `delivery_started`: `true` once an export has started a send, through Orthanc or directly.
  Cancelling cannot then stop it.
- `inference_path`: the [inference path](#inference-paths) frozen at queue time. `identity` is
  `default` or `<id>|<device>|<gpu_name>`; compare it whole, never parse it.
- `started_by`: on `GET /api/jobs/{id}` and `GET /api/cases/{id}/jobs` only.

`inference_path` appears on a `segment` job that named a path, `default` included, and on a
`review_predict` that runs the model and named a path or belongs to a draft bound to one. Other
jobs omit it, including direct and relationship-only `review_predict`.

### ReviewSession

```text
{id, case_id, revision_id, structure_id, method, status, generation, prompts, options, batches,
 relationship, relationship_suggestions, relationship_dirty, dirty, pending_count,
 applied_prompt_count, job_id, candidate: Candidate | null, error, accepted_revision_id,
 created_at, updated_at, new_structure, inference_path, started_by, target?, preview_revision_id?}
```

- `status`: `draft`, `predicting`, `ready`, `accepting`, `accepted` or `discarded`. `accepted` and
  `discarded` are closed. A failed or cancelled job returns to `draft`, or `ready` if a candidate
  remains, with `error` set and prompts kept.
- `target` and `preview_revision_id` appear when `new_structure` is true.
- `inference_path`: the path that made the current result, as `{id, label, identity}`. Null while
  `batches` is empty, or if the publishing job named no path. Only a published model result changes
  it.

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
- `warnings`: lines to show the reader, copied into the saved revision's `qc`. An nnInteractive
  candidate adds one when it removes 5% or more of the saved mask's interior, voxels over 2 mm
  deep, more than 40 mm from every mark.
- `gpu_name`: the GPU the run measured, when the job named a path and the run reported one. A
  relationship-only Apply keeps the one it reuses.

## Route index

| Method and path                                                                                                                                                                                      | Purpose                                                             |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| `/api/session`, `POST .../sign-out-others`, `POST .../redeem`                                                                                                                                        | [Sign-in](#sign-in-routes)                                          |
| `/api/people`, `POST /api/people/{id}/...`                                                                                                                                                           | [People](#people-routes)                                            |
| `GET /api/health`, `/api/imaging-capabilities`, `/api/segmentation-options`, `/api/interactive/status`, `GET`, `PUT`, `DELETE /api/totalsegmentator/license`                                         | [System routes](#system-routes)                                     |
| `GET /api/agreement`                                                                                                                                                                                 | Pooled [agreement](#agreement) report                               |
| `GET /api/cases`, `GET`, `PATCH /api/cases/{id}`, `POST .../open`, `.../trash`, `GET /api/trash`, `POST /api/trash/{id}/...`, `GET /api/sizes`, `GET /api/storage`, `POST /api/storage/clear-caches` | Case records and the [Library](#library)                            |
| `POST /api/cases/demo`, `.../import`, `.../import-local`, `/api/imports/...`                                                                                                                         | [Imports](#imports)                                                 |
| `/api/orthanc/...`, `/api/cases/{id}/orthanc-series`                                                                                                                                                 | [Orthanc routes](#orthanc-routes)                                   |
| `/api/archive/...`, `/api/cases/{id}/archive-series`                                                                                                                                                 | [DICOMweb archive](#dicomweb-archive)                               |
| `POST /api/cases/{id}/process`, `.../segment`, `.../refinements`, `.../activate`                                                                                                                     | [Preparation and segmentation](#preparation-and-segmentation)       |
| `POST /api/cases/{id}/corrections`, `.../dicom-seg/imports`                                                                                                                                          | [Slicer](#slicer) correction, [DICOM SEG import](#dicom-seg-import) |
| `GET /api/cases/{id}/images`, `.../images/{image}`                                                                                                                                                   | Image assets                                                        |
| `GET /api/cases/{id}/jobs`, `/api/jobs/{id}/...`                                                                                                                                                     | A case's jobs, newest first; [Jobs](#jobs)                          |
| `GET /api/cases/{id}/revisions`, `GET`, `PATCH .../revisions/{rid}`                                                                                                                                  | Revision history, one revision, set review status                   |
| `/api/cases/{id}/reviews/...`                                                                                                                                                                        | [Correction drafts](#session-routes)                                |

Revision routes below are relative to `/api/cases/{id}/revisions/{rid}`:

| Method and path                                     | Purpose                                           | Candidate alias |
| --------------------------------------------------- | ------------------------------------------------- | --------------- |
| `GET .../image-volume`, `.../{key}/chunks/{index}`  | Scalar descriptor and chunks (legacy `ct-volume`) | Yes             |
| `GET .../mask-volume`, `.../{key}/chunks/{index}`   | Cropped mask and parent                           | Yes             |
| `GET .../volume-mask`, `.../{key}/chunks/{index}`   | Display union                                     | Yes             |
| `GET .../volume-labels`, `.../{key}/chunks/{index}` | Display label union                               | Yes             |
| `GET .../review-frame`                              | Exact slice with binary overlays                  | Yes             |
| `GET .../probe`                                     | Voxel value at a RAS point                        | Yes             |
| `GET .../model-manifest`                            | Per-structure surface URLs                        | If prepared     |
| `GET .../surfaces/{key}.glb`                        | One structure's surface                           | If prepared     |
| `GET .../model.glb`                                 | Combined surface model                            | No              |
| `GET .../agreement`                                 | Agreement with model outputs                      | No              |
| `GET`, `POST .../coronary-analysis`                 | Coronary analysis runs and start                  | No              |
| `GET .../coronary-analysis/{analysis_id}`           | One coronary analysis result                      | No              |
| `GET .../cinematic-reference`                       | Cardiac HU references                             | No              |
| `GET .../slicer.zip`                                | Slicer bundle                                     | No              |
| `GET`, `POST .../export.html`                       | Offline HTML viewer                               | No              |
| `GET`, `POST .../dicom-seg`                         | SEG and OsiriX ROI export                         | No              |
| `GET .../dicom-seg/{export_id}/segment.dcm`         | Prepared SEG file                                 | No              |
| `GET`, `POST .../dicom-view`                        | 3D view export status and start                   | No              |
| `GET`, `POST .../dicom-view-file`                   | 3D view file status and start                     | No              |
| `GET .../dicom-view-file/{export_id}/view.dcm`      | Prepared 3D view file                             | No              |

## System routes

**`GET /api/health`** returns `{status, worker, lanes, modalities, gpu, version}` and never loads
PyTorch. With sign-in on, `gpu` is left out for a request with no session.

- `lanes` maps `gpu` and `cpu` to thread liveness, and `worker` is true only when all are alive.
  Without the worker lock, `lanes` is empty and `worker` false.
- `modalities`: what this installation offers.
- `gpu`: the default path only, with `ready`, `device` (the `SCAN3D_DEVICE` request), `dcm2niix` and
  `reason`.
- `ready`: a GPU verified by the `scripts/check_gpu.py` proof, so `false` on a CPU installation. A
  proof adds `torch_version`, and a matching one `hip_version`, `cuda_version`, `devices` and
  `checked_at`.

**`GET /api/imaging-capabilities`** returns `{schema_version, modalities: [{adapter, modality, kind,
capabilities, presentation, enabled}]}`.

**`GET /api/segmentation-options?modality=CT`** returns `{engine_version, tasks, license_configured,
defaults, remove_small_blobs_mm3, inference_paths}`. The default task is `total` for CT and
`total_mr` for MR. Each task has `id`, `label`, `modality`, `classes`, `class_count`,
`model_class_count`, `description`, `engine`, `license_required`, `license_available`, `available`,
`uses_organ_crop`, `robust_crop_default`, `robust_crop_supported`, `body_seg_supported`,
`remove_small_blobs_supported` and `high_order_resampling_supported`. A pinned nnU-Net model adds `engine_version` and `comparison: true`,
and `available` is false until its weights are installed.

**License.** `GET /api/totalsegmentator/license` returns `{configured, validated, validated_at}`
offline. `PUT` takes `{license_key}`, checks its format, validates it with the TotalSegmentator
service and stores it. `DELETE` removes it, and both changes are 409 while any job is queued or
running.

**`GET /api/interactive/status`** returns `{installed, version, model_available, available, reason,
model_id, model_revision, license, capabilities, inference_paths}`, from packages and files only.
`capabilities` are `point`, `bbox2d`, `scribble`, `lasso` and `initial_label`, with `bbox3d` false.

[Listing paths](#listing-paths) describes `inference_paths`.

## Imports

**`POST /api/cases/import`** takes multipart `files` (folders and ZIPs, at most 1,000 files) and
optional `label`, and returns a Case.

**`POST /api/cases/import-local`** takes `{path, label?}` for a directory or ZIP under
`SCAN3D_IMPORT_ROOTS`, and copies the files. Symlinks and anything inside the data directory are
refused.

**Staged uploads** are how the browser imports, up to 16 GiB and 100,000 files. Uploads idle for a
day are removed.

1. `POST /api/imports` with `{label?, total_files, total_bytes}` returns `{id, max_parts,
received_files, total_files, received_bytes, total_bytes}`. `max_parts` is 256.
2. `POST /api/imports/{id}/parts` with multipart `manifest` (JSON `[{path, offset, size}]`) and one
   `files` part per entry returns progress. Larger files arrive as ordered byte ranges. Resending
   matching bytes is idempotent, different bytes are a 400, and a gap is 409.
3. `POST /api/imports/{id}/finish` returns a Case, the same one if repeated, or 409 until every
   declared file and byte has arrived.
4. `DELETE /api/imports/{id}` discards an unfinished upload and returns `{id, status: "cancelled"}`.

**`POST /api/cases/demo`** creates a synthetic phantom with masks, for tests. `synthetic: true`
keeps it out of real agreement statistics.

### DICOMweb archive

These mirror the [Orthanc routes](#orthanc-routes) for the archive in [ARCHIVES.md](ARCHIVES.md).
UIDs travel in request bodies, never in paths.

| Route                                 | Body                                                   | Returns                                            |
| ------------------------------------- | ------------------------------------------------------ | -------------------------------------------------- |
| `GET /api/archive/status`             |                                                        | `{configured, connected, name, wildcards?, error}` |
| `POST /api/archive/studies/query`     | As `POST /api/orthanc/studies/query`                   | `{studies, offset, limit, has_more}`               |
| `POST /api/archive/studies/series`    | `{study_uid}`                                          | `{study, series}`                                  |
| `POST /api/archive/imports`           | `{study_uid, series_uid, snapshot, label?, reimport?}` | An `archive_import` Job                            |
| `POST /api/archive/launch`            | `{study_uid, series_uid, source_key}`                  | `{study, series, case_id, job}`                    |
| `GET /api/cases/{id}/archive-series`  |                                                        | `{study, series}`, each with `in_case`             |
| `POST /api/cases/{id}/archive-series` | `{study_uid, series_uid, snapshot}`                    | An `archive_import` Job                            |

- A series row has `snapshot`, `stable`, `completeness` (`receiving` or `unknown`) and `ready_in`,
  the seconds until an unchanged series can be imported. Import before then is a 409.
- `reimport: true` makes a new case even when this snapshot was imported before.
- A case's `import_source` has the Orthanc keys, with `kind: "dicomweb"`.

## Preparation and segmentation

**`POST /api/cases/{id}/process`** takes `{series_uid, profile, phase, cardiac_phase?, rr_percent?,
expected_active_revision_id?}` and returns a `process` job, which creates an image-only revision
with no inference. Unsupported series and disabled modalities are refused. The labels are metadata
only: they never select frames, change intensities, crop anatomy or choose a task.

- `profile`: `chest`, `abdomen`, `chest_abdomen` (the default), `corcta`, `cardiac`,
  `chest_abdomen_pelvis`, `abdomen_pelvis`, `pelvis`, `head_neck`, `breast`, `whole_body`, `other`
  or `unknown`.
- `phase`: required free text of up to 80 characters, for contrast timing.
- `cardiac_phase` (`unknown`, `best_diastolic`, `best_systolic`, `diastolic`, `systolic`, `other`)
  and `rr_percent` (0–100): only for `cardiac` and `corcta`.
- `expected_active_revision_id`: the active revision the page shows. If another is active when the
  job saves, it fails with `The active revision changed; start from the current revision`, keeping
  the newer revision. Without it, the new image becomes active regardless.

**`POST /api/cases/{id}/segment`** takes `{revision_id, task?, robust_crop?, body_seg?,
remove_small_blobs?, high_order_resampling?, inference_path?}` and returns a `segment` job. `revision_id` must be active.
See [Choosing a path](#choosing-a-path).

- `task` must be in the modality's catalog. Licensed tasks need a configured key.
- MR refuses `robust_crop` and `body_seg`. Body cropping cannot combine with an organ-cropped task.
- The run replaces its task's structures and same-named ones of other tasks; see
  [ARCHITECTURE.md](ARCHITECTURE.md#store-and-revision-model). Ambiguous names and conflicting
  relationships fail before publishing.
- A pinned nnU-Net model runs alone, with no crop or small-blob options, and needs its weights
  installed. Its structures match only that model's. `imagecasx_cas_net` is pinned the same way,
  though it is not an nnU-Net, and its run's source is also `nnunet_model`.
- `operation` records `replacement_policy`, `replaced_structure_ids` and `replacement_id_map`, plus
  [`runtime`](#what-a-run-records) if the run named a path.

**`POST /api/cases/{id}/refinements`** takes `{revision_id, structure_id, operation}` and returns a
`refine` job. `operation` is `{type, seed_ras?, bounds_ras?}`. A result that changes nothing or
empties the mask is refused. If `revision_id` is no longer active when the job saves, it fails with
`The active revision changed; start from the current revision` and the newer revision stays.

- `keep_component` and `remove_component` need a seed inside the mask. Components are six-connected.
- `keep_region` and `erase_region` need bounds with positive extent, and select voxel centres inside
  world-axis-aligned bounds.

**`POST /api/cases/{id}/activate`** takes `{revision_id, expected_active_revision_id?}` and returns
the Case. This is undo and redo: masks, volumes and relationships return exactly. Reset to scan is
this route with the image-only revision.

- If `expected_active_revision_id` is no longer active, it answers 409 `Someone else or another tab
saved a newer version of this case. Show it, then try again.` and changes nothing. Without it,
  the route activates the requested revision regardless.

**`PATCH /api/cases/{id}/revisions/{rid}`** takes `{review_status}` and returns the Revision.
Image-only revisions are refused.

## Library

**`GET /api/cases`** lists every case, the ones this account opened last first, then newest.

**`PATCH /api/cases/{id}`** takes `{label}`, trimmed, and returns the Case. Over 128 characters is
a 422, an empty name or a control character is 400 `A case name must contain 1 to 128 printable
characters`, and a case still importing is 409 `This case is still importing. Rename it when the
import finishes.`

**`POST /api/cases/{id}/open`** records an open for this account, putting the case first in its
list, and returns `{id}`.

**`POST /api/cases/{id}/trash`** hides the case, stops its jobs and returns `{id, jobs_stopped,
sends_continuing}`, the last counting sends already started, which finish. Nothing on disk moves,
and Trash is never refused for a job, a draft or a stuck import.

**`GET /api/trash`** lists the cases in Trash, last trashed first, each a Case with `trashed_at`.
**`POST /api/trash/{id}/restore`** returns the Case, or 404 `Case not found` if it is not in Trash.
Stopped jobs stay cancelled, so an import in progress comes back as `import_cancelled`.

**`POST /api/trash/{id}/purge`** deletes a case in Trash forever and returns `{id}`. It takes
`{password}`, the admin's own, in password mode, and `{confirm: true}` with sign-in off.

- Every row of the case goes in one transaction, and its `trash` row and the audit log keep only its
  ID. A background janitor removes its files moments later.
- A case that cannot go yet is refused before any password is checked. The password shares
  sign-in's throttle.

| Status | `detail`                                                            | When                                                                     |
| ------ | ------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| 400    | `Enter your password to delete forever.`                            | Password mode, no password                                               |
| 400    | `Confirm to delete forever.`                                        | Sign-in off, no `confirm: true`                                          |
| 403    | `Delete forever through the HTTPS address`                          | Password mode, `Origin` is not https and `Host` is not loopback          |
| 403    | `Administrator access is required`                                  | A member, or an admin demoted or disabled while the password was checked |
| 403    | `Wrong password.`                                                   | Never 401, which a browser reads as a lost session                       |
| 404    | `Case not found`                                                    | Not in Trash, or already deleted                                         |
| 409    | `A send to Orthanc is still finishing. Try again in a few minutes.` | A send of the case to Orthanc is running                                 |
| 409    | `Work on this case is still stopping. Try again in a minute.`       | Another job of the case is running                                       |
| 409    | `This case is still in use. Try again in a minute.`                 | A request on the case is in flight                                       |
| 422    | `Invalid delete request.`                                           | Schema failure                                                           |
| 429    | `Too many attempts. Try again in N seconds.`                        | As for sign-in                                                           |

**`GET /api/sizes`** returns `{sizes}`, allocated bytes by case ID for every case, Trash included,
measured on each request and leaving out shared caches.

**`GET /api/storage`** returns `{cases, totals, disk}`, in bytes:

- `cases`: every case as `{id, label, owner, created_at, trashed_at, size}`, where `owner` is the
  display name of whoever added it, or `null`.
- `totals`: `{cases, trash, shared_caches, other, rebuildable}`. `rebuildable` is what Clear caches
  would free.
- `disk`: `{total, free}` for the data directory's volume.

**`POST /api/storage/clear-caches`** empties the caches that rebuild when next needed: every review
volume, the analysis and agreement caches and the surface contours. Saved surfaces, cases and
their coronary analysis results stay.
It returns `{freed, storage}`, the bytes freed and a fresh storage report, or one of these 409s:

| `detail`                                                                                      | When                                                                       |
| --------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `A backup, a deletion or another 3DScan command is running. Try again when it finishes.`      | Something holds `maintenance.lock`                                         |
| `Another 3DScan command was running when 3DScan started. Restart 3DScan, then try again.`     | Another process held `worker.lock` at start                                |
| `Jobs are running. Try again when they finish.`                                               | A job is running                                                           |
| `Images are still loading. Try again in a minute.`                                            | Another request is in flight, or a review volume is being written          |
| `3DScan cannot remove <name> in its caches. Check its owner and permissions, then try again.` | A cache file or folder the service cannot remove, such as one `sudo` wrote |

## Jobs

**`POST /api/jobs/{id}/cancel`** returns the Job. A queued job cancels at once, and a running job
stops at its next check. Once a DICOM SEG or view send has started, this stops only the local wait;
see [DICOM SEG export](#dicom-seg-export). Moving a case to Trash cancels its jobs the same way.

**`GET /api/jobs/{id}/measurements`** returns `{structures, completed, total}` while surfaces build.
Ordinary polls leave them out.

## Corrections

### Session routes

**`GET /api/cases/{id}/reviews`** lists the case's correction sessions, newest first and closed ones
included, as `ReviewSession[]`. **`GET .../reviews/{sid}`** returns one.

**`POST /api/cases/{id}/reviews`** takes `{revision_id, structure_id, relationship?, method?}`.
`method` is `direct` or `nninteractive`, the API default. The revision must be active and contain
the structure, and starting a review changes no mask.

- An open draft for that revision, structure and method, the newest if two are open, is returned as
  it stands. `relationship` is validated but not applied to it; use `PATCH`. Closed drafts are never
  returned.
- Check `generation` and `prompts` before saving, because `PATCH` replaces every prompt.

**`POST /api/cases/{id}/reviews/new-structure`** takes `{revision_id, name, group?, color?,
relationship?, method?}` and creates an empty draft target on the reference grid with a
server-generated `custom:<id>`. `review-<sid>-0` exposes it on the read-only routes. Duplicate names
keep distinct IDs. The revision may be image-only, and saving then makes its first segmentation.

**`PATCH /api/cases/{id}/reviews/{sid}`** takes `{expected_generation, prompts, options?,
relationship?}` and replaces the complete ordered prompt list, with no inference.

- Dropping the last prompt is undo. An empty list resets.
- Appending keeps the last candidate for preview and marks the draft dirty.
- Editing an applied prompt drops later boundaries and invalidates the candidate.
- Undoing to an exact applied snapshot restores identical bytes under a fresh alias.

**`POST .../reviews/{sid}/predict`** takes `{generation, full_refine?, inference_path?, replay?, polarity_guard?}`
and returns a `review_predict` job, which applies every pending mark and publishes a candidate, not
a revision. With nothing pending, it needs `full_refine`, a changed relationship or `replay`, or
marks with no `candidate`, whose applied steps it repeats. See [Choosing a path](#choosing-a-path).

- Queuing a run moves `generation`, and publishing its result does not. A client that has not seen
  the run gets 409, so it can neither cancel the run nor replace its result.
- A policy-only Apply reuses the previous prediction with no inference.
- After Undo, a failure or a restart, the worker restores the longest run of applied steps from its
  stored result with no forward pass, if an intact one matches its marks, options, sources, card and
  build, and runs only the steps after it. Otherwise, or for a schema-1 draft's pointwise steps, it
  replays from the original mask.
- `full_refine` (nnInteractive only) refines the whole mask from the current candidate, and can take
  longer than a local correction. Earlier marks keep guiding later runs. If it fails or is cancelled
  with nothing pending, the earlier candidate returns under a fresh alias and can be accepted.
- `polarity_guard` (nnInteractive only) guards the step this run adds for pending marks: if they are
  all Include, no voxel is removed, and if all Exclude, none is added. Mixed marks and the
  whole-mask pass are unguarded, and replayed steps keep their own records.

**`POST .../reviews/{sid}/accept`** takes `{generation}` and returns a `review_accept` job, which
marks the new revision `corrected`. The candidate must be current and change masks or relationships,
on a still-active base. Only a direct edit of an existing segment may save empty, at 0 mL.

**`POST .../reviews/{sid}/discard`** takes `{expected_generation}`, closes the draft, invalidates
its aliases and cancels a pending run. A running `review_accept` refuses it with 409.

Stale generations, closed or busy sessions and a changed active base return 409. Prompts and
completed candidates survive a restart.

### Prompts

`id` is unique within a draft: 1–80 letters, digits, underscores or hyphens. `include` is a boolean.

| Kind       | Fields                                                                 | Method        |
| ---------- | ---------------------------------------------------------------------- | ------------- |
| `point`    | `ras: [R, A, S]`                                                       | nnInteractive |
| `bbox`     | `axis`, `index`, `vertices: [[u0, v0], [u1, v1]]`                      | nnInteractive |
| `lasso`    | `axis`, `index`, `vertices`, at least 3 with nonzero area              | nnInteractive |
| `scribble` | `axis`, `index`, `vertices`, at least 2; `radius_mm` 0.5–30, default 3 | Both          |
| `scissors` | `operation`, `shape`, `vertices`, `ras_to_clip`; `include: false`      | Direct        |

- **Limits.** 128 prompts, 2048 vertices per gesture, 8192 vertices per draft.
- **Points** must lie inside the reference grid. An omitted `kind` means `point`.
- **Drawings.** `axis` is native i, j or k (0, 1, 2), and `index` an integer slice. u, v are the two
  remaining axes in increasing order, as continuous voxel-centre coordinates within `[0, shape-1]`.
- **Coverage.** Saving a new `lasso` or `scribble` that selects no voxel centre is 400, naming the
  mark, as in `Lasso mark 2 covers no voxel centre on its slice. Draw it larger.` One already in
  the draft, as from an older build, still saves, and a run fails naming it.
- **Boxes** span exactly one slice. 3D boxes are not supported, and a patient-aligned rectangle on
  an oblique plane never becomes a native box.
- **Direct edits** accept `scribble` (`include: true` paints, `false` erases) and `scissors` only.
- **Scissors.** `operation` is `erase_inside` or `erase_outside`. `shape` is `freehand`, `rectangle`
  (four ordered axis-aligned corners) or `circle`. A cut covers the segment's whole depth.
- **Scissors geometry.** `vertices` are 3–2048 screen points within [−1, 1]. `ras_to_clip` is an
  invertible orthographic 4×4 matrix; perspective is refused.

`options` defaults to `{autozoom: true, start_empty: false}`. `start_empty` changes the proposal's
initialization, not the saved mask. Changing options resets applied boundaries.

`batches` records each Apply as `{end, autozoom, full_refine, legacy?, polarity_guard?}`: `end` is
the cumulative prompt count, `legacy` marks older pointwise sessions, and `polarity_guard` is
present only on a guarded step, which every replay guards again.

### Candidate aliases

A candidate's `revision_id` is `review-<session>-<generation>`. It works on the routes marked in the
[route index](#route-index), never appears in history and cannot supply exports.

`GET .../revisions/{alias}` returns a read-only view whose `source` is `direct_edit_candidate` or
`nninteractive_candidate`. Both kinds set `preview_3d_ready` and `model_manifest_url`.

## Segment relationships

Relationships describe voxel ownership. A draft stores
`{kind, parent_id?, exclusive_with: string[]}`.

| Kind          | Requires                      | Effect                                                    |
| ------------- | ----------------------------- | --------------------------------------------------------- |
| `independent` | No parent or peers            | Keeps every other mask; overlap is reported               |
| `subregion`   | `parent_id`, no peers         | Preserves the parent; does not crop or prove containment  |
| `subdivision` | At least one `exclusive_with` | Named peers lose overlapping voxels; a parent is optional |

- For each selected peer B and target A, `B_candidate = B_original & ~A_candidate`.
- Parents, ancestors and descendants are protected from replacement. Peers with declared parents
  must share the target's parent.
- Exclusivity applies only to selected peers, never transitively. Cycles, self-references and
  foreign IDs fail.
- Names, colours, display groups and geometric intersection never authorize a removal. Exact
  TotalSegmentator lung-lobe IDs give a `relationship_suggestions` entry, which still needs
  selecting.
- Changing the policy invalidates acceptance of an older candidate.
- Save updates reciprocal peer links. Removing an exclusive link restores no earlier voxels;
  activating the parent revision does.
- An existing nonempty segment can save a relationship-only change.

## Inference paths

An inference path is an interpreter plus a device that AI jobs run on. `default` is the API's own
interpreter with `SCAN3D_DEVICE`, and an added path is one proof file
([INSTALL.md](INSTALL.md#add-a-second-gpu), [ARCHITECTURE.md](ARCHITECTURE.md#inference-paths)).
With none named, a request runs on the default path, or on a draft's own path once it has one.

### Listing paths

`GET /api/segmentation-options` and `GET /api/interactive/status` return the same `inference_paths`
rows, `default` first, then added paths by `id`. Neither starts a process, loads PyTorch or touches
a GPU. No row holds a local path or proof text.

| Field            | Meaning                                                                                                                                                            |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `id`             | The value to send as `inference_path`                                                                                                                              |
| `label`          | The GPU's name. `default` falls back to `Default device`, or `CPU` when `SCAN3D_DEVICE` is `cpu`. An added path whose proof names no GPU shows its `id`            |
| `memory_gib`     | The GPU's memory to the nearest GiB, or null if unknown                                                                                                            |
| `shares_display` | True when a GPU of this vendor has a connected display now, so its free memory depends on the screen. False when unreadable                                        |
| `available`      | Whether a request may name this path: what its proof records is still installed, and its last check passed. Never means a scan will fit. Always true for `default` |
| `reason`         | A fixed sentence, or null                                                                                                                                          |

An added path with `available: false` gives the first of these that applies:

- `Its GPU check record cannot be read.`
- `Its last GPU check failed.`
- `Its last install did not finish.`
- `Its Python environment is missing.`
- `Its PyTorch or GPU runtime packages changed after the last GPU check.`
- `Its TotalSegmentator, nnU-Net or nnInteractive version differs from this workstation's.` This
  also covers `nninteractive-client`.
- `It has cupy or cucim installed, which would change resampling.`
- `The GPUs in this workstation changed after the last GPU check.`

`default` gives null or the caution `Its last GPU check failed or is out of date.`, which blocks
nothing. A proof taken before its vendor's cards changed also sets `label` to `Default device` and
`memory_gib` to null, which a job queued then freezes. `identity` stays `default`.

### Choosing a path

`inference_path` is optional on `POST .../segment` and `POST .../reviews/{sid}/predict`. It must
match `^[a-z0-9][a-z0-9-]{0,31}$`, and never goes inside `options` or `batches`.

- On a segmentation, omitting it and sending `default` run alike and share saved results.
- `scan3d segment --inference-path ID` takes the same IDs and refusals. `scan3d doctor` lists the
  rows.
- A job's path is frozen at queue time. An added path is rechecked as the job starts, and never
  falls back to another path or the CPU.
- `default` follows `SCAN3D_DEVICE`, so under `auto` it can still run on the CPU.

A draft is free until a model result publishes, then stays on that path while `batches` is
non-empty:

| Draft           | Request                                              | Result                                                                           |
| --------------- | ---------------------------------------------------- | -------------------------------------------------------------------------------- |
| `batches` empty | `inference_path`, or none for `default`              | Runs there, and a published result binds the draft                               |
| `batches` set   | None, or the draft's own path                        | Runs on the draft's path                                                         |
| `batches` set   | Another path, with `replay` false                    | 409                                                                              |
| `batches` set   | None, and another card now holds the draft's path ID | 409, with `replay` too                                                           |
| `batches` set   | `replay: true`                                       | Repeats the draft's steps on the named path, or on its own path if none is named |

**`replay`** repeats every recorded batch, in order, from the original mask, plus any pending marks
as one more batch. Another card or build can give a slightly different result.

- On its own path, a worker that holds those batches, or can rebuild them from their stored result,
  runs only pending marks.
- Nothing need be pending, and a pending relationship goes with it as a model run. With `batches`
  empty, it adds nothing.
- Once queued, the new generation makes the old candidate `dirty`, so it cannot be accepted. It
  stays in `candidate` until the new one publishes, which also changes the draft's
  `inference_path`.
- After a failed or cancelled re-run it stays, still `dirty`, and the draft can re-run on any
  available path.

Direct Apply and a relationship-only Apply run no model. They ignore `inference_path`, name no path
and stay on the CPU lane. A direct draft ignores `replay` too.

### What a run records

A segment revision has `operation.runtime` only if its job named a path, `default` included.

| Key                    | Meaning                                                                                                   |
| ---------------------- | --------------------------------------------------------------------------------------------------------- |
| `inference_path`       | The path's `id`                                                                                           |
| `gpu_name`             | The GPU the child measured, or null on the CPU                                                            |
| `device`               | The device it selected, such as `gpu:0` or `cpu`                                                          |
| `torch_version`        | The PyTorch version it measured                                                                           |
| `hip_version`          | The build's HIP version, or null                                                                          |
| `cuda_version`         | The build's CUDA version, or null                                                                         |
| `reused`               | True when this revision reused a saved result, so these values are that run's                             |
| `results_moved_to_cpu` | True when nnU-Net re-ran part of a task with results in system memory, usually because GPU memory ran out |

Only added paths check that last key, so on `default`, false means not detected. True also adds
`Part of <task> re-ran with results in system memory on <label>, usually because GPU memory ran out.
The run took longer.` to the revision's `qc`.

A saved correction's `operation.runtime` records what made its candidate:

- **nnInteractive.** The same `device`, `gpu_name`, `torch_version`, `hip_version` and
  `cuda_version`, plus `inference_path` if named. `miopen_find_mode` is null unless the build is
  HIP. `restored` is `{batches, mask_sha256}` when the run rebuilt its first steps from a stored
  result, else null, and `stored_result_refused` says why one was not used.
- **nnInteractive replay check.** When a refused stored result was still trusted, `replay_check` is
  `{batches, changed_voxels}` from comparing the replay with it at that step, and moved voxels add a
  candidate warning.
- **Direct edit.** `device: "cpu"` and no GPU or PyTorch keys.
- **Relationship-only.** Its prediction's provenance, or `engine: "mask-relationship-processing"`
  and none of these keys with no prediction to reuse.

### Refusals and failures

A bad request is refused before any job exists:

| Status | When                                                                                | `detail`                                                                             |
| ------ | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| 422    | A malformed ID, or `replay` on a segment                                            | FastAPI's validation list                                                            |
| 400    | The path is not set up                                                              | `This GPU is not set up on this workstation.`                                        |
| 400    | The path is unavailable                                                             | `<label> is unavailable: <reason>`                                                   |
| 400    | `replay` with `full_refine`                                                         | `A re-run repeats the draft's own steps, so it cannot add a whole-object refinement` |
| 409    | Another path on a draft with `batches`, or none once another card holds its path ID | `This draft's AI results came from <label>. Re-run its steps to use another GPU.`    |

Once queued, a problem fails the job, never the route. The job has `status: "failed"` and keeps its
`inference_path`, and a review job's `error` also reaches the draft's `error`. On an added path:

- Removed path: `<label> is no longer available: This GPU is not set up on this workstation. Nothing
ran and no other GPU was tried.`
- Unavailable path: `<label> is no longer available: <reason> Nothing ran and no other GPU was
tried.`
- Different card or device under that ID: `<label> changed after this job was queued. Nothing ran
and no other GPU was tried. Start it again.`
- PyTorch reports no GPU, in either engine: `GPU inference was requested but PyTorch cannot access a
GPU; check the driver, then repeat this GPU's check (see Add a second GPU in docs/INSTALL.md). No
other GPU was tried.` `SCAN3D_DEVICE=cpu` cannot change an added path.
- A card this build cannot see, such as `gpu:1` with one card in view: `Requested GPU index is
unavailable`. This one names no card.
- Any other device mismatch: `Expected <label>, but the GPU check found <name> (<device>, <build>).
No result was kept and no other GPU was tried.` `<build>` is `CUDA` or `no CUDA`.
- A crop-model task such as `teeth` on a GPU index above 0: `TotalSegmentator would run part of
<task> on the first GPU, so it cannot run on <label>. Nothing ran and no other GPU was tried.`
- A stopped task: `TotalSegmentator <task> failed on <label>: <cause>. No other GPU was tried.`
  `<cause>` is `it ran out of GPU memory`, `the process was killed, usually because system memory
ran out` or `exit <code>`.
- A report from another PyTorch: `TotalSegmentator <task> ran on PyTorch <version>, not the
<version> that <label> was checked on. No result was kept and no other GPU was tried.`

`default` gives none of these. On a job that named a path, `default` included, a child that will not
stop gives `The AI process did not stop in time.`, and an absolute path in any other error becomes
`<local path>`. The service log keeps the detail.

## Viewer data

### Volume descriptors

`quality` is `full` (default) or `preview`.

```text
{schema_version: 1, key, kind: "ct", dtype, quality, approximate, preview_quantization_hu,
 reference_geometry, shape, affine_ras, geometry, slope, intercept, byte_length, chunk_depth: 16,
 chunks: [{index, start_k, depth, byte_offset, byte_length, url}],
 reference_image_id, modality, scalar: {units}, presentation, source}
```

Samples are little-endian in i-fastest order, in gzip slabs of 16 k-slices. `key` is a
content-derived SHA-256.

- **Full**: exact int16 or uint16 samples with `slope` and `intercept`, or float32 with scaling
  applied for other source types.
- **Preview**: at most 192 per dimension, with `approximate: true`. `shape` and `affine_ras` differ
  from `reference_geometry`, but voxel faces match its field of view. CT uses int16 at a 1 HU step,
  and a zero `preview_quantization_step` keeps float32 for MRI signal.

**`mask-volume`** takes `structure_id`, `compare`, `quality` and `margin_mm` (0–5), and returns
`{schema_version, current, parent}`. `current` and `parent` are each null or a cropped binary
descriptor with `kind: "mask"`, `dtype: "uint8"`, `origin_ijk` and `empty`.

`compare` adds the parent revision's structure. A nonzero `margin_mm` grows a display copy only.
Chunk URLs carry `structure_id`, `parent`, `quality` and `margin_mm`.

**`volume-mask`** takes repeated `structure_ids` and `margin_mm`, and returns `{schema_version,
source_id: "volume-mask-union:<key>", current}`. An all-empty selection is a valid zero mask. An
empty selection is a 400.

`current` is one OR union, cropped to the selection's bounds and capped at 8 MiB, with
`presentation_only: true`, `volume_mask_union: true`, `pooling: "foreground-centre-occupancy"`,
`approximate`, `selected_structure_ids`, `native_crop_origin_ijk` and `native_crop_shape`.

**`volume-labels`** takes repeated `structure_ids` in painting order, and returns the same shape
with `source_id: "volume-label-union:<key>"`, `volume_label_union: true`, `volume_mask_union: false`
and `labels`. Value _n_ marks the _n_-th listed structure, 0 is background, and later structures win
overlaps. At most 255 structures, and no margin.

Both unions are presentation artifacts. Never measure them or use them as masks.

### Review frames

**`review-frame`** returns one exact reference-grid slice, as gzip bytes with its metadata in the
`X-CT-Frame` header.

| Parameter                     | Meaning                                                    |
| ----------------------------- | ---------------------------------------------------------- |
| `plane`, `position_mm`        | `axial`, `coronal` or `sagittal`, at a patient position    |
| `native_axis`, `native_index` | A source-index plane instead of a patient plane; send both |
| `structure_id`                | One overlay, using the original response schema            |
| `structure_ids` (repeated)    | Up to 128 independent overlays                             |
| `compare`                     | Adds each structure from the parent revision               |

The header holds `{meta, dtype: "float32", hu_encoding: "byte-shuffle", pixels, current_offset,
parent_offset, overlays?}` plus the image context. `meta` has `width`, `height`, `pixel_to_ras`,
`position_mm`, `plane`, `extent_mm`, `overlay_colors` and, for native planes,
`native: {axis, index}`.

- The decompressed body starts with `pixels × 4` bytes of unwindowed float32 values, their four byte
  planes grouped. Reconstruct by interleaving those planes.
- Binary overlays follow at the listed byte offsets, one byte per pixel.
- With `structure_ids`, `overlays` lists `{id, current_offset, parent_offset}`. A missing parent
  label has a null offset.
- On a native plane `native` is authoritative: `plane` echoes the request, `position_mm` holds the
  index, and pixels map back through `pixel_to_ras`.
- Native frames keep one pixel per voxel. A frame above 8 Mi pixels, or 256 MiB decoded with its
  overlays, fails rather than being reduced.

**`probe?x=&y=&z=`** returns `{hu, value, units, voxel, reference_image_id}` at a RAS point. `hu`
and `value` hold the same number.

### Surfaces

**`model-manifest`** returns `{schema_version: 1, structures: [{id, key, url}]}`, and
**`surfaces/{key}.glb`** serves one structure. Reuse unchanged objects by `key`.

**`model.glb?detail=false`** serves the combined model. `detail=true` builds a detailed export on
first request, and never activates a revision or changes review status.

**Coordinates.** World coordinates, after node transforms, are metres: scene = (−R, S, A) / 1000,
and RAS = (−x, z, y) × 1000. GLB nodes are named by structure ID.

**Display encoding.** Display GLBs need `KHR_mesh_quantization`. Use world-space results, never raw
position attributes.

- Positions: uint16, byte stride 8, on one absolute 2⁻¹⁵ m grid shared by all structures,
  dequantized by the node's uniform `scale` and `translation`, within 0.0153 mm per axis (0.0264 mm
  in 3D).
- Normals: normalized int8, byte stride 4, within 0.4°. Indices: uint16.
- Meshes above 65,535 vertices become consecutive primitives, repeating only seam vertices.

Detailed GLBs are float32 and uint32 with untransformed nodes. Older revisions keep float32 display
assets until `scan3d optimize` re-imports them.

**Negotiation.** These routes serve stored gzip by `Accept-Encoding` and vary on it, honouring
quality weights and explicit refusal. Forbidding every representation gets 406. ETags are per
representation, `If-None-Match` takes precedence over `If-Modified-Since`, and byte ranges use the
original GLB bytes with `If-Range`.

### Cinematic references

**`cinematic-reference`** measures median HU for a saved revision, for the Cinematic heart preset.
It returns `{schema_version, myocardium_hu, blood_hu, myocardium_structure_ids, blood_structure_ids,
myocardium_voxels, blood_voxels, blood_source, method}`.

It pools native samples from myocardium-named and aorta-named masks, falling back to left heart,
excluding values at or below −900 HU. Masks without 100 HU of separation return 400, with guidance
to enter values manually.

## Agreement

Read-only reports comparing each saved structure with its model original: the nearest mask in the
parent lineage that is a saved inference output with a matching ID.

**`GET .../revisions/{rid}/agreement`** returns:

```text
{schema_version: 1, case_id, revision_id, review_status, synthetic, active,
 aggregate_pool: "real" | "synthetic" | null, implementation, hd95_exact,
 grid: {voxel_volume_ml, spacing_mm, sheared},
 summary: {structures, compared, unchanged, changed, excluded, dice_mean, dice_median,
           abs_volume_change_ml_mean, edit_kinds},
 structures, excluded, cache: {hits, computed}}
```

Each `structures` row has `id`, `name`, `task`, `group`, `origin: {kind, revision_id, run_id?,
task?, engine_version?}`, `basis` (`same_file`, `same_digest` or `voxels`), `unchanged`, `dice`,
`hd95_mm`, original and current voxels and volumes, `volume_change_ml` (accepted minus original),
`added_voxels`, `removed_voxels`, `edit_kind` and `edits`. Synthetic demo cases use their phantom
as the origin.

- Dice is `2|A∩B| / (|A| + |B|)`.
- HD95 is the larger of the two directed 95th percentiles of distances between face-exposed surface
  voxel centres, in millimetres through the full affine. Null when either mask is empty.
- `edits` lists the `source` of each revision that saved a new mask since the original, oldest
  first. A new structure's save is an edit of each exclusive sibling it carved.
- `edit_kind` is null for an unchanged mask. Otherwise it is `ai_assisted` if any edit is
  `nninteractive_correction` or `nninteractive_new_structure`, `direct_edit` if every edit is
  `direct_edit_correction` or `direct_edit_new_structure`, and `other` otherwise.
- An AI-assisted save keeps the model's rewrite away from the marks, so its figures overstate the
  reader's change.
- `edit_kinds` maps each kind present to `{changed, dice_mean, dice_median,
abs_volume_change_ml_mean}`.

`excluded` rows have `{id, name, task, group, code, reason}`, where `code` is `reviewer_created`,
`no_model_output`, `model_output_unavailable`, `different_image` or `unreadable`.

**`GET /api/agreement`** pools each case's active revision when its status is `reviewed` or
`corrected`:

```text
{schema_version: 1, implementation, selection,
 cases: {total, included, skipped: {no_active_revision, no_segmentation, unreviewed, unavailable}},
 pools: {real: Pool, synthetic: Pool},
 revisions: [{case_id, revision_id, review_status, pool}]}
```

A pool is `{cases, excluded, summary, tasks: [{task, ...stats, structures: [{id, name, task,
...stats}]}]}`. Stats are `reviewed`, `unchanged`, `unchanged_percent`, `dice_mean`, `dice_median`,
`abs_volume_change_ml_mean` and `edit_kinds`. Synthetic cases never enter the real pool.

## Coronary analysis

Research use only, not for diagnostic use. A `coronary_analysis` job traces centrelines in a saved
revision's coronary lumen mask, measures lumen area along them and solves steady hyperaemic pressure
to give FFR: Poiseuille resistance along the vessels, and Young–Tsai viscous and expansion losses
across each narrowing. It reads the masks on their CT grid and commits no revision.

**`POST .../coronary-analysis`** takes:

| Field        | Rule                                                                                              |
| ------------ | ------------------------------------------------------------------------------------------------- |
| `lumen`      | Structure ID in the revision, required                                                            |
| `aorta`      | Structure ID or null. Lumen within 1.5 mm of it gives one root per contact patch                  |
| `myocardium` | Structure ID or null. LV mass is its volume times 1.05 g/mL, and must be 20 to 1000 g             |
| `lv_mass`    | g, 20 to 1000. Required without `myocardium`, refused with it                                     |
| `sbp`, `dbp` | mmHg, both or neither: SBP 60 to 300 above DBP 20 to 200. Neither means 120/80                    |
| `heart_rate` | bpm, 20 to 250. Recorded only: the model does not use it, so it is never `assumed`                |
| `share`      | More than 0 and at most 1: the share of whole-heart flow in the trees solved                      |
| `ostia`      | Up to four RAS points, each snapped to lumen within 3 mm, and dropped within 3 mm of another root |

It returns the Job. A queued, running or complete job for the same revision and inputs is returned
instead of a new one; a failed or cancelled one, or one being cancelled, is not. Field bounds fail
as 422. These are 400: neither `aorta` nor `ostia`, a structure ID not in the revision or used
twice, both or neither of `myocardium` and `lv_mass`, refused blood pressures, a non-CT image and a
candidate alias.

The job fails when the lumen is empty, an ostium point is more than 3 mm from the lumen, or no root
is found: `The lumen does not touch the aorta; add an ostium point`. It also fails on an ostium
point more than 5 mm from the aorta partway along lumen an aortic root already reaches, since that
would split the vessel. Its stages are `Loading masks`, `Centrelines`, `Areas`, `Pressure` and `Saving`, and its
`result` is `{analysis_id, key, summary}`.

**`GET .../coronary-analysis`** returns `{analyses: [{analysis_id, state, stage, created_at,
finished_at, error, inputs, summary}]}`, this revision's runs, newest first. `analysis_id` is the job
ID. `summary` is `{trees, branches, lesions, lowest_distal_ffr}` once complete, else null.

**`GET .../coronary-analysis/{analysis_id}`** returns the result. It is 404 for a job of another
case, revision or kind, before it completes, and when its saved files fail their check.

```text
{format: "scan3d-coronary-v1", notice, analysis_id, case_id, revision_id, key, created_at,
 inputs: {lumen, aorta, myocardium, lv_mass, lv_mass_source: "myocardium mask" | "typed", sbp, dbp,
          heart_rate, share, ostia},
 constants: {perfusion, heart_factor, tcri, exponent, venous, viscosity, density, kt, spacing,
             window, report_diameter, report_beyond},
 aortic_pressure, assumed, flags,
 roots: [{tree, point, source: "aorta" | "ostium point"}],
 pieces: [{point, volume}],
 branches: [{id, parent, tree, gap, length, flow, reason, distal: {ffr, station, note},
             stations: {point, s, area, diameter, reference_diameter, ffr, pressure, reported, held,
                        junction, spur, contact}}],
 lesions: [{id, branch, throat, start, end, length, mld, stenosis, held,
            beyond: {ffr, branch, station, note}, before: {ffr, branch, station}, delta_ffr}]}
```

- Points are RAS mm. Lengths are mm, areas mm², flow mL/min, pressure mmHg. NaN is null.
- `stations` holds one array per field, one entry per station 0.5 mm apart. `diameter` is
  2·√(area/π) and `reference_diameter` the fitted healthy diameter.
- `parent` is -1 at a root. `start` and `end` are mm from the ostium along the tree, and `stenosis`
  is the diameter stenosis at the throat, 0 to 1.
- `held` stations take the nearest trusted station's area: the first 1 mm of a root branch, the last
  1.5 mm of an end, junction zones, and spur runs that touch them. A branch with no trusted station
  takes its reference area.
- A lesion is a stretch under 0.7 of the reference for at least 1 mm. Stretches less than 1 mm apart
  are one lesion, with the deepest throat.
- `reason` says why a branch's FFR is not reported, such as one tree with no `share`, or a lumen gap.
- `assumed` is `["blood pressure"]` when neither pressure is given, else empty.
- `pieces` are lumen components no root reaches, not solved.
- `flags` lists assumed values, gaps, dropped ostium points, a `share` applied to more than one tree,
  unmeasured voxels and lumen that touches lumen far along the tree. Plausibility flags change no
  number and come as at most one line per kind, naming the branches: hyperaemic mean speed at a
  solved branch's start under 0.15 or over 1.5 m/s where the reference is at least 1.8 mm, and a
  parent's reference under 0.85 of its daughters' Murray size.

Reading rules:

- A station is `reported` when its FFR is solved and its reference diameter is at least 1.8 mm.
- A lesion is listed only when its reference diameter at the throat is at least 1.8 mm, and listed
  lesions are numbered from 0. A thinner one still adds its loss to the solve.
- `distal` is the branch's last reported station. With none, `ffr` is null and `note` says why.
- `beyond` reads the station 20 mm past the lesion's end along the tree, taking the daughter with the
  larger median reference over its first 3 mm at each junction. If the vessel ends first, or that
  station is not reported, it reads the last reported station on the way, and `note` says how far
  past the lesion that is and why.
- `before` is the last reported station before the lesion starts, on its branch or a parent.
- `delta_ffr` is `before.ffr` minus `beyond.ffr`.

The result is saved in `cases/<case>/coronary/<key>/`, keyed by the code, image, masks and inputs.
A later revision with the same masks reuses it.

## Exports

### Slicer

**`GET .../slicer.zip`** returns `reference.nii.gz`, `segmentation.seg.nrrd` and `mapping.json`,
with overlapping masks in separate layers and stable IDs preserved.

**`POST /api/cases/{id}/corrections`** takes multipart `revision_id` and `segmentation` and returns
a `correction` job. It verifies geometry, segment identity and the source hash, handles cropped
labelmaps, and marks the new revision `corrected`. A segment whose voxels match its mask in
`revision_id` keeps that saved file. Like a refine, it fails if `revision_id` is no longer active
when it saves.

### Offline HTML

**`POST .../export.html`** takes `{layers, state}` and streams a self-contained HTML file. **`GET
.../export.html`** returns a segmentation-only viewer with default settings.

`layers` is `{segmentation, ct_volume, ct_plane}`, with at least one true. Excluding segmentation
omits its GLB, and excluding both CT layers omits all voxel data. Masks are embedded only when plane
overlays or volume masking need them.

`state` carries the saved display snapshot:

- `display`: per-structure `{visible, opacity, color?}`, plus `selected` and `segmentation_opacity`.
- `volume`: `{preset, shift, density, quality, crop, cinematic, signal_window?}`. `preset` is a
  bundled Slicer appearance ID. MR exports must use `MR-SignalWindow`.
- `volume_mask`: `{enabled, mode: "inside" | "outside", ids, margin_mm}`.
- `plane`: `{plane, axis, index, window, level, overlays}`, where `axis` must match `plane`.
- `camera`: `{position_ras, target_ras, up_ras, zoom, height_mm, name?}`.

Structure references must belong to the saved revision. Limits are 512 MiB of CT samples, 192 MiB of
selected masks, 128 MiB of surfaces, 512 MiB of HTML and 256 embedded masks, with 413 before
streaming. Paths and acquisition identifiers stay out of the manifest.

## DICOM SEG

### DICOM SEG import

**`POST /api/cases/{id}/dicom-seg/imports`** takes multipart `revision_id` and `files` (1–64 local
SEG objects, not ZIPs, bounded by 512 MiB and the upload limit) and returns a `dicom_seg_import`
job. The revision must be active with a prepared classic CT or classic single-frame MR and its
complete original series. It needs no AI segmentation or Orthanc connection, and sends nothing.

| Rule     | Values                                                                                                    |
| -------- | --------------------------------------------------------------------------------------------------------- |
| Accepted | Part 10 Segmentation Storage with `BINARY` pixels in uncompressed Explicit or Implicit VR Little Endian   |
| Refused  | Fractional, labelmap, concatenated, compressed, deflated and resampled SEG                                |
| Limits   | 1000 segments, 100,000 frames, a 512 Mi voxel grid, 16 Mi pixels per frame, 64 MiB of metadata per object |

- **Identity.** Patient, study, frame-of-reference and referenced series and SOP identities must
  match the CT. Repeated assignments, unknown segments and foreign references are refused.
- **Geometry.** Patient functional groups must map every frame corner through the full affine to
  native integer voxels, within both 0.05 voxel per axis and 0.05 mm. Values are never thresholded
  or interpolated.
- **Frames.** Cropped, reordered and reflected frames and sparse planes are accepted if that
  holds.
- **Result.** Each segment gets a `dicom_seg:<SOP hash>:<SegmentNumber>` identity, keeping its
  label, colour and coding provenance. Names never replace existing anatomy, and duplicates are
  refused.
- **Revision.** Existing masks stay in the new unreviewed revision, with
  `source: "dicom_seg_import"`, which activates only while its parent remains active. Matching
  duplicate requests reuse the queued or running job.

### DICOM SEG export

| Route            | Needs                                                                                                                                  |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Archive and send | A configured Orthanc, and a saved revision whose reference image is an original classic CT or MR imported from that archive            |
| File             | No Orthanc. A saved revision whose reference image was prepared from a classic CT or MR this case still holds, however it was imported |

SEG, OsiriX ROI and view exports from MR also need `MR` in `SCAN3D_MODALITIES`, and refuse Enhanced
MR.

`SCAN3D_ORTHANC_EXPORT_DESTINATIONS` allowlists configured Orthanc modality IDs. Empty allows
archive-only export.

**`GET .../dicom-seg`** returns `{available, reason?, destinations: [{id, name, aet}], exports:
Job[], encoding_version, file: {available, reason?}, osirix_roi: {encoding_version}}`.

- `available` and `reason` describe archive and send, for SEG and OsiriX ROIs alike, and `file` the
  file route. `exports` lists this revision's SEG and OsiriX ROI jobs, newest first.
- Without archive access it contacts no Orthanc: `available` is false, `destinations` empty and
  `reason` is `Your account cannot use the archive. An administrator can turn it on in People.`

**`POST .../dicom-seg`** takes `{structure_ids?, destination?, resend?, overlap_priority?,
delivery?, format?}` and returns an `orthanc_export` job. Export never changes review status, adds
a revision or activates one.

- Omitted IDs select all nonempty masks. A null destination archives only.
- `delivery` defaults to `orthanc`. `file` prepares the SEG here, takes no `destination` or
  `resend`, needs no archive access and never contacts an archive.
- `format` defaults to `seg`. `osirix_roi` returns an `orthanc_roi_export` job
  [instead](#osirix-roi-export).

**`GET .../dicom-seg/{export_id}/segment.dcm`** returns the prepared `application/dicom` attachment
`scan3d-seg-<revision>-<export>.dcm`. The name holds no patient text, but the file carries the
source's patient and study identity. It is 404 without a completed file job of this revision and
`export_id`, and 400 if the bytes no longer match `export.json`.

**Encoding.** One binary SEG instance in a new series, with numbered segments identified on each
frame, keeping original patient, study, frame and source references, names and colours. Overlaps are
preserved.

- Each segment states its own mask's history, not the revision's `source`. A mask that is still a
  saved model output is automatic in every revision that carries it, and any other mask is
  semiautomatic.
- A corrected mask stays semiautomatic in later revisions that carry it, and a later run that
  replaces it is automatic again.
- A Slicer import keeps the file of each segment it did not change, so only an edited segment
  becomes semiautomatic. Earlier Slicer imports re-saved every segment, and those stay
  semiautomatic.
- The grid is never resampled. Source pixels must map to unique integer native voxels within the
  import tolerances, with complete slice coverage.
- The cap is 512 MiB and 100,000 frames, enforced before upload or download.
- The private coding scheme `99SCAN3D` defines `REGION` and `SEGMENTATION`, which can raise
  unknown-vocabulary validator warnings.

**Overlap resolution.** `overlap_priority` ranks every selected structure once, highest first, and
needs at least two. Each voxel stays only in the highest-ranked structure holding it, in this SEG
only. Saved masks, measurements and review status do not change.

- Segment numbers follow the rank, `SegmentsOverlap` is `NO`, the series description ends
  `overlaps resolved`, and a segment that lost voxels is described as semiautomatic.
- A structure that would lose every voxel stops the export, naming its rank.
- Each ranking is its own artifact, so **Send again** needs the exact ranking.
- `export.json` adds `overlap_priority`, and `removed_voxel_count` on segments that lost voxels. Its
  `voxel_count` is the exported count, and `mask_sha256` still names the saved mask. The job's
  `dicom_seg` result adds `overlap_priority` and `removed_voxels` (keyed by structure ID).

**Identity.** Enqueue freezes mask file identities, and later changes are refused. `export.json`
pins the source, reference, mask and SEG hashes before any network write, and `delivery.json` holds
receipts. A CT not from Orthanc is identified by the SHA-256 of its series files in sorted path
order, with no `source_snapshot` or `archive_key`.

**Result.** `result.dicom_seg` has `export_id`, `encoding_version`, `archived`, `sent`,
`segments_count`, `instance_count`, `structure_ids` and optional `destination` and `destination_id`,
with no patient fields. `sent` is true only after an exact successful C-STORE count. A file job adds
`file: true`, with `archived` and `sent` false.

**Retries and delivery.**

- A duplicate request reuses the job and artifact. File and archive exports of the same selection
  and ranking share one artifact and SOP instance.
- A retry reuses the exact SOP bytes and checks any known remote job. If Orthanc no longer has that
  job, the outcome is unknown and the next retry sends again.
- `resend: true` needs a destination and a known successful send, and makes a new delivery attempt
  with the same SOPs.
- A new encoder version makes fresh SOPs on explicit export.
- Polling stops after 120 seconds with an unconfirmed outcome.
- Startup fails interrupted and queued exports locally and never contacts Orthanc.
- Cancelling before delivery stops the export, but a SEG already uploaded stays in Orthanc, and its
  `result` still says `archived`.
- After C-STORE has started, cancelling cannot stop the remote transfer, and the job says so.

### OsiriX ROI export

`format: "osirix_roi"` writes the selected masks as native OsiriX MD brush ROIs instead of a SEG.
This format is experimental and takes the archive and send route only: any `delivery` other than
`orthanc`, and any `overlap_priority`, are 400.

- **Encoding.** One Basic Text SR, series description `OsiriX ROI SR` and series number 5002, for
  each source slice any selected mask touches. It holds one brush ROI per mask on that slice, named
  and coloured as saved, and a mask's ROIs share one group across slices.
- **Pixels.** Brushes cover the slice's exact source pixels, mapped as for a SEG and never
  resampled. Overlaps stay. Patient and study elements come from the source slice, in UTF-8.
- **Names.** OsiriX tells ROIs apart by name, so selected masks that share a name are 400.
- **Limits**, checked before upload: 1000 reports, 100,000 ROIs and 512 MiB.

Identity, retries and delivery follow [DICOM SEG export](#dicom-seg-export), with their own encoding
version, so a SEG send never counts as an ROI send. As for a [view](#dicom-view-export), Orthanc's
patient ID for the source study must equal the reports', and every upload must land in the source
study, or nothing is sent.

- Once the first report is uploaded, a cancel waits until every report is stored, then stops before
  sending.
- If an upload fails partway, the job fails, says how many reports reached Orthanc, and a retry
  stores the rest with the same instances.

**Result.** `result.osirix_roi` has `export_id`, `encoding_version`, `archived`, `sent`,
`segments_count` (masks), `structure_ids` and optional `destination` and `destination_id`, with no
patient fields. Once the masks are encoded it adds `roi_count` and `instance_count` (reports).

## DICOM view export

This turns one capture of the 3D view into a new series in the original study, archives it in
Orthanc, and can send it to a configured destination. It needs the archive and send source of
[DICOM SEG export](#dicom-seg-export), and no masks.

**`GET .../dicom-view`** returns `{available, reason?, destinations, exports: Job[],
encoding_version}`. `exports` lists view jobs only.

**`POST .../dicom-view`** takes multipart `image` (a PNG), `reference_image_id`, optional
`destination` and optional `resend`, and returns an `orthanc_view_export` job. Export never changes
review status, adds a revision or activates one.

- Leave `destination` out to archive only: an empty value is a 422, and the text `null` is an
  unknown destination.
- `reference_image_id` must be the reference image of the CT that drew the picture.

**Limits.**

- A plain `Content-Length` and no `Transfer-Encoding`, or 411.
- A PNG of at most 128 MiB or the upload limit, whichever is lower. A body more than 1 MiB over that
  gets 413 before it is read.
- A complete, still, 8-bit RGB or RGBA PNG of at most 16384 pixels per axis and 36 Mi pixels, not
  one uniform colour. Otherwise 400 with a fixed message, leaving nothing behind.

**Encoding.** One uncompressed Secondary Capture instance in a new series: Explicit VR Little
Endian, `Modality` OT, `ConversionType` WSD, `ImageType` `DERIVED\SECONDARY\OTHER`, `SeriesNumber`
501, `InstanceNumber` 1 and `SeriesDescription` `3DScan view <rev8>`, where `rev8` starts the
revision ID. Text is UTF-8, alpha blends over black, and nothing is burned in.

- Uncalibrated: no pixel spacing, image position, image orientation or frame of reference, and an
  empty `PatientOrientation`. `ImageComments` says it is not calibrated, has no orientation labels
  and is for research use only.
- `DerivationDescription` names the revision and source series UID, with no source reference
  sequences.
- `BurnedInAnnotation` is `YES` if any source header says so. `RecognizableVisualFeatures` is
  absent.

**Identity.**

- Patient and study elements, with `IssuerOfPatientID` and `Laterality` when present, are copied
  exactly from the source headers, which must agree on one patient, study and series.
- The job fails if the revision, reference image, source manifest or PNG hash change after enqueue.
- Orthanc's patient ID for the source study must equal the instance's, or nothing is stored.
- After upload, Orthanc's parent study must be the source study, or the job fails before C-STORE and
  leaves the stored instance for inspection.

**Retries.** These follow [DICOM SEG export](#dicom-seg-export), keyed on the revision, reference
image, source manifest, PNG hash and encoding version.

- Identical bytes are the same export on one SOP. A failed job retries it. A completed one is
  returned as it stands, delivering nothing, unless `resend` is set.
- A retry needs the same bytes from the client, so a job's `error` never offers one.
- Different bytes make a new series, so if an earlier unconfirmed delivery did land, the receiver
  gets both.

**Result.** `result.dicom_view` has `export_id`, `encoding_version`, `archived`, `sent`,
`instance_count` and optional `destination` and `destination_id`, with no patient fields or PNG
hash.

### DICOM view file

The same capture becomes a file to download, with nothing archived or sent. It needs no archive
access, and works for any CT or classic MR prepared from DICOM, folder and ZIP imports included.

- **`GET .../dicom-view-file`** returns `{available, reason?, exports: Job[], encoding_version}`.
- **`POST .../dicom-view-file`** takes multipart `image` and `reference_image_id`, with the limits
  above, and returns a `view_file_export` job. Identical bytes reuse one job and SOP.
- **`GET .../dicom-view-file/{export_id}/view.dcm`** serves a completed file's verified bytes, or 404.
- The encoding and identity rules are the ones above, with no archive checks. `result.dicom_view`
  adds `file: true`.

## DICOMweb archive export

For a case whose `import_source.kind` is `dicomweb`, the SEG and view routes above archive in the
[DICOMweb archive](ARCHIVES.md#archive-results) instead of Orthanc. Files are unchanged.

- `GET .../dicom-seg` and `GET .../dicom-view` add `archive_name` and have no `destinations`, and
  `dicom-seg` has no `osirix_roi`. `available` and `reason` describe archiving there.
- `POST .../dicom-seg` without `delivery: "file"` returns an `archive_seg_export` job, and
  `POST .../dicom-view` an `archive_view_export` job. A `destination` or `resend` is refused.
- The job stores each instance with STOW-RS, then reads it back. `delivery-v2.json`, not
  `delivery.json`, records each stored SOP by archive, with any STOW warning codes and `verified`:
  `bytes`, or `canonical` for the same identity, patient and decoded pixels.
- The result adds `archive`, the archive's name.

## Direct DICOM send

With `SCAN3D_DICOM_DESTINATIONS_FILE` set, a case whose `import_source.kind` is not `orthanc` can
also [send](ARCHIVES.md#direct-dicom-sends) its SEG or view to a configured node with C-STORE. A
case from Orthanc refuses it with 400.

- `GET .../dicom-seg` and `GET .../dicom-view` add `direct: {available, reason?, destinations:
[{id, name, aet, key}]}`, absent when nothing is configured or the case came from Orthanc.
  `exports` includes these jobs.
- `key` names the destination's address, as `destination_key` does in a direct job's result, so a
  send to an earlier address of the same `id` is not a send to this one.
- `POST .../dicom-seg` with `delivery: "direct"` and a `destination` returns a `direct_seg_export`
  job, and `POST .../dicom-view` with form field `delivery=direct` a `direct_view_export` job.
- The SEG is the same instance as a file or archived SEG of the same masks. `export_key` names the
  destination by AE title, host, port and TLS, so a moved destination is a new export, and a job
  queued before the move fails without sending.
- `delivery-v2.json` records per destination its `state` (`starting`, `success`, `failure`,
  `refused` or `unknown`), its attempt, and each SOP's status and transfer syntax.
- `starting` is written before connecting, and a restart turns it into `unknown`, stage
  `Delivery not confirmed`. A retry of `unknown` sends the same instance again.

## Orthanc routes

These are off until `SCAN3D_ORTHANC_URL` is set. See [ORTHANC.md](ORTHANC.md#configure-3dscan).

**`GET /api/orthanc/status`** returns `{configured, connected, name, error, version?,
api_version?}`, reporting an unconfigured or unreachable archive without upstream detail.

**`POST /api/orthanc/studies/query`** takes `{patient_id?, patient_name?, accession_number?,
study_date?, offset?, limit?}` and returns `{studies, offset, limit, has_more}`. `limit` is 1–50,
and values use Orthanc's DICOM query syntax. Identity fields go in the body, never the URL.

A study is `{id, study_uid, description, study_date, patient_id, patient_name, accession_number,
series_count, modalities, stable}`.

**`GET /api/orthanc/studies/{id}/series`** returns `{study, series}`. A series is `{id, description,
modality, series_uid, instance_count, expected_instances, stable, completeness, snapshot, warnings,
supported, unsupported_reason}`. `completeness` is `receiving`, `incomplete`, `complete` or
`unknown`.

**`POST /api/orthanc/imports`** takes `{series_id, snapshot, label?}` and returns an
`orthanc_import` job.

- Receiving and incomplete series, and any change since selection, return 409.
- A stable series with an unknown expected count imports with a warning.
- A completed case holds the originals with no revision yet. `process` prepares it next.
- Requests from anyone for the same snapshot return the same job. Anyone can retry a failed or
  cancelled import, which reuses its case, keeps who added it and never overwrites earlier bytes.

**`GET /api/cases/{id}/orthanc-series`** returns `{study, series}` for the case's own Orthanc study,
each series with `in_case`. **`POST`** takes `{series_id, snapshot}` and returns an `orthanc_import`
job that adds the series to the case, which keeps its status and open result. It is refused for a
case not imported from Orthanc, a series of another study, or a series the case already holds.

**`POST /api/orthanc/launch`** takes `{series_id, source_key}` and returns `{study, series, case_id,
job}`, creating nothing and transferring no DICOM.

- `case_id` identifies the library's valid completed import, whoever made it, `job` its pending one,
  otherwise both are null.
- `source_key` is the SHA-256 of the normalized archive URL: an identity check, not a credential.
- A different archive is refused before any network access. An import failing verification is 409.

The viewer accepts launch links as `#orthanc-series=<id>&orthanc-source=<key>`. New imports still
need an explicit action.

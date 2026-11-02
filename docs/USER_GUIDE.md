# User guide

3DScan runs on your own computer. Import a CT study, open one series, segment it with
TotalSegmentator, correct the masks against the source images and export. Every saved change is a
new revision, so earlier work is never overwritten.

> **Research use only · not for diagnostic use.** The bundled models are research tools and
> 3DScan has no clinical validation. Real cases and portable exports carry this label. Always
> verify anatomy on the source images.

Start 3DScan with `.venv/bin/scan3d serve` and open **http://127.0.0.1:9010**. See
[installation](INSTALL.md) and [configuration](CONFIGURATION.md). **Explore synthetic demo** opens
a phantom with ready-made masks and no patient data.

## Sign in

On the default `127.0.0.1` address sign-in is off and the app opens directly. On any other address,
or with `SCAN3D_AUTH=password`, everyone needs an account. See
[configuration](CONFIGURATION.md#sign-in).

**First account.** The app shows this command until an account exists. Run it on the workstation:

```bash
.venv/bin/scan3d users add NAME --admin
```

It asks for a password of 10 to 1,024 characters, twice. `--display-name` sets the name shown in the
header. The first admin receives every case that has no owner.

**Signing in.** Enter your username and password. Over a network, use the HTTPS address. Each
account sees only its own cases. Cases created on the command line go to the oldest admin.

**Shared computer.** Tick this on a computer other people use. You are signed out when the browser
closes, and after 12 hours at most, and the browser stores no scan data for this session.

**Signing out.** Choose your name in the header, then **Sign out**. **Sign out other devices** ends
this account's other sessions and keeps this one.

**When a session ends.** A session lasts 30 days from its last use. If yours ends while you work, a
sign-in screen covers the page. Sign in as the same person and the page carries on with your marks
intact. Signing in as someone else reloads the page.

**Forgotten password.** Run this on the workstation. It also signs the account out everywhere:

```bash
.venv/bin/scan3d users passwd NAME
```

`.venv/bin/scan3d users list` shows the accounts. Anyone with a shell on the machine can still read
every file, so protect the machine and the data directory as well.

## Import a study

Choose **+ Import scan** in the header. Do not put patient identifiers in **Case label**.

**Browser upload.** On the **Folder / ZIP** tab, drop files or use **Choose folder** or **Choose
ZIP / files**, then choose **Import scan →**. The limit is 16 GiB in total. **Cancel upload**
discards the staged files.

**Path on the server.** The path is a folder or ZIP inside `SCAN3D_IMPORT_ROOTS` (default: the
3DScan directory). Folders with symbolic links are refused.

```bash
.venv/bin/scan3d import /absolute/path/to/study --label "Teaching case"
```

**Orthanc.** If an archive is [configured](ORTHANC.md):

1. On the **Orthanc archive** tab, filter by **Patient ID**, **Patient name**, **Accession
   number** or **Study date** and choose **Search studies**.
2. Select a study, then one series under **Choose a reconstruction**.
3. Choose **Import selected series →**.

The series is copied locally and the archive stays unchanged. A series still arriving or
incomplete cannot be imported. If the expected image count is unknown, 3DScan imports the listed
images and says so: confirm that the acquisition is complete.

## Open a series

1. On **Choose the right series.**, select one series. Expand the checks under an unavailable
   series to see why.
2. Set **Scan region / study** and **Contrast phase**, using **Unknown** if unsure. **Coronary CTA
   (CorCTA)** and **Cardiac CT** also offer **Cardiac reconstruction** and **Reconstruction phase
   (% R–R)**.
3. Choose **Open CT →**.

These fields are labels only: they change no pixels, rendering or tasks. **Select series** in the
toolbar opens another series and keeps your result, and **Source image** switches between opened
images.

## Run TotalSegmentator

1. Choose **Segment CT…**.
2. Pick a **Task**. **Find a task or structure** searches tasks and anatomy, and **Included
   structures** lists the outputs.
3. Set any options the task supports: **Robust crop** (`-rc`), **Body crop** (`-bs`) and **Remove
   small blobs** (`-rmb`, components below 200 mm³).
4. Choose **Run segmentation**.

Each run creates a revision that replaces that task's results **and** any segments with matching
anatomical names, including manual corrections. Matching ignores capitalization, underscores and
whitespace. Other segments are kept, and the earlier revision stays in **Revision history**.

A task's weights download on its first run. AI jobs run one at a time across all GPUs, on the device
set by `SCAN3D_DEVICE` unless you [choose another GPU](#choose-a-gpu), and a CPU is much slower.
Progress shows at the top of the window, with **Cancel job**. Imports, opening a CT, direct edits
and saves run on a separate queue, so they do not wait behind a segmentation.

### TotalSegmentator license

Tasks that need a license show **· license required**. Under **Segment CT…**, expand
**TotalSegmentator license**, enter your key (it begins with `aca_`) and choose **Validate and
save**. If validation fails, the previous key stays.

The key is checked with TotalSegmentator's service and stored for all cases in `config.json` under
`TOTALSEG_HOME_DIR`, never in case records. **Remove key** deletes it, and existing segmentations
stay. License changes wait for queued and running jobs.

**Validated when saved** does not promise access to every task. See the
[provider's license page](https://backend.totalsegmentator.com/license-academic/).

### Choose a GPU

With [a second GPU set up](INSTALL.md#add-a-second-gpu), **Run on** appears after **Task**. With
one GPU, skip this section. An NVIDIA RTX 5070 Ti is supported as a second GPU beside an AMD Radeon
AI PRO R9700, for segmentation and AI assist.

**Run on** lists each GPU with its memory and starts on the default. Your browser remembers your
choice, separately for segmentation and [AI assist](#choose-a-gpu-for-ai-assist). A line under the
form reports on the selected GPU:

- **Unavailable:** marked **· unavailable** with its reason, and cannot be picked. A GPU chosen
  earlier stays selected, and **Run segmentation** stays off until you choose another or fix it.
  **Check again** reads the list again.
- **Removed:** replaced by the first available GPU, and the line says so.
- **Default:** can always be chosen, with a warning if its last GPU check failed or is out of date.
  It can read **Default device**, or **CPU** when `SCAN3D_DEVICE` is `cpu`.
- **Drives your display:** **This GPU also drives the display, so free memory depends on what is
  on screen.**

A job on an added GPU runs there or fails saying why: it never moves to another GPU or the CPU.
With `SCAN3D_DEVICE=auto`, the default entry uses the CPU if PyTorch sees no GPU, even if the list
names one.

Results from the two cards are not identical, so treat a result from another GPU as new and inspect
it.

**Revision history** names the GPU beside each run and can add:

- **reused saved result:** nothing ran again.
- **part re-ran in system memory:** usually GPU memory ran out, so the run took longer. Only runs
  on an added GPU are checked for this.

## Arrange the workspace

The sidebar holds the anatomy list and the **Selected structure**, **Refinement** and **Revision
history** tabs. The stage tiles the 3D view and **CT review**.

- The layout buttons beside **Panels** show views side by side, stacked or alone.
- **Panels** toggles each area, and **Reset layout** restores the defaults.
- Drag a divider to resize, and double-click it to reset.
- **Layers** folds the 3D controls, and **Display** the CT controls.

**Show all**, **Hide all** and **Show selected only** apply to the whole case even when the list is
filtered. A section's eye hides it, and its arrow collapses it.

## View the images

### CT review

Three linked planes share one position, with its patient RAS coordinates and HU value below.

- **Navigate:** click to move, slider to change slice, right drag for window/level, middle drag to
  pan, scroll to zoom. **Reset planes** restores them.
- **Window:** **Soft tissue**, **Lung**, **Bone** or **Vessels**, or type **W** and **L**.
- **Projection:** **MPR**, **MIP** or **MinIP**, with **Slab mm**. A slab shows overlays on its
  centre slice and pauses drawing. **Rotatable crosshairs** adds crosshairs you can rotate.
- **Overlays:** **Segments** chooses what shows, **Use selected segment** follows the selection and
  **Clear overlays** shows CT alone. **Fill**, **Outline** and **2D overlay opacity** set the style.

Tilted-gantry (sheared) scans are straightened for display only. Your marks still map to the
original voxels.

### 3D view

**Layers** opens the **Rendering stack**: **Segmentation**, **CT volume** and **CT plane**. Drag to
rotate, right drag to pan, scroll to zoom. **Expand ↗** fills the screen, and **Restore ↙** or
**Escape** returns.

- With **CT volume** visible, surfaces are solid. Turn it off to use **All segment opacity**.
- **CT plane** shows one native slice: choose **Native i**, **j** or **k**, move its slider, or use
  **Face plane**.
- Tilted-gantry scans, MRI and the [portable HTML](#portable-offline-html) page use Three.js instead
  of Cornerstone3D.
- The browser memory budget is up to 3 GiB. If a view does not fit, close CT review or use **Show
  selected only**.

### CT volume settings

These settings change the display only, never source images, masks or measurements.

- **Appearance** picks a preset, adjusted by **Intensity** and **Opacity**. **Crop volume** limits
  the native i, j and k ranges.
- **Mask to segmentation** shows the CT only in chosen segments. Pick them with **Use selected**,
  **Use visible** or the list, and set **Show volume** to inside or outside. **Display margin**
  retains thin walls.
- **Color segments** (Cornerstone3D) tints the CT inside chosen segments with their anatomy-list
  colors, blended by **Color strength**. Change a color under **Selected structure**.
- **Lighting and shadows** (Cornerstone3D) sets the **Rendering method**: **Preset lighting**,
  **Standard**, **Cinematic (VolView Hybrid)** or **Ambient occlusion**. **Reset lighting** returns
  to the preset.
- **Cinematic heart** is a cardiac appearance. **Measure from segments** sets its reference HU from
  myocardium and aorta or left-heart masks. Or enter **Myocardium** and **Blood pool** and choose
  **Apply reference HU**.

## Review and correct masks

Select anatomy in the list or on the model, inspect its overlay in all three planes and check any
QC messages under **Selected structure** against the CT. Then choose **Review segmentation ↗** and
pick the target under **Structure**.

Review opens on your main-page views. **CT volume** turns off, and slab projection and rotatable
crosshairs pause, until **Exit review**.

- Planes lock to the native source slices.
- Marks autosave into a draft that survives reloads and restarts. Accepted masks change only when
  you save.
- Mark only a settled, full-resolution view.
- **↶ Undo** and **↷ Redo** step through your marks.

### Direct edit

**Direct edit** is the default. It is exact, runs on the CPU and needs no AI model.

- **Paint** and **Erase:** set **Radius** in millimetres and draw on a native slice. Only the voxel
  centres you brush change, in stroke order.
- **Scissors:** draw a cut on the 3D view. Choose a **Shape** (**Freehand**, **Rectangle** or
  **Circle**) and a **Cut** (**Erase inside** or **Erase outside**). It cuts the full depth of the
  selected segment from the current camera angle. Right drag rotates, and **Escape** cancels.

Then choose **Apply edits · N**.

### AI assist

**AI assist** uses nnInteractive for larger corrections and can change voxels beyond your marks.
It needs the [model installed](INSTALL.md#ai-models), whose pretrained checkpoint is licensed for
noncommercial use only. Inference stays on your computer and never trains the model.

1. Choose **+ Include** or **− Exclude**.
2. Mark with **Point**, **Hint brush**, **Lasso** or **Box** (2D, on one native slice).
3. Choose **Run AI · N** once for all pending marks. This can take a minute or more, even on a GPU.

**Options +** holds:

- **Native slice planes**, required for hint brush, lasso and box
- **Automatically focus inference**, on by default
- **Start from an empty mask**, which leaves the saved mask unchanged
- **Refine whole mask**, a slower extra model stage

Save or discard your draft before switching between **Direct edit** and **AI assist**.

### Choose a GPU for AI assist

With [a second GPU set up](INSTALL.md#add-a-second-gpu), **Options +** adds **Run on**, with the
same [status lines and **Check again**](#choose-a-gpu), and the toolbar shows **AI ·** and the GPU
in use. Neither appears with one GPU or in **Direct edit**.

The first AI result holds the draft on its GPU. **AI result from** beside **Exit review** names it,
as does **Revision history** once you save. These free the draft and drop the current result:

- **↶ Undo** past the first result, unless it came from **Refine whole mask** with no marks
- changing **Automatically focus inference** or **Start from an empty mask**

A held draft shows **Re-run draft on** instead of **Run on**. To move it, choose a GPU there, then
**Re-run on this GPU**. A re-run:

- repeats every Apply step from the original mask, with any pending marks last
- replaces the current result only when it finishes
- can change regions where you made no marks, so inspect the new result before saving

If a re-run fails or is cancelled, the old result stays visible but cannot be saved. Choose
**Re-run on this GPU** again, or **Discard draft**. Your marks are never lost.

**If a GPU cannot be used**, a message beside **Exit review** gives the reason, with **Check
again**. **Run AI** for marks and **Refine whole mask** stop until it is fixed. Choose another GPU,
or re-run or discard a draft that has a result. Work that needs no GPU continues.

**If a GPU has been removed**, **Run on** moves to the first available GPU and says so. A draft from
that GPU keeps **Re-run on this GPU**, even with one GPU left. If the GPU comes back, **Check again**
clears the message.

**With a draft in two tabs**, if the other tab changed or ran the draft, this page offers **Reload
saved work** and saves nothing over it. If the other tab started the draft first, a mark made here
is not saved. **Reload saved work** or **Discard draft** opens that draft and clears the mark, so
place it again there.

### Add a missing structure

Choose **+ Add segment**, enter a **Name**, **Group** and **Color**, choose a relationship, then
**Create empty segment**. Mark its extent on the CT, apply, inspect and choose **Save new segment**.

### Mark in the 3D view

**Mark on**, under **Review tools**, selects **Surface point**, which locks picking to the target,
or **CT plane**, which takes all the drawing tools on a native slice. Right drag rotates while
marking, and a 3D point moves the linked CT crosshair so you can confirm it. **Face plane** faces
the CT plane at any time.

**Review tools** starts folded. A mark cannot start underneath it, so fold it to reach that part of
the view.

### Toolbar messages and panels

Errors, warnings, results and 3D preview progress appear beside **Exit review**, errors first. Past
two lines, scroll that area. Some errors carry a button, such as **Retry preview**.

**+ Add segment**, **Relationship**, **Impact preview** and **Options +** open over the viewers. A
mark cannot start underneath an open panel, so choose the button again to fold it.

### Compare and save

Applying creates a candidate and does not change accepted masks.

1. Use **Original** and **Candidate** to compare the CT mask and the 3D surface.
2. Read the voxels changed and volume before and after, beside **Exit review**.
3. If other segments are affected, open **Impact preview** and choose **Show affected in CT**.
4. Add marks and apply again as needed. Saving waits until pending marks are applied.
5. Choose **Save correction** only after inspecting the result.

Saving updates that structure and any selected siblings in a new revision. **Discard draft** closes
a proposal instead. A candidate that changes nothing cannot be saved. Direct erasing can leave an
existing segment empty at 0 mL, but empty new segments and empty AI candidates cannot be saved.

### Handle overlapping segments

**Relationship** sets how a segment treats overlaps:

- **Independent** preserves them.
- **Subregion of** records a parent, and overlap with it is preserved.
- **Subdivision alongside** moves overlapping voxels to the target, only from siblings you select
  under **Replace overlaps only in:**.

### Undo and revisions

In **Revision history**, **↶ Undo to parent** restores the previous revision exactly, and
**Activate** switches to any other. Nothing is deleted. **Current** and **Before** in the 3D header
compare surfaces with the parent, and **Compare CT overlay with parent** does so on the slices.

To record your review, set **Revision status** under **Review status & tools** to **Unreviewed**,
**Reviewed** or **Corrected**. It applies to the whole revision.

### Other correction routes

The **Refinement** tab offers **Keep component at seed**, **Remove component at seed**, **Keep
inside region** and **Erase inside region**. Click a slice or surface to place a seed, or enter
coordinates or bounds in patient RAS millimetres, then choose **Create refinement revision →**.

For 3D Slicer, expand **External correction with Slicer** under **Review status & tools**:

1. **Slicer bundle ↓** downloads the reference CT, a `.seg.nrrd` and a mapping file.
2. Edit in 3D Slicer, keeping the segment tags.
3. Choose **Import correction ↑** with the self-contained `.seg.nrrd`. 3DScan checks segment
   identities, the source CT hash and geometry, then creates a revision.

## Measurements and model agreement

Volumes are in millilitres, counted from mask voxels.

**Model agreement**, at the top of **Revision history**, compares accepted masks with each
structure's saved TotalSegmentator output. A later run that replaced a structure becomes its new
original. **Details** opens the table:

| Column         | Meaning                                        |
| -------------- | ---------------------------------------------- |
| **Dice**       | Voxel overlap. 1 is identical.                 |
| **HD95**       | Larger 95th-percentile surface distance in mm. |
| **ΔV**         | Accepted volume minus original volume.         |
| **Voxels +/−** | Voxels added and removed.                      |

**All cases ↗** pools the active revision of each case marked **Reviewed** or **Corrected**, by
task and structure: how often the model was accepted unchanged, Dice and mean absolute ΔV.
Synthetic demo cases form a separate pool.

- These numbers measure reviewer edits, not ground truth. An uncorrected mask can still be wrong.
- Review status covers a whole revision, so a structure nobody inspected counts as accepted.
- HD95 ignores the farthest 5% of the surface, so a small fix can show 0 mm while Dice is below 1.
- Segments added by a reviewer are not compared.

## Export

The HTML, DICOM SEG and DICOM view exports read the saved revision: first save or discard any
draft, return to the current revision and turn off comparison. Exports can contain patient images
and metadata, so treat them like the study itself.

### PNG

**PNG ↗** in the 3D view saves the current view, including any review marks.

### DICOM view

**DICOM ↗** in the 3D view adds the current view to the original study as a new one-image series.
It needs a configured Orthanc and a CT imported from that archive. Choose **Archive in Orthanc**, or
pick a **Send destination** and choose **Send to** it. See
[send a 3D view back](ORTHANC.md#send-a-3d-view-back).

The image is a picture. It has no calibration and no orientation labels, so do not measure on it,
and nothing in it marks left from right.

- The button is disabled during review and for the same reasons as the other exports. A capture is
  refused while the view is updating or showing an error.
- Exporting an unchanged view again delivers nothing new. **Send again** deliberately resends it.
- After a failure, retry from the dialog. If the dialog has gone, check the study in Orthanc and
  capture the view again.
- If the dialog says the outcome is unknown, press the same button again.
- You can close the dialog during an export. The result appears as a notice, or in the error banner
  if it fails or cannot be confirmed.

### Portable offline HTML

1. Choose **Export viewer ↗**.
2. Under **Include layers**, choose **Segmentation**, **CT volume** and **CT plane**.
3. Choose **Download HTML**.

The file opens offline in a desktop browser with the camera, appearances, visibility, crop, mask
and plane position of the selected layers. Unselected data is left out.

- It renders with Three.js, so the image can differ. Native lighting and **Color segments** are not
  exported.
- It must stay under 512 MiB. If the CT is too large, export the segmentation alone.
- Editing and three-plane CT review stay in the app.

### Slicer bundle

**Slicer bundle ↓**, under [other correction routes](#other-correction-routes), also serves as an
overlap-preserving export for 3D Slicer.

### DICOM SEG

**Export SEG…** works for classic CT from a folder, a ZIP or Orthanc. Selected masks go into one SEG
object with overlaps preserved. All nonempty masks start selected. **Select visible** takes a
snapshot of the masks switched on in the anatomy list.

To save a file, with no Orthanc needed:

1. Select the masks.
2. Choose **Prepare SEG file**. You can close the dialog while it builds.
3. When the button changes, choose **Download SEG file**.

The file carries the CT's patient and study details, so handle it like the study.

If the CT came from a configured Orthanc, you can also choose **Archive in Orthanc**, or pick a
**Send destination** and choose **Send to** it. See
[send a saved segmentation back](ORTHANC.md#send-a-saved-segmentation-back). **Send again**
deliberately resends the same instances.

**Cancel export** stops an export in progress, but closing the dialog does not. A SEG that has
reached Orthanc stays there, and a send cannot be stopped once Orthanc has started it.

If the dialog says the CT has no verified original series, prepare its series again. The synthetic
demo case has none.

To give each voxel to one structure, tick **Resolve overlaps in this export** and rank the masks
with the arrows. The higher mask keeps overlapping voxels.

- Only this SEG changes, and its series is labelled `overlaps resolved`. Saved masks, measurements
  and review status stay as they are.
- If a mask would lose every voxel, the export stops and names its rank.
- Each ranking is a separate export, and sending one does not count as sending another.

## Import existing DICOM SEG masks

**Import DICOM SEG…** adds masks made elsewhere to an open CT, whether that CT came from a folder, a
ZIP or Orthanc. It needs no AI and no Orthanc connection.

1. Open the original CT on its current revision, with no draft open.
2. Choose **Import DICOM SEG…** and select up to 64 SEG files, 512 MiB in total, not a ZIP or the
   source images.
3. Choose **Import masks**.

A CPU job checks the source references and exact voxel alignment, then adds independent segments in
a new revision. Existing masks stay intact, and **↶ Undo to parent** restores the prior revision.

Only binary, uncompressed, little-endian SEG on the CT's native grid is accepted. Fractional,
labelmap, concatenated, compressed and resampled SEG is refused, as is SEG that references a
different source. SEG stored in Orthanc is not attached automatically: select the file here.

## Back up the case library

```bash
.venv/bin/scan3d backup /path/on/another/disk/scan3d-backups
```

Each run writes `scan3d-<UTC time>/` with a database snapshot, the case folders, the surface cache
and a `manifest.json`. The server can keep running.

To restore, stop the server and copy a snapshot's contents over the same data directory, since
records store absolute paths. A snapshot from before November 2026 has no `scan3d.sqlite3-wal`, so
first move any `scan3d.sqlite3-wal` and `scan3d.sqlite3-shm` out of the data directory.

A backup holds patient images, so protect it like the library. It does not cover anything held in
Orthanc. It keeps accounts and the audit log but no sessions, so everyone signs in again after a
restore.

## Experimental MRI

MRI is off by default. Set `SCAN3D_MODALITIES=CT,MR` and restart to offer static 3D MR series. Labels
change to **Open MR →** and **Segment MR…**, and `total_mr` is the default task.

MR signal values are not HU, so review each sequence separately. MR uses Three.js, and DICOM SEG
import and export are CT only.

## Command line

These commands use the same library as the app. Job commands wait for the result; if the server is
running, it does the work.

```bash
.venv/bin/scan3d process CASE_ID --series SERIES_UID --profile chest_abdomen --phase venous
.venv/bin/scan3d segment CASE_ID --task total --body-seg --remove-small-blobs
.venv/bin/scan3d include-all CASE_ID
.venv/bin/scan3d refine CASE_ID --revision REVISION_ID --structure total:liver --recipe recipe.json
.venv/bin/scan3d optimize CASE_ID
.venv/bin/scan3d job JOB_ID
```

- `segment --inference-path ID` runs on [an added GPU](#choose-a-gpu). `scan3d doctor` lists the IDs.
- `include-all` restores labels filtered out earlier, as a new revision that keeps corrections.
- `optimize` prepares viewer assets for an older case without changing masks or revisions.

A `refine` recipe is JSON in patient RAS millimetres:

```json
{ "type": "erase_region", "bounds_ras": [[-80, -60, -40], [-40, 20, 30]] }
```

The operations are `keep_region` and `erase_region` with `bounds_ras`, and `keep_component` and
`remove_component` with `seed_ras: [R, A, S]`. Operations with no effect, or that delete a whole
structure, are rejected.

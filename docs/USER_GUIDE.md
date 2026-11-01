# User guide

3DScan runs on your own computer. You import a CT study, open one series, segment it with
TotalSegmentator, check the masks against the source images, correct them and export the
result. Every saved change is a new revision, so earlier work is never overwritten.

> **Research use only · not for diagnostic use.** The bundled models are research tools and
> 3DScan has no clinical validation. Real cases and portable exports carry this label. Always
> verify anatomy on the source images.

This guide assumes 3DScan is [installed](INSTALL.md); settings are in
[configuration](CONFIGURATION.md). Start it with `.venv/bin/scan3d serve` and open
**http://127.0.0.1:9010**. To try the interface without a scan, choose **Explore synthetic
demo**: a labelled phantom with ready-made masks, no patient data and no model inference.

## Import a study

Choose **+ Import scan** in the header. Any **Case label** should be pseudonymous, so do not
type patient identifiers.

**Browser upload.** On the **Folder / ZIP** tab, drop files onto the dialog or use **Choose
folder** or **Choose ZIP / files**, then choose **Import scan →**. Files upload in batches, up
to 16 GiB in total. Interrupted batches are resent automatically, **Cancel upload** discards
the staged files, and the case appears only after every file has arrived.

**Path on the server.** There is no button for this; use the command line:

```bash
.venv/bin/scan3d import /absolute/path/to/study --label "Teaching case"
```

The path is a folder or ZIP file inside `SCAN3D_IMPORT_ROOTS`, which defaults to the 3DScan
directory. Folders are copied into the case, and folders containing symbolic links are refused.

**Orthanc.** If an archive is [configured](ORTHANC.md):

1. On the **Orthanc archive** tab, filter by **Patient ID**, **Patient name**, **Accession
   number** or **Study date** and choose **Search studies**.
2. Select a study, then one series under **Choose a reconstruction**.
3. Choose **Import selected series →**.

The series is copied to your local library and the archive stays unchanged. A series that is
still arriving or is incomplete cannot be imported. If the expected image count is unknown,
3DScan imports the images currently listed and says so: confirm that the acquisition is complete.

## Open a series

1. Select one series on the **Choose the right series.** page. Expand the checks under an
   unavailable series to see why it cannot be opened.
2. Set **Scan region / study** and **Contrast phase**, using **Unknown** if unsure. For
   **Coronary CTA (CorCTA)** and **Cardiac CT** you can also record **Cardiac reconstruction**
   and **Reconstruction phase (% R–R)**.
3. Choose **Open CT →**.

These fields are labels: they do not change pixels, rendering or tasks. Opening prepares the
series on the CPU and runs no model. Phases and reconstructions stay independent. **Select
series** in the toolbar opens another one while your result stays available, and **Source
image** switches between images you have opened.

## Run TotalSegmentator

1. Choose **Segment CT…**.
2. Pick a **Task**. **Find a task or structure** searches task names and output anatomy, and
   **Included structures** lists the selected task's outputs.
3. Set any options the task supports: **Robust crop** (`-rc`), **Body crop** (`-bs`) and
   **Remove small blobs** (`-rmb`, components below 200 mm³).
4. Choose **Run segmentation**.

A task's weights download the first time it runs. AI jobs run one at a time on the device set
by `SCAN3D_DEVICE`, unless you [choose another GPU](#choose-a-gpu), and a CPU is much slower than
a GPU. Progress appears at the top of the window, beside the case selector, and **Cancel job**
stops a running job.
Imports, opening a CT, direct edits and saves use a separate queue, so they do not wait behind a
segmentation.

Each run creates a new revision that replaces that task's results **and** any segments with
matching anatomical names, including manual corrections. Matching ignores capitalization,
underscores and whitespace. Unmatched segments are kept, and the earlier revision stays in
**Revision history**.

### TotalSegmentator license

Tasks that need a license show **· license required**. Under **Segment CT…**, expand
**TotalSegmentator license**, enter your key (it begins with `aca_`) and choose **Validate and
save**. The key is checked with TotalSegmentator's service and stored for all cases in
`config.json` under `TOTALSEG_HOME_DIR`, with private file permissions. It is never returned to
the browser or saved in case records. If validation fails, the previous key stays. **Remove
key** deletes it, and existing segmentations stay available. License changes wait for queued
and running jobs to finish.

**Validated when saved** records that earlier check. It does not promise access to every task:
licensed models check access when their weights download. See the
[provider's license page](https://backend.totalsegmentator.com/license-academic/) for terms.

### Choose a GPU

On a workstation with [a second GPU set up](INSTALL.md#add-a-second-gpu), **Run on** appears
after **Task**. With one GPU there is no **Run on**, and nothing in this section applies.

**Run on** lists each GPU with its memory, and starts on the default GPU. Your browser remembers
what you choose, separately for segmentation and for [AI assist](#choose-a-gpu-for-ai-assist).
The list is read again each time you open **Segment CT…**, and after a run is refused, because a
refusal can mean the list is out of date. A line under the form says what you need to know about
the selected GPU:

- **An unavailable GPU** is marked **· unavailable** and cannot be picked. Each one gets a line
  giving its reason, whether you chose it or not, and **Check again** beside **Run on** reads the
  list again once it is fixed. If you chose it earlier it stays selected, and **Run segmentation**
  stays off until you choose another or the problem is fixed. 3DScan never swaps it quietly.
- **A GPU that has been removed** is replaced by the first available one, and the line says so.
- **The default GPU** can always be chosen. If its last GPU check failed or is out of date, the
  line says so as a caution. It reads **Default device** when no GPU check names a card, or when
  that vendor's cards have changed since the check, which also earns the caution, and **CPU** when
  `SCAN3D_DEVICE` is `cpu`.
- **A GPU that drives your display** gets the line **This GPU also drives the display, so free
  memory depends on what is on screen.**

One AI job runs at a time across all GPUs, so a job on one GPU waits for a job on the other. A job
on an added GPU runs there or fails saying why: it never moves to another GPU or to the CPU. The
default entry follows `SCAN3D_DEVICE`, so with `auto`, the default setting, it uses the CPU if
PyTorch sees no GPU, even if the list still names one. The top of the window names your choice
while the job waits and runs, and if it fails.

Each GPU keeps its own saved results, so a repeat run reuses only a result from the same GPU. AMD
and NVIDIA software are not assumed to give identical results, and how far they differ has not
been measured. Treat a result from another GPU as a new result, and inspect it.

**Revision history** names the GPU beside each run, as **Run on** and its name, and adds **reused
saved result** when nothing ran again. It can also add **part re-ran in system memory**: nnU-Net
repeated part of the task on the same GPU, keeping its results in system memory, usually because
GPU memory ran out. The run took longer, and an automated check note under **Review status &
tools** records it. A GPU that drives the display has less free memory, so expect this more often
there. Only runs on an added GPU are checked for this, so its absence on the default GPU proves
nothing.

## Arrange the workspace

The sidebar holds the anatomy list, with **Selected structure**, **Refinement** and **Revision
history** tabs below it. The stage tiles the 3D view and **CT review**.

- The layout buttons beside **Panels** show the views side by side, stacked or alone.
- **Panels** toggles each area, and **Reset layout** restores the defaults.
- Drag a divider to resize. Double-click it to reset.
- **Layers** folds the 3D controls, and **Display** folds the CT controls.

In the anatomy list, **Show all**, **Hide all** and **Show selected only** apply to the whole
case even when the list is filtered. The eye beside a section heading hides that section, and
the arrow collapses it.

## View the images

### CT review

Three linked planes share one position. Below them are its patient RAS coordinates and HU value.

- **Navigate:** click to move the position, use the slider under a plane to change slice, right
  drag for window/level, middle drag to pan, scroll to zoom. **Reset planes** restores them.
- **Window:** choose **Soft tissue**, **Lung**, **Bone** or **Vessels**, or type **W** and **L**.
- **Projection:** **MPR**, **MIP** or **MinIP**, with **Slab mm**. During slab projection,
  overlays show the centre slice and drawing is paused. **Rotatable crosshairs** adds crosshairs
  you can rotate.
- **Overlays:** **Segments** chooses what appears on the slices. **Use selected segment** returns
  to following the selection, and **Clear overlays** shows CT alone. **Fill**, **Outline** and
  **2D overlay opacity** set how they are drawn.

Tilted-gantry (sheared) scans show exact resliced stacks. Their planes are straightened for
display only, and your marks still map to the original voxels.

### 3D view

**Layers** opens the **Rendering stack**: **Segmentation**, **CT volume** and **CT plane**,
together or separately. Drag to rotate, right drag to pan and scroll to zoom. **Expand ↗** fills
the screen, and **Restore ↙** or **Escape** returns. With **CT volume** visible, surfaces are
solid so that depth stays correct; turn it off to use **All segment opacity**. **CT plane** shows
one native slice: choose **Native i**, **j** or **k**, move its slider, or use **Face plane**.

CT renders with Cornerstone3D. Tilted-gantry scans, MRI and the
[portable HTML](#portable-offline-html) page use Three.js automatically.
Rendering shares a browser memory budget of up to 3 GiB. If a view does not fit, close CT review
or use **Show selected only** to load fewer surfaces.

### CT volume settings

- **Appearance** picks a preset, adjusted by **Intensity** and **Opacity**. **Crop volume** limits
  the native i, j and k ranges.
- **Mask to segmentation** shows the CT only in chosen segments. Choose them with **Use
  selected**, **Use visible** or the list, then set **Show volume** to inside or outside them.
  **Display margin** grows the region to retain thin walls.
- **Color segments** (Cornerstone3D) tints the CT inside chosen segments with their anatomy-list
  colors, blended by **Color strength**. Change a color under **Selected structure**.
- **Lighting and shadows** (Cornerstone3D) sets the **Rendering method**: **Preset lighting**,
  **Standard**, **Cinematic (VolView Hybrid)** or **Ambient occlusion**. **Reset lighting**
  returns to the preset.
- **Cinematic heart** is a cardiac appearance. **Measure from segments** sets its reference HU
  from myocardium and aorta or left-heart masks, or enter **Myocardium** and **Blood pool** and
  choose **Apply reference HU**.

These settings change the display only, never source images, masks or measurements.

## Review and correct masks

Select anatomy in the list or on the model and inspect its overlay in all three planes. Read any
QC messages under **Selected structure** against the CT. Then choose **Review segmentation ↗** and
pick the target under **Structure**. **Exit review** leaves.

Review opens on the views you left on the main page: the same tiles and split, each slice at the
same zoom and centre, and the 3D camera where it was. Your window and overlays carry over too.
Marking needs a few changes while you review: the **CT volume** layer turns off, **Segmentation**
and the **CT plane** show in the 3D view, and slab projection and rotatable crosshairs pause.
**Exit review** puts all of this back.

During review the planes lock to the native source slices, so drawings keep their true geometry
on oblique scans. Marks autosave into a draft that survives reloads and restarts, and nothing
changes the accepted masks until you save. Mark only a settled, full-resolution view. **↶ Undo**
and **↷ Redo** step through your marks without running a model.

### Direct edit

**Direct edit** is the default. It is exact, runs on the CPU and needs no AI model.

- **Paint** and **Erase:** set **Radius** in millimetres and draw on a native slice. Only the
  voxel centres you brush on that slice change, in stroke order.
- **Scissors:** a cut drawn on the 3D view. Choose a **Shape** (**Freehand**, **Rectangle** or
  **Circle**) and a **Cut** (**Erase inside** or **Erase outside**). It goes through the full depth
  of the selected segment from the current camera angle. Right drag rotates, and **Escape**
  cancels a cut in progress.

Add as many strokes as you like, then choose **Apply edits · N**.

### AI assist

**AI assist** uses nnInteractive for larger corrections and can change voxels beyond your marks.
It needs the [model installed](INSTALL.md#ai-models), whose pretrained checkpoint is licensed for
noncommercial use only. Inference stays on your computer and never trains the model.

1. Choose **+ Include** or **− Exclude**.
2. Mark with **Point**, **Hint brush**, **Lasso** or **Box**. The box is 2D, on one native slice.
3. Choose **Run AI · N** once for all pending marks.

A prediction can take a minute or more, even on a GPU. Nearby marks share a model region, and
distant marks keep separate regions so that none are omitted. Batches and settings are recorded
for exact replay and undo.

**Options +** holds **Native slice planes**, required for hint brush, lasso and box;
**Automatically focus inference**, on by default; **Start from an empty mask**, which proposes
afresh without changing the saved mask; and **Refine whole mask**, a slower extra model stage.

Save or discard your draft before switching between **Direct edit** and **AI assist**.

### Choose a GPU for AI assist

With [a second GPU set up](INSTALL.md#add-a-second-gpu), **Options +** adds **Run on**, with the
same [status lines and **Check again**](#choose-a-gpu) as segmentation, and the toolbar shows
**AI ·** and the GPU that **Run AI** uses. With one GPU neither appears. **Direct edit** needs no
GPU, so it shows neither.

Until the first AI result you can choose freely. That result keeps the draft on its GPU, so each
step builds on the last and all of a saved correction comes from one GPU. The summary beside
**Exit review** reads **AI result from** that GPU, and **Revision history** names it once you
save. A first run that fails or is cancelled leaves the draft free. **↶ Undo** back past the first
result frees it, unless that result came from **Refine whole mask** with no marks, and changing
**Automatically focus inference** or **Start from an empty mask** always frees it. Both drop the
current result.

After that, **Run AI** and **Refine whole mask** stay on the draft's GPU, whatever the list shows,
and **Run on** becomes **Re-run draft on**. To move the draft, choose a GPU there, then choose
**Re-run on this GPU**. A re-run:

- repeats every Apply step from the original mask, with any pending marks as a last step
- replaces the current result only when it finishes
- can change regions where you made no marks, so inspect the new result before saving

While it runs, the toolbar names the GPU doing the work. If the re-run fails or you cancel it, the
toolbar goes back to the draft's GPU, and the old result stays visible as the previous result but
cannot be saved. Choose **Re-run on this GPU** again, now on any available GPU including the one
that made that result, or choose **Discard draft**. Back on the GPU that made the result, 3DScan
reuses any steps still held in memory instead of repeating them. Your marks are never lost.

**If a GPU cannot be used**, a message beside **Exit review** gives the reason, with **Check
again** to read the list again once it is fixed. **Run AI** for marks and **Refine whole mask**
stop until then. Whatever needs no GPU keeps working: marking, **↶ Undo**, a **Direct edit**
draft, applying a relationship change and saving a current result. A draft with no result can
choose another GPU under **Run on**. One with a result can re-run on another GPU or be discarded.
A run that is refused or fails reads the list by itself, so this message can arrive unasked.

**If a GPU has been removed**, **Run on** moves to the first available GPU and says so. A draft
with results from that GPU says it is no longer set up, and it keeps **Re-run draft on** and
**Re-run on this GPU** even with one GPU left, so it always has a way out. If the GPU comes back,
**Check again** clears the message.

**With a draft open in two tabs**, a refused **Run AI** brings this page up to date with the other
tab's result or job. If the other tab has changed the draft, the page offers **Reload saved work**
and saves nothing over it. A mark that could not be saved while the other tab's job ran is tried
again when that job ends, and its message stays until then.

### Add a missing structure

Choose **+ Add segment**, enter a **Name**, **Group** and **Color**, choose a relationship, then
**Create empty segment**. Mark its extent on the CT, apply and inspect, and choose **Save new
segment**. This adds one structure in a new revision.

### Mark in the 3D view

While the 3D view is visible during review, **Mark on** selects **Surface point**, which locks
picking to the target, or **CT plane**, which supports all the drawing tools on a native slice.
Right drag rotates while marking, and a 3D point moves the linked CT crosshair so that you can
confirm it.

**Mark on** and the other 3D controls are under **Review tools**, which starts folded in each
review so that nothing covers the view you mark on. Switching from surface marking to a drawing
tool moves to the CT plane and faces it; **Face plane** does the same at any time, including when
review has just opened on your own camera angle. While **Review tools** is open a mark cannot
start underneath it, so fold it again to reach that part of the view.

### Toolbar messages and panels

The review toolbar keeps one height, so the viewers stay where they are while you work.

Errors, warnings, the result of **Apply edits** or **Run AI** and the progress of the 3D preview
appear beside **Exit review**. They never cover a viewer. When there are more than two lines,
scroll that area; errors come first, with any button you need, such as **Reload saved work** or
**Retry preview**.

**+ Add segment**, **Relationship**, **Impact preview** and **Options +** open over the viewers
only when you choose them. A mark cannot start underneath an open panel, so choose the same
button again to fold it.

### Compare and save

Applying creates a candidate. It does not change accepted masks.

1. Use **Original** and **Candidate** to compare the CT mask and the 3D surface.
2. Read the summary of voxels changed and volume before and after, beside **Exit review**.
3. If other segments are affected, open **Impact preview** and choose **Show affected in CT**.
4. Add more marks and apply again as needed. Saving waits until pending marks are applied.
5. Choose **Save correction** only after inspecting the result.

Saving updates that structure and any explicitly selected siblings, with their measurements and
surfaces, in a new revision. **Discard draft** closes a proposal instead. A candidate must change
masks or relationship metadata. Direct erasing can leave an existing segment empty at 0 mL, but
empty new segments and empty AI candidates cannot be saved.

### Handle overlapping segments

**Relationship** sets how a segment treats overlaps:

- **Independent** preserves them.
- **Subregion of** records a parent, and overlap with it is preserved.
- **Subdivision alongside** moves overlapping voxels to the target, only from siblings you select
  under **Replace overlaps only in:**.

### Undo and revisions

Every revision records its parent, its operation and the voxel and volume changes. In **Revision
history**, **↶ Undo to parent** restores the previous revision exactly, and **Activate** switches
to any other. Nothing is deleted. **Current** and **Before** in the 3D header compare surfaces with
the parent, and **Compare CT overlay with parent** does the same on the slices.

To record your review, open **Review status & tools** and set **Revision status** to
**Unreviewed**, **Reviewed** or **Corrected**. It applies to the whole revision.

### Other correction routes

The **Refinement** tab offers **Keep component at seed**, **Remove component at seed**, **Keep
inside region** and **Erase inside region**. Click a slice or surface to place a seed, or enter
coordinates or region bounds in patient RAS millimetres, then choose **Create refinement
revision →**.

For external editing, expand **External correction with Slicer** under **Review status & tools**.
**Slicer bundle ↓** downloads the reference CT, a `.seg.nrrd` and a mapping file. Edit in 3D Slicer,
keeping the segment tags, then choose **Import correction ↑** with the self-contained `.seg.nrrd`.
The import checks segment identities, the source CT hash and geometry, then creates a revision.

## Measurements and model agreement

Volumes are in millilitres and are counted from mask voxels. Display surfaces never feed
measurements. HU is read from the CT at the linked position.

**Model agreement**, at the top of **Revision history**, shows how far accepted masks have moved
from the model. Each structure is compared with its saved TotalSegmentator output, traced through
the revisions. A later run that replaced a structure becomes its new original. **Details** opens
the table:

| Column         | Meaning                                                                  |
| -------------- | ------------------------------------------------------------------------ |
| **Dice**       | Voxel overlap. 1 is identical.                                           |
| **HD95**       | Larger 95th-percentile surface distance in mm, using the full affine.    |
| **ΔV**         | Accepted volume minus original volume.                                   |
| **Voxels +/−** | Voxels added and removed.                                                |

**All cases ↗** pools the active revision of each case marked **Reviewed** or **Corrected**, by
task and structure: how often the model was accepted unchanged, Dice and mean absolute ΔV.
Synthetic demo cases form a separate pool.

These numbers measure reviewer edits, not ground truth, and an uncorrected mask can still be
wrong. Review status covers a whole revision, so a structure nobody inspected counts as accepted.
HD95 ignores the farthest 5% of the surface, so a small local fix can show 0 mm while Dice is
below 1. Segments added by a reviewer have no model original and are not compared.

## Export

The HTML and DICOM SEG exports read the saved revision: first save or discard any draft, return to
the current revision and turn off comparison. Exports can contain patient images and metadata, so
treat them like the study itself.

### PNG

**PNG ↗** in the 3D view saves the current view, including any review marks.

### Portable offline HTML

1. Choose **Export viewer ↗**.
2. Under **Include layers**, choose **Segmentation**, **CT volume** and **CT plane**.
3. Choose **Download HTML**.

The one file opens offline in a desktop browser, keeping the camera, appearances, visibility, crop,
mask and plane position of the selected layers. It renders with Three.js, so the image can differ
from the app's, and native lighting and **Color segments** are not exported. Unselected data is
left out. The file must stay
under 512 MiB; if the CT is too large, export the segmentation alone. Editing and three-plane CT
review stay in the app.

### Slicer bundle

**Slicer bundle ↓**, described under [other correction routes](#other-correction-routes), also
serves as an overlap-preserving export for 3D Slicer.

### DICOM SEG

**DICOM SEG…** needs a configured Orthanc, and the CT must have been imported from that archive.
Select the masks, then choose **Archive in Orthanc**, or pick a **Send destination** and choose
**Send to** it. All masks go into one SEG object with overlaps preserved. Reopening the dialog
never sends anything, and **Send again** deliberately resends the same instances. See
[send a saved segmentation back](ORTHANC.md#send-a-saved-segmentation-back).

To give each voxel to one structure, tick **Resolve overlaps in this export** and rank the masks
with the arrows. Where masks overlap, the higher mask keeps those voxels. The smallest masks start
at the top, because they would lose the largest share. Only this SEG changes: saved masks,
measurements and review status stay as they are, and the series is labelled `overlaps resolved`.
The result reports how many voxels were removed. If a mask would lose every voxel, the export stops
and names its rank, so nothing is dropped silently. Each ranking is a separate export, and sending
one does not count as sending another. After an interrupted ranked export, reopening the dialog
restores its masks and ranking, so a retry reuses the same SEG.

## Import existing DICOM SEG masks

**Import DICOM SEG…** adds masks made elsewhere to an open CT, without any AI. It works for CT from
a folder, a ZIP or Orthanc, and needs no Orthanc connection.

1. Open the original CT on its current revision, with no draft open.
2. Choose **Import DICOM SEG…** and select up to 64 SEG files, 512 MiB in total. Select the SEG
   objects themselves, not a ZIP or the source images.
3. Choose **Import masks**.

A CPU job checks the source references and exact voxel alignment, then adds independent segments in
a new revision. Existing masks and overlaps stay intact, and **↶ Undo to parent** restores the prior
revision.

Only binary, uncompressed, little-endian SEG on the CT's native grid is accepted. Fractional,
labelmap, concatenated, compressed and resampled SEG is refused, as is any SEG that references a
different source. SEG stored in Orthanc is not attached automatically: select the file here.

## Back up the case library

```bash
.venv/bin/scan3d backup /path/on/another/disk/scan3d-backups
```

Each run writes `scan3d-<UTC time>/` with a consistent database snapshot, the case folders, the
surface cache and a `manifest.json`. It only reads live data, so the server can keep running. Later
runs hard-link unchanged files and copy only new data. Caches that rebuild on demand are skipped.

To restore, stop the server and copy a snapshot's contents back into the same data directory, since
records store absolute paths. A backup holds patient images, so protect it like the library. It does
not cover anything held in Orthanc.

## Experimental MRI

MRI is off by default. Set `SCAN3D_MODALITIES=CT,MR` and restart to offer static 3D MR series. Labels
change to **Open MR →** and **Segment MR…**, and `total_mr` is the default task. MR signal values are
not HU, so review each sequence separately. MR uses the Three.js renderer, and DICOM SEG import and
export are CT only.

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

`segment --inference-path ID` runs on [an added GPU](#choose-a-gpu), and `scan3d doctor` lists the
IDs. `include-all` restores labels that were filtered out earlier, from saved model outputs, as a new
revision that keeps corrections. `optimize` prepares viewer assets for an older case without
changing its masks or revisions. A `refine` recipe is JSON in patient RAS millimetres:

```json
{
  "type": "erase_region",
  "bounds_ras": [
    [-80, -60, -40],
    [-40, 20, 30]
  ]
}
```

The operations are `keep_region` and `erase_region` with `bounds_ras`, and `keep_component` and
`remove_component` with `seed_ras: [R, A, S]`. Operations with no effect, or that delete a whole
structure, are rejected.

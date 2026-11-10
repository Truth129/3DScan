# User guide

3DScan runs on your own computer. Import a CT study, open one series, segment it with
TotalSegmentator, correct the masks against the source images and export. Every saved change is a
new revision, so earlier work is never overwritten.

> **Research use only · not for diagnostic use.** The bundled models are research tools and
> 3DScan has no clinical validation. Real cases and portable exports carry this label. Always
> verify anatomy on the source images.

Start 3DScan with `.venv/bin/scan3d serve` and open **http://127.0.0.1:9010**. See
[installation](INSTALL.md) and [configuration](CONFIGURATION.md). **Explore synthetic demo**, on an
empty Library and in the import dialog, opens a phantom with ready-made masks and no patient data.

## Sign in

Sign-in is off on the default `127.0.0.1` address. On any other address, or with
`SCAN3D_AUTH=password`, everyone needs an account: see [configuration](CONFIGURATION.md#sign-in).
Over a network, use the HTTPS address.

**First account.** Until an account exists, the app shows this command. Run it on the workstation:

```bash
.venv/bin/scan3d users add NAME --admin
```

It asks for a password of 10 to 1,024 characters. `--display-name` sets the name in the header.

**Setting your password.** Open the link an admin sent you, type a password twice and choose **Set
password**. That signs you in, in place of anyone signed in on this browser. The link works once,
within a day, and the browser's history keeps it until then, so use it promptly. If it has expired
or was used, ask for a new one.

**Signing in.** Everyone signed in sees and works on the same cases. A session lasts 30 days from
its last use. If it ends while you work, sign in again as the same person to carry on with your
marks intact. Signing in as someone else reloads the page.

**Shared computer.** Tick this on a computer other people use. You are signed out when the browser
closes, or after 12 hours at most, and the browser stores no scan data for the session.

Choose your name in the header for **Sign out**, or **Sign out other devices** to end this account's
other sessions. For a forgotten password, ask an admin for a reset link; an admin with nobody to ask
uses `scan3d users passwd` on the [command line](#command-line).

Anyone with a shell on the machine can still read every file, so protect the machine and the data
directory as well.

### People

An admin adds people on the Library's **People** tab. Enter a username, an optional display name
and a role, choose **Add and make link**, then **Copy link** and send it. Make links through the
HTTPS address, because a link made on `127.0.0.1` works only on that computer.

A member works on every case but has no People or Storage tab, cannot delete forever and cannot
change the TotalSegmentator key. Changes on a person's row apply from their next request:

| Control                                            | Effect                                                                                            |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| **Make member** or **Make administrator**          | Changes the role.                                                                                 |
| **Turn archive off**                               | Stops Orthanc, DICOMweb and direct DICOM sends for the person. Files still download.              |
| **Reset link** (**New invite link** for an invite) | Makes a new link and ends any earlier one. For yourself, it changes your own password.            |
| **Sign out everywhere**                            | Ends the person's sessions.                                                                       |
| **Disable**                                        | Also ends their links and stops them signing in until **Enable**. Jobs they started still finish. |

Nobody changes their own role or disables themselves, and the last admin who can sign in cannot be
made a member or disabled. The links an admin made stop working when that admin is disabled, made a
member or given a new password.

## Library

The Library lists every case, the ones you opened last first, with **Added by** on cases someone
else added. Cases made on the command line or with sign-in off name the oldest admin. Choose the
case name in the header to open the Library; **Esc** or **×** returns to your case.

- **Search cases:** every word must appear in the case name, who added it, a series description or
  modality, or the status. **Enter** opens the first match.
- **Rename**, then **Save**. Do not put patient identifiers in a case name. A case still importing
  can be renamed once the import finishes.
- **Move to Trash** deletes nothing and asks for no confirmation. It saves marks in progress first,
  and the message says if any could not be. **Undo** shows for 10 seconds and reopens the case if
  it was open and you have not opened another; after that, anyone can choose **Restore** on the
  shared **Trash** tab.

A case in Trash keeps its disk space. Its jobs stop and do not restart, but a send already started,
through Orthanc or to a DICOM node, finishes. Importing it from Orthanc again meanwhile makes a new
case.

**Deleting forever.** Only an admin can: **Delete forever** on a row of the **Trash** tab, or
**Empty Trash** for every case in Trash, whatever the search shows. With sign-in on, it asks for
your password. It cannot be undone, and it is an ordinary file delete, not secure erasure.

The dialog lists the copies that stay, such as studies in Orthanc, backups and downloads. A case
whose job or send to Orthanc is still finishing is not deleted yet; choose **Try again** later.
Nothing leaves Trash on its own, so the **Cases** tab tells an admin about cases there for over 30
days.

**Storage.** Each row shows its case's size, leaving out caches that several cases share. An
admin's **Storage** tab adds the totals, the free space, and every case with who added it, its date
and its size.

**Clear caches**, on that tab, empties the caches that rebuild when next needed; saved surfaces and
cases stay. It is refused while jobs run, images load, a backup runs or another 3DScan command runs,
and the tab says what to wait for.

If a case you have open is moved to Trash elsewhere, the page says so, points you to Trash and
returns to the Library, within a minute or when you return to the tab. If a case will not open, the
page says why and returns to the case you had open.

## Working on the same case

Two people, or two tabs, can work on one case at once, and no saved version or draft is replaced
unseen. A job or draft someone else started shows **started by** and their name.

When your page is not running its own work and someone else or another tab saves a newer version,
the header says so within a minute, or when you return to the tab: choose **Show the newer
version**. Until then a refine, a correction, opening a series or **Activate** is refused, and a
correction made on the old version cannot be saved.

If another person or tab changed or ran a draft you have open, your page offers **Reload saved
work** and saves nothing over theirs. If they started the draft first, a mark made here is not
saved: **Reload saved work** or **Discard draft** opens their draft, so place the mark again there.

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

The series is copied locally and the archive stays unchanged. See
[Import a series](ORTHANC.md#import-a-series) for which series cannot be imported yet and what to
confirm.

**DICOMweb archive.** A [configured](ARCHIVES.md) archive has a tab with its name and works the
same way. Searches match exactly unless the archive allows `*` and `?`. A newly listed series shows
**Import opens in N s** until its image list stays unchanged that long, then the list refreshes.

On that tab, **Import again** copies a series once more as a new case. Use it if the archive may
have replaced its files.

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

For a case from an archive, **Add series from Orthanc** or **Add series from DICOM archive**, under
the series list, adds another series of that study. Each series keeps its own segments.

## Run TotalSegmentator

1. Choose **Segment CT…**.
2. Pick a **Task**. **Find a task or structure** searches tasks and anatomy, and **Included
   structures** lists the outputs.
3. Set any options the task supports: **Robust crop** (`-rc`), **Body crop** (`-bs`), **Remove
   small blobs** (`-rmb`, components below 200 mm³) and **Higher-order resampling** (`-ho`, smoother
   edges on scans finer than the model's spacing, with more time and memory).
4. Choose **Run segmentation**.

Each run creates a revision that replaces that task's results **and** any segments with matching
anatomical names, including manual corrections. Matching ignores capitalization, underscores and
whitespace. Other segments are kept, and the earlier revision stays in **Revision history**.

A task's weights download on its first run. AI jobs run one at a time across all GPUs, on the device
set by `SCAN3D_DEVICE` unless you [choose another GPU](#choose-a-gpu), and a CPU is much slower.
Progress shows at the top of the window, with **Cancel job**. Imports, opening a CT, direct edits
and saves run on a separate queue, so they do not wait behind a segmentation.

### Compare a second model

**Whole body — MRSegmentator** is a separate model for CT and MRI, with 40 structures. Install it
once with `scripts/download_segmentation_model.py mrsegmentator`. It runs alone, not with other
tasks, without the crop or small-blob options, and always with all five folds. The job shows which
fold it is on.

Its segments sit in their own **MRSegmentator** group, named like **Liver (MRSegmentator)**. A run
of either model replaces only that model's earlier segments, so the two sets stay side by side.

**Coronary arteries — ImageCAS-X nnU-Net** is a CT model for the coronary lumen on gated coronary
CT angiography, from the ImageCAS-X benchmark. Install it with
`scripts/download_segmentation_model.py imagecasx_coronary`. Its segment is **Coronary arteries
(ImageCAS-X nnU-Net)**, beside TotalSegmentator's. It was trained at one centre on scans without
artefacts, and it works at 0.35 mm, so a large non-cardiac CT needs a lot of memory and time.
As in the benchmark, pieces of its mask smaller than 100 voxels are removed.

**MS lesions — FLAMeS** marks multiple sclerosis white-matter lesions on a FLAIR series. Install it
with `scripts/download_segmentation_model.py flames_ms_lesions`, which also installs HD-BET. HD-BET
removes the skull first, because FLAMeS was trained on images without one; **Brain — HD-BET** shows
that brain mask on its own. The segment is **White matter lesions (FLAMeS)**. It was trained on 668
scans and often misses lesions under about 10 mm³.

**Breasts — BreastDivider** marks the left and right breast on MRI of the breasts in any common
sequence: T1 with or without contrast, T2 or DWI. Install it with
`scripts/download_segmentation_model.py breastdivider`. Its segments are **Left breast
(BreastDivider)** and **Right breast (BreastDivider)**, by the patient's side. It was trained on
13,752 public scans, but its accuracy has been measured only against labels it helped make.

**Breast tumour — MAMA-MIA** marks the primary breast tumour on one post-contrast phase of a DCE
series; the authors used the first. Install it as [Install](INSTALL.md#ai-models) describes: the
weights need a free Synapse sign-in. The segment is **Breast tumour (MAMA-MIA nnU-Net)**. It was
trained on 1,506 cases and scored Dice 0.69 on 516 exams from three unseen centres. In a fifth of
its own test cases it also marked tissue well away from the tumour, so check what it outlines.

### TotalSegmentator license

Tasks that need a license show **· license required**. Under **Segment CT…**, expand
**TotalSegmentator license**, enter your key (it begins with `aca_`) and choose **Validate and
save**. If validation fails, the previous key stays. Only an admin saves or removes the key; a
member sees its status.

The key is checked with TotalSegmentator's service and stored for all cases in `config.json` under
`TOTALSEG_HOME_DIR`, never in case records. **Remove key** deletes it, and existing segmentations
stay. License changes wait for queued and running jobs.

**Validated when saved** does not promise access to every task. See the
[provider's license page](https://backend.totalsegmentator.com/license-academic/).

### Choose a GPU

With [a second GPU set up](INSTALL.md#add-a-second-gpu), **Run on** appears after **Task**. With
one GPU, skip this section.

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

- **Navigate:** click to move, scroll or the slider to change slice, left or right drag for
  window/level, middle drag to pan, Ctrl or ⌘ scroll (or pinch) to zoom. Scrolling down moves
  one slice towards the feet, back or patient's left. While drawing, left drag draws and right
  drag still sets window/level. **Reset planes** restores them.
- **Window:** **Soft tissue**, **Lung**, **Bone** or **Vessels**, or type **W** and **L**.
- **Projection:** **MPR**, **MIP** or **MinIP**, with **Slab mm**. A slab shows overlays on its
  centre slice and pauses drawing. **Rotatable crosshairs** adds crosshairs you can drag and
  rotate by their lines.
- **Overlays:** **Segments** chooses what shows, **Use selected segment** follows the selection and
  **Clear overlays** shows CT alone. **Fill**, **Outline** and **2D overlay opacity** set the style.
- **Colors:** bones are yellow. Change any color under **Selected structure**; your browser keeps
  it.

Tilted-gantry (sheared) scans are straightened for display only. Your marks still map to the
original voxels.

### 3D view

**Layers** opens the **Rendering stack**: **Segmentation**, **CT volume** and **CT plane**. Drag to
rotate, right drag to pan, scroll to zoom. **Expand ↗** fills the screen, and **Restore ↙** or
**Escape** returns.

- **All segment opacity** works with **CT volume** visible: CT in front of a segment covers it,
  and CT behind shows through. If the browser lacks a feature this needs or GPU memory runs short,
  segments stay solid and the **Rendering stack** says why. If the GPU cannot compile the
  see-through shader, the 3D view fails instead.
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
  colors, blended by **Color strength**.
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

Review opens on your main-page views, and slab projection and rotatable crosshairs pause until
**Exit review**. **CT volume** starts as you last left it in review, off at first, and **Exit
review** restores the main page's choice. MRI and tilted-gantry CT keep it off.

- Planes lock to the native source slices, shown the same way up as on the main page.
- Marks autosave into a draft that survives reloads and restarts. Accepted masks change only when
  you save.
- Mark only a settled, full-resolution view.
- **↶ Undo** and **↷ Redo** step through your marks. Undo back to an applied step shows its result
  again, with no new run.
- A drawing that covers no voxel centre is not saved. It disappears, and a message names it.

### Direct edit

**Direct edit** is the default. It is exact, runs on the CPU and needs no AI model.

- **Paint** and **Erase:** set **Radius** in millimetres and draw on a native slice. Only the voxel
  centres you brush change, in stroke order.
- **Scissors:** draw a cut on the 3D view. Choose a **Shape** (**Freehand**, **Rectangle** or
  **Circle**) and a **Cut** (**Erase inside** or **Erase outside**). It cuts the full depth of the
  selected segment from the current camera angle. Right drag rotates, and **Escape** cancels.

Then choose **Apply edits · N**.

### AI assist

**AI assist** uses nnInteractive for larger corrections. It needs the
[model installed](INSTALL.md#ai-models), whose pretrained checkpoint is licensed for noncommercial
use only. Inference stays on your computer and never trains the model.

It can change voxels far beyond your marks: one mark can remove other separate parts of the
segment or loops of a folded tube, and an Exclude on a large leak can remove much of the organ.
**Include only adds**, under **Options +**, stops an Include-only run removing anything.

1. Choose **+ Include** or **− Exclude**.
2. Mark with **Point**, **Hint brush**, **Lasso** or **Box** (2D, on one native slice).
3. Choose **Run AI · N** once for all pending marks. This can take a minute or more, even on a GPU.

- Mark every separate part, or points along the whole of a long tube, in one **Run AI**.
- Draw a long, thin structure as several short **Hint brush** strokes, not one long one.
- Prefer **Lasso** to **Box** on a large or irregular structure, where a box spreads into its
  corners and neighbouring organs.
- A mark after **↶ Undo** continues from the last whole **Run AI** before it, and reruns marks of a
  run the Undo went inside, which can move voxels you already checked.

A warning beside **Exit review** says when a result removes 5% or more of the mask's interior, the
part over 2 mm inside, more than 40 mm from every mark. The volume line can hide such a loss, so
check the whole structure on the CT and in 3D before saving.

**Options +** holds:

- **Native slice planes**, required for hint brush, lasso and box
- **Automatically focus inference**, on by default
- **Include only adds**, off until you turn it on, and remembered in this browser. A **Run AI** with
  only Include marks then never removes voxels, and one with only Exclude marks never adds. It cannot
  tidy the boundary near an Include. Mixed runs and **Refine whole mask** are not guarded.
- **Start from an empty mask**, which leaves the saved mask unchanged
- **Refine whole mask**, a slower extra model stage. The marks made before it keep guiding later
  runs. If it fails or is cancelled with no marks waiting, the result before it stays and can be
  saved.

Save or discard your draft before switching between **Direct edit** and **AI assist**.

### Choose a GPU for AI assist

With [a second GPU set up](INSTALL.md#add-a-second-gpu), **Options +** adds **Run on**, with the
same [status lines and **Check again**](#choose-a-gpu), and the toolbar shows **AI ·** and the GPU
in use. Neither appears with one GPU or in **Direct edit**.

The first AI result holds the draft on its GPU, named by **AI result from** beside **Exit review**
and, once you save, in **Revision history**. These free the draft and drop the current result:

- **↶ Undo** past the first result, unless it came from **Refine whole mask** with no marks
- changing **Automatically focus inference** or **Start from an empty mask**

A held draft shows **Re-run draft on** instead of **Run on**. To move it, choose a GPU there, then
**Re-run on this GPU**. A re-run repeats every Apply step from the original mask, with any pending
marks last, and replaces the current result only when it finishes.

A re-run can change regions you did not mark: a warning beside **Exit review** counts any voxels of
the result you saw that moved. If it fails or is cancelled, the old result stays visible but cannot
be saved; re-run again or choose **Discard draft**. Your marks are never lost.

**If a GPU cannot be used**, a message beside **Exit review** says why, with **Check again**, and
**Run AI** and **Refine whole mask** stop. Choose another GPU, or re-run or discard a draft that has
a result. Work that needs no GPU continues.

**If a GPU has been removed**, **Run on** moves to the first available GPU and says so. A draft
from the removed GPU keeps **Re-run on this GPU**, even with one GPU left. If the GPU comes back,
**Check again** clears the message.

### Add a missing structure

Choose **+ Add segment**, enter a **Name**, **Group** and **Color**, choose a relationship, then
**Create empty segment**. Mark its extent on the CT, apply, inspect and choose **Save new segment**.

A scan with no segmentation needs no model run first: choose **+ Add segment ↗** in the toolbar or
**+ Add segment by hand** in the anatomy panel. Review opens on that form, in **Direct edit**. Saving
makes the scan's first segmentation revision.

### Mark in the 3D view

**Mark on**, under **Review tools**, selects **Surface point**, which locks picking to the target,
**CT plane**, which takes all the drawing tools on a native slice, or, with **CT volume** on,
**Volume point**. Right drag rotates while marking, and a 3D point moves the linked CT crosshair so
you can confirm it. **Face plane** faces the CT plane at any time.

With **CT volume** on in review:

- **Volume point** marks the voxel centre nearest where your click first meets half-opaque volume.
  In AI assist, **Point** adds it as an Include or Exclude; other tools move the crosshair there.
- A click on no visible volume, or on an opaque surface or the CT plane in front of it, makes no
  mark; see-through surfaces do not block it. **Surface point** refuses a surface the volume covers.
- While you mark on the **CT plane**, the volume on your side of it is cut away. **Focus** keeps the
  whole CT in depth.
- **Mask to segmentation** masks by the saved segmentation, not your draft, so your edits do not
  change it until you save. **Volume point** sees only the CT the mask shows.
- **Color segments** and **Cinematic heart** stay off. A Cinematic heart volume shows the default
  preset, and returns on **Exit review** unless you choose another.

**Review tools** starts folded. A mark cannot start underneath it, so fold it to reach that part of
the view.

### Toolbar messages and panels

Errors, warnings, results and 3D preview progress appear beside **Exit review**, errors first, in an
area that scrolls past two lines. Some errors carry a button, such as **Retry preview**.

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

Once a segmentation is active, **Reset to scan** activates the image-only revision, with no
segmentation. Nothing is deleted, an open draft stays with its version, and the case leaves pooled
agreement until a segmentation is active again. The demo does not offer it.

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
   identities, the source CT hash and geometry, then creates a revision. Segments you did not
   change keep their saved masks, and so keep their provenance.

## Measurements and model agreement

Volumes are in millilitres, counted from mask voxels.

**Model agreement**, at the top of **Revision history**, compares accepted masks with each
structure's saved model output. A later run that replaced a structure becomes its new original.
**Details** opens the table:

| Column         | Meaning                                                                                                                                                      |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Change**     | **AI assist\*** if any save since the original used AI assist, **Direct edit** if every save was one, else **Other**, such as a refinement or Slicer import. |
| **Dice**       | Voxel overlap. 1 is identical.                                                                                                                               |
| **HD95**       | Larger 95th-percentile surface distance in mm.                                                                                                               |
| **ΔV**         | Accepted volume minus original volume.                                                                                                                       |
| **Voxels +/−** | Voxels added and removed.                                                                                                                                    |

**All cases ↗** pools the active revision of each case marked **Reviewed** or **Corrected**, by
task and structure: how often the model was accepted unchanged, how many were AI-assisted saves,
Dice and mean absolute ΔV. Synthetic demo cases form a separate pool.

Both views also total changed masks by kind of save. A save that adds a segment counts for each
mask it took voxels from.

- These numbers measure reviewer edits, not ground truth. An uncorrected mask can still be wrong.
- \*An AI-assisted save includes the model's own changes away from your marks, so its figures
  overstate your edit.
- Review status covers a whole revision, so a structure nobody inspected counts as accepted.
- HD95 ignores the farthest 5% of the surface, so a small fix can show 0 mm while Dice is below 1.
- Segments added by a reviewer are not compared.

## Coronary CT-FFR

> **Research use only · not for diagnostic use.** CT-FFR here is an unvalidated research model.
> Check the centrelines and diameters on the source images before reading any FFR.

The **Coronary FFR** tab appears when the revision has **Coronary arteries** from ImageCAS-X or
TotalSegmentator. It traces centrelines in the lumen mask on its own grid, measures lumen area along
them and solves steady hyperaemic pressure.

Inputs:

- **Lumen**, **Aorta** and **Myocardium** come from the revision's segments.
- **LV mass** is required without a myocardium mask. With one, its volume × 1.05 g/mL is used.
- **SBP**, **DBP** and **heart rate** are optional. Missing blood pressure is taken as 120/80 and
  marked as assumed. Heart rate is recorded only.
- **Flow share** is the fraction of the heart's flow in the trees solved. With one tree found, FFR
  is not reported until you give it. It scales the flow into every tree solved, so clear it once
  both trees are found. The result flags a share applied to more than one tree.
- **Add ostium at crosshair** adds a tree root, up to four. Use it when the lumen does not touch the
  aorta mask, or without an aorta.

**Run CT-FFR** works in the background. The same inputs on unchanged masks return the earlier
result at once. Earlier results for the revision load when you open the tab.

The table lists each tree, its roots and branches, and each lesion. Select a row to move the
crosshair there.

| Column     | Meaning                                                                                                                                                                                      |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Vessel** | The branch or lesion. Below it, a branch's parent and length, or a lesion's length, its span from the ostium and its minimum lumen diameter (MLD).                                           |
| **%DS**    | Diameter stenosis against the fitted healthy reference. Lesions are 30% or more over 1 mm or longer.                                                                                         |
| **FFR**    | Branch rows: distal FFR, at the last station whose reference diameter is at least 1.8 mm. Lesion rows: FFR +20 mm, 20 mm beyond the lesion end, following the larger daughter at a junction. |
| **ΔFFR**   | FFR before the lesion minus FFR beyond it.                                                                                                                                                   |

FFR +20 mm is read nearer, with a note, if the vessel ends or FFR is not reported there. Stations
under 1.8 mm are solved but not reported. Notes under a row say why a value is missing. Every
input, assumption and model constant is listed under the table, with units.

In the native 3D view a centreline tube shows the result, sized to the lumen, which fades behind
it. Colour by **FFR**: red at 0.75 and below, amber at 0.80, blue from 0.90, grey where FFR is not
reported. Or by **Diameter**, dark to light blue from 1 to 5 mm. The legend sits at the lower left.
PNG and DICOM view exports leave the tube out and draw the lumen as set. The Three.js view (MR,
sheared grids) shows the table only.

Limits:

- Blunt or oblique vessel ends can still fork the centreline.
- Where two vessels touch, their areas can mix. Such branches carry a contact note.
- A lumen gap withholds FFR on the branch that ends at it, since the flow beyond is unknown. FFR
  above the gap is still shown, with a flag that it may read low.

## Export

The HTML, DICOM SEG and DICOM view exports read the saved revision: first save or discard any
draft, return to the current revision and turn off comparison. Exports can contain patient images
and metadata, so treat them like the study itself.

**DICOM ↗** and **Export SEG…** deliver to these places, depending on where the scan came from:

| Delivery         | Choose                                                                                              | Needs                                                                                   |
| ---------------- | --------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| File             | **Prepare file**, then **Download file** (**Prepare SEG file** and **Download SEG file** for a SEG) | A CT or classic MR prepared from DICOM, a folder or ZIP import included. No archive.    |
| Orthanc          | **Archive in Orthanc**, or a **Send destination** and **Send to** it                                | A CT or classic MR imported from a [configured Orthanc](ORTHANC.md).                    |
| DICOMweb archive | **Archive in** and the archive's name                                                               | A CT or classic MR imported from that [archive](ARCHIVES.md#archive-results).           |
| DICOM node       | A **Send destination** and **Send to** it                                                           | A scan not from Orthanc, and [configured destinations](ARCHIVES.md#direct-dicom-sends). |

Archiving and sending need archive access on your account; files do not. **Send again**
deliberately resends the same instances. A DICOM node that does not accept the object is sent
nothing. If the dialog says a send to a DICOM node may have arrived, check the node before you
retry, since a retry sends the same instances again.

### PNG

**PNG ↗** in the 3D view saves the current view, including any review marks.

### DICOM view

**DICOM ↗** in the 3D view saves the current view as a one-image series in the original patient
and study, delivered as in [Export](#export). For Orthanc, see
[send a 3D view back](ORTHANC.md#send-a-3d-view-back).

The image is a picture. It has no calibration and no orientation labels, so do not measure on it,
and nothing in it marks left from right.

- The button is disabled during review and for the same reasons as the other exports, and a capture
  is refused while the view is updating or showing an error.
- Exporting an unchanged view again delivers nothing new.
- After a failure, retry from the dialog, or if it has gone, check the study in Orthanc and capture
  again. If the dialog says the outcome is unknown, press the same button again.
- You can close the dialog during an export: the result appears as a notice or in the error banner.
  A file that finishes meanwhile waits for the next **DICOM ↗**, with **Download file**.

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

**Export SEG…** works for classic CT and classic single-frame MR from any import. Selected masks go
into one SEG object with overlaps preserved. All nonempty masks start selected, and **Select
visible** takes a snapshot of the masks switched on in the anatomy list.

Deliver it as in [Export](#export); a file can build with the dialog closed. For Orthanc, see
[send a saved segmentation back](ORTHANC.md#send-a-saved-segmentation-back). A SEG uses the colors
saved with the segmentation, not ones changed in your browser.

- **Cancel export** stops an export in progress, but closing the dialog does not. A SEG that has
  reached Orthanc stays there, and a send cannot be stopped once it has started.
- If the dialog says the CT has no verified original series, prepare its series again. The
  synthetic demo case has none.

To give each voxel to one structure, tick **Resolve overlaps in this export** and rank the masks
with the arrows. The higher mask keeps overlapping voxels.

- Only this SEG changes, and its series is labelled `overlaps resolved`. Saved masks, measurements
  and review status stay as they are.
- If a mask would lose every voxel, the export stops and names its rank.
- Each ranking is a separate export, and sending one does not count as sending another.

#### OsiriX ROIs

If the CT came from a configured Orthanc, **Format** also offers **OsiriX ROIs**: native OsiriX
brush ROIs, one per mask on each slice it touches. Choose it, select the masks, then choose
**Archive in Orthanc** or **Send to** your OsiriX destination.

Overlaps stay, so there is no ranking, and there is no file. Sending ROIs and sending a SEG of the
same masks count separately. The format is experimental, checked in OsiriX MD 14.3 on synthetic
scans only, so compare the ROIs with the masks before you rely on them.

## Import existing DICOM SEG masks

**Import DICOM SEG…** adds masks made elsewhere to an open classic CT or classic single-frame MR,
from any import. It needs no AI and no archive connection.

1. Open the original scan on its current revision, with no draft open.
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

Each run writes `scan3d-<UTC time>/` with a database snapshot, the case folders, the saved surfaces
and a `manifest.json`. The server can keep running.

To restore, stop the server and copy a snapshot's contents over the same data directory, since
records store absolute paths. If the snapshot has no `scan3d.sqlite3-wal`, first move any
`scan3d.sqlite3-wal` and `scan3d.sqlite3-shm` out of the data directory.

A backup holds patient images, so protect it like the library. It does not cover anything held in
Orthanc. It keeps cases in Trash, accounts and the audit log, but no sessions or set-password links,
so everyone signs in again after a restore. A case deleted forever stays in earlier backups, and
restoring one brings it back.

## Experimental MRI

MRI is off by default. Set `SCAN3D_MODALITIES=CT,MR` and restart to offer static 3D MR series. Labels
change to **Open MR →** and **Segment MR…**, and `total_mr` is the default task.

One phase of a dynamic scan imports as a static volume when it is its own series. A series that
holds several phases is refused.

MR signal values are not HU, so review each sequence separately. MR uses Three.js.

SEG import and export, OsiriX ROIs and **DICOM ↗** work for classic single-frame MR as for CT, and
refuse Enhanced MR. They have been checked on synthetic MR only, not yet in OsiriX or another viewer.

## Command line

These commands use the same library as the app. Job commands wait for the result; if the server is
running, it does the work. Commands ignore Trash, but a job on a case in Trash stops when it starts.

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
- `archive-check` checks the configured DICOMweb archive without changing it, and sends C-ECHO to
  each DICOM destination. See [ARCHIVES.md](ARCHIVES.md#check-an-archive).

A `refine` recipe is JSON in patient RAS millimetres:

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

Accounts can also be managed on the workstation, with the [People](#people) limits. A change
applies from the account's next request. `passwd` and `disable` also end the account's sessions and
links, including links it made, and `enable` brings back neither.

```bash
.venv/bin/scan3d users list                 # role, archive access and state
.venv/bin/scan3d users passwd NAME
.venv/bin/scan3d users role NAME member     # or admin
.venv/bin/scan3d users disable NAME
.venv/bin/scan3d users enable NAME
```

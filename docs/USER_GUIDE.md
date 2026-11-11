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

It asks for a password of 10 to 1,024 characters. `--display-name` sets the name in the top bar.

**Setting your password.** Open the link an admin sent you, type a password twice and choose **Set
password**. That signs you in, in place of anyone signed in on this browser. The link works once,
within a day, and the browser's history keeps it until then, so use it promptly. If it has expired
or was used, ask for a new one.

**Signing in.** Everyone signed in sees and works on the same cases. A session lasts 30 days from
its last use. If it ends while you work, sign in again as the same person to carry on with your
marks intact. Signing in as someone else reloads the page.

**Shared computer.** Tick this on a computer other people use. You are signed out when the browser
closes, or after 12 hours at most, and the browser stores no scan data for the session.

Choose your name in the top bar for **Sign out**, or **Sign out other devices** to end this account's
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
case name in the top bar to open the Library; **Esc** or **×** returns to your case.

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
the top bar says so within a minute, or when you return to the tab: choose **Show the newer
version**. Until then a refine, a correction, opening a series or **Activate** is refused, and a
correction made on the old version cannot be saved.

If another person or tab changed or ran a draft you have open, your page offers **Reload saved
work** and saves nothing over theirs. If they started the draft first, a mark made here is not
saved: **Reload saved work** or **Discard draft** opens their draft, so place the mark again there.

## Import a study

Choose **+ Import scan** in the top bar. Do not put patient identifiers in **Case label**.

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

These fields are labels only: they change no pixels, rendering or tasks. **Series…** in the top bar
opens another series and keeps your result, and **Source image** beside it switches between opened
images.

For a case from an archive, **Add series from Orthanc** or **Add series from DICOM archive**, under
the series list, adds another series of that study. Each series keeps its own segments.

## Run TotalSegmentator

1. Choose **Segment CT…**. Its settings open at the top of the side panel.
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
Progress shows in a chip at the top right, with **Cancel job**; choose the chip for the full status,
the stage and the measurements so far. Imports, opening a CT, direct edits and saves run on a separate queue, so
they do not wait behind a segmentation.

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

**Coronary arteries — ImageCAS-X CAS-Net** is the same benchmark's CAS-Net, which scored above its
nnU-Net with fewer stray pieces. Install it with `scripts/download_segmentation_model.py
imagecasx_cas_net`. Its segment is **Coronary arteries (ImageCAS-X CAS-Net)**. It runs as one
network at 0.5 mm, so a large non-cardiac CT still needs a lot of memory and time, and pieces smaller
than 100 voxels are removed in the same way. The job shows which of its four mirrored passes it is on.

**MS lesions — FLAMeS** marks multiple sclerosis white-matter lesions on a FLAIR series. Install it
with `scripts/download_segmentation_model.py flames_ms_lesions`, which also installs HD-BET. HD-BET
removes the skull first, because FLAMeS was trained on images without one; **Brain — HD-BET** shows
that brain mask on its own. The segment is **White matter lesions (FLAMeS)**. It was trained on 668
scans and often misses lesions under about 10 mm³.

**Spinal cord MS lesions — Spinal Cord Toolbox** marks multiple sclerosis lesions in the spinal
cord on an MRI series of the spine, axial or sagittal, such as T2, T2\*, STIR or PSIR. Install it
with `scripts/download_segmentation_model.py sct_cord_ms_lesions`, which also installs the sc-crop
cord detector. The detector finds the cord and the model runs on a box around it. The run fails and
says why if it finds no cord, if the slices are too narrow to search, or if it marks no lesion. The
segment is **Spinal cord lesions (Spinal Cord Toolbox)**. It was trained and tested on 4,428 images
from 1,849 people with MS at 23 centres, and it can miss small lesions. On an AMD GPU a run took
about 23 GB of GPU memory whatever the scan's size, and on the CPU about 7 GB of RAM.

**Spinal cord — Spinal Cord Toolbox** marks the spinal cord on an MRI series of the spine in any
common contrast, such as T2, T2\*, T1, STIR or PSIR. Install it with
`scripts/download_segmentation_model.py sct_spinal_cord`. Like the lesion model, it runs on a box
the sc-crop detector finds around the cord, and the run fails and says why if it finds no cord. It
keeps only the largest piece of the cord it marks. On a tilted series the box can cut the cord, and
cord outside it is not marked. The segment is **Spinal cord (Spinal Cord Toolbox)**. It was trained
and tested on about 3,450 scans from 1,631 people, healthy and with MS, spinal cord injury,
degenerative cervical myelopathy and other cord diseases, in many contrasts.

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

The top bar reads in the order of the work: the case and its review status, **Series…** and
**+ Import scan**, then **Segment CT…**, the **View** | **Review segmentation** switch and the
exports, then the layout, any job and your name. The worker shows at the bottom left.

Icon buttons name themselves on hover. An arrow after a link means it opens in a new tab.

- Below 1400 px wide, **Import DICOM SEG…** moves under the **▾** beside **Segment CT…** when that
  is offered, and **Export SEG…** and **Export viewer** under **Export ▾**. Below 1200 px, or
  below 1400 px while a job or newer-version chip shows, **Series…**, **Source image** and
  **+ Import scan** move under **⋯**.
- At 1000 px and narrower, every case action but **View** and review moves under **⋯**; the job
  chip and your name stay. The layout control is hidden, and **Anatomy** shows the side panel.
- While such a chip shows below 1500 px, only the layout **▾** stays; it has every arrangement.
- Disabled buttons give their reason on hover, and in a menu under the item.
- **Esc** closes a menu and returns focus to its button; a click elsewhere closes it too. **Esc**
  also closes the segmentation settings.
- In a menu, **↓** and **↑** move between its items and **Home** and **End** jump to the first and
  last. **↓** on a menu's button opens it, except your name's, which opens with **Enter**.
- A click on an image that closes a menu does only that: it never moves the crosshair or starts a
  mark.

The side panel on the left shows one tab at a time: **Segments**, **3D**, **Refine** and
**History**, with **FFR** last when the revision has a coronary lumen. At its widest, 480 px, the
panel spells them out, such as **Revision history**, unless **FFR** shows. Your browser remembers
the tab. The stage tiles the 3D view and **CT review**.

Review's tools and the segmentation settings open at the top of the side panel, above the tabs, and
change the viewers' size only to show a hidden side panel. They take at most about 60% of the
panel, and leave the Segments list room for a few rows.

- **‹** at the end of the tabs hides the side panel, and **›** on the left edge brings it back.
  **\\** does both.
- The layout icons at the top right show both views, the 3D view alone or CT review alone. In
  review they read **CT + 3D**, **Focus 3D** and **Focus CT**. Their **▾** chooses side by side or
  stacked, shows or hides each view or the **Side panel**, and has **Reset layout**.
- Drag a divider to resize, and double-click it to reset.
- **Layers** in the 3D header folds the 3D view's layer card without resizing the view.

**Segments** lists the anatomy. Select a row to open it: the model that made it, **Color**,
**Opacity**, **Focus**, which frames it in 3D, and any QC messages. A structure picked in 3D, on a
slice or in another tab opens the same way, even in a folded section. A row's **◎** shows it alone,
and its eye hides it.

**Show all**, **Hide all** and **Show selected only** apply to the whole case even when the list is
filtered. A section's eye hides it, and its arrow collapses it.

A scan with no segmentation shows its size and spacing under **Segments** instead, with **Choose
segmentation task** and **+ Add segment by hand**. In review, a new segment shows at the top of the
tab until you save it.

## View the images

### CT review

Three linked planes share one position, with its patient RAS coordinates and HU value in the strip
below them. Hover the header for what links them to the 3D view.

The header holds the title, the window preset with **W** and **L**, **Projection**, **Overlays ▾**,
**Slices ▾**, a download dot, **⤢** and **×**. In a narrow tile **W** and **L** fold under
**W/L ▾**, **Slab mm**, **Cache** and **Cancel** under **Slices ▾**, then the words and the title go,
and in the narrowest the menus' arrows.

- **Navigate:** click to move there, scroll or the slider to change slice, left or right drag for
  window/level, middle drag to pan, Ctrl or ⌘ scroll (or pinch) to zoom. Scrolling down moves
  one slice towards the feet, back or patient's left. While drawing, left drag draws and right
  drag still sets window/level.
- **Centre on click** under **Slices ▾:** on by default, and your browser keeps the choice. A click
  on a slice, a navigate click in 3D or a Coronary **Show** button also centres all three planes on
  that point at their zoom. Tilted scans do not centre.
- **Panes:** the top left corner names the plane, then **MPR**, **MIP** or **MinIP**, the native
  slice number, or the position in mm. A green dot means full resolution, and an amber ring a
  preview. **⟲** (**Reset pan and zoom**) at the top right resets that pane's pan and zoom. It
  shows on hover, stays on the pane you pressed last, and always shows on a touch screen.
- **Window:** **Soft tissue**, **Lung**, **Bone** or **Vessels**, or type **W** and **L**.
- **Projection:** **MPR**, **MIP** or **MinIP**, with **Slab mm**. A slab shows overlays on its
  centre slice and pauses drawing. Review hides it while its planes stay on the source slices.
- **Slices ▾:** **Rotatable crosshairs** adds crosshairs you can drag and rotate by their lines.
  **Reset planes** restores the planes.
- **Overlays ▾:** **2D overlay opacity**, **Fill** and **Outline** set the style. **Segments**
  chooses what shows, **Use selected** follows the selection and **Clear** shows CT alone. The
  button counts the overlays shown.
- **Download:** the dot is amber until the full CT is cached, then green. Hover it for the detail.
  **Cancel** stops the full download, and **Cache** starts it again.
- **⤢** (**Expand**) fills the screen, and **Restore** or **Escape** returns. In review, **⤢** gives
  the stage to CT review, and **CT + 3D** shares it again.
- **Colors:** bones are yellow. Change any color in the segment's row under **Segments**; your
  browser keeps it.

Tilted-gantry (sheared) scans are straightened for display only. Your marks still map to the
original voxels.

### 3D view

The 3D view's header holds its title, **Current** and **Before**, the volume preset, **PNG**,
**DICOM**, **Layers**, **⤢** and **×**. Hover the title for its detail, the coordinate system and
the mouse controls. In a narrow view the detail folds away, and **PNG** and **DICOM** fold under
**Capture ▾**.

**Layers** switches what is drawn: **Segmentation**, **CT volume** and **CT plane**. Drag to
rotate, right drag to pan, scroll to zoom. **⤢** fills the screen, and **Restore** or **Escape**
returns. In review, **⤢** gives the stage to the 3D view, and **CT + 3D** shares it again.

- The rail on the left edge turns the camera: **A**nterior, **P**osterior, **R**ight, **L**eft,
  **S**uperior and **Obl**ique, or keys **1** to **6**. The current view is lit. A heart button
  joins them with **Cinematic heart**. Below them, **+** and **−** zoom, **Focus** frames the
  selected structure or fits the CT volume, and **⟲** (**Reset view**) resets the view.
- The keys do nothing while you type, pick from a list or fill in a form.
- The preset in the header opens the volume settings, as **Settings** does.

- The slider beside **Segmentation** is **All segment opacity**. It works with **CT volume**
  visible: CT in front of a segment covers it, and CT behind shows through. If the browser lacks a
  feature this needs or GPU memory runs short, segments stay solid; the slider reads **Solid** and
  its tooltip says why. If the GPU cannot compile the see-through shader, the 3D view fails instead.
- **Settings** on the **CT volume** row opens the side panel's **3D** tab, showing the panel if
  it is hidden. In the expanded view and at 1000 px or narrower, it opens the settings inside
  **Layers** instead. If the volume fails, the row shows **Retry volume** or **Retry mask**, and the
  tab a dot.
- **CT plane** shows one native slice, and **Face plane** turns the camera to it. While the plane
  shows, a bar along the bottom of the view chooses **Native i**, **j** or **k** and moves the
  slice. **All three** there adds the other two native slices through the same point, which
  follows CT review. It carries into review and back. Three.js and the portable page show one plane.
- Tilted-gantry scans, MRI and the [portable HTML](#portable-offline-html) page use Three.js instead
  of Cornerstone3D.
- The browser memory budget is up to 3 GiB. If a view does not fit, close CT review or use **Show
  selected only**.

### CT volume settings

These settings change the display only, never source images, masks or measurements. They sit in
the side panel's **3D** tab, in one column. The header shows the volume's state and **Reset
volume**; while the volume is off it shows **Turn on**, and the sections stay listed but greyed.
Each section folds. A folded section shows a summary, opens by itself when its setting changes, and
your browser remembers which are open. **ⓘ** gives help on hover, or when you click or tap it.

- **Appearance:** **Preset**, then **Intensity**, **Volume opacity** and **Quality**. Under the
  menu, a swatch for each CT preset shows its colours; choose one to use it.
- **Lighting** (Cornerstone3D): the **Method** is **Preset lighting**, **Standard**, **Cinematic
  (VolView Hybrid)** or **Ambient occlusion**. **Reset lighting** returns to the preset.
- **Limit to segments:** its switch shows the CT only inside or outside chosen segments. Pick them
  under **Choose segments** with **Use selected**, **Use visible**, **Clear** or the list. Every
  segment list uses these words, and **Find segments…** to search. **Margin** retains thin walls.
- **Color segments** (Cornerstone3D, not in review): its switch tints the CT inside chosen segments
  with their anatomy-list colors, blended by **Color strength**.
- **Crop** limits the native i, j and k ranges.
- **Cinematic heart**, under **Appearance**, is a cardiac preset. **Measure from segments** sets
  its reference HU from myocardium and aorta or left-heart masks. Or enter **Myocardium** and
  **Blood pool** and choose **Apply reference HU**.

## Review and correct masks

Select anatomy in the list or on the model, inspect its overlay in all three planes and check any
QC messages in its row under **Segments** against the CT. Then choose **Review segmentation** and
pick the target under **Structure**. The review panel opens at the top of the side panel, which
shows first if it was hidden. **View** in the top bar or **Exit review** leaves review; your draft
stays saved.

Review opens on your main-page views, and slab projection and rotatable crosshairs pause until you
leave it. **CT volume** starts as you last left it in review, off at first, and leaving review
restores the main page's choice. MRI and tilted-gantry CT keep it off.

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

**Grow branch** appears on the CAS-Net coronary segment. Click a branch CAS-Net missed, on a slice
or the 3D view, and it adds the connected voxels where CAS-Net's own probability is at least
**10%** (or **5%**, under **Probability**), more than 1 mm from the current mask, then saves a
revision at once; **↶ Undo to parent** in **Revision history** reverts it. It refuses an addition
over 0.5 mL, and says when CAS-Net saw nothing there. On a scan segmented before this tool, run
CAS-Net again to use it.

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
- **Remove mark** takes out one mark. Point at it in the 3D view or on a slice: the mark outlined in
  white goes when you click, and **↶ Undo** puts the mark back.
- Removing a mark that was already run discards the result on screen, and **↶ Undo** brings back the
  mark but not that result. So the first click asks, and a second click on the same mark removes
  it; with no result on screen a mark goes at once. **Run AI** then makes a new result from the
  marks that remain. The marks after the removed one run together, so the mask can differ a little:
  check it again.

A warning in the review panel's messages says when a result removes 5% or more of the mask's interior, the
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
same [status lines and **Check again**](#choose-a-gpu), and the review panel shows **AI ·** and the
GPU in use. Neither appears with one GPU or in **Direct edit**.

The first AI result holds the draft on its GPU, named by **AI result from** in the review panel's
messages and, once you save, in **Revision history**. These free the draft and drop the current result:

- **↶ Undo** past the first result, or **Remove mark** on a mark its run used, unless that result
  came from **Refine whole mask** with no marks
- changing **Automatically focus inference** or **Start from an empty mask**

A held draft shows **Re-run draft on** instead of **Run on**. To move it, choose a GPU there, then
**Re-run on this GPU**. A re-run repeats every Apply step from the original mask, with any pending
marks last, and replaces the current result only when it finishes.

A re-run can change regions you did not mark: a warning in the messages counts any voxels of
the result you saw that moved. If it fails or is cancelled, the old result stays visible but cannot
be saved; re-run again or choose **Discard draft**. Your marks are never lost.

**If a GPU cannot be used**, a message in the review panel says why, with **Check again**, and
**Run AI** and **Refine whole mask** stop. Choose another GPU, or re-run or discard a draft that has
a result. Work that needs no GPU continues.

**If a GPU has been removed**, **Run on** moves to the first available GPU and says so. A draft
from the removed GPU keeps **Re-run on this GPU**, even with one GPU left. If the GPU comes back,
**Check again** clears the message.

### Add a missing structure

Choose **+ Add segment**, enter a **Name**, **Group** and **Color**, choose a relationship, then
**Create empty segment**. Mark its extent on the CT, apply, inspect and choose **Save new segment**.

A scan with no segmentation needs no model run first: choose **+ Add segment** in the top bar or
**+ Add segment by hand** under **Segments**. Review opens on that form, in **Direct edit**. Saving
makes the scan's first segmentation revision.

### Mark in the 3D view

**Mark on**, in the 3D header, selects **Surface point**, which locks picking to the target,
**CT plane**, which takes all the drawing tools on a native slice, or, with **CT volume** on,
**Volume point**. Right drag rotates while marking, and a 3D point moves the linked CT crosshair so
you can confirm it. **Face plane** faces the CT plane at any time.

With **All three**, the other two planes stay on while you mark on the CT plane, but keep only
their side beyond it, so nothing covers the slice you draw on. **Surface point** refuses a surface
that a CT plane covers.

**Remove mark** works with any **Mark on** setting, and reaches marks behind the volume or a
surface. For MRI and tilted-gantry CT, remove marks on the slices.

With **CT volume** on in review:

- **Volume point** marks the voxel centre nearest where your click first meets half-opaque volume.
  In AI assist, **Point** adds it as an Include or Exclude; other tools move the crosshair there.
- A click on no visible volume, or on an opaque surface or a CT plane in front of it, makes no
  mark; see-through surfaces do not block it. **Surface point** refuses a surface the volume covers.
- While you mark on the **CT plane**, the volume on your side of it is cut away. **Focus** keeps
  the whole CT in depth.
- **Limit to segments** masks by the saved segmentation, not your draft, so your edits do not
  change it until you save. **Volume point** sees only the CT the mask shows.
- **Color segments** and **Cinematic heart** stay off. A Cinematic heart volume shows the default
  preset, and returns when you leave review unless you choose another.

**Layers** starts folded in review. A mark cannot start underneath it, so fold it to reach that
part of the view.

### The review panel

The panel reads down: **Exit review**, **Structure** and **+ Add segment**, the messages,
**Relationship**, **Direct edit** or **AI assist**, the tools and their settings, the draft's
status, **Impact preview** and **Options +**. **Original** and **Candidate**, **↶ Undo**, **↷ Redo**,
**Apply edits**, **Save correction** and **Discard draft** stay at the bottom.

- Errors, warnings, results and 3D preview progress appear under the header, errors first, in an
  area that scrolls past four lines. Some errors carry a button, such as **Retry preview**.
- **+ Add segment**, **Relationship**, **Impact preview** and **Options +** open in the panel, so
  nothing covers a viewer. A short panel scrolls its tools; the bottom buttons always show, and so
  does **Create empty segment**.
- While the side panel is hidden, a dot on its edge means review has a message. **›** or
  **Review segmentation** brings the panel back.
- At 1000 px and narrower, the panel sits under the 3D view or CT review, and scrolls on a short
  screen.

### Compare and save

Applying creates a candidate and does not change accepted masks.

1. Use **Original** and **Candidate** to compare the CT mask and the 3D surface, or
   **Compare candidate with original masks** under **Overlays ▾** to see both on the slices.
2. Read the voxels changed and volume before and after in the panel's messages.
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
compare surfaces with the parent, and **Compare CT overlay with parent** under **Overlays ▾** does
so on the slices.

Once a segmentation is active, **Reset to scan** activates the image-only revision, with no
segmentation. Nothing is deleted, an open draft stays with its version, and the case leaves pooled
agreement until a segmentation is active again. The demo does not offer it.

To record your review, choose the status chip beside the case name and set **Revision status** to
**Unreviewed**, **Reviewed** or **Corrected**. It applies to the whole revision.

### Other correction routes

The **Refine** tab offers **Keep component at seed**, **Remove component at seed**, **Keep
inside region** and **Erase inside region**. Click a slice or surface to place a seed, or enter
coordinates or bounds in patient RAS millimetres, then choose **Create refinement revision →**. The
tab names the saved segment it changes, and a click on a surface changes which.

For 3D Slicer, use **External correction with Slicer** under the status chip:

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

**All cases** pools the active revision of each case marked **Reviewed** or **Corrected**, by
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

The **FFR** tab appears when the revision has **Coronary arteries** from ImageCAS-X
(nnU-Net or CAS-Net) or TotalSegmentator. It traces centrelines in the lumen mask on its own grid,
measures lumen area along them and solves steady hyperaemic pressure: Poiseuille resistance along
the vessels, and Young–Tsai viscous and expansion losses across each narrowing.

Inputs:

- **Lumen**, **Aorta** and **Myocardium** come from the revision's segments. The lumen starts as
  CAS-Net's, then the ImageCAS-X nnU-Net's, then TotalSegmentator's, whichever the revision has.
- **LV mass** is required without a myocardium mask. With one, its volume × 1.05 g/mL is used.
- **SBP**, **DBP** and **heart rate** are optional. Missing blood pressure is taken as 120/80 and
  marked as assumed. Heart rate is recorded only and never assumed.
- **Flow share** is for one tree only: the fraction of the heart's flow it carries. With one tree
  found, FFR is not reported until you give it. It scales the flow into every tree solved, so clear
  it once both trees are found. A share applied to more than one tree is warned of above the table,
  and is not prefilled from that result.
- **Add ostium at crosshair** adds a tree root, up to four. Use it only for an artery whose start
  does not touch the aorta mask, or without an aorta: put the crosshair on the lumen where it
  starts. A point partway along a vessel already traced from the aorta, more than 5 mm from it, is
  refused.

**Run CT-FFR** works in the background. The same inputs on unchanged masks return the earlier
result at once. Earlier results for the revision load when you open the tab.

The table lists each tree, its roots and branches, and each lesion. Select a row to move the
crosshair there. Each tree row gives the tree's hyperaemic inflow in mL/min and its percentage of
all trees' inflow.

| Column     | Meaning                                                                                                                                                                                      |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Vessel** | The branch or lesion. Below it, a branch's parent, length and hyperaemic inflow, or a lesion's length, its span from the ostium and its minimum lumen diameter (MLD).                        |
| **%DS**    | Diameter stenosis against the fitted healthy reference. Lesions are 30% or more over 1 mm or longer, where the reference is at least 1.8 mm. Runs less than 1 mm apart are one lesion.       |
| **FFR**    | Branch rows: distal FFR, at the last station whose reference diameter is at least 1.8 mm. Lesion rows: FFR +20 mm, 20 mm beyond the lesion end, following the larger daughter at a junction. |
| **ΔFFR**   | FFR before the lesion minus FFR beyond it.                                                                                                                                                   |

FFR +20 mm is read nearer, with a note, if the vessel ends or FFR is not reported there. Stations
under 1.8 mm are solved but not reported. Notes under a row say why a value is missing, or that a
lesion uses a neighbour's area (ostium, junction or vessel end), so its %DS and drop are uncertain.
Every input, assumption and model constant is listed under the table, with units.

Each FFR shows a band in brackets, such as 0.84 (0.78–0.90): the lowest and highest FFR when the
solve is repeated with every lumen half a voxel wider or narrower, resting flow 25% lower or higher,
TCRI 0.22 or 0.40 and flow-split exponent 3 or 7/3, in all 16 combinations. It is a range over
assumptions, not a confidence interval. **Indeterminate** means the band crosses 0.80, so those
assumptions alone decide which side of 0.80 the reading falls. The value before the brackets is the
nominal solve.

In the native 3D view a centreline tube shows the result, sized to the lumen. Every coronary lumen
segment fades behind it, or hides where surfaces are drawn solid. Colour by **FFR**: red at 0.75
and below, amber at 0.80, blue from 0.90, grey where FFR is not reported. Or by **Diameter**, dark
to light blue from 1 to 5 mm. The legend sits at the lower left. The tube shows while the **FFR**
tab is open, or the **3D** or **Segments** tab opened from it.
PNG and DICOM view exports leave the tube out and draw the lumens as set. The Three.js view (MR,
sheared grids) shows the table only.

Limits:

- Blunt or oblique distal vessel ends can still fork the centreline.
- The ostium face is not traced as branches, so a side branch about 1 mm across within about
  1.5 mm of the aorta may be lost.
- Where two vessels touch, their areas can mix. Such branches carry a contact note.
- A lumen gap withholds FFR on the branch that ends at it, since the flow beyond is unknown. FFR
  above the gap is still shown, with a flag that it may read low.
- A flag that flow is slow or fast for a vessel's size means its flow share or reference diameter
  may be off, so doubt its FFR. A parent thinner than its daughters imply may read narrow.

## Export

The HTML, DICOM SEG and DICOM view exports read the saved revision: first save or discard any
draft, return to the current revision and turn off comparison. Exports can contain patient images
and metadata, so treat them like the study itself.

**DICOM** and **Export SEG…** deliver to these places, depending on where the scan came from:

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

**PNG** in the 3D view saves the current view, including any review marks.

### DICOM view

**DICOM** in the 3D view saves the current view as a one-image series in the original patient
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
  A file that finishes meanwhile waits for the next **DICOM**, with **Download file**.

### Portable offline HTML

1. Choose **Export viewer**.
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
into one SEG object with overlaps preserved. All nonempty masks start selected. **Use all**, **Use
visible** and **Clear** change the selection; **Use visible** takes a snapshot of the masks switched
on in the **Segments** list.

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

SEG import and export, OsiriX ROIs and **DICOM** work for classic single-frame MR as for CT, and
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

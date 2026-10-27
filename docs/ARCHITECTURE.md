# 3DScan architecture

3DScan is an AI-assisted analysis workbench for radiologists that complements
viewers such as OsiriX. A high-quality, responsive viewer supports testing,
comparing, correcting and using segmentation and other analysis tools.

This describes the implementation as of **27 October 2026**. The live service is
the CT-only `ct-appearance-20261027` release. Static 3D MRI is implemented and
tested separately on `expansion`; X-ray, mammography, ultrasound, catheter
angiography and cine ingestion still need their own adapters. Modality enum
values alone do not enable those workflows.

Future clinic integration must account for the user's planned Orthanc-based
replacement of an existing imaging workflow. The proposed boundaries and open
questions are recorded in [CLINIC_INTEGRATION.md](./CLINIC_INTEGRATION.md);
they are separate from the implemented system shown below.

![3DScan system architecture](./diagrams/architecture.svg)

The editable Mermaid flows are below. The standalone overview is also available
as [SVG](./diagrams/architecture.svg) and [PNG](./diagrams/architecture.png);
its layout source is [render_architecture.py](./diagrams/render_architecture.py).

## System boundaries

```mermaid
flowchart LR
  subgraph CLIENT["Remote desktop browser"]
    direction TB
    UI["Vue 3 / TypeScript workspace<br/>Cases, series, panels, tasks and review controls"]
    RAM["Shared source cache in client RAM<br/>Preview then native volume; local reslice worker"]
    CT["Cornerstone3D<br/>Linked slices, overlays, crosshairs and windowing"]
    WEBGL["Three.js / WebGL2 on the client GPU<br/>Segmentation surfaces, CT planes and volume rendering"]
    UI --> CT
    UI --> WEBGL
    RAM --> CT
    RAM --> WEBGL
  end

  subgraph HOST["Processing workstation"]
    direction TB
    API["FastAPI<br/>Import, jobs, revisions, review, transport and exports"]
    QUEUE["SQLite-backed job queue<br/>One locked background worker<br/>Progress, cancellation and restart recovery"]
    PREP["Source preparation<br/>pydicom indexing and geometry checks<br/>dcm2niix conversion to a reference NIfTI grid"]
    GPU["Workstation GPU: PyTorch / ROCm / R9700<br/>TotalSegmentator with nnU-Net<br/>Optional warm nnInteractive runtime"]
    CPU["Bounded CPU processing<br/>Native-mask QC and voxel measurements<br/>Direct edits, refinement and overlap rules<br/>VTK surfaces and GLB generation"]
    TRANSPORT["Presentation transport<br/>Compressed scalar slabs, native overlay crops, GLB<br/>Cropped display-mask union at most 8 MiB"]
    API --> QUEUE
    QUEUE --> PREP
    QUEUE --> GPU
    QUEUE --> CPU
    PREP --> CPU
    GPU --> CPU
    CPU --> TRANSPORT
    TRANSPORT --> API
  end

  subgraph STATE["Persistent state on the workstation"]
    direction TB
    DB[("SQLite<br/>Cases, image assets, jobs,<br/>revisions and review sessions")]
    FILES[("Case files<br/>Original DICOM, reference NIfTI,<br/>native masks, reports and provenance")]
    CACHE[("Derived caches<br/>Conversion, inference, analysis,<br/>meshes, slabs and display unions")]
  end

  UI <-->|"LAN / WAN · private Tailscale HTTPS"| API
  API -->|"Scalar chunks and descriptors"| RAM
  API -->|"GLB meshes and mask slabs"| WEBGL
  API <--> DB
  QUEUE <--> DB
  PREP --> FILES
  CPU --> FILES
  FILES --> TRANSPORT
  CPU <--> CACHE
  GPU <--> CACHE
  TRANSPORT <--> CACHE

  API --> HTML["Self-contained offline HTML<br/>Chosen segmentation / volume / plane layers"]
  API <-->|"Geometry-checked correction round trip"| SLICER["Slicer bundle<br/>Source CT and named .seg.nrrd masks"]
  WEBGL --> PNG["Browser PNG export"]
```

The workstation parses DICOM; Cornerstone receives prepared image frames rather
than raw DICOM in this pipeline. The browser GPU renders the remote viewer;
the workstation ROCm GPU runs model inference. Warm navigation, windowing and
camera changes use resident client data.

Native reference images and masks remain authoritative for editing and
measurements. GLB meshes and the bounded 3D mask union are derived presentation
artifacts. Their simplification never changes mask-derived volumes.

## Scan and correction lifecycle

```mermaid
flowchart TD
  INPUT["DICOM folder or ZIP"] --> IMPORT["Copy originals and index series"]
  IMPORT --> SELECT["Select one reconstruction / phase"]
  SELECT --> OPEN["Queue Open scan"]
  OPEN --> CONVERT["Validate and convert to reference grid"]
  CONVERT --> IMAGE["Save an image-only revision<br/>No segmentation starts automatically"]
  IMAGE --> VIEW["Review source images and render CT"]
  IMAGE --> REQUEST["User requests segmentation<br/>Task, options and license validation"]
  REQUEST --> INFER["Reuse valid cached inference or run TotalSegmentator"]
  INFER --> MASKS["Native segmentation masks"]
  MASKS --> ARTIFACTS["QC, voxel measurements and surfaces<br/>Reuse unchanged cached artifacts"]
  ARTIFACTS --> SAVE["Commit a source-bound segmentation revision"]
  SAVE --> VIEW

  VIEW --> METHOD{"Correction method"}
  METHOD --> DIRECT["Direct paint / erase / 3D cut<br/>CPU mask operations"]
  METHOD --> AI["Optional nnInteractive prompts<br/>Workstation GPU inference"]
  DIRECT --> CANDIDATE["Candidate mask<br/>Geometry / overlap checks and change summary"]
  AI --> CANDIDATE
  CANDIDATE --> PREVIEW["Linked CT / 3D candidate preview"]
  PREVIEW -->|"Keep editing"| METHOD
  PREVIEW -->|"Discard"| VIEW
  PREVIEW -->|"Explicit acceptance"| NEW["Create a new mask revision<br/>Preserve its parent and original masks"]
  NEW --> ARTIFACTS
  SAVE -->|"Undo: activate an earlier revision"| VIEW
```

The worker serializes queued processing; it is a background thread backed by
SQLite and a filesystem process lock, not an external broker. Dense mask
analysis and surface preparation use a bounded CPU thread pool internally.
Completed stages remain reusable after cancellation or restart.

## Code map

- [API and transport](../scan3d/api.py), [job dispatch and revision building](../scan3d/service.py), [SQLite and activation](../scan3d/store.py).
- [DICOM preparation](../scan3d/dicom.py), [inference](../scan3d/segmentation.py), [native operations and measurements](../scan3d/imaging.py), [CPU surfaces](../scan3d/surfaces.py).
- [Candidate review and acceptance](../scan3d/review.py), [scalar/mask transport](../scan3d/review_volume.py), [display union](../scan3d/volume_mask_union.py).
- [Browser source worker](../web/src/lib/ct-volume.ts), [Cornerstone adapter](../web/src/lib/cornerstone-runtime.ts), [3D scene](../web/src/lib/scene.ts), [volume renderer](../web/src/lib/ct-volume-renderer.ts).
- [Portable export](../scan3d/portable_export.py), [embedded offline data](../web/src/lib/portable-viewer.ts), [MRI expansion boundaries](./EXPANSION_FOUNDATION.md).

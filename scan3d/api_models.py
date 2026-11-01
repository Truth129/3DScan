"""Request bodies; unknown fields and non-finite numbers are rejected."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, model_validator

from .acquisition import CardiacPhase, StudyProfile
from .inference_paths import ID_PATTERN
from .orthanc_models import Digest, OrthancID


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ImportLocal(Input):
    path: str
    label: str | None = Field(None, max_length=128)


class ImportStart(Input):
    label: str | None = Field(None, max_length=128)
    total_files: int = Field(ge=1, le=100_000)
    total_bytes: int = Field(ge=0)


class ImportPart(Input):
    path: str = Field(min_length=1, max_length=1024)
    offset: int = Field(ge=0)
    size: int = Field(ge=0)


import_manifest = TypeAdapter(list[ImportPart])


class OrthancQuery(Input):
    patient_id: str | None = Field(None, max_length=256)
    patient_name: str | None = Field(None, max_length=256)
    accession_number: str | None = Field(None, max_length=256)
    study_date: str | None = Field(None, max_length=256)
    offset: int = Field(0, ge=0, le=1_000_000)
    limit: int = Field(25, ge=1, le=50)


class OrthancImport(Input):
    series_id: str = Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{8}){4}$")
    snapshot: str = Field(pattern=r"^[0-9a-f]{64}$")
    label: str | None = Field(None, max_length=128)


class OrthancLaunch(Input):
    series_id: OrthancID
    source_key: Digest


class DicomSegExport(Input):
    structure_ids: list[Annotated[str, Field(strict=True, min_length=1, max_length=200)]] | None = Field(None, min_length=1, max_length=1000)
    destination: str | None = Field(None, strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    resend: bool = Field(False, strict=True)
    overlap_priority: list[Annotated[str, Field(strict=True, min_length=1, max_length=200)]] | None = Field(None, min_length=2, max_length=1000)
    delivery: Literal["orthanc", "file"] = "orthanc"


class Process(Input):
    series_uid: str
    profile: StudyProfile = "chest_abdomen"
    phase: str = Field(min_length=1, max_length=80)
    cardiac_phase: CardiacPhase | None = None
    rr_percent: float | None = Field(default=None, ge=0, le=100, strict=True)


class Segment(Input):
    revision_id: str
    task: str | None = Field(default=None, min_length=1, max_length=80)
    robust_crop: bool = False
    body_seg: bool = False
    remove_small_blobs: bool = False
    inference_path: str | None = Field(default=None, max_length=32, pattern=ID_PATTERN)


class TotalSegmentatorLicense(Input):
    license_key: SecretStr = Field(max_length=128)


class Operation(Input):
    type: Literal["keep_component", "remove_component", "keep_region", "erase_region"]
    seed_ras: tuple[float, float, float] | None = None
    bounds_ras: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None

    @model_validator(mode="after")
    def valid_operation(self):
        if self.type.endswith("component") and self.seed_ras is None:
            raise ValueError("A patient-space seed is required")
        if self.type.endswith("region"):
            if self.bounds_ras is None or any(a >= b for a, b in zip(*self.bounds_ras)):
                raise ValueError("Bounds must have positive extent on all three axes")
        return self


class Refine(Input):
    revision_id: str
    structure_id: str
    operation: Operation


class Activate(Input):
    revision_id: str


class Review(Input):
    review_status: Literal["unreviewed", "reviewed", "corrected"]


class InteractivePrompt(Input):
    id: str = Field(min_length=1, max_length=80)
    kind: Literal["point"] = "point"
    ras: tuple[float, float, float]
    include: bool


class InteractiveDrawing(Input):
    id: str = Field(min_length=1, max_length=80)
    include: bool
    axis: int = Field(ge=0, le=2)
    index: int = Field(ge=0)
    vertices: list[tuple[float, float]] = Field(min_length=2, max_length=2048)


class InteractiveBox(InteractiveDrawing):
    kind: Literal["bbox"]
    vertices: list[tuple[float, float]] = Field(min_length=2, max_length=2)


class InteractiveScribble(InteractiveDrawing):
    kind: Literal["scribble"]
    radius_mm: float = Field(default=3, ge=0.5, le=30)


class InteractiveLasso(InteractiveDrawing):
    kind: Literal["lasso"]
    vertices: list[tuple[float, float]] = Field(min_length=3, max_length=2048)


class InteractiveScissors(Input):
    id: str = Field(min_length=1, max_length=80)
    kind: Literal["scissors"]
    include: Literal[False] = False
    operation: Literal["erase_inside", "erase_outside"]
    shape: Literal["freehand", "rectangle", "circle"]
    vertices: list[tuple[float, float]] = Field(min_length=3, max_length=2048)
    ras_to_clip: tuple[
        tuple[float, float, float, float], tuple[float, float, float, float], tuple[float, float, float, float], tuple[float, float, float, float]
    ]


class InteractiveOptions(Input):
    autozoom: bool = True
    start_empty: bool = False


class InteractiveRelationship(Input):
    kind: Literal["independent", "subregion", "subdivision"] = "independent"
    parent_id: str | None = Field(default=None, max_length=256)
    exclusive_with: list[str] = Field(default_factory=list, max_length=128)


class InteractiveCreate(Input):
    revision_id: str
    structure_id: str
    relationship: InteractiveRelationship | None = None
    method: Literal["direct", "nninteractive"] = "nninteractive"


class InteractiveNewStructure(Input):
    revision_id: str
    name: str = Field(min_length=1, max_length=128)
    group: str = Field(default="Custom", min_length=1, max_length=64)
    color: str = Field(default="#e3a35d", pattern=r"^#[a-fA-F0-9]{6}$")
    relationship: InteractiveRelationship | None = None
    method: Literal["direct", "nninteractive"] = "nninteractive"


class InteractiveUpdate(Input):
    expected_generation: int = Field(ge=0)
    prompts: list[InteractivePrompt | InteractiveBox | InteractiveScribble | InteractiveLasso | InteractiveScissors] = Field(max_length=128)
    options: InteractiveOptions | None = None
    relationship: InteractiveRelationship | None = None


class InteractiveGeneration(Input):
    generation: int = Field(ge=0)


class InteractivePredict(InteractiveGeneration):
    full_refine: bool = False
    inference_path: str | None = Field(default=None, max_length=32, pattern=ID_PATTERN)
    replay: bool = False


class InteractiveDiscard(Input):
    expected_generation: int = Field(ge=0)

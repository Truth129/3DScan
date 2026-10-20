from contextlib import asynccontextmanager
from collections import OrderedDict
from functools import lru_cache
import base64
import json
import math
from pathlib import Path
import re
import threading
from typing import Literal
import uuid
import zipfile

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__
from .config import Settings
from .service import Service, extract_zip, safe_relative
from .store import ReviewConflict, public_case, public_job, public_revision


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ImportLocal(Input):
    path: str
    label: str | None = Field(None, max_length=128)


class Process(Input):
    series_uid: str
    profile: Literal["chest", "abdomen", "chest_abdomen"] = "chest_abdomen"
    phase: str = Field(min_length=1, max_length=80)


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
    radius_mm: float = Field(default=3, ge=.5, le=30)


class InteractiveLasso(InteractiveDrawing):
    kind: Literal["lasso"]
    vertices: list[tuple[float, float]] = Field(min_length=3, max_length=2048)


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


class InteractiveNewStructure(Input):
    revision_id: str
    name: str = Field(min_length=1, max_length=128)
    group: str = Field(default="Custom", min_length=1, max_length=64)
    color: str = Field(default="#e3a35d", pattern=r"^#[a-fA-F0-9]{6}$")
    relationship: InteractiveRelationship | None = None


class InteractiveUpdate(Input):
    expected_generation: int = Field(ge=0)
    prompts: list[InteractivePrompt | InteractiveBox | InteractiveScribble | InteractiveLasso] = Field(max_length=128)
    options: InteractiveOptions | None = None
    relationship: InteractiveRelationship | None = None


class InteractiveGeneration(Input):
    generation: int = Field(ge=0)


class InteractivePredict(InteractiveGeneration):
    full_refine: bool = False


class InteractiveDiscard(Input):
    expected_generation: int = Field(ge=0)


class _FrameCache:
    """Bound compressed interactive frames by bytes as well as entry count."""

    def __init__(self, max_bytes=64 * 1024 ** 2, max_entries=18):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.entries = OrderedDict()
        self.byte_length = 0
        self.lock = threading.RLock()
        self.stripes = tuple(threading.Lock() for _ in range(32))

    def get_or_create(self, key, create):
        with self.stripes[hash(key) % len(self.stripes)]:
            with self.lock:
                if key in self.entries:
                    self.entries.move_to_end(key)
                    return self.entries[key]
            result = create()
            size = len(result["body"]) + len(json.dumps(result["frame"]))
            if size <= self.max_bytes:
                with self.lock:
                    while self.entries and (len(self.entries) >= self.max_entries
                            or self.byte_length + size > self.max_bytes):
                        _, previous = self.entries.popitem(last=False)
                        self.byte_length -= len(previous["body"]) + len(json.dumps(previous["frame"]))
                    self.entries[key] = result
                    self.byte_length += size
            return result


def create_app(settings: Settings | None = None):
    service = Service(settings)

    @asynccontextmanager
    async def lifespan(app):
        service.start()
        yield
        service.stop()

    app = FastAPI(title="3DScan", version=__version__, lifespan=lifespan)
    app.state.service = service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(service.settings.allowed_hosts))

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc).strip("'")})

    @app.exception_handler(ReviewConflict)
    async def review_conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        # Local filesystem endpoints must not be callable by an unrelated browser origin.
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            from urllib.parse import urlsplit
            if urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse(status_code=403, content={"detail": "Cross-origin mutations are not allowed"})
        return await call_next(request)

    @app.get("/api/health")
    def health():
        from .doctor import diagnose
        return {"status": "ok", "worker": bool(service.thread and service.thread.is_alive()),
                "gpu": diagnose(live=False), "version": __version__}

    @app.get("/api/cases")
    def cases():
        return [public_case(case) for case in service.store.list("cases")]

    @app.get("/api/interactive/status")
    def interactive_status():
        from .interactive import status
        return {**status(), "capabilities": {"point": True, "bbox2d": True, "bbox3d": False,
                                             "scribble": True, "lasso": True, "initial_label": True}}

    @app.get("/api/cases/{case_id}/reviews")
    def interactive_reviews(case_id: str):
        from .review import public_review
        return [public_review(record) for record in service.reviews.list(case_id)]

    @app.post("/api/cases/{case_id}/reviews")
    def interactive_create(case_id: str, body: InteractiveCreate):
        from .review import public_review
        return public_review(service.reviews.create(case_id, body.revision_id, body.structure_id,
            relationship=body.relationship.model_dump(exclude_none=True) if body.relationship else None))

    @app.post("/api/cases/{case_id}/reviews/new-structure")
    def interactive_new_structure(case_id: str, body: InteractiveNewStructure):
        from .review import public_review
        return public_review(service.reviews.create_structure(case_id, body.revision_id, body.name, body.group, body.color,
            relationship=body.relationship.model_dump(exclude_none=True) if body.relationship else None))

    @app.get("/api/cases/{case_id}/reviews/{session_id}")
    def interactive_review(case_id: str, session_id: str):
        from .review import public_review
        return public_review(service.reviews.get(case_id, session_id))

    @app.patch("/api/cases/{case_id}/reviews/{session_id}")
    def interactive_update(case_id: str, session_id: str, body: InteractiveUpdate):
        from .review import public_review
        return public_review(service.reviews.update(case_id, session_id, body.expected_generation,
                                                   [prompt.model_dump() for prompt in body.prompts],
                                                   options=body.options.model_dump() if body.options else None,
                                                   relationship=body.relationship.model_dump(exclude_none=True) if body.relationship else None))

    @app.post("/api/cases/{case_id}/reviews/{session_id}/predict")
    def interactive_predict(case_id: str, session_id: str, body: InteractivePredict):
        return public_job(service.reviews.predict(case_id, session_id, body.generation, full_refine=body.full_refine))

    @app.post("/api/cases/{case_id}/reviews/{session_id}/accept")
    def interactive_accept(case_id: str, session_id: str, body: InteractiveGeneration):
        return public_job(service.reviews.accept(case_id, session_id, body.generation))

    @app.post("/api/cases/{case_id}/reviews/{session_id}/discard")
    def interactive_discard(case_id: str, session_id: str, body: InteractiveDiscard):
        from .review import public_review
        return public_review(service.reviews.discard(case_id, session_id, body.expected_generation))

    @app.post("/api/cases/demo")
    def demo():
        from .demo import create_demo
        return public_case(create_demo(service))

    @app.post("/api/cases/import-local")
    def import_local(body: ImportLocal):
        return public_case(service.import_local(body.path, body.label))

    @app.post("/api/cases/import")
    def import_files(files: list[UploadFile] = File(...), label: str | None = Form(None)):
        if label and len(label) > 128:
            raise ValueError("Case label is too long")
        case = service.new_case(label)
        source = service.store.case_dir(case["id"]) / "dicom"
        source.mkdir()
        total = 0
        try:
            if len(files) > 100_000:
                raise ValueError("Too many uploaded files")
            for uploaded in files:
                relative = safe_relative(uploaded.filename or "scan.dcm")
                destination = source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ValueError("Duplicate upload filename")
                with destination.open("wb") as output:
                    while chunk := uploaded.file.read(1024 * 1024):
                        total += len(chunk)
                        if total > service.settings.upload_limit:
                            raise ValueError("Upload exceeds import limit")
                        output.write(chunk)
                if zipfile.is_zipfile(destination):
                    expanded = source / (relative.stem + "_expanded")
                    extract_zip(destination, expanded, service.settings.upload_limit - total)
                    total += sum(p.stat().st_size for p in expanded.rglob("*") if p.is_file())
                    if total > service.settings.upload_limit:
                        raise ValueError("Expanded uploads exceed import limit")
            return public_case(service.finish_import(case, source))
        except Exception as exc:
            case.update(status="import_failed", error=str(exc))
            service.store.save("cases", case)
            raise

    @app.get("/api/cases/{case_id}")
    def case(case_id: str):
        return public_case(service.store.get("cases", case_id))

    @app.post("/api/cases/{case_id}/process")
    def process(case_id: str, body: Process):
        return public_job(service.process(case_id, body.series_uid, body.profile, body.phase))

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return public_job(service.store.get("jobs", job_id))

    @app.get("/api/jobs/{job_id}/measurements")
    def measurements(job_id: str):
        record = service.store.get("jobs", job_id)
        structures = record.get("measurements", [])
        return {"structures": structures, "completed": len(structures),
                "total": record.get("progress", {}).get("total", 0)}

    @app.get("/api/cases/{case_id}/jobs")
    def case_jobs(case_id: str):
        service.store.get("cases", case_id)
        return [public_job(record) for record in service.store.list("jobs", case_id)]

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        job = service.store.get("jobs", job_id)
        if job["status"] in ("queued", "running"):
            changes = dict(cancel_requested=True)
            if job["status"] == "queued":
                changes.update(status="cancelled", stage="Cancelled")
            job = service.store.update_job(job_id, **changes)
        return public_job(job)

    @app.get("/api/cases/{case_id}/revisions")
    def revisions(case_id: str):
        service.store.get("cases", case_id)
        return [public_revision(r) for r in service.store.list("revisions", case_id)]

    @app.get("/api/cases/{case_id}/revisions/{revision_id}")
    def revision(case_id: str, revision_id: str):
        return public_revision(service.store.revision(case_id, revision_id))

    @app.post("/api/cases/{case_id}/refinements")
    def refine(case_id: str, body: Refine):
        parent = service.store.revision(case_id, body.revision_id)
        if body.structure_id not in {s["id"] for s in parent["structures"]}:
            raise ValueError("Structure does not belong to the selected revision")
        return public_job(service.store.enqueue(case_id, "refine", body.model_dump()))

    @app.post("/api/cases/{case_id}/activate")
    def activate(case_id: str, body: Activate):
        return public_case(service.store.activate(case_id, body.revision_id))

    @app.patch("/api/cases/{case_id}/revisions/{revision_id}")
    def review(case_id: str, revision_id: str, body: Review):
        return public_revision(service.store.set_review_status(case_id, revision_id, body.review_status))

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/model.glb")
    def model(case_id: str, revision_id: str, detail: bool = False):
        revision = service.store.revision(case_id, revision_id)
        target = service.ensure_detailed(case_id, revision_id) if detail else Path(revision["directory"]) / "model.glb"
        return FileResponse(target, media_type="model/gltf-binary")

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/model-manifest")
    def model_manifest(case_id: str, revision_id: str):
        revision = service.store.revision(case_id, revision_id)
        prefix = f"/api/cases/{case_id}/revisions/{revision_id}/surfaces"
        return {"schema_version": 1, "structures": [
            {"id": asset["id"], "key": asset["key"], "url": f"{prefix}/{asset['key']}.glb"}
            for asset in revision.get("surface_assets", [])]}

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/surfaces/{key}.glb")
    def surface(case_id: str, revision_id: str, key: str):
        from .surfaces import surface_asset_path
        revision = service.store.revision(case_id, revision_id)
        if key not in {asset["key"] for asset in revision.get("surface_assets", [])}:
            raise KeyError("Surface does not belong to this revision")
        target = surface_asset_path(service.surface_cache, key)
        if not target.is_file():
            raise HTTPException(503, "Cached surface is unavailable; regenerate the model")
        return FileResponse(target, media_type="model/gltf-binary",
                            headers={"Cache-Control": "private, max-age=31536000, immutable"})

    review_cache = service.settings.data_dir / "review-cache"

    def ct_revision(case_id, revision_id):
        # A candidate is a read-only CT context, never an accepted revision or
        # a new CT volume. Its immutable generation authorizes the mask paths.
        if revision_id.startswith("review-"):
            return service.reviews.candidate_revision(case_id, revision_id)
        return service.store.revision(case_id, revision_id)

    def review_ct(case_id, revision_id, quality="full"):
        from .review_volume import ct_volume
        revision = ct_revision(case_id, revision_id)
        return ct_volume(revision["ct_file"], review_cache, quality)

    def review_mask(case_id, revision_id, structure_id, parent=False, quality="full"):
        from .review_volume import mask_volume
        revision = ct_revision(case_id, revision_id)
        if structure_id not in {s["id"] for s in revision["structures"]}:
            raise ValueError("Unknown structure")
        selected = revision
        if parent:
            if not revision.get("parent_id"):
                return None
            selected = ct_revision(case_id, revision["parent_id"])
        structure = next((s for s in selected["structures"] if s["id"] == structure_id), None)
        return mask_volume(revision["ct_file"], structure["mask_file"], review_cache, quality) if structure else None

    def with_chunk_urls(descriptor, prefix, query=""):
        return {**descriptor, "chunks": [
            {**chunk, "url": f"{prefix}/{descriptor['key']}/chunks/{chunk['index']}{query}"}
            for chunk in descriptor["chunks"]]}

    def volume_chunk(descriptor, key, index):
        from .review_volume import chunk_path
        if descriptor is None:
            raise KeyError("Review volume does not belong to this revision")
        return FileResponse(chunk_path(review_cache, descriptor, key, index),
                            media_type="application/octet-stream", headers={
                                "Content-Encoding": "gzip", "Vary": "Accept-Encoding",
                                "Cache-Control": "private, max-age=31536000, immutable",
                                "X-Content-Type-Options": "nosniff",
                            })

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/ct-volume")
    def ct_volume_manifest(case_id: str, revision_id: str, quality: Literal["preview", "full"] = "full"):
        descriptor = review_ct(case_id, revision_id, quality)
        prefix = f"/api/cases/{case_id}/revisions/{revision_id}/ct-volume"
        return JSONResponse(with_chunk_urls(descriptor, prefix, f"?quality={quality}"),
                            headers={"Cache-Control": "private, no-store"})

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/ct-volume/{key}/chunks/{index}")
    def ct_volume_chunk(case_id: str, revision_id: str, key: str, index: int,
                        quality: Literal["preview", "full"] = "full"):
        return volume_chunk(review_ct(case_id, revision_id, quality), key, index)

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/mask-volume")
    def mask_volume_manifest(case_id: str, revision_id: str, structure_id: str, compare: bool = False,
                             quality: Literal["preview", "full"] = "full"):
        from urllib.parse import urlencode
        prefix = f"/api/cases/{case_id}/revisions/{revision_id}/mask-volume"
        current = review_mask(case_id, revision_id, structure_id, quality=quality)
        parent = review_mask(case_id, revision_id, structure_id, True, quality) if compare else None
        def descriptor(value, is_parent):
            query = "?" + urlencode({"structure_id": structure_id, "parent": str(is_parent).lower(), "quality": quality})
            return with_chunk_urls(value, prefix, query) if value else None
        return JSONResponse({"schema_version": 1, "current": descriptor(current, False),
                             "parent": descriptor(parent, True)}, headers={"Cache-Control": "private, no-store"})

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/mask-volume/{key}/chunks/{index}")
    def mask_volume_chunk(case_id: str, revision_id: str, key: str, index: int,
                          structure_id: str, parent: bool = False,
                          quality: Literal["preview", "full"] = "full"):
        return volume_chunk(review_mask(case_id, revision_id, structure_id, parent, quality), key, index)

    frame_cache = _FrameCache()

    def cached_review_frame(ct, mask, parent, plane, position_mm, overlays=None, native_axis=None, native_index=None):
        from .review_volume import review_frame
        key = (ct, mask, parent, plane, position_mm, overlays, native_axis, native_index)
        return frame_cache.get_or_create(key, lambda: review_frame(
            Path(ct[0]), Path(mask[0]) if mask else None, plane, position_mm,
            Path(parent[0]) if parent else None, overlays=None if overlays is None else tuple(
                (structure_id, Path(current[0]), Path(previous[0]) if previous else None)
                for structure_id, current, previous in overlays),
            native_axis=native_axis, native_index=native_index))

    def selected_slice_masks(case_id, revision, structure_id, structure_ids, compare):
        requested = ([structure_id] if structure_id else []) + (structure_ids or [])
        ids = tuple(dict.fromkeys(requested))
        if len(ids) > 128:
            raise ValueError("Select at most 128 structures for CT review")
        structures = {s["id"]: s for s in revision["structures"]}
        # Check every selected id before stat'ing or decompressing any mask.
        if any(structure_id not in structures for structure_id in ids):
            raise ValueError("Unknown structure")
        previous = {}
        if compare and ids and revision.get("parent_id"):
            parent = ct_revision(case_id, revision["parent_id"])
            previous = {s["id"]: s for s in parent["structures"]}
        return tuple((structures[structure_id], previous.get(structure_id)) for structure_id in ids)

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/review-frame")
    def raw_review_frame(case_id: str, revision_id: str, plane: str = "axial", position_mm: float = 0,
                         structure_id: str | None = None, compare: bool = False,
                         structure_ids: list[str] | None = Query(None), native_axis: int | None = None,
                         native_index: int | None = None):
        revision = ct_revision(case_id, revision_id)
        selected = selected_slice_masks(case_id, revision, structure_id, structure_ids, compare)
        structure, previous = selected[0] if selected else (None, None)
        overlays = None if structure_ids is None else tuple(
            (current["id"], slice_file_key(current["mask_file"]),
             slice_file_key(old["mask_file"]) if old else None) for current, old in selected)
        result = cached_review_frame(slice_file_key(revision["ct_file"]),
                                     slice_file_key(structure["mask_file"]) if structure else None,
                                     slice_file_key(previous["mask_file"]) if previous else None,
                                     plane, position_mm, overlays, native_axis, native_index)
        return Response(result["body"], media_type="application/octet-stream", headers={
            "Content-Encoding": "gzip", "Vary": "Accept-Encoding", "Cache-Control": "private, no-store",
            "X-CT-Frame": json.dumps(result["frame"], separators=(",", ":"), allow_nan=False),
            "X-Content-Type-Options": "nosniff",
        })

    # Legacy clients can request the image and its metadata concurrently.
    # functools.lru_cache alone permits both misses to perform the same render;
    # bounded striped locks let identical requests share one result without
    # serializing the three anatomical views or retaining a lock per position.
    slice_locks = tuple(threading.Lock() for _ in range(64))

    def slice_file_key(filename):
        if not filename:
            return None
        path = Path(filename).resolve()
        stat = path.stat()
        return (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    @lru_cache(maxsize=48)
    def cached_slice(ct, mask, parent_mask, plane, position, window, level, overlays=None, native_axis=None, native_index=None):
        from . import imaging
        arguments = (Path(ct[0]), Path(mask[0]) if mask else None, plane, position, window, level,
                     Path(parent_mask[0]) if parent_mask else None)
        native = {"native_axis": native_axis, "native_index": native_index} if native_axis is not None or native_index is not None else {}
        if overlays is None:
            return imaging.slice_image(*arguments, **native)
        return imaging.slice_image(*arguments, overlays=tuple(
            (structure_id, Path(current[0]), Path(previous[0]) if previous else None, color)
            for structure_id, current, previous, color in overlays), **native)

    def slice_data(ct, mask, parent_mask, plane, position, window, level, native_axis=None, native_index=None):
        key = (slice_file_key(ct), slice_file_key(mask), slice_file_key(parent_mask),
               plane, position, window, level, None, native_axis, native_index)
        with slice_locks[hash(key) % len(slice_locks)]:
            return cached_slice(*key)

    def get_slice(case_id, revision_id, plane, position_mm, window, level, structure_id, compare, structure_ids=None,
                  native_axis=None, native_index=None):
        if plane not in ("axial", "coronal", "sagittal"):
            raise ValueError("Unknown anatomical plane")
        if not all(math.isfinite(v) for v in (position_mm, window, level)) or window <= 0:
            raise ValueError("Window and positions must be finite, with a positive window")
        revision = ct_revision(case_id, revision_id)
        selected = selected_slice_masks(case_id, revision, structure_id, structure_ids, compare)
        structure, previous = selected[0] if selected else (None, None)
        if structure_ids is not None:
            from .imaging import presentation_color
            overlays = tuple((current["id"], slice_file_key(current["mask_file"]),
                              slice_file_key(old["mask_file"]) if old else None, presentation_color(current))
                             for current, old in selected)
            key = (slice_file_key(revision["ct_file"]), None, None, plane, position_mm, window, level, overlays, native_axis, native_index)
            with slice_locks[hash(key) % len(slice_locks)]:
                return cached_slice(*key)
        return slice_data(revision["ct_file"], structure["mask_file"] if structure else None,
                          previous["mask_file"] if previous else None, plane, position_mm, window, level, native_axis, native_index)

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/slice")
    def slice_png(case_id: str, revision_id: str, plane: str = "axial", position_mm: float = 0,
                  window: float = 400, level: float = 40, structure_id: str | None = None, compare: bool = False,
                  structure_ids: list[str] | None = Query(None), native_axis: int | None = None,
                         native_index: int | None = None):
        result = get_slice(case_id, revision_id, plane, position_mm, window, level, structure_id, compare, structure_ids, native_axis, native_index)
        return Response(result["image"], media_type="image/png", headers={
            # The bitmap and its coordinate transform must describe the same
            # requested frame, including during rapid crosshair changes.
            "X-Slice-Meta": json.dumps(result["meta"], separators=(",", ":"), allow_nan=False),
            "Cache-Control": "private, no-store",
        })

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/slice-meta")
    def slice_meta(case_id: str, revision_id: str, plane: str = "axial", position_mm: float = 0,
                   window: float = 400, level: float = 40, structure_id: str | None = None, compare: bool = False,
                   structure_ids: list[str] | None = Query(None), native_axis: int | None = None,
                         native_index: int | None = None):
        return get_slice(case_id, revision_id, plane, position_mm, window, level, structure_id, compare, structure_ids, native_axis, native_index)["meta"]

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/probe")
    def probe(case_id: str, revision_id: str, x: float, y: float, z: float):
        from . import imaging
        revision = ct_revision(case_id, revision_id)
        return imaging.probe(Path(revision["ct_file"]), [x, y, z])

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/slicer.zip")
    def slicer_bundle(case_id: str, revision_id: str):
        from . import slicer_io
        revision = service.store.revision(case_id, revision_id)
        target = Path(revision["directory"]) / "slicer.zip"
        slicer_io.export_bundle(Path(revision["ct_file"]), revision["structures"], target)
        return FileResponse(target, filename=f"Scan3D-{case_id[:8]}-Slicer.zip")

    @app.post("/api/cases/{case_id}/corrections")
    def corrections(case_id: str, revision_id: str = Form(...), segmentation: UploadFile = File(...)):
        service.store.revision(case_id, revision_id)
        path = service.store.case_dir(case_id) / "uploads" / f"{uuid.uuid4().hex}.seg.nrrd"
        path.parent.mkdir(exist_ok=True)
        total = 0
        with path.open("wb") as out:
            while chunk := segmentation.file.read(1024 * 1024):
                total += len(chunk)
                if total > service.settings.upload_limit:
                    raise ValueError("Correction exceeds import limit")
                out.write(chunk)
        return public_job(service.store.enqueue(case_id, "correction", {"revision_id": revision_id, "seg_file": str(path)}))

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/export.html")
    def export(case_id: str, revision_id: str):
        revision = service.store.revision(case_id, revision_id)
        bundle = service.settings.web_dir / "offline-viewer.js"
        if not bundle.exists():
            raise HTTPException(503, "Build the frontend before exporting: cd web && npm run build")
        portable = public_revision(revision)
        for key in ("case_id", "id", "parent_id", "model_url", "model_manifest_url", "surface_assets", "acquisition", "operation"):
            portable.pop(key, None)
        portable["label"] = f"Scan3D case {case_id[:8]}"
        manifest = json.dumps(portable, allow_nan=False).replace("<", "\\u003c")
        script = re.sub(r"</script", r"<\\/script", bundle.read_text(), flags=re.I)
        css_file = service.settings.web_dir / "offline-viewer.css"
        css = css_file.read_text() if css_file.exists() else ""
        encoded = base64.b64encode((Path(revision["directory"]) / "model.glb").read_bytes()).decode("ascii")
        document = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="referrer" content="no-referrer"><title>3DScan anatomy</title><style>{css}</style></head><body><div id="app"></div>'
            f'<script>{script}</script><script>Scan3DOffline.initOffline({manifest},"{encoded}");</script></body></html>')
        return HTMLResponse(document, headers={"Content-Disposition": f'attachment; filename="Scan3D-{case_id[:8]}.html"'})

    if service.settings.web_dir.is_dir():
        app.mount("/", StaticFiles(directory=service.settings.web_dir, html=True), name="web")
    else:
        @app.get("/", response_class=HTMLResponse)
        def build_notice():
            return '<h1>3DScan API is running</h1><p>Build the interface with <code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code>.</p><a href="/docs">API documentation</a>'
    return app

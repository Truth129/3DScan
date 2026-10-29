"""Viewer data: surfaces, chunked CT and mask volumes, review frames, probes and exports."""

from collections import OrderedDict
from email.utils import parsedate
import hashlib
import json
from pathlib import Path
import threading

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .compressed_files import cached_gzip
from .domain import VolumeQuality
from .errors import NotFound
from .portable_export import ExportRequest


class _FrameCache:
    """Bound compressed interactive frames by bytes as well as entry count."""

    def __init__(self, max_bytes=64 * 1024**2, max_entries=18):
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
                    while self.entries and (len(self.entries) >= self.max_entries or self.byte_length + size > self.max_bytes):
                        _, previous = self.entries.popitem(last=False)
                        self.byte_length -= len(previous["body"]) + len(json.dumps(previous["frame"]))
                    self.entries[key] = result
                    self.byte_length += size
            return result


def with_image_context(value, asset):
    return {
        **value,
        "reference_image_id": asset["id"],
        "modality": asset["modality"],
        "scalar": {"units": asset["scalar"]["units"]},
        "presentation": asset["presentation"],
        "source": asset["source"],
    }


def with_chunk_urls(descriptor, prefix, query=""):
    return {**descriptor, "chunks": [{**chunk, "url": f"{prefix}/{descriptor['key']}/chunks/{chunk['index']}{query}"} for chunk in descriptor["chunks"]]}


def slice_file_key(filename):
    if not filename:
        return None
    path = Path(filename).resolve()
    stat = path.stat()
    return (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _encoding_preferences(value):
    qualities = {}
    for item in value.split(","):
        encoding, *parameters = item.strip().lower().split(";")
        quality = 1.0
        for parameter in parameters:
            name, _, weight = parameter.strip().partition("=")
            if name.strip() == "q":
                try:
                    quality = float(weight)
                except ValueError:
                    quality = 0.0
                if not 0 <= quality <= 1:
                    quality = 0.0
        qualities[encoding.strip()] = quality
    gzip = qualities.get("gzip", qualities.get("*", 0))
    identity = qualities.get("identity", 0 if qualities.get("*") == 0 else 1)
    # An implicit identity preference does not defeat an explicitly requested
    # coding; an explicit higher identity weight does.
    return gzip > 0 and gzip >= qualities.get("identity", 0), identity > 0


def _glb_response(target, request, cache_control=None):
    headers = {"Vary": "Accept-Encoding"}
    if cache_control:
        headers["Cache-Control"] = cache_control
    gzip, identity = _encoding_preferences(request.headers.get("accept-encoding", ""))
    selected = Path(target)
    encoding = "identity"
    # Keep byte ranges in the original downloadable GLB representation.
    if gzip and "range" not in request.headers:
        compressed = cached_gzip(selected)
        if compressed is not None:
            selected, encoding = compressed, "gzip"
            headers["Content-Encoding"] = encoding
    if encoding == "identity" and not identity:
        raise HTTPException(406, "No acceptable GLB encoding is available", headers=headers)
    stat = selected.stat()
    signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, encoding)
    headers["ETag"] = '"' + hashlib.sha256(repr(signature).encode()).hexdigest() + '"'
    response = FileResponse(selected, media_type="model/gltf-binary", headers=headers, stat_result=stat)
    if_none_match = request.headers.get("if-none-match")
    if if_none_match is not None:
        not_modified = any(tag.strip().removeprefix("W/") in ("*", headers["ETag"]) for tag in if_none_match.split(","))
    else:
        since = parsedate(request.headers.get("if-modified-since", ""))
        modified = parsedate(response.headers["last-modified"])
        not_modified = since is not None and modified is not None and since >= modified
    if not_modified:
        return Response(status_code=304, headers={**headers, "Last-Modified": response.headers["last-modified"]})
    return response


def add_routes(app, service):
    @app.get("/api/cases/{case_id}/revisions/{revision_id}/model.glb")
    def model(case_id: str, revision_id: str, request: Request, detail: bool = False):
        if revision_id.startswith("review-"):
            raise NotFound("Candidate models are available through their surface manifest")
        revision = service.store.revision(case_id, revision_id)
        if not revision["structures"]:
            raise ValueError("This image has no segmentation surfaces yet")
        target = service.ensure_detailed(case_id, revision_id) if detail else Path(revision["directory"]) / "model.glb"
        return _glb_response(target, request)

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/model-manifest")
    def model_manifest(case_id: str, revision_id: str):
        revision = service.ct_revision(case_id, revision_id)
        if revision_id.startswith("review-") and not revision.get("preview_3d_ready"):
            raise NotFound("This candidate has no prepared 3D preview")
        prefix = f"/api/cases/{case_id}/revisions/{revision_id}/surfaces"
        result = {
            "schema_version": 1,
            "structures": [{"id": asset["id"], "key": asset["key"], "url": f"{prefix}/{asset['key']}.glb"} for asset in revision.get("surface_assets", [])],
        }
        return JSONResponse(result, headers={"Cache-Control": "private, no-store"}) if revision_id.startswith("review-") else result

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/surfaces/{key}.glb")
    def surface(case_id: str, revision_id: str, key: str, request: Request):
        from .surfaces import surface_asset_path

        revision = service.ct_revision(case_id, revision_id)
        if key not in {asset["key"] for asset in revision.get("surface_assets", [])}:
            raise NotFound("Surface does not belong to this revision")
        target = surface_asset_path(service.surface_cache, key)
        if not target.is_file():
            raise HTTPException(503, "Cached surface is unavailable; regenerate the model")
        return _glb_response(target, request, "private, max-age=31536000, immutable")

    review_cache = service.settings.data_dir / "review-cache"

    def review_ct(case_id, revision_id, quality="full"):
        from .review_volume import scalar_volume

        revision = service.ct_revision(case_id, revision_id)
        asset = service.store.resolve_reference(revision)
        descriptor = scalar_volume(asset["file_path"], review_cache, quality, presentation=asset["presentation"], reference_image_id=asset["id"])
        return with_image_context(descriptor, asset)

    def review_mask(case_id, revision_id, structure_id, parent=False, quality="full", margin_mm=0):
        from .review_volume import mask_volume

        revision = service.ct_revision(case_id, revision_id)
        if structure_id not in {s["id"] for s in revision["structures"]}:
            raise ValueError("Unknown structure")
        selected = revision
        if parent:
            if not revision.get("parent_id"):
                return None
            selected = service.ct_revision(case_id, revision["parent_id"])
        structure = next((s for s in selected["structures"] if s["id"] == structure_id), None)
        if structure is None:
            return None
        asset = service.store.resolve_reference(revision)
        selected_asset = service.store.resolve_reference(selected)
        if selected_asset["id"] != asset["id"]:
            raise ValueError("Comparison masks must belong to the same reference image")
        descriptor = mask_volume(asset["file_path"], structure["mask_file"], review_cache, quality, margin_mm=margin_mm, reference_image_id=asset["id"])
        return with_image_context(descriptor, asset)

    def review_volume_mask(case_id, revision_id, structure_ids, margin_mm=0, cancelled=None):
        from .volume_mask_union import volume_mask_union

        revision = service.ct_revision(case_id, revision_id)
        asset = service.store.resolve_reference(revision)
        selected_ids = sorted(set(structure_ids))
        if not selected_ids:
            raise ValueError("Select at least one structure for volume masking")
        structures = {structure["id"]: structure for structure in revision["structures"]}
        if any(structure_id not in structures for structure_id in selected_ids):
            raise ValueError("Unknown structure")
        descriptor = volume_mask_union(
            asset["file_path"],
            [(structure_id, structures[structure_id]["mask_file"]) for structure_id in selected_ids],
            review_cache,
            margin_mm,
            reference_image_id=asset["id"],
            cancelled=cancelled,
        )
        return with_image_context(descriptor, asset)

    def volume_chunk(descriptor, key, index):
        from .review_volume import chunk_path

        if descriptor is None:
            raise NotFound("Review volume does not belong to this revision")
        return FileResponse(
            chunk_path(review_cache, descriptor, key, index),
            media_type="application/octet-stream",
            headers={
                "Content-Encoding": "gzip",
                "Vary": "Accept-Encoding",
                "Cache-Control": "private, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/ct-volume")
    def ct_volume_manifest(case_id: str, revision_id: str, quality: VolumeQuality = VolumeQuality.FULL):
        descriptor = review_ct(case_id, revision_id, quality)
        prefix = f"/api/cases/{case_id}/revisions/{revision_id}/ct-volume"
        return JSONResponse(with_chunk_urls(descriptor, prefix, f"?quality={quality}"), headers={"Cache-Control": "private, no-store"})

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/ct-volume/{key}/chunks/{index}")
    def ct_volume_chunk(case_id: str, revision_id: str, key: str, index: int, quality: VolumeQuality = VolumeQuality.FULL):
        return volume_chunk(review_ct(case_id, revision_id, quality), key, index)

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/image-volume")
    def image_volume_manifest(case_id: str, revision_id: str, quality: VolumeQuality = VolumeQuality.FULL):
        descriptor = review_ct(case_id, revision_id, quality)
        prefix = f"/api/cases/{case_id}/revisions/{revision_id}/image-volume"
        return JSONResponse(with_chunk_urls(descriptor, prefix, f"?quality={quality}"), headers={"Cache-Control": "private, no-store"})

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/image-volume/{key}/chunks/{index}")
    def image_volume_chunk(case_id: str, revision_id: str, key: str, index: int, quality: VolumeQuality = VolumeQuality.FULL):
        return volume_chunk(review_ct(case_id, revision_id, quality), key, index)

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/mask-volume")
    def mask_volume_manifest(
        case_id: str,
        revision_id: str,
        structure_id: str,
        compare: bool = False,
        quality: VolumeQuality = VolumeQuality.FULL,
        margin_mm: float = Query(0, ge=0, le=5),
    ):
        from urllib.parse import urlencode

        prefix = f"/api/cases/{case_id}/revisions/{revision_id}/mask-volume"
        current = review_mask(case_id, revision_id, structure_id, quality=quality, margin_mm=margin_mm)
        parent = review_mask(case_id, revision_id, structure_id, True, quality, margin_mm) if compare else None

        def descriptor(value, is_parent):
            query = "?" + urlencode(
                {"structure_id": structure_id, "parent": str(is_parent).lower(), "quality": quality, **({"margin_mm": margin_mm} if margin_mm else {})}
            )
            return with_chunk_urls(value, prefix, query) if value else None

        return JSONResponse(
            {"schema_version": 1, "current": descriptor(current, False), "parent": descriptor(parent, True)}, headers={"Cache-Control": "private, no-store"}
        )

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/mask-volume/{key}/chunks/{index}")
    def mask_volume_chunk(
        case_id: str,
        revision_id: str,
        key: str,
        index: int,
        structure_id: str,
        parent: bool = False,
        quality: VolumeQuality = VolumeQuality.FULL,
        margin_mm: float = Query(0, ge=0, le=5),
    ):
        return volume_chunk(review_mask(case_id, revision_id, structure_id, parent, quality, margin_mm), key, index)

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/volume-mask")
    async def volume_mask_manifest(
        request: Request, case_id: str, revision_id: str, structure_ids: list[str] = Query(default=[]), margin_mm: float = Query(0, ge=0, le=5)
    ):
        from urllib.parse import urlencode
        import asyncio
        from .review_volume import display_margin

        cancelled = threading.Event()
        work = asyncio.create_task(asyncio.to_thread(review_volume_mask, case_id, revision_id, structure_ids, margin_mm, cancelled.is_set))
        try:
            while not work.done():
                await asyncio.wait({work}, timeout=0.1)
                if not work.done() and await request.is_disconnected():
                    raise HTTPException(499, "Volume mask request cancelled")
            current = await work
        finally:
            cancelled.set()
            if not work.done():
                # A native decode already in progress finishes into its reusable
                # cache; the cooperative union runner then stops before publish.
                work.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        prefix = f"/api/cases/{case_id}/revisions/{revision_id}/volume-mask"
        query = "?" + urlencode([*(("structure_ids", value) for value in sorted(set(structure_ids))), ("margin_mm", display_margin(margin_mm))])
        return JSONResponse(
            {"schema_version": 1, "source_id": f"volume-mask-union:{current['key']}", "current": with_chunk_urls(current, prefix, query)},
            headers={"Cache-Control": "private, no-store"},
        )

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/volume-mask/{key}/chunks/{index}")
    def volume_mask_chunk(
        case_id: str, revision_id: str, key: str, index: int, structure_ids: list[str] = Query(default=[]), margin_mm: float = Query(0, ge=0, le=5)
    ):
        return volume_chunk(review_volume_mask(case_id, revision_id, structure_ids, margin_mm), key, index)

    frame_cache = _FrameCache()

    def cached_review_frame(ct, mask, parent, plane, position_mm, overlays=None, native_axis=None, native_index=None, background_value=-1024.0):
        from .review_volume import review_frame

        key = (ct, mask, parent, plane, position_mm, overlays, native_axis, native_index, background_value)
        return frame_cache.get_or_create(
            key,
            lambda: review_frame(
                Path(ct[0]),
                Path(mask[0]) if mask else None,
                plane,
                position_mm,
                Path(parent[0]) if parent else None,
                overlays=None
                if overlays is None
                else tuple((structure_id, Path(current[0]), Path(previous[0]) if previous else None) for structure_id, current, previous in overlays),
                native_axis=native_axis,
                native_index=native_index,
                background_value=background_value,
            ),
        )

    def selected_slice_masks(case_id, revision, structure_id, structure_ids, compare):
        requested = ([structure_id] if structure_id else []) + (structure_ids or [])
        ids = tuple(dict.fromkeys(requested))
        if len(ids) > 128:
            raise ValueError("Select at most 128 structures for image review")
        structures = {s["id"]: s for s in revision["structures"]}
        # Check every selected id before stat'ing or decompressing any mask.
        if any(structure_id not in structures for structure_id in ids):
            raise ValueError("Unknown structure")
        previous = {}
        if compare and ids and revision.get("parent_id"):
            parent = service.ct_revision(case_id, revision["parent_id"])
            if service.store.resolve_reference(parent)["id"] != service.store.resolve_reference(revision)["id"]:
                raise ValueError("Comparison masks must belong to the same reference image")
            previous = {s["id"]: s for s in parent["structures"]}
        return tuple((structures[structure_id], previous.get(structure_id)) for structure_id in ids)

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/review-frame")
    def raw_review_frame(
        case_id: str,
        revision_id: str,
        plane: str = "axial",
        position_mm: float = 0,
        structure_id: str | None = None,
        compare: bool = False,
        structure_ids: list[str] | None = Query(None),
        native_axis: int | None = None,
        native_index: int | None = None,
    ):
        revision = service.ct_revision(case_id, revision_id)
        asset = service.store.resolve_reference(revision)
        selected = selected_slice_masks(case_id, revision, structure_id, structure_ids, compare)
        structure, previous = selected[0] if selected else (None, None)
        overlays = (
            None
            if structure_ids is None
            else tuple((current["id"], slice_file_key(current["mask_file"]), slice_file_key(old["mask_file"]) if old else None) for current, old in selected)
        )
        result = cached_review_frame(
            slice_file_key(revision["ct_file"]),
            slice_file_key(structure["mask_file"]) if structure else None,
            slice_file_key(previous["mask_file"]) if previous else None,
            plane,
            position_mm,
            overlays,
            native_axis,
            native_index,
            asset["presentation"]["background_value"],
        )
        return Response(
            result["body"],
            media_type="application/octet-stream",
            headers={
                "Content-Encoding": "gzip",
                "Vary": "Accept-Encoding",
                "Cache-Control": "private, no-store",
                "X-CT-Frame": json.dumps(with_image_context(result["frame"], asset), separators=(",", ":"), allow_nan=False),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/probe")
    def probe(case_id: str, revision_id: str, x: float, y: float, z: float):
        from . import imaging

        revision = service.ct_revision(case_id, revision_id)
        asset = service.store.resolve_reference(revision)
        sample = imaging.probe(Path(asset["file_path"]), [x, y, z])
        return {**sample, "value": sample["hu"], "units": asset["scalar"]["units"], "reference_image_id": asset["id"]}

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/slicer.zip")
    def slicer_bundle(case_id: str, revision_id: str):
        from . import slicer_io

        revision = service.store.revision(case_id, revision_id)
        if not revision["structures"]:
            raise ValueError("Run segmentation before exporting a Slicer segmentation bundle")
        target = Path(revision["directory"]) / "slicer.zip"
        slicer_io.export_bundle(Path(revision["ct_file"]), revision["structures"], target)
        return FileResponse(target, filename=f"Scan3D-{case_id[:8]}-Slicer.zip")

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/export.html")
    def export(case_id: str, revision_id: str):
        return portable_export(case_id, revision_id, ExportRequest(), legacy=True)

    @app.post("/api/cases/{case_id}/revisions/{revision_id}/export.html")
    def export_layers(case_id: str, revision_id: str, body: ExportRequest):
        return portable_export(case_id, revision_id, body)

    def portable_export(case_id: str, revision_id: str, body: ExportRequest, *, legacy=False):
        from .portable_export import ExportTooLarge, prepare_export

        revision = service.store.revision(case_id, revision_id)
        try:
            plan = prepare_export(revision, case_id, service.settings, body, legacy=legacy)
        except ExportTooLarge as exc:
            raise HTTPException(413, str(exc)) from None
        except FileNotFoundError:
            raise HTTPException(503, "A required viewer artifact is unavailable; prepare the case and build the portable viewer before exporting") from None
        return StreamingResponse(
            plan.chunks(),
            media_type="text/html",
            headers={
                "Content-Disposition": f'attachment; filename="Scan3D-{case_id[:8]}.html"',
                "Content-Length": str(plan.byte_length),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

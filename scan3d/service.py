"""Persistent import, processing and revision orchestration shared by HTTP and CLI."""

import hashlib
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import __version__, inference_paths
from .accounts import Accounts
from .config import Settings
from .fileio import atomic_json, atomic_text, code_fingerprint, extract_zip
from .store import LANES, Store, identifier, job_lane, now

log = logging.getLogger(__name__)


class Service:
    def __init__(self, settings=None):
        self.settings = settings or Settings.from_env()
        self.store = Store(self.settings.data_dir)
        self.accounts = Accounts(self.store)
        self.stop_event = threading.Event()
        self.threads = {}
        self.worker_lock = None
        self.interactive_runtime = None
        self._reviews = None
        self._orthanc = None
        self._orthanc_exports = None
        self._orthanc_view_exports = None
        self._lane_guard = threading.Lock()
        self._running_lanes = 0
        self._preparation_slots = None

    @property
    def thread(self):
        """The GPU lane's thread; health checks also report the CPU lane."""
        return self.threads.get("gpu")

    @property
    def reviews(self):
        if self._reviews is None:
            from .review import ReviewManager

            self._reviews = ReviewManager(self)
        return self._reviews

    @property
    def orthanc(self):
        if self._orthanc is None:
            from .orthanc_imports import OrthancImports

            self._orthanc = OrthancImports(self)
        return self._orthanc

    @property
    def orthanc_exports(self):
        if self._orthanc_exports is None:
            from .orthanc_exports import OrthancExports

            self._orthanc_exports = OrthancExports(self)
        return self._orthanc_exports

    @property
    def orthanc_view_exports(self):
        if self._orthanc_view_exports is None:
            from .orthanc_view_exports import OrthancViewExports

            self._orthanc_view_exports = OrthancViewExports(self)
        return self._orthanc_view_exports

    def _interactive(self):
        if self.interactive_runtime is None:
            # The warm engine runs in a supervised child process that the
            # first prediction starts; this process never imports torch.
            from .interactive_worker import InteractiveWorker

            self.interactive_runtime = InteractiveWorker()
        return self.interactive_runtime

    def _release_interactive(self):
        runtime, self.interactive_runtime = self.interactive_runtime, None
        if runtime is not None:
            runtime.release()

    def start(self, lanes=LANES):
        """Run one thread per lane while this process holds the data-dir lock.

        The GPU lane runs TotalSegmentator and nnInteractive one job at a time;
        the CPU lane runs work that never touches the GPU. A CLI command starts
        only the lane its job needs, so it never begins unrelated work there.
        """
        if any(thread.is_alive() for thread in self.threads.values()):
            return
        lock = (self.settings.data_dir / "worker.lock").open("a")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return
        self.worker_lock = lock
        self.store.recover()
        self.orthanc.recover()
        self.orthanc_exports.recover()
        self.orthanc_view_exports.recover()
        self.stop_event.clear()
        self._running_lanes = len(lanes)
        self.threads = {lane: threading.Thread(target=self._loop, args=(lane,), daemon=True, name=f"scan3d-{lane}-worker") for lane in lanes}
        for thread in self.threads.values():
            thread.start()

    def stop(self):
        self.stop_event.set()
        # Running jobs observe the stop as a cancellation; one deadline bounds
        # the whole shutdown rather than each lane in turn.
        deadline = time.monotonic() + 15
        for thread in self.threads.values():
            thread.join(timeout=max(0, deadline - time.monotonic()))

    def _loop(self, lane):
        try:
            while not self.stop_event.is_set():
                try:
                    job = self.store.claim(lane)
                    if job:
                        self.execute(job)
                    else:
                        self.stop_event.wait(0.25)
                except Exception:
                    log.exception("%s worker iteration failed", lane.upper())
                    self.stop_event.wait(1)
        finally:
            if lane == "gpu":
                try:
                    self._release_interactive()
                except Exception:
                    log.exception("Could not release the interactive inference runtime")
            with self._lane_guard:
                self._running_lanes -= 1
                last = not self._running_lanes
            # Hold the lock until every lane has finished its job, so no other
            # process can start workers or recover a job still running here.
            if last and self.worker_lock:
                self.worker_lock.close()
                self.worker_lock = None

    def new_case(self, label=None):
        record = dict(
            id=identifier(),
            label=label or "New imaging case",
            created_at=now(),
            status="importing",
            active_revision_id=None,
            selected_series_uid=None,
            phase=None,
            series=[],
            references={},
        )
        return self.store.create_case(record)

    def finish_import(self, case, source):
        from .dicom import index_dicom

        series = index_dicom(source)
        if not series:
            raise ValueError("No supported DICOM image series found")
        case.update(series=series, source_dir=str(source), status="imported")
        return self.store.save("cases", case)

    def import_local(self, path, label=None):
        path = self.settings.import_path(path)
        data_dir = self.settings.data_dir.resolve()
        # From inside it, an import would copy another case's scans into a new case.
        if path == data_dir or data_dir in path.parents or path in data_dir.parents:
            raise ValueError("Select a scan outside the application's data directory")
        case = self.new_case(label)
        destination = self.store.case_dir(case["id"]) / "dicom"
        try:
            if path.is_dir():
                # Copy input files so subsequent external moves or edits cannot change a case.
                total = 0
                for file in path.rglob("*"):
                    if file.is_symlink():
                        raise ValueError("Local imports cannot contain symlinks")
                    if not file.is_file():
                        continue
                    total += file.stat().st_size
                    if total > self.settings.upload_limit:
                        raise ValueError("Import exceeds size limit")
                    output = destination / file.relative_to(path)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, output)
            elif zipfile.is_zipfile(path):
                destination.mkdir(parents=True, exist_ok=True)
                extract_zip(path, destination, self.settings.upload_limit)
            else:
                raise ValueError("Select a DICOM directory or ZIP archive")
            return self.finish_import(case, destination)
        except Exception as exc:
            case.update(status="import_failed", error=str(exc))
            self.store.save("cases", case)
            raise

    def process(self, case_id, series_uid, profile, phase, *, cardiac_phase=None, rr_percent=None):
        """Prepare a reference image for viewing, without running segmentation."""
        from .acquisition import acquisition_labels

        case = self.store.get("cases", case_id)
        selected = next((s for s in case["series"] if s["uid"] == series_uid), None)
        if not selected:
            raise ValueError("Select a series belonging to this case")
        if selected.get("supported") is False:
            raise ValueError(selected.get("unsupported_reason") or "This series is unavailable for volume preparation")
        if reason := self.settings.modality_unavailable(selected.get("modality", "CT")):
            raise ValueError(reason)
        if selected.get("modality", "CT") != "CT" and (cardiac_phase is not None or rr_percent is not None):
            raise ValueError("Cardiac CT timing does not apply to a static MR volume")
        acquisition = acquisition_labels(series_uid, profile, phase, cardiac_phase=cardiac_phase, rr_percent=rr_percent)
        return self.store.enqueue(case_id, "process", acquisition)

    def segment_case(self, case_id, revision_id, task=None, robust_crop=False, body_seg=False, remove_small_blobs=False, inference_path=None):
        from .segmentation_options import default_task, normalize_options

        parent = self.store.revision(case_id, revision_id)
        if self.store.get("cases", case_id).get("active_revision_id") != revision_id:
            raise ValueError("The active revision changed; start segmentation from the current image")
        acquisition = parent.get("acquisition") or {}
        profile = acquisition.get("profile", "chest_abdomen")
        reference = self.store.resolve_reference(parent)
        if "segmentation" not in reference["capabilities"]:
            raise ValueError("This source image does not support segmentation")
        if reason := self.settings.modality_unavailable(reference["modality"]):
            raise ValueError(reason)
        options = normalize_options(
            profile,
            dict(tasks=[task or default_task(reference["modality"])], robust_crop=robust_crop, body_seg=body_seg, remove_small_blobs=remove_small_blobs),
            modality=reference["modality"],
        )
        # Top-level and public, so the card is still named once a failure overwrites the stage.
        fields = {} if inference_path is None else {"inference_path": inference_paths.binding(inference_path)}
        return self.store.enqueue(case_id, "segment", dict(revision_id=revision_id, options=options, reference_image_id=reference["id"]), **fields)

    def include_all_outputs(self, case_id):
        """Restore saved model outputs omitted by the former anatomy filter."""
        case = self.store.get("cases", case_id)
        if not case.get("active_revision_id"):
            raise ValueError("The case has no completed segmentation")
        return self.store.enqueue(case_id, "include_all", {"revision_id": case["active_revision_id"]})

    def cancelled(self, job):
        return self.stop_event.is_set() or self.store.get("jobs", job["id"]).get("cancel_requested", False)

    def check_cancel(self, job):
        if self.cancelled(job):
            raise InterruptedError("Processing cancelled")

    def stage(self, job, name):
        self.check_cancel(job)
        self.store.update_job(job["id"], stage=name)

    @property
    def surface_cache(self):
        return self.settings.data_dir / "surface-cache"

    def ct_revision(self, case_id, revision_id):
        # A candidate is a read-only CT context, never an accepted revision or
        # a new CT volume. Its immutable generation authorizes the mask paths.
        if revision_id.startswith("review-"):
            return self.reviews.candidate_revision(case_id, revision_id)
        return self.store.revision(case_id, revision_id)

    def _analyze(self, ct, structure, geometry, on_decoded=None):
        """Reuse exact measurements only for identical mask bytes and CT grids."""
        from . import imaging, surfaces

        identity = {
            "mask": surfaces.hash_file(Path(structure["mask_file"])),
            "geometry": geometry,
            "implementation": hashlib.sha256(Path(imaging.__file__).read_bytes()).hexdigest(),
        }
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        path = self.settings.data_dir / "analysis-cache" / key[:2] / f"{key}.json"
        required = {"volume_ml", "voxel_count", "centroid_ras", "bounds_ras", "qc"}
        try:
            record = json.loads(path.read_text())
            analysis = record["analysis"]
            digest = hashlib.sha256(json.dumps(analysis, sort_keys=True, allow_nan=False).encode()).hexdigest()
            if isinstance(analysis, dict) and record["key"] == key and record["sha256"] == digest and required <= analysis.keys():
                return analysis, True
        except (OSError, ValueError, KeyError, TypeError):
            pass
        reference, affine = imaging._image(ct)
        decoded = imaging._binary(Path(structure["mask_file"]), reference, affine)
        analysis = imaging.analyze_mask(ct, Path(structure["mask_file"]), decoded_mask=decoded)
        atomic_json(
            path, {"key": key, "analysis": analysis, "sha256": hashlib.sha256(json.dumps(analysis, sort_keys=True, allow_nan=False).encode()).hexdigest()}
        )
        if on_decoded:
            on_decoded(decoded)
        return analysis, False

    @staticmethod
    def _requested_workers():
        requested = int(os.environ.get("SCAN3D_POSTPROCESS_WORKERS", "4"))
        if requested < 1:
            raise ValueError("SCAN3D_POSTPROCESS_WORKERS must be positive")
        return min(requested, os.cpu_count() or 1)

    def _preparation_budget(self):
        """Both lanes, and API demo builds, can prepare revisions at once.

        Their mask workers share one service-wide budget, so concurrent jobs
        never hold more dense-mask workspaces or native CPU threads together
        than one job could before.
        """
        with self._lane_guard:
            if self._preparation_slots is None:
                self._preparation_slots = threading.BoundedSemaphore(self._requested_workers())
            return self._preparation_slots

    @staticmethod
    def _preparation_workers(geometry):
        requested = Service._requested_workers()
        available = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
                    break
        except OSError:
            pass
        # Full masks, decompression/copies and foreground/QC workspaces coexist.
        # Reserve most RAM for the CT cache, API, browser and inference process.
        voxels = 1
        for extent in geometry["shape"]:
            voxels *= extent
        memory_limit = max(1, available // max(1, voxels * 12 * 4))
        return max(1, min(requested, memory_limit))

    def execute(self, job):
        try:
            if job["kind"] == "orthanc_import":
                self.orthanc.run(job)
                return
            elif job["kind"] == "orthanc_export":
                self.orthanc_exports.run(job)
                return
            elif job["kind"] == "orthanc_view_export":
                self.orthanc_view_exports.run(job)
                return
            elif job["kind"] == "process":
                revision = self._process(job)
            elif job["kind"] == "segment":
                # Stop the warm review worker, returning its VRAM, only when
                # TotalSegmentator inference is requested.
                self._release_interactive()
                revision = self._segment(job)
            elif job["kind"] == "review_predict":
                # The lane decision also decides runtime use: CPU work gets none.
                self.reviews.run_predict(job, self._interactive() if job_lane(job) == "gpu" else None)
                return
            elif job["kind"] == "review_accept":
                revision = self.reviews.run_accept(job)
            elif job["kind"] == "refine":
                revision = self._refine(job)
            elif job["kind"] == "correction":
                revision = self._correct(job)
            elif job["kind"] == "dicom_seg_import":
                from .dicom_seg_jobs import run_import

                revision = run_import(self, job)
            elif job["kind"] == "include_all":
                revision = self._include_all(job)
            else:
                raise ValueError("Unknown job kind")
            self.check_cancel(job)
            expected = job["payload"]["revision_id"] if job["kind"] in ("segment", "include_all", "review_accept", "dicom_seg_import") else None
            review_guard = {key: job["payload"][key] for key in ("session_id", "generation")} if job["kind"] == "review_accept" else None
            self.store.commit_revision(revision, job, expected_active_revision_id=expected, review_guard=review_guard)
        except InterruptedError as exc:
            self.store.update_job(job["id"], status="cancelled", stage="Cancelled", error=str(exc))
            if job["kind"].startswith("review_"):
                self.reviews.get(job["case_id"], job["payload"]["session_id"])
        except Exception as exc:
            log.exception("Job %s failed", job["id"])
            # A named path's interpreter and environment stay in the journal, out of public text.
            error = inference_paths.public_error(exc) if job.get("inference_path") else str(exc)
            self.store.update_job(job["id"], status="failed", stage="Failed", error=error)
            if job["kind"].startswith("review_"):
                self.reviews.get(job["case_id"], job["payload"]["session_id"])

    def _process(self, job):
        from . import dicom

        case = self.store.get("cases", job["case_id"])
        series = next(s for s in case["series"] if s["uid"] == job["payload"]["series_uid"])
        if series.get("supported") is False:
            raise ValueError(series.get("unsupported_reason") or "This series is unavailable for volume preparation")
        if reason := self.settings.modality_unavailable(series.get("modality", "CT")):
            raise ValueError(reason)
        case_dir = self.store.case_dir(case["id"])
        self.stage(job, "Checking and converting selected DICOM series")
        digest = hashlib.sha256()
        for name in sorted(series["files"]):
            with Path(name).open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
        conversion = case_dir / "converted" / digest.hexdigest()
        ct = conversion / "reference.nii.gz"
        if not ct.is_file():
            conversion.mkdir(parents=True, exist_ok=True)
            ct = dicom.convert_series(series["files"], conversion)
        self.check_cancel(job)
        modality = series.get("modality", "CT")
        self.stage(job, f"Preparing {modality} for viewing; segmentation has not been requested")
        image_source = {key: series[key] for key in ("study_uid", "frame_of_reference_uid", "sop_class_uids") if key in series}
        archive = case.get("import_source")
        if archive and archive.get("series_uid") == series["uid"]:
            image_source.update(
                source_archive_key=archive["source_key"], source_content_sha256=archive["content_digest"], source_manifest_sha256=archive["manifest_sha256"]
            )
        return self.build_ct_revision(
            case["id"],
            ct,
            acquisition=job["payload"],
            image_source=image_source,
            modality=modality,
            presentation_metadata=series,
            frame_selector=series.get("frame_selector"),
        )

    def _reference_for_build(
        self, case_id, image_file, geometry, *, parent=None, acquisition=None, image_source=None, frame_selector=None, modality=None, presentation_metadata=None
    ):
        from .image_assets import create_image_asset, assert_reference_image

        if parent:
            previous = self.store.revision(case_id, parent)
            asset = self.store.resolve_reference(previous)
            if modality is not None and modality != asset["modality"]:
                raise ValueError("A derived revision must keep its reference image modality")
            assert_reference_image(asset, image_file, geometry)
            series_uid = (acquisition or {}).get("series_uid")
            if series_uid is not None and series_uid != asset["source"].get("series_uid"):
                raise ValueError("A derived revision must keep its reference image series")
            if frame_selector is not None and frame_selector != asset["frame_selector"]:
                raise ValueError("A derived revision must keep its reference image frame")
            if image_source is not None:
                requested = create_image_asset(
                    case_id,
                    image_file,
                    acquisition=acquisition,
                    source=image_source,
                    frame_selector=asset["frame_selector"],
                    modality=asset["modality"],
                    presentation_metadata=presentation_metadata,
                )
                if requested["source"] != asset["source"]:
                    raise ValueError("A derived revision must keep its reference image source")
        else:
            asset = create_image_asset(
                case_id,
                image_file,
                acquisition=acquisition,
                source=image_source,
                frame_selector=frame_selector,
                modality=modality or "CT",
                presentation_metadata=presentation_metadata,
            )
            try:
                existing = self.store.get("image_assets", asset["id"])
            except KeyError:
                pass
            else:
                if existing["case_id"] != case_id:
                    raise ValueError("Reference image belongs to another case")
                # Immutable presentation metadata belongs to the source asset;
                # evolving adapter defaults apply only to newly seen images.
                asset = assert_reference_image(existing, image_file, geometry)
        return self.store.register_image_asset(asset)

    def build_ct_revision(self, case_id, ct, acquisition=None, *, image_source=None, frame_selector=None, modality="CT", presentation_metadata=None):
        """A scalar image revision; the method name remains a CT compatibility alias."""
        from . import imaging

        geometry = imaging.geometry(ct)
        asset = self._reference_for_build(
            case_id,
            ct,
            geometry,
            acquisition=acquisition,
            image_source=image_source,
            frame_selector=frame_selector,
            modality=modality,
            presentation_metadata=presentation_metadata,
        )
        from .image_assets import public_image_asset

        revision_id = identifier()
        directory = self.store.case_dir(case_id) / "revisions" / revision_id
        directory.mkdir(parents=True, exist_ok=True)
        result = dict(
            id=revision_id,
            case_id=case_id,
            parent_id=None,
            kind="ct" if asset["modality"] == "CT" else "image",
            created_at=now(),
            review_status="unreviewed",
            source="dicom",
            operation=None,
            geometry=geometry,
            structures=[],
            qc=[],
            directory=str(directory),
            ct_file=str(ct),
            acquisition=acquisition,
            reference_image_id=asset["id"],
            image_asset=public_image_asset(asset),
            synthetic=bool(self.store.get("cases", case_id).get("synthetic")),
            model_url=None,
            model_manifest_url=None,
            surface_assets=[],
            mesh_statistics={},
            pipeline_version=__version__,
            pipeline_code_sha256=code_fingerprint(),
            inference_manifest=None,
        )
        atomic_json(directory / "revision.json", result)
        return result

    def build_image_revision(self, case_id, image_file, acquisition=None, **metadata):
        return self.build_ct_revision(case_id, image_file, acquisition=acquisition, **metadata)

    def _segment(self, job):
        from . import segmentation
        from .segmentation_options import normalize_options, run_source

        parent = self.store.revision(job["case_id"], job["payload"]["revision_id"])
        if self.store.get("cases", job["case_id"]).get("active_revision_id") != parent["id"]:
            raise ValueError("The active revision changed; start segmentation from the current image")
        ct = Path(parent["ct_file"])
        acquisition = parent.get("acquisition") or {}
        profile = acquisition.get("profile", "chest_abdomen")
        reference = self.store.resolve_reference(parent)
        if "segmentation" not in reference["capabilities"]:
            raise ValueError("This source image does not support segmentation")
        if job["payload"].get("reference_image_id", reference["id"]) != reference["id"]:
            raise ValueError("Segmentation job belongs to another reference image")
        options = normalize_options(profile, job["payload"]["options"], modality=reference["modality"])
        case_dir = self.store.case_dir(job["case_id"])
        self.stage(job, "Checking selected task, settings and reusable model outputs")
        source_options = {"modality": reference["modality"]} if reference["modality"] != "CT" else {}
        # Before the lookup, because a hit never reaches the device check. The default path passes
        # no keyword, so what it fingerprints and runs is what it always was.
        if inference_path := inference_paths.require(job.get("inference_path")):
            source_options["inference_path"] = inference_path
        fingerprint = segmentation.model_fingerprint(profile, options=options, **source_options)
        cache_parameters = {
            "input_sha256": segmentation._digest(ct),
            "profile": profile,
            "options": options,
            "versions": fingerprint["versions"],
            "device": fingerprint["device_request"],
            "wrapper_sha256": fingerprint["wrapper_sha256"],
            "pipeline_version": __version__,
        }
        key = hashlib.sha256(json.dumps(cache_parameters, sort_keys=True).encode()).hexdigest()
        inference = case_dir / "inference" / key
        cached = inference / "complete.json"
        run_manifest = {}
        if cached.exists():
            try:
                cache_record = json.loads(cached.read_text())
                structures = cache_record["structures"]
                if (
                    not structures
                    or not all(Path(s["mask_file"]).is_file() for s in structures)
                    or not fingerprint["weights_complete"]
                    or cache_record["model_fingerprint"]["digest"] != fingerprint["digest"]
                ):
                    cached.unlink()
                else:
                    run_manifest = cache_record["manifest"]
            except (ValueError, KeyError, TypeError):
                cached.unlink()
        reused = cached.exists()
        if not reused:
            inference.mkdir(parents=True, exist_ok=True)
            structures = segmentation.segment(
                ct, inference, profile, lambda name: self.stage(job, name), lambda: self.cancelled(job), options=options, **source_options
            )
            self.check_cancel(job)
            run_manifest = json.loads((inference / "inference.json").read_text())
            atomic_text(cached, json.dumps({"structures": structures, "model_fingerprint": run_manifest["model_fingerprint"], "manifest": run_manifest}))
        self.stage(job, "Checking masks and generating surfaces")
        from .segmentation_merge import merge_task_outputs

        merged, replacement = merge_task_outputs(parent["structures"], structures, options["tasks"], reference_image_id=reference["id"])
        operation = {"type": "segment", "input_revision_id": parent["id"], "options": options, **replacement}
        if bound := job.get("inference_path"):
            operation["runtime"] = self._run_record(bound, run_manifest, reused)
        result = self.build_revision(
            job["case_id"],
            ct,
            merged,
            source=run_source(options),
            parent=parent["id"],
            acquisition=acquisition,
            job=job,
            extra_qc=run_manifest.get("qc", []),
            operation=operation,
            inference_manifest=run_manifest,
        )
        result["segmentation_settings"] = options
        result["segmentation_runs"] = [
            *parent.get("segmentation_runs", []),
            {
                "tasks": options["tasks"],
                "options": options,
                "input_revision_id": parent["id"],
                **{key: run_manifest[key] for key in ("run_id", "engine_version", "started_at", "finished_at") if key in run_manifest},
                "model_fingerprint_digest": run_manifest.get("model_fingerprint", {}).get("digest"),
            },
        ]
        atomic_json(Path(result["directory"]) / "revision.json", result)
        return result

    @staticmethod
    def _run_record(bound, manifest, reused):
        """What ran a segment revision, publicly, under nnInteractive's provenance names.

        It sits on the operation because children do not inherit that, and a later revision can
        merge masks from several runs. The manifest stays as the run left it, because
        include-all compares it with the run's own file. On a reused result the values are the
        original run's, which `reused` explains.
        """
        measured = manifest.get("runtime", {})
        return {
            "inference_path": bound["id"],
            "gpu_name": measured.get("gpu_name"),
            "device": measured.get("selected_device"),
            "torch_version": measured.get("torch"),
            "hip_version": measured.get("hip"),
            "cuda_version": measured.get("cuda"),
            "reused": reused,
            "results_moved_to_cpu": any(task.get("results_moved_to_cpu") for task in manifest.get("tasks", [])),
        }

    def build_revision(
        self,
        case_id,
        ct,
        structures,
        source,
        parent=None,
        operation=None,
        directory=None,
        acquisition=None,
        job=None,
        extra_qc=None,
        inference_manifest=None,
        *,
        image_source=None,
        frame_selector=None,
    ):
        from . import imaging, surfaces
        from .image_assets import bind_structures, public_image_asset

        geometry = imaging.geometry(ct)
        asset = self._reference_for_build(
            case_id, ct, geometry, parent=parent, acquisition=acquisition, image_source=image_source, frame_selector=frame_selector
        )
        structures = bind_structures(structures, asset["id"])
        revision_id = identifier()
        directory = directory or self.store.case_dir(case_id) / "revisions" / revision_id
        directory.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        analysis_hits = 0
        measurement_timings = {}
        reviewed_structures = [None] * len(structures)
        prepared_surfaces = [None] * len(structures)
        workers = min(len(structures), self._preparation_workers(geometry))
        if not workers:
            raise ValueError("No anatomical masks were produced")
        if job:
            self.store.update_job(job["id"], measurements=[], progress={"phase": "measurements", "completed": 0, "total": len(structures)})

        def prepare(index, structure):
            if job:
                self.check_cancel(job)
            entry = dict(structure)
            measured = time.perf_counter()
            surface = None
            surface_seconds = 0.0

            def prepare_decoded(decoded):
                nonlocal surface, surface_seconds
                surface_start = time.perf_counter()
                surface = surfaces.prepare_surface(
                    entry, self.surface_cache, decoded_mask=decoded, progress=(lambda _: self.check_cancel(job)) if job else None
                )
                surface_seconds += time.perf_counter() - surface_start

            if "volume_ml" not in entry:
                analysis, hit = self._analyze(ct, entry, geometry, on_decoded=prepare_decoded)
                entry.update(analysis)
            else:
                hit = True
            analysis_seconds = time.perf_counter() - measured - surface_seconds
            if surface is None:
                surface = surfaces.prepare_surface(entry, self.surface_cache, progress=(lambda _: self.check_cancel(job)) if job else None)
            entry.setdefault("qc", [])
            return index, entry, hit, analysis_seconds, surface

        slots = self._preparation_budget()

        def budgeted(index, structure):
            with slots:
                return prepare(index, structure)

        # Limit both concurrent dense-mask workspaces and native CPU work. A
        # decoded mask feeds QC and contour extraction in the same worker.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan3d-mask") as executor:
            futures = [executor.submit(budgeted, index, structure) for index, structure in enumerate(structures)]
            try:
                for completed_count, future in enumerate(as_completed(futures), 1):
                    index, entry, hit, elapsed, surface = future.result()
                    reviewed_structures[index] = entry
                    prepared_surfaces[index] = surface
                    analysis_hits += int(hit)
                    measurement_timings[entry["id"]] = elapsed
                    if job:
                        self.stage(job, f"Checked {completed_count}/{len(structures)} structures ({entry['name']}); preparing display surfaces")
                        measurements = [{k: v for k, v in s.items() if k != "mask_file"} for s in reviewed_structures if s]
                        self.store.update_job(
                            job["id"], measurements=measurements, progress={"phase": "measurements", "completed": completed_count, "total": len(structures)}
                        )
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        measurements_seconds = time.perf_counter() - started
        if job:
            self.stage(job, "Generating display meshes")
        mesh_completed = 0

        def progress(name):
            nonlocal mesh_completed
            if not job:
                return
            self.stage(job, name)
            count = re.search(r"(\d+)/(\d+)", name)
            if count:
                mesh_completed = max(0, int(count[1]) - 1)
            elif name.startswith("Packaging surface:"):
                mesh_completed = len(structures)
            self.store.update_job(
                job["id"],
                progress={"phase": "meshes", "completed": mesh_completed, "total": len(structures), "timings_seconds": {"measurements": measurements_seconds}},
            )

        model_started = time.perf_counter()
        display = surfaces.build_model(reviewed_structures, directory / "model.glb", progress=progress, cache_dir=self.surface_cache)
        # Aggregation reads prepared assets; report extraction/cache work from
        # preparation, rather than counting each structure twice as a cache hit.
        display["cache"] = {key: sum(s["cache"][key] for s in prepared_surfaces) for key in display["cache"]}
        display["timings_seconds"] = {
            key: value + sum(s["timings_seconds"][key] for s in prepared_surfaces) for key, value in display["timings_seconds"].items()
        }
        assets = [{k: a[k] for k in ("id", "key", "triangles")} for a in display.pop("surface_assets", [])]
        timings = {
            "preparation_seconds": measurements_seconds,
            "display_seconds": time.perf_counter() - model_started,
            "total_seconds": time.perf_counter() - started,
            "measurement_cache_hits": analysis_hits,
            "measurement_structures": measurement_timings,
            "preparation_workers": workers,
            "measurement_cpu_seconds": sum(measurement_timings.values()),
            "parallel_stage_times_are_summed_worker_seconds": True,
        }
        result = dict(
            id=revision_id,
            case_id=case_id,
            parent_id=parent,
            kind="segmentation",
            created_at=now(),
            review_status="unreviewed",
            source=source,
            operation=operation,
            geometry=geometry,
            structures=reviewed_structures,
            synthetic=bool(self.store.get("cases", case_id).get("synthetic")),
            qc=sorted({warning for s in reviewed_structures for warning in s["qc"]} | set(extra_qc or [])),
            directory=str(directory),
            ct_file=str(ct),
            acquisition=acquisition,
            reference_image_id=asset["id"],
            image_asset=public_image_asset(asset),
            model_url=f"/api/cases/{case_id}/revisions/{revision_id}/model.glb",
            model_manifest_url=f"/api/cases/{case_id}/revisions/{revision_id}/model-manifest",
            surface_assets=assets,
            mesh_statistics={"display": display, "detailed": None},
            processing_timings=timings,
            pipeline_version=__version__,
            pipeline_code_sha256=code_fingerprint(),
            inference_manifest=inference_manifest,
        )
        if parent:
            previous = self.store.revision(case_id, parent)
            for key in ("segmentation_settings", "segmentation_runs"):
                if key in previous:
                    result[key] = previous[key]
        # A readable provenance file travels with all case derivatives.
        atomic_json(directory / "revision.json", result)
        if job:
            self.store.update_job(
                job["id"],
                progress={
                    "phase": "complete",
                    "completed": len(structures),
                    "total": len(structures),
                    "timings_seconds": {"measurements": measurements_seconds, "display": timings["display_seconds"]},
                },
            )
        return result

    def ensure_detailed(self, case_id, revision_id):
        """Generate detailed geometry only for an explicit export request."""
        from . import surfaces

        revision = self.store.revision(case_id, revision_id)
        if not revision["structures"]:
            raise ValueError("This image has no segmentation surfaces yet")
        target = Path(revision["directory"]) / "detailed.glb"
        if target.is_file() and revision.get("mesh_statistics", {}).get("detailed"):
            return target
        # One export at a time across API threads/processes bounds memory use.
        with (self.settings.data_dir / "detailed-export.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            revision = self.store.revision(case_id, revision_id)
            if not target.is_file() or not revision.get("mesh_statistics", {}).get("detailed"):
                started = time.perf_counter()
                report = surfaces.build_model(revision["structures"], target, detailed=True, cache_dir=self.surface_cache)
                report.pop("surface_assets", None)
                self.store.record_detailed_export(case_id, revision_id, report, time.perf_counter() - started)
        return target

    def optimize_assets(self, case_id):
        from . import surfaces

        case = self.store.get("cases", case_id)
        if not case.get("active_revision_id"):
            raise ValueError("The case has no completed segmentation")
        revision = self.store.revision(case_id, case["active_revision_id"])
        report = surfaces.import_display_assets(
            revision["structures"],
            Path(revision["directory"]) / "model.glb",
            self.surface_cache,
            revision["mesh_statistics"]["display"]["display_simplification"],
        )
        assets = report["surface_assets"]
        self.store.attach_surface_assets(case_id, revision["id"], assets)
        return {
            "case_id": case_id,
            "revision_id": revision["id"],
            "surface_count": len(assets),
            "status": "optimized",
            "cache": report["cache"],
            "timings_seconds": report["timings_seconds"],
        }

    def _refine(self, job):
        from . import imaging

        payload = job["payload"]
        parent = self.store.revision(job["case_id"], payload["revision_id"])
        selected = next((s for s in parent["structures"] if s["id"] == payload["structure_id"]), None)
        if not selected:
            raise ValueError("Unknown structure")
        self.stage(job, "Applying refinement to a copy of the selected mask")
        folder = self.store.case_dir(job["case_id"]) / "refinements" / job["id"]
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / "mask.nii.gz"
        changes = imaging.refine_mask(Path(parent["ct_file"]), Path(selected["mask_file"]), output, payload["operation"])
        structures = []
        for entry in parent["structures"]:
            copy = dict(entry)
            if entry["id"] == selected["id"]:
                copy = {k: v for k, v in copy.items() if k not in ("volume_ml", "voxel_count", "bounds_ras", "centroid_ras", "qc")}
                copy["mask_file"] = str(output)
            structures.append(copy)
        operation = dict(**payload, **changes, code_version=__version__, code_sha256=code_fingerprint())
        return self.build_revision(
            job["case_id"],
            Path(parent["ct_file"]),
            structures,
            source="refinement",
            parent=parent["id"],
            operation=operation,
            acquisition=parent.get("acquisition"),
            job=job,
            extra_qc=self._inherited_qc(parent),
            inference_manifest=parent.get("inference_manifest"),
        )

    def _include_all(self, job):
        from . import imaging, segmentation, surfaces

        parent = self.store.revision(job["case_id"], job["payload"]["revision_id"])
        if self.store.get("cases", job["case_id"])["active_revision_id"] != parent["id"]:
            raise ValueError("The active revision changed; retry including outputs from the current revision")
        manifest = parent.get("inference_manifest") or {}
        run_id = manifest.get("run_id", "")
        if manifest.get("status") != "completed" or not re.fullmatch(r"run-[a-f0-9]{32}", run_id):
            raise ValueError("This revision has no completed native inference run to restore")
        runs = list((self.store.case_dir(job["case_id"]) / "inference").glob(f"*/{run_id}"))
        if len(runs) != 1 or not (runs[0] / "inference.json").is_file():
            raise ValueError("The original saved inference outputs are unavailable; no inference was rerun")
        run = runs[0]
        if json.loads((run / "inference.json").read_text()) != manifest:
            raise ValueError("The saved inference manifest does not match this revision")
        ct = Path(parent["ct_file"])
        self.stage(job, "Verifying the saved inference run and reference CT")
        if segmentation._digest(ct) != manifest.get("input_sha256"):
            raise ValueError("The saved inference run belongs to a different reference CT")
        tasks = manifest.get("tasks", [])
        if not tasks or len({t["task"] for t in tasks}) != len(tasks):
            raise ValueError("The saved inference task manifest is incomplete")
        outputs = []
        from .segmentation_options import task_ids

        reference = self.store.resolve_reference(parent)
        allowed_tasks = set(task_ids(reference["modality"]))
        for task in tasks:
            task_name = task["task"]
            labels = task.get("labels", [])
            if task_name not in allowed_tasks or task.get("status") != "completed" or not labels or len(set(labels)) != len(labels):
                raise ValueError("The saved inference task manifest is incomplete")
            for name in labels:
                if not re.fullmatch(r"[A-Za-z0-9_]+", name):
                    raise ValueError("Invalid saved structure name")
                path = run / task_name / f"{name}.nii.gz"
                if not path.is_file():
                    raise ValueError(f"Saved output {task_name}:{name} is unavailable; no inference was rerun")
                group, color = segmentation._appearance(name, task_name)
                outputs.append(
                    dict(
                        id=f"{task_name}:{name}",
                        name=segmentation._structure_name(name, task_name),
                        task=task_name,
                        group=group,
                        color=color,
                        mask_file=str(path),
                    )
                )
        # Keep every current mask, including Slicer edits and refinement results.
        # Only presentation metadata is refreshed for the expanded anatomy groups.
        appearances = {s["id"]: s for s in outputs}
        structures = []
        for original in parent["structures"]:
            entry = dict(original)
            if entry["id"] in appearances:
                entry.update({key: appearances[entry["id"]][key] for key in ("group", "color")})
            structures.append(entry)
        existing = {s["id"] for s in structures}
        missing = [entry for entry in outputs if entry["id"] not in existing]
        geometry = imaging.geometry(ct)
        added, empty = [], []
        for index, entry in enumerate(missing, 1):
            self.stage(job, f"Checking saved label {index}/{len(missing)}: {entry['name']}")

            def prepare(decoded):
                if decoded[0].any():
                    surfaces.prepare_surface(entry, self.surface_cache, decoded_mask=decoded, progress=lambda _: self.check_cancel(job))

            # A cold mask is decoded once for validation, measurements and surface
            # extraction. Existing measurements and display meshes remain cached.
            analysis, _ = self._analyze(ct, entry, geometry, on_decoded=prepare)
            if not analysis["voxel_count"]:
                empty.append(entry["id"])
                continue
            entry.update(analysis)
            structures.append(entry)
            added.append(entry["id"])
        operation = dict(
            type="include_all_outputs",
            source_run_id=run_id,
            added_structure_ids=added,
            empty_labels=empty,
            code_version=__version__,
            code_sha256=code_fingerprint(),
        )
        qc = self._inherited_qc(parent) + [f"Empty label omitted: {label}; inspect scan coverage" for label in empty]
        return self.build_revision(
            job["case_id"],
            ct,
            structures,
            source="include_all_outputs",
            parent=parent["id"],
            operation=operation,
            acquisition=parent.get("acquisition"),
            job=job,
            extra_qc=qc,
            inference_manifest=manifest,
        )

    @staticmethod
    def _inherited_qc(parent):
        return list((parent.get("inference_manifest") or {}).get("qc", [])) + [warning for warning in parent.get("qc", []) if warning.startswith("Synthetic")]

    def _correct(self, job):
        from . import slicer_io

        parent = self.store.revision(job["case_id"], job["payload"]["revision_id"])
        folder = self.store.case_dir(job["case_id"]) / "corrections" / job["id"]
        folder.mkdir(parents=True, exist_ok=True)
        self.stage(job, "Validating Slicer correction geometry and segment identifiers")
        structures = slicer_io.import_segmentation(
            Path(parent["ct_file"]), Path(job["payload"]["seg_file"]), parent["structures"], folder, require_source_hash=True
        )
        for structure in structures:
            for key in ("volume_ml", "voxel_count", "centroid_ras", "bounds_ras", "qc"):
                structure.pop(key, None)
        revision = self.build_revision(
            job["case_id"],
            Path(parent["ct_file"]),
            structures,
            source="slicer_correction",
            parent=parent["id"],
            operation={"type": "slicer_import", "code_version": __version__, "code_sha256": code_fingerprint()},
            acquisition=parent.get("acquisition"),
            job=job,
            extra_qc=self._inherited_qc(parent),
            inference_manifest=parent.get("inference_manifest"),
        )
        revision["review_status"] = "corrected"
        (Path(revision["directory"]) / "revision.json").write_text(json.dumps(revision, indent=2))
        return revision

    def wait(self, job_id, timeout=3600):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.store.get("jobs", job_id)
            if job["status"] not in ("queued", "running"):
                return job
            time.sleep(0.25)
        raise TimeoutError("Job remains active; inspect its status through the app")

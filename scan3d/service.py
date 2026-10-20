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

from . import __version__
from .config import Settings
from .store import Store, identifier, now

log = logging.getLogger(__name__)


def code_fingerprint():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{identifier()}.writing")
    try:
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_relative(name):
    path = Path(name.replace("\\", "/"))
    if path.is_absolute() or any(p in ("..", "") for p in path.parts) or not path.parts:
        raise ValueError("Unsafe archive or upload filename")
    return path


def extract_zip(archive, target, limit):
    with zipfile.ZipFile(archive) as z:
        entries = z.infolist()
        if len(entries) > 100_000 or sum(e.file_size for e in entries) > limit:
            raise ValueError("Archive exceeds expanded import limit")
        for entry in entries:
            path = target / safe_relative(entry.filename)
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Archive symlinks are not supported")
            if entry.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    raise ValueError("Duplicate archive filename")
                with z.open(entry) as src, path.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)


class Service:
    def __init__(self, settings=None):
        self.settings = settings or Settings.from_env()
        self.store = Store(self.settings.data_dir)
        self.stop_event = threading.Event()
        self.thread = None
        self.worker_lock = None
        self.interactive_runtime = None
        self._reviews = None

    @property
    def reviews(self):
        if self._reviews is None:
            from .review import ReviewManager
            self._reviews = ReviewManager(self)
        return self._reviews

    def _interactive(self):
        if self.interactive_runtime is None:
            from .interactive import InteractiveRuntime
            self.interactive_runtime = InteractiveRuntime()
        return self.interactive_runtime

    def _release_interactive(self):
        runtime, self.interactive_runtime = self.interactive_runtime, None
        if runtime is not None:
            runtime.release()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        lock = (self.settings.data_dir / "worker.lock").open("a")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return
        self.worker_lock = lock
        self.store.recover()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="scan3d-single-worker")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)

    def _loop(self):
        try:
            while not self.stop_event.is_set():
                try:
                    job = self.store.claim()
                    if job:
                        self.execute(job)
                    else:
                        self.stop_event.wait(.25)
                except Exception:
                    log.exception("Worker iteration failed")
                    self.stop_event.wait(1)
        finally:
            try:
                self._release_interactive()
            except Exception:
                log.exception("Could not release the interactive inference runtime")
            if self.worker_lock:
                self.worker_lock.close()
                self.worker_lock = None

    def new_case(self, label=None):
        record = dict(id=identifier(), label=label or "New CT case", created_at=now(), status="importing",
                      active_revision_id=None, selected_series_uid=None, phase=None, series=[], references={})
        self.store.save("cases", record)
        return record

    def finish_import(self, case, source):
        from .dicom import index_dicom
        series = index_dicom(source)
        if not series:
            raise ValueError("No supported CT DICOM series found")
        case.update(series=series, source_dir=str(source), status="imported")
        return self.store.save("cases", case)

    def import_local(self, path, label=None):
        path = self.settings.import_path(path)
        if path.is_dir() and (path == self.settings.data_dir or path in self.settings.data_dir.parents):
            raise ValueError("Select a scan directory that does not contain the application's data directory")
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

    def process(self, case_id, series_uid, profile, phase):
        case = self.store.get("cases", case_id)
        selected = next((s for s in case["series"] if s["uid"] == series_uid), None)
        if not selected:
            raise ValueError("Select a series belonging to this case")
        if profile not in ("chest", "abdomen", "chest_abdomen"):
            raise ValueError("Unknown processing profile")
        if not phase or len(phase) > 80:
            raise ValueError("Specify the contrast phase or 'unknown'")
        return self.store.enqueue(case_id, "process", dict(series_uid=series_uid, profile=profile, phase=phase))

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

    def _analyze(self, ct, structure, geometry, on_decoded=None):
        """Reuse exact measurements only for identical mask bytes and CT grids."""
        from . import imaging, surfaces
        identity = {"mask": surfaces.hash_file(Path(structure["mask_file"])), "geometry": geometry,
                    "implementation": hashlib.sha256(Path(imaging.__file__).read_bytes()).hexdigest()}
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
        atomic_json(path, {"key": key, "analysis": analysis,
                          "sha256": hashlib.sha256(json.dumps(analysis, sort_keys=True, allow_nan=False).encode()).hexdigest()})
        if on_decoded:
            on_decoded(decoded)
        return analysis, False

    @staticmethod
    def _preparation_workers(geometry):
        requested = int(os.environ.get("SCAN3D_POSTPROCESS_WORKERS", "4"))
        if requested < 1:
            raise ValueError("SCAN3D_POSTPROCESS_WORKERS must be positive")
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
        return max(1, min(requested, os.cpu_count() or 1, memory_limit))

    def execute(self, job):
        try:
            if job["kind"] == "process":
                # Serial GPU execution is insufficient if the idle interactive
                # model still owns VRAM while TotalSegmentator starts.
                self._release_interactive()
                revision = self._process(job)
            elif job["kind"] == "review_predict":
                self.reviews.run_predict(job, None if job["payload"].get("relationship_only") else self._interactive())
                return
            elif job["kind"] == "review_accept":
                revision = self.reviews.run_accept(job)
            elif job["kind"] == "refine":
                revision = self._refine(job)
            elif job["kind"] == "correction":
                revision = self._correct(job)
            elif job["kind"] == "include_all":
                revision = self._include_all(job)
            else:
                raise ValueError("Unknown job kind")
            self.check_cancel(job)
            expected = job["payload"]["revision_id"] if job["kind"] in ("include_all", "review_accept") else None
            review_guard = ({key: job["payload"][key] for key in ("session_id", "generation")}
                            if job["kind"] == "review_accept" else None)
            self.store.commit_revision(revision, job, expected_active_revision_id=expected, review_guard=review_guard)
        except InterruptedError as exc:
            self.store.update_job(job["id"], status="cancelled", stage="Cancelled", error=str(exc))
            if job["kind"].startswith("review_"):
                self.reviews.get(job["case_id"], job["payload"]["session_id"])
        except Exception as exc:
            log.exception("Job %s failed", job["id"])
            self.store.update_job(job["id"], status="failed", stage="Failed", error=str(exc))
            if job["kind"].startswith("review_"):
                self.reviews.get(job["case_id"], job["payload"]["session_id"])

    def _process(self, job):
        from . import dicom, segmentation
        case = self.store.get("cases", job["case_id"])
        series = next(s for s in case["series"] if s["uid"] == job["payload"]["series_uid"])
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
        fingerprint = segmentation.model_fingerprint(job["payload"]["profile"])
        cache_parameters = {"input_sha256": digest.hexdigest(), "profile": job["payload"]["profile"],
                            "versions": fingerprint["versions"], "device": fingerprint["device_request"],
                            "wrapper_sha256": fingerprint["wrapper_sha256"], "pipeline_version": __version__}
        key = hashlib.sha256(json.dumps(cache_parameters, sort_keys=True).encode()).hexdigest()
        inference = case_dir / "inference" / key
        cached = inference / "complete.json"
        run_manifest = {}
        if cached.exists():
            try:
                cache_record = json.loads(cached.read_text())
                structures = cache_record["structures"]
                if (not structures or not all(Path(s["mask_file"]).is_file() for s in structures)
                        or not fingerprint["weights_complete"]
                        or cache_record["model_fingerprint"]["digest"] != fingerprint["digest"]):
                    cached.unlink()
                else:
                    run_manifest = cache_record["manifest"]
            except (ValueError, KeyError, TypeError):
                cached.unlink()
        if not cached.exists():
            inference.mkdir(parents=True, exist_ok=True)
            structures = segmentation.segment(ct, inference, job["payload"]["profile"],
                lambda name: self.stage(job, name), lambda: self.cancelled(job))
            self.check_cancel(job)
            run_manifest = json.loads((inference / "inference.json").read_text())
            pending = cached.with_suffix(".json.writing")
            pending.write_text(json.dumps({"structures": structures,
                                           "model_fingerprint": run_manifest["model_fingerprint"],
                                           "manifest": run_manifest}))
            pending.replace(cached)
        self.stage(job, "Checking masks and generating surfaces")
        return self.build_revision(case["id"], ct, structures, source="totalsegmentator",
                                   acquisition=job["payload"], job=job, extra_qc=run_manifest.get("qc", []),
                                   inference_manifest=run_manifest)

    def build_revision(self, case_id, ct, structures, source, parent=None, operation=None, directory=None, acquisition=None, job=None,
                       extra_qc=None, inference_manifest=None):
        from . import imaging, surfaces
        revision_id = identifier()
        directory = directory or self.store.case_dir(case_id) / "revisions" / revision_id
        directory.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        geometry = imaging.geometry(ct)
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
                surface = surfaces.prepare_surface(entry, self.surface_cache, decoded_mask=decoded,
                    progress=(lambda _: self.check_cancel(job)) if job else None)
                surface_seconds += time.perf_counter() - surface_start
            if "volume_ml" not in entry:
                analysis, hit = self._analyze(ct, entry, geometry, on_decoded=prepare_decoded)
                entry.update(analysis)
            else:
                hit = True
            analysis_seconds = time.perf_counter() - measured - surface_seconds
            if surface is None:
                surface = surfaces.prepare_surface(entry, self.surface_cache,
                    progress=(lambda _: self.check_cancel(job)) if job else None)
            entry.setdefault("qc", [])
            return index, entry, hit, analysis_seconds, surface

        # Limit both concurrent dense-mask workspaces and native CPU work. A
        # decoded mask feeds QC and contour extraction in the same worker.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan3d-mask") as executor:
            futures = [executor.submit(prepare, index, structure) for index, structure in enumerate(structures)]
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
                        self.store.update_job(job["id"], measurements=measurements,
                            progress={"phase": "measurements", "completed": completed_count, "total": len(structures)})
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
            self.store.update_job(job["id"], progress={"phase": "meshes",
                "completed": mesh_completed, "total": len(structures),
                "timings_seconds": {"measurements": measurements_seconds}})
        model_started = time.perf_counter()
        display = surfaces.build_model(reviewed_structures, directory / "model.glb", progress=progress, cache_dir=self.surface_cache)
        # Aggregation reads prepared assets; report extraction/cache work from
        # preparation, rather than counting each structure twice as a cache hit.
        display["cache"] = {key: sum(s["cache"][key] for s in prepared_surfaces) for key in display["cache"]}
        display["timings_seconds"] = {key: value + sum(s["timings_seconds"][key] for s in prepared_surfaces)
                                       for key, value in display["timings_seconds"].items()}
        assets = [{k: a[k] for k in ("id", "key", "triangles")} for a in display.pop("surface_assets", [])]
        timings = {"preparation_seconds": measurements_seconds, "display_seconds": time.perf_counter() - model_started,
                   "total_seconds": time.perf_counter() - started, "measurement_cache_hits": analysis_hits,
                   "measurement_structures": measurement_timings, "preparation_workers": workers,
                   "measurement_cpu_seconds": sum(measurement_timings.values()),
                   "parallel_stage_times_are_summed_worker_seconds": True}
        result = dict(id=revision_id, case_id=case_id, parent_id=parent, created_at=now(), review_status="unreviewed",
                      source=source, operation=operation, geometry=geometry, structures=reviewed_structures,
                      synthetic=bool(self.store.get("cases", case_id).get("synthetic")),
                      qc=sorted({warning for s in reviewed_structures for warning in s["qc"]} | set(extra_qc or [])),
                      directory=str(directory), ct_file=str(ct), acquisition=acquisition,
                      model_url=f"/api/cases/{case_id}/revisions/{revision_id}/model.glb",
                      model_manifest_url=f"/api/cases/{case_id}/revisions/{revision_id}/model-manifest",
                      surface_assets=assets, mesh_statistics={"display": display, "detailed": None}, processing_timings=timings,
                      pipeline_version=__version__,
                      pipeline_code_sha256=code_fingerprint(),
                      inference_manifest=inference_manifest)
        # A readable provenance file travels with all case derivatives.
        atomic_json(directory / "revision.json", result)
        if job:
            self.store.update_job(job["id"], progress={"phase": "complete", "completed": len(structures), "total": len(structures),
                "timings_seconds": {"measurements": measurements_seconds, "display": timings["display_seconds"]}})
        return result

    def ensure_detailed(self, case_id, revision_id):
        """Generate detailed geometry only for an explicit export request."""
        from . import surfaces
        revision = self.store.revision(case_id, revision_id)
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
        report = surfaces.import_display_assets(revision["structures"], Path(revision["directory"]) / "model.glb",
            self.surface_cache, revision["mesh_statistics"]["display"]["display_simplification"])
        assets = report["surface_assets"]
        self.store.attach_surface_assets(case_id, revision["id"], assets)
        return {"case_id": case_id, "revision_id": revision["id"], "surface_count": len(assets), "status": "optimized",
                "cache": report["cache"], "timings_seconds": report["timings_seconds"]}

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
        return self.build_revision(job["case_id"], Path(parent["ct_file"]), structures, source="refinement",
                                   parent=parent["id"], operation=operation, acquisition=parent.get("acquisition"), job=job,
                                   extra_qc=self._inherited_qc(parent), inference_manifest=parent.get("inference_manifest"))

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
        for task in tasks:
            task_name = task["task"]
            labels = task.get("labels", [])
            if (task_name not in {"total", "lung_vessels"} or task.get("status") != "completed"
                    or not labels or len(set(labels)) != len(labels)):
                raise ValueError("The saved inference task manifest is incomplete")
            for name in labels:
                if not re.fullmatch(r"[A-Za-z0-9_]+", name):
                    raise ValueError("Invalid saved structure name")
                path = run / task_name / f"{name}.nii.gz"
                if not path.is_file():
                    raise ValueError(f"Saved output {task_name}:{name} is unavailable; no inference was rerun")
                group, color = segmentation._appearance(name)
                outputs.append(dict(id=f"{task_name}:{name}", name=name.replace("_", " ").capitalize(),
                                    task=task_name, group=group, color=color, mask_file=str(path)))
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
                    surfaces.prepare_surface(entry, self.surface_cache, decoded_mask=decoded,
                                             progress=lambda _: self.check_cancel(job))

            # A cold mask is decoded once for validation, measurements and surface
            # extraction. Existing measurements and display meshes remain cached.
            analysis, _ = self._analyze(ct, entry, geometry, on_decoded=prepare)
            if not analysis["voxel_count"]:
                empty.append(entry["id"])
                continue
            entry.update(analysis)
            structures.append(entry)
            added.append(entry["id"])
        operation = dict(type="include_all_outputs", source_run_id=run_id,
                         added_structure_ids=added, empty_labels=empty,
                         code_version=__version__, code_sha256=code_fingerprint())
        qc = self._inherited_qc(parent) + [f"Empty label omitted: {label}; inspect scan coverage" for label in empty]
        return self.build_revision(job["case_id"], ct, structures, source="include_all_outputs", parent=parent["id"],
                                   operation=operation, acquisition=parent.get("acquisition"), job=job,
                                   extra_qc=qc, inference_manifest=manifest)

    @staticmethod
    def _inherited_qc(parent):
        return list((parent.get("inference_manifest") or {}).get("qc", [])) + [
            warning for warning in parent.get("qc", []) if warning.startswith("Synthetic")]

    def _correct(self, job):
        from . import slicer_io
        parent = self.store.revision(job["case_id"], job["payload"]["revision_id"])
        folder = self.store.case_dir(job["case_id"]) / "corrections" / job["id"]
        folder.mkdir(parents=True, exist_ok=True)
        self.stage(job, "Validating Slicer correction geometry and segment identifiers")
        structures = slicer_io.import_segmentation(Path(parent["ct_file"]), Path(job["payload"]["seg_file"]),
                                                    parent["structures"], folder, require_source_hash=True)
        for structure in structures:
            for key in ("volume_ml", "voxel_count", "centroid_ras", "bounds_ras", "qc"):
                structure.pop(key, None)
        revision = self.build_revision(job["case_id"], Path(parent["ct_file"]), structures,
            source="slicer_correction", parent=parent["id"], operation={"type": "slicer_import", "code_version": __version__, "code_sha256": code_fingerprint()},
            acquisition=parent.get("acquisition"), job=job, extra_qc=self._inherited_qc(parent),
            inference_manifest=parent.get("inference_manifest"))
        revision["review_status"] = "corrected"
        (Path(revision["directory"]) / "revision.json").write_text(json.dumps(revision, indent=2))
        return revision

    def wait(self, job_id, timeout=3600):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.store.get("jobs", job_id)
            if job["status"] not in ("queued", "running"):
                return job
            time.sleep(.25)
        raise TimeoutError("Job remains active; inspect its status through the app")

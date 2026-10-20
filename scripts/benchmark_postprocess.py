"""Read a completed case; benchmark derivatives in an isolated validation store.

No inference, source-mask edits, clinical case activation or review changes.
An optional --cache-dir warms the application's content-addressed surface cache.
"""
import argparse
import json
import resource
from pathlib import Path
import time

import numpy as np

from scan3d.config import Settings, ROOT
from scan3d.service import Service
from scan3d.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/validation/postprocess")
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    original_store = Store(ROOT / "data")
    case = original_store.get("cases", args.case)
    original = original_store.revision(args.case, case["active_revision_id"])
    args.output.mkdir(parents=True, exist_ok=True)

    class BenchmarkService(Service):
        @property
        def surface_cache(self):
            return args.cache_dir or super().surface_cache

    service = BenchmarkService(Settings(args.output / "store", (ROOT,)))
    snapshot = service.new_case("Isolated performance validation")
    structures = [{k: v for k, v in s.items() if k not in
                   ("volume_ml", "voxel_count", "centroid_ras", "bounds_ras", "qc")}
                  for s in original["structures"]]
    report = {"source_case": args.case, "source_revision": original["id"], "structures": len(structures),
              "shape": original["geometry"]["shape"], "source_masks_unchanged": True}

    def record(name, revision):
        stats = revision["mesh_statistics"]["display"]
        report[name] = {"timings": revision["processing_timings"], "cache": stats.get("cache"),
                        "mesh_stage_seconds": stats.get("timings_seconds"), "mesh_processing": stats.get("mesh_processing"),
                        "triangles": stats["triangles"], "detailed_generated": (Path(revision["directory"]) / "detailed.glb").exists()}
        (args.output / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps({name: {k: v for k, v in report[name].items() if k != "timings"},
                          "total_seconds": revision["processing_timings"]["total_seconds"]}), flush=True)

    print("Cold measurements/display build started", flush=True)
    cold = service.build_revision(snapshot["id"], Path(original["ct_file"]), structures, "performance_validation")
    record("cold", cold)
    service.store.commit_revision(cold)
    assert all(a["voxel_count"] == b["voxel_count"] and np.isclose(a["volume_ml"], b["volume_ml"], rtol=0, atol=1e-8)
               for a, b in zip(cold["structures"], original["structures"]))
    print("Warm identical build started", flush=True)
    warm = service.build_revision(snapshot["id"], Path(original["ct_file"]), structures, "performance_validation")
    record("warm", warm)
    from scan3d import imaging
    liver = next(s for s in cold["structures"] if s["id"] == "total:liver")
    mask, _, affine = imaging._binary(Path(liver["mask_file"]))
    voxel = np.argwhere(mask)[0]
    seed = imaging._world(affine, voxel)
    del mask
    job = service.store.enqueue(snapshot["id"], "refine", {"revision_id": cold["id"], "structure_id": liver["id"],
        "operation": {"type": "erase_region", "bounds_ras": [(seed - .1).tolist(), (seed + .1).tolist()]}})
    print("Single-voxel liver refinement started (isolated copy)", flush=True)
    started = time.perf_counter()
    service.execute(job)
    completed = service.store.get("jobs", job["id"])
    assert completed["status"] == "completed", completed
    refined = service.store.revision(snapshot["id"], completed["revision_id"])
    record("one_structure_refinement", refined)
    report["one_structure_refinement"]["end_to_end_seconds"] = time.perf_counter() - started
    report["one_structure_refinement"]["changed_voxels"] = refined["operation"]["changed_voxels"]
    assert refined["operation"]["changed_voxels"] == 1
    assert refined["mesh_statistics"]["display"]["cache"]["surface_hits"] == len(structures) - 1
    started = time.perf_counter()
    service.ensure_detailed(snapshot["id"], cold["id"])
    detailed = service.store.revision(snapshot["id"], cold["id"])
    report["on_demand_detailed"] = {"seconds": time.perf_counter() - started, **detailed["mesh_statistics"]["detailed"]}
    report["status"] = "passed"
    report["peak_process_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    (args.output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"status": "passed", "refinement_seconds": report["one_structure_refinement"]["end_to_end_seconds"],
                      "on_demand_detailed_seconds": report["on_demand_detailed"]["seconds"]}), flush=True)


if __name__ == "__main__":
    main()

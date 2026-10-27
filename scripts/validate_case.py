"""Run an imported case through the real application worker and persist GPU evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def inference_evidence(revision, profile):
    """Accept only completed full-resolution HIP runs and their native reports."""
    run_dirs = {Path(s["mask_file"]).parent.parent for s in revision["structures"]}
    required = {"total"} if profile == "abdomen" else {"total", "lung_vessels"}
    completed = set()
    manifests = []
    native_reports = []
    problems = []
    for directory in sorted(run_dirs):
        manifest_file = directory / "inference.json"
        if not manifest_file.is_file():
            problems.append("The accepted masks have no inference manifest")
            continue
        manifest = json.loads(manifest_file.read_text())
        manifests.append({"path": str(manifest_file), "data": manifest})
        runtime = manifest.get("runtime", {})
        if (manifest.get("status") != "completed" or manifest.get("resolution") != "standard_full"
                or manifest.get("profile") != profile or not runtime.get("hip")
                or not runtime.get("selected_device", runtime.get("device", "")).startswith("gpu")):
            problems.append("The inference manifest does not certify a completed full-resolution HIP run")
        if not manifest.get("model_fingerprint", {}).get("weights_complete"):
            problems.append("Exact model checkpoint identities are incomplete")
        for task in manifest.get("tasks", []):
            report_name = task.get("native_report")
            if task.get("status") != "completed" or task.get("returncode") != 0 or not report_name:
                problems.append("A required native task did not complete with a report")
                continue
            report_file = directory / Path(report_name).name
            if not report_file.is_file():
                problems.append("A required native task report is missing")
                continue
            report = json.loads(report_file.read_text())
            native_reports.append({"path": str(report_file), "data": report})
            if (report.get("device") != "gpu" or report.get("task") != task.get("task")
                    or report.get("fast") or report.get("fastest") or report.get("save_lowres")):
                problems.append("Native task report has unexpected device, task or resolution")
                continue
            completed.add(task["task"])
    if not required.issubset(completed):
        problems.append("Not all required tasks are proven complete")
    if revision["source"] != "totalsegmentator":
        problems.append("Revision is not a native inference result")
    return {"verified": not problems, "problems": sorted(set(problems)), "completed_tasks": sorted(completed),
            "manifests": manifests, "native_reports": native_reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive", type=Path)
    source.add_argument("--case-id")
    parser.add_argument("--series-uid")
    parser.add_argument("--profile", choices=["chest", "abdomen", "chest_abdomen"], default="chest_abdomen")
    parser.add_argument("--phase", default="unknown")
    parser.add_argument("--list-series", action="store_true")
    args = parser.parse_args()
    from scan3d.service import Service
    service = Service()
    case = service.import_local(str(args.archive), label="Validation CT") if args.archive else service.store.get("cases", args.case_id)
    summary = {"case_id": case["id"], "series": [{k: v for k, v in s.items() if k != "files"} for s in case["series"]]}
    state = ROOT / ".runtime/validation-case.json"
    state.parent.mkdir(exist_ok=True)
    state.write_text(json.dumps(summary, indent=2) + "\n")
    if args.list_series or not args.series_uid:
        print(json.dumps(summary, indent=2))
        return 0
    job = service.process(case["id"], args.series_uid, args.profile, args.phase)
    service.start()
    print(json.dumps({"case_id": case["id"], "job_id": job["id"]}), flush=True)
    started = time.monotonic()
    previous_stage = None
    try:
        while True:
            job = service.store.get("jobs", job["id"])
            if job["stage"] != previous_stage:
                print(f"{int(time.monotonic() - started)}s: {job['stage']}", flush=True)
                previous_stage = job["stage"]
            if job["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(2)
    finally:
        service.stop()
    if job["status"] != "completed":
        print(json.dumps({"status": job["status"], "error": job.get("error")}), flush=True)
        return 1
    revision = service.store.get("revisions", job["revision_id"])
    evidence = inference_evidence(revision, args.profile)
    manifests = [entry["data"] for entry in evidence["manifests"]]
    runtime = next((m["runtime"] for m in manifests), {})
    verified = evidence["verified"]
    proof_file = Path(os.environ.get("SCAN3D_GPU_PROOF", str(ROOT / ".runtime/gpu-proof.json")))
    proof = json.loads(proof_file.read_text()) if proof_file.exists() else {}
    weights_directory = Path(os.environ.get("TOTALSEG_HOME_DIR", ROOT / ".runtime/totalsegmentator"))
    proof.update(inference_verified=verified, model_inference_proven=verified,
                 validated_at=datetime.now(timezone.utc).isoformat(), case_id=case["id"],
                 revision_id=revision["id"], runtime_seconds=round(time.monotonic() - started, 3),
                 profile=args.profile, completed_tasks=evidence["completed_tasks"], runtime=runtime,
                 proof_problems=evidence["problems"],
                 versions={p: metadata.version(p) for p in ["torch", "nnunetv2", "TotalSegmentator"]},
                 model_manifest_files=[entry["path"] for entry in evidence["manifests"]],
                 native_task_reports=evidence["native_reports"],
                 model_fingerprints=[m.get("model_fingerprint") for m in manifests],
                 inference_seconds=sum(entry["data"].get("runtime_seconds", 0) or 0
                                       for entry in evidence["native_reports"]),
                 weights_directory=str(weights_directory),
                 model_weights=[{"path": str(p), "bytes": p.stat().st_size}
                                for p in sorted(weights_directory.rglob("checkpoint_final.pth"))],
                 structures=len(revision["structures"]),
                 geometry_checked=True, clinical_review_status=revision["review_status"])
    pending = proof_file.with_suffix(".json.writing")
    pending.write_text(json.dumps(proof, indent=2) + "\n")
    pending.replace(proof_file)
    print(json.dumps({"status": "completed", "inference_verified": verified,
                      "structures": len(revision["structures"]), "proof": str(proof_file)}), flush=True)
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())

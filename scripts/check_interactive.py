"""Prove native nnInteractive correction without changing a case or revision."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np

from scan3d import imaging, interactive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ct", type=Path, required=True)
    parser.add_argument("--mask", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New directory for proof artifacts")
    parser.add_argument("--point-ijk", type=int, nargs=3, action="append", required=True)
    parser.add_argument("--exclude-last", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("The proof output directory must be new")
    args.output.mkdir(parents=True)
    ct, affine = imaging._image(args.ct)
    original = interactive._sha256(interactive._identity(args.mask))
    runtime, prompts, records = interactive.InteractiveRuntime(), [], []
    try:
        for index, voxel in enumerate([None, *args.point_ijk]):
            if voxel is not None:
                if any(v < 0 or v >= s for v, s in zip(voxel, ct.shape)):
                    raise ValueError("Proof point lies outside the reference CT")
                ras = (affine @ np.r_[voxel, 1])[:3].tolist()
                prompts.append({"id": f"proof-{index}", "ras": ras,
                                "include": not (args.exclude_last and index == len(args.point_ijk))})
            record = runtime.predict(args.ct, args.mask, prompts, args.output / f"candidate-{index}.nii.gz",
                                     lambda message: print(message, flush=True), lambda: False, "native-proof")
            records.append(record)
            print(json.dumps({"prompt_count": record["prompt_count"], "timings": record["timings"],
                              "changed_voxels": record["changed_voxels"], "device": record["device"]}), flush=True)
        if original != interactive._sha256(interactive._identity(args.mask)):
            raise RuntimeError("Native proof modified the source segmentation")
        if records[0]["changed_voxels"] != 0:
            raise RuntimeError("Initial-mask setup changed voxels before a prompt")
        if any(item["predictions_this_request"] != 1 or not item["reused_prompt_prefix"] for item in records[1:]):
            raise RuntimeError("Native proof did not reuse the warm prompt prefix")
        report = {"verified": True, "source_preserved": True, "records": records}
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print("Native GPU correction, unchanged initialization, and incremental prompts verified.")
        return 0
    finally:
        runtime.release()


if __name__ == "__main__":
    sys.exit(main())

"""Acquire the versioned public model; never send patient data to a model host."""

import hashlib
import os

from scan3d.config import ROOT, load_environment
from scan3d.interactive import _model_path

load_environment()
os.environ.setdefault("HF_HUB_CACHE", str(ROOT / ".cache" / "huggingface"))
from huggingface_hub import snapshot_download

REVISION = "3f308d751c00644e4fde6f09c600264b393b21b5"
CHECKPOINT_SHA256 = "b3ac4421f85457bbd1aa0d87f5e67bcb7bc8e2ce6b824b6ac45077cc5d630ea9"


def main():
    model = _model_path()
    snapshot_download(repo_id="MIC-DKFZ/nnInteractive", revision=REVISION, allow_patterns=[f"{model.name}/**"], local_dir=model.parent, max_workers=2)
    checkpoint = model / "fold_0" / "checkpoint_final.pth"
    sha = hashlib.sha256()
    with checkpoint.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            sha.update(block)
    if sha.hexdigest() != CHECKPOINT_SHA256:
        raise RuntimeError("Downloaded nnInteractive checkpoint failed its pinned checksum")
    print(f"nnInteractive_v1.0 verified at {REVISION}; checkpoint {sha.hexdigest()}")
    print("Checkpoint license: CC BY-NC-SA 4.0")


if __name__ == "__main__":
    main()

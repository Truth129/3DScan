"""Acquire a pinned public segmentation model; never send patient data to a model host."""

import argparse
import os
from pathlib import Path
import tempfile
import zipfile

import requests

from scan3d.config import load_environment
from scan3d.fileio import hash_file
from scan3d.nnunet_models import MODELS, model_dir


def install(model, archive: Path, target: Path):
    """Unpack only the pinned files, and publish the folder only once all of them match."""
    if hash_file(archive) != model.archive_sha256:
        raise RuntimeError(f"Downloaded {model.name} archive failed its pinned checksum")
    with tempfile.TemporaryDirectory(dir=target.parent) as staging, zipfile.ZipFile(archive) as bundle:
        folder = Path(staging) / target.name
        for name, expected in model.files.items():
            destination = folder / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(name) as source, destination.open("wb") as output:
                while block := source.read(4 * 1024 * 1024):
                    output.write(block)
            if hash_file(destination) != expected:
                raise RuntimeError(f"{model.name} file {name} failed its pinned checksum")
        os.replace(folder, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=sorted(MODELS))
    args = parser.parse_args()
    load_environment()
    model = MODELS[args.model]
    target = model_dir(model)
    if target.exists():
        raise SystemExit(f"{target} already exists; move it aside to download {model.name} again")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as staging:
        archive = Path(staging) / "weights.zip"
        with requests.get(model.url, stream=True, timeout=60) as response, archive.open("wb") as output:
            response.raise_for_status()
            for block in response.iter_content(4 * 1024 * 1024):
                output.write(block)
        install(model, archive, target)
    print(f"{model.name} {model.version} verified at {target}")
    print(f"License: {model.license}")


if __name__ == "__main__":
    main()

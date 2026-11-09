"""Acquire a pinned public segmentation model; never send patient data to a model host."""

import argparse
import os
from pathlib import Path
import posixpath
import tempfile
import zipfile

import requests

from scan3d.config import load_environment
from scan3d.fileio import hash_file
from scan3d.nnunet_models import MODELS, model_dir, required


def install(model, archive: Path, target: Path):
    """Unpack only the pinned files, and publish the folder only once all of them match."""
    if hash_file(archive) != model.archive_sha256:
        raise RuntimeError(f"Downloaded {model.name} archive failed its pinned checksum")
    with tempfile.TemporaryDirectory(dir=target.parent) as staging, zipfile.ZipFile(archive) as bundle:
        folder = Path(staging) / target.name
        for name, expected in model.files.items():
            destination = folder / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(posixpath.join(model.archive_folder, name)) as source, destination.open("wb") as output:
                while block := source.read(4 * 1024 * 1024):
                    output.write(block)
            if hash_file(destination) != expected:
                raise RuntimeError(f"{model.name} file {name} failed its pinned checksum")
        os.replace(folder, target)


def fetch(url, destination: Path):
    with requests.get(url, stream=True, timeout=60) as response, destination.open("wb") as output:
        response.raise_for_status()
        for block in response.iter_content(4 * 1024 * 1024):
            output.write(block)


def install_files(model, target: Path):
    """For a model published as separate files: fetch each, and publish the folder only once all of them match."""
    with tempfile.TemporaryDirectory(dir=target.parent) as staging:
        folder = Path(staging) / target.name
        for name, expected in model.files.items():
            destination = folder / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            fetch(model.url + posixpath.join(model.archive_folder, name), destination)
            if hash_file(destination) != expected:
                raise RuntimeError(f"{model.name} file {name} failed its pinned checksum")
        os.replace(folder, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=sorted(MODELS))
    parser.add_argument("--archive", type=Path, help="Install the model from this zip, downloaded by hand, instead of fetching it")
    args = parser.parse_args()
    load_environment()
    model = MODELS[args.model]
    if model_dir(model).exists():
        raise SystemExit(f"{model_dir(model)} already exists; move it aside to download {model.name} again")
    if model.sign_in and not args.archive:
        raise SystemExit(f"{model.name} needs a sign-in to download: get the zip from {model.url}, then run this again with --archive FILE")
    # A model that runs another first needs that one too; one already here is checked before each run.
    for part in reversed(required(model)):
        target = model_dir(part)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if part is model and args.archive:
            install(part, args.archive, target)
        elif not part.archive_sha256:
            install_files(part, target)
        else:
            with tempfile.TemporaryDirectory(dir=target.parent) as staging:
                archive = Path(staging) / "weights.zip"
                fetch(part.url, archive)
                install(part, archive, target)
        print(f"{part.name} {part.version} verified at {target}")
        print(f"License: {part.license}")


if __name__ == "__main__":
    main()

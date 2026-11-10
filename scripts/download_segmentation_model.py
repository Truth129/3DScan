"""Acquire a pinned public segmentation model; never send patient data to a model host."""

import argparse
import contextlib
import os
from pathlib import Path
import posixpath
import tempfile
import zipfile

import requests

from scan3d.config import load_environment
from scan3d.fileio import hash_file
from scan3d.nnunet_models import MODELS, model_dir, required


def install(model, archive, target: Path):
    """Unpack only the pinned files, and publish the folder only once all of them match.

    archive is one zip, or a list of them for a model published as several; every zip must
    match its own pin before anything is unpacked.
    """
    archives = [archive] if isinstance(archive, (str, os.PathLike)) else list(archive)
    pinned = set(model.archives.values()) if model.archives else {model.archive_sha256}
    digests = [hash_file(path) for path in archives]
    if set(digests) - pinned:
        raise RuntimeError(f"Downloaded {model.name} archive failed its pinned checksum")
    if pinned - set(digests):
        raise RuntimeError(f"{model.name} needs all {len(pinned)} of its pinned archives")
    with tempfile.TemporaryDirectory(dir=target.parent) as staging, contextlib.ExitStack() as stack:
        bundles = [stack.enter_context(zipfile.ZipFile(path)) for path in archives]
        folder = Path(staging) / target.name
        for name, expected in model.files.items():
            destination = folder / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            member = posixpath.join(model.archive_folder, name)
            bundle = next((bundle for bundle in bundles if member in bundle.namelist()), bundles[0])
            with bundle.open(member) as source, destination.open("wb") as output:
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
    parser.add_argument(
        "--archive",
        type=Path,
        action="append",
        help="Install the model from this zip, downloaded by hand, instead of fetching it; repeat it for a model published as several zips",
    )
    args = parser.parse_args()
    load_environment()
    model = MODELS[args.model]
    # A model that runs another first needs that one too; one already here is checked before each run.
    missing = [part for part in reversed(required(model)) if not model_dir(part).exists()]
    if not missing:
        raise SystemExit(f"{model_dir(model)} already exists; move it aside to download {model.name} again")
    if missing[-1] is model and model.sign_in and not args.archive:
        raise SystemExit(f"{model.name} needs a sign-in to download: get the zip from {model.url}, then run this again with --archive FILE")
    for part in missing:
        target = model_dir(part)
        target.parent.mkdir(parents=True, exist_ok=True)
        if part is model and args.archive:
            install(part, args.archive, target)
        elif part.archives:
            with tempfile.TemporaryDirectory(dir=target.parent) as staging:
                archives = [Path(staging) / name for name in part.archives]
                for archive in archives:
                    fetch(part.url + archive.name, archive)
                install(part, archives, target)
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

"""Production shell selection must not inherit experimental medical-data paths."""

import os
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def production_checkout(tmp_path):
    """Selector tests never read or edit the actual deployment pointer."""
    project = tmp_path / "editable checkout"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    for name in ("production-env.sh", "run-production.sh"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    package = project / "scan3d"
    package.mkdir()
    (package / "__init__.py").write_text("RELEASE_TAG = 'editable'\n")
    (project / "web/dist").mkdir(parents=True)
    (project / "web/dist/index.html").write_text("editable viewer")
    return project


def source_environment(project, extra=None):
    env = dict(os.environ)
    env.pop("SCAN3D_CODE_DIR", None)
    env.pop("SCAN3D_IMPORT_ROOTS", None)
    env.update(extra or {})
    script = """source "$1"
printf '%s\\n' "$SCAN3D_CODE_DIR" "$PYTHONPATH" "$PYTHONSAFEPATH" "$SCAN3D_IMPORT_ROOTS" "$SCAN3D_DATA_DIR"
"""
    return subprocess.run(
        ["bash", "-c", script, "production-env", str(project / "scripts/production-env.sh")], env=env, cwd=project, check=True, capture_output=True, text=True
    ).stdout.splitlines()


def frozen_release(tmp_path, name="frozen release"):
    release = tmp_path / name
    (release / "scan3d").mkdir(parents=True)
    (release / "scan3d/__init__.py").write_text("RELEASE_TAG = 'frozen'\n")
    (release / "web/dist").mkdir(parents=True)
    (release / "web/dist/index.html").write_text("frozen viewer")
    return release


def select_release(project, release):
    pointer = project / ".runtime/rocm10/code-dir"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(str(release) + "\n")


def test_production_paths_override_inherited_experimental_paths():
    env = dict(
        os.environ,
        SCAN3D_DATA_DIR="/experiment/data",
        TOTALSEG_HOME_DIR="/experiment/config",
        TOTALSEG_WEIGHTS_PATH="/experiment/weights",
        SCAN3D_NNINTERACTIVE_MODEL="/experiment/model",
        SCAN3D_GPU_PROOF="/experiment/proof.json",
        ROCM_PATH="/opt/old-rocm",
        HIP_PATH="/opt/old-hip",
        LD_LIBRARY_PATH="/opt/old-libraries",
        HSA_OVERRIDE_GFX_VERSION="fake",
    )
    script = """source "$1"
printf '%s\n' "$SCAN3D_DATA_DIR" "$TOTALSEG_HOME_DIR" "$TOTALSEG_WEIGHTS_PATH" "$SCAN3D_NNINTERACTIVE_MODEL" "$SCAN3D_GPU_PROOF" "$XDG_CACHE_HOME" "$MIOPEN_USER_DB_PATH" "$MIOPEN_CUSTOM_CACHE_DIR" "${ROCM_PATH-unset}" "${HIP_PATH-unset}" "${LD_LIBRARY_PATH-unset}" "${HSA_OVERRIDE_GFX_VERSION-unset}"
"""
    result = subprocess.run(
        ["bash", "-c", script, "production-env", str(ROOT / "scripts/production-env.sh")], env=env, check=True, capture_output=True, text=True
    )
    assert result.stdout.splitlines() == [
        str(ROOT / "data"),
        str(ROOT / ".runtime/totalsegmentator"),
        str(ROOT / ".runtime/totalsegmentator/nnunet/results"),
        str(ROOT / ".runtime/nninteractive/nnInteractive_v1.0"),
        str(ROOT / ".runtime/rocm10/gpu-proof.json"),
        str(ROOT / ".cache/rocm10"),
        str(ROOT / ".cache/rocm10/miopen-db"),
        str(ROOT / ".cache/rocm10/miopen-kernels"),
        "unset",
        "unset",
        "unset",
        "unset",
    ]


def test_absent_release_pointer_keeps_the_checkout_default(production_checkout):
    project = production_checkout
    assert source_environment(project) == [str(project), str(project), "1", str(project), str(project / "data")]
    assert not (project / "data").exists(), "Sourcing configuration must not open the case store"


def test_release_pointer_changes_only_source_selection(production_checkout, tmp_path):
    project = production_checkout
    release = frozen_release(tmp_path)
    select_release(project, release)
    assert source_environment(project) == [str(release), str(release), "1", str(project), str(project / "data")]


def test_explicit_code_override_precedes_release_pointer(production_checkout, tmp_path):
    project = production_checkout
    select_release(project, frozen_release(tmp_path))
    assert source_environment(project, {"SCAN3D_CODE_DIR": str(project)})[:3] == [str(project), str(project), "1"]


def test_python_from_editable_checkout_imports_only_selected_release(production_checkout, tmp_path):
    project = production_checkout
    release = frozen_release(tmp_path)
    select_release(project, release)
    env = dict(os.environ, PYTHONPATH=str(project), PYTHONSAFEPATH="0")
    env.pop("SCAN3D_CODE_DIR", None)
    script = """source "$1"
exec "$2" -c 'import scan3d; print(scan3d.RELEASE_TAG); print(scan3d.__file__)'
"""
    result = subprocess.run(
        ["bash", "-c", script, "production-env", str(project / "scripts/production-env.sh"), sys.executable],
        env=env,
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == ["frozen", str(release / "scan3d/__init__.py")]


def test_explicit_import_allowlist_survives_source_selection(production_checkout, tmp_path):
    project = production_checkout
    select_release(project, frozen_release(tmp_path))
    allowed = str(tmp_path / "external scans")
    assert source_environment(project, {"SCAN3D_IMPORT_ROOTS": allowed})[3] == allowed


def test_launcher_uses_absolute_runtime_and_selected_release(production_checkout, tmp_path):
    """Exercise the launcher without opening a socket or the real case store."""
    installed = ROOT / ".venv-rocm10-py314"
    if not (installed / "bin/python").is_file():
        pytest.skip("The separately provisioned production interpreter is required")
    project = production_checkout
    (project / ".venv-rocm10-py314").symlink_to(installed, target_is_directory=True)
    (project / "scan3d/__main__.py").write_text("raise AssertionError('Editable source was launched')\n")
    release = frozen_release(tmp_path)
    (release / "scan3d/__main__.py").write_text(
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "print(json.dumps({'source': str(Path(__file__).resolve()), 'cwd': os.getcwd(), "
        "'executable': sys.executable, 'args': sys.argv[1:], "
        "'data': os.environ['SCAN3D_DATA_DIR'], 'imports': os.environ['SCAN3D_IMPORT_ROOTS']}))\n"
    )
    select_release(project, release)
    env = dict(os.environ, SCAN3D_PORT="9013", SCAN3D_HOST="127.0.0.1")
    env.pop("SCAN3D_CODE_DIR", None)
    env.pop("SCAN3D_IMPORT_ROOTS", None)
    result = subprocess.run(["bash", str(project / "scripts/run-production.sh")], env=env, cwd=project, capture_output=True, text=True, check=True)
    launched = json.loads(result.stdout)
    assert launched == {
        "source": str(release / "scan3d/__main__.py"),
        "cwd": str(release),
        "executable": str(project / ".venv-rocm10-py314/bin/python"),
        "args": ["serve", "--host", "127.0.0.1", "--port", "9013"],
        "data": str(project / "data"),
        "imports": str(project),
    }


@pytest.mark.parametrize("invalid", ["relative-release", "missing-package", "missing-viewer", "multiline"])
def test_invalid_release_fails_before_interpreter_or_data_access(production_checkout, tmp_path, invalid):
    project = production_checkout
    interpreter = project / ".venv-rocm10-py314/bin/python"
    interpreter.parent.mkdir(parents=True)
    marker = tmp_path / "interpreter-was-started"
    interpreter.write_text('#!/bin/sh\ntouch "$INTERPRETER_TEST_MARKER"\nexit 99\n')
    interpreter.chmod(0o700)
    release = frozen_release(tmp_path)
    if invalid == "relative-release":
        chosen = "relative"
    elif invalid == "missing-package":
        (release / "scan3d/__init__.py").unlink()
        chosen = str(release)
    elif invalid == "missing-viewer":
        (release / "web/dist/index.html").unlink()
        chosen = str(release)
    else:
        chosen = str(release) + "\nextra-line"
    env = dict(os.environ, SCAN3D_CODE_DIR=chosen, INTERPRETER_TEST_MARKER=str(marker))
    result = subprocess.run(["bash", str(project / "scripts/run-production.sh")], env=env, cwd=project, capture_output=True, text=True)
    assert result.returncode == 1
    assert "absolute release directory" in result.stderr
    assert not marker.exists()
    assert not (project / "data").exists()

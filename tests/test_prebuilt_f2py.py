"""test_prebuilt_f2py
----------------------------------

Exercises the provenance-based pre-generated source takeover modes of
``add_f2py_target`` (PREBUILT REQUIRE / PREFER / REGENERATE and the
F2PY_PREBUILT cache variable) using the `prebuilt-f2py` sample project.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from scikit_build_core.errors import FailedLiveProcessError

from . import (
    cmake_build_dir,
    execute_setup_py,
    get_ext_suffix,
    initialize_git_repo_and_commit,
    prepare_project,
    push_env,
)

pytest.importorskip("numpy")

pytestmark = [
    pytest.mark.fortran,
    pytest.mark.skipif(sys.platform.startswith("win"), reason="Fortran not supported on Windows"),
    pytest.mark.skipif(not ("FC" in os.environ or shutil.which("gfortran")), reason="GFortran required"),
]

_F2PY_OUTPUTS = (
    "_hellomodule.c",
    "_hello-f2pywrappers.f",
    "_hello-f2pywrappers2.f90",
)


@pytest.fixture(scope="module")
def shipped_prebuilt(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the sample once and return a directory with the shipped files."""
    project_dir = tmp_path_factory.mktemp("prebuilt-f2py-golden")
    prepare_project("prebuilt-f2py", project_dir)
    initialize_git_repo_and_commit(project_dir, verbose=False)

    with execute_setup_py(project_dir, ["build"]):
        pass

    assert not (project_dir / "hello" / "_hellomodule.c").exists()
    build_dir = cmake_build_dir(project_dir)
    assert build_dir is not None

    stash = tmp_path_factory.mktemp("prebuilt-f2py-shipped")
    module_source: Path | None = None
    for name in _F2PY_OUTPUTS:
        matches = sorted(build_dir.glob(f"**/{name}"))
        assert len(matches) == 1, (name, matches)
        shutil.copy2(matches[0], stash / name)
        if name == "_hellomodule.c":
            module_source = matches[0]

    assert module_source is not None
    source_credential = Path(f"{module_source}.provenance")
    assert source_credential.is_file()
    shutil.copy2(source_credential, stash / "_hellomodule.c.provenance")
    return stash


def _change_pyf(project: Path) -> None:
    # The .pyf is the f2py generator input; the .f90 implementation is a
    # regular library source and is not covered by the credential.
    with (project / "hello" / "_hello.pyf").open("a") as f:
        f.write("! a change the credential cannot know about\n")


def _drop_credential(project: Path) -> None:
    (project / "hello" / "_hellomodule.c.provenance").unlink()


@contextmanager
def _prepared_project(
    tmp_path: Path,
    shipped: Path,
    *,
    keyword: str | None = None,
    cache: str | None = None,
    mutate: Callable[[Path], None] | None = None,
) -> Iterator[Path]:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    prepare_project("prebuilt-f2py", project)

    for name in (*_F2PY_OUTPUTS, "_hellomodule.c.provenance"):
        shutil.copy2(shipped / name, project / "hello" / name)
    if mutate is not None:
        mutate(project)

    initialize_git_repo_and_commit(project, verbose=False)

    env: dict[str, str] = {}
    if keyword is not None:
        env["SKB_F2PY_PREBUILT_KEYWORD"] = keyword
    if cache is not None:
        env["SKB_F2PY_PREBUILT_CACHE"] = cache

    with push_env(**env):
        with execute_setup_py(project, ["build"]):
            yield project


def test_require_takes_over_verified_sources(tmp_path, shipped_prebuilt, capfd):
    with _prepared_project(tmp_path, shipped_prebuilt, keyword="REQUIRE") as project:
        candidates = [p for p in project.glob(f"build/**/_hello{get_ext_suffix()}") if "CMakeFiles" not in p.parts]
        assert candidates, "no compiled extension found in the build directory"
        check = (
            "import importlib.util, os; "
            "spec = importlib.util.spec_from_file_location('_hello', os.environ['SKB_EXT']); "
            "module = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(module); "
            "assert hasattr(module, 'hello')"
        )
        result = subprocess.run(
            [sys.executable, "-c", check],
            env={**os.environ, "SKB_EXT": str(candidates[0])},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    out, err = capfd.readouterr()
    assert "taking over verified pre-generated sources" in out + err


def test_require_with_changed_input_fails(tmp_path, shipped_prebuilt, capfd):
    with pytest.raises(FailedLiveProcessError) as exc_info:
        with _prepared_project(tmp_path, shipped_prebuilt, keyword="REQUIRE", mutate=_change_pyf):
            pass

    assert "CMake configuration failed" in str(exc_info.value)
    _, err = capfd.readouterr()
    assert "changed since the credential was written" in err


def test_prefer_regenerates_without_credential(tmp_path, shipped_prebuilt, capfd):
    with _prepared_project(tmp_path, shipped_prebuilt, cache="PREFER", mutate=_drop_credential):
        pass

    out, err = capfd.readouterr()
    assert "provenance credential not found" in out + err
    assert "regenerating with f2py" in out + err

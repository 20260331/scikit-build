"""test_prebuilt_cython
----------------------------------

Exercises the provenance-based pre-generated source takeover modes of
``add_cython_target`` (PREBUILT REQUIRE / PREFER / REGENERATE and the
CYTHON_PREBUILT cache variable) using the `prebuilt-cython` sample project.

A first, normal build produces ``_hello.cxx`` and its ``.provenance``
credential in the CMake build tree.  They are then "shipped" next to the
``_hello.pyx`` input of fresh project copies, like an sdist would do.
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

pytestmark = pytest.mark.filterwarnings(
    "ignore:.*ends with a trailing slash, which is not supported by setuptools:FutureWarning"
)


@pytest.fixture(scope="module")
def shipped_prebuilt(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """Build the sample once and return the generated source + credential."""
    project_dir = tmp_path_factory.mktemp("prebuilt-cython-golden")
    prepare_project("prebuilt-cython", project_dir)
    initialize_git_repo_and_commit(project_dir, verbose=False)

    with execute_setup_py(project_dir, ["build"]):
        pass

    # Default behavior: generation happens in the build tree and a credential
    # is written next to the generated source; nothing leaks into the tree.
    assert not (project_dir / "hello" / "_hello.cxx").exists()
    build_dir = cmake_build_dir(project_dir)
    assert build_dir is not None

    generated = sorted(build_dir.glob("**/_hello.cxx"))
    assert len(generated) == 1, generated
    credential = Path(f"{generated[0]}.provenance")
    assert credential.is_file()
    assert "provenance-version: 1" in credential.read_text()

    stash = tmp_path_factory.mktemp("prebuilt-cython-shipped")
    source = stash / "_hello.cxx"
    sidecar = stash / "_hello.cxx.provenance"
    shutil.copy2(generated[0], source)
    shutil.copy2(credential, sidecar)
    return source, sidecar


def _change_pyx(project: Path) -> None:
    with (project / "hello" / "_hello.pyx").open("a") as f:
        f.write("\n# a change the credential cannot know about\n")


def _tamper_candidate(project: Path) -> None:
    with (project / "hello" / "_hello.cxx").open("a") as f:
        f.write("\n/* edited after the credential was written */\n")


def _drop_credential(project: Path) -> None:
    (project / "hello" / "_hello.cxx.provenance").unlink()


@contextmanager
def _prepared_project(
    tmp_path: Path,
    shipped: tuple[Path, Path],
    *,
    keyword: str | None = None,
    cache: str | None = None,
    hide_cython: bool = False,
    cython_flags: str | None = None,
    mutate: Callable[[Path], None] | None = None,
) -> Iterator[Path]:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    prepare_project("prebuilt-cython", project)

    # Emulate an sdist shipping the pre-generated source and its credential.
    shutil.copy2(shipped[0], project / "hello" / "_hello.cxx")
    shutil.copy2(shipped[1], project / "hello" / "_hello.cxx.provenance")
    if mutate is not None:
        mutate(project)

    initialize_git_repo_and_commit(project, verbose=False)

    env: dict[str, str] = {}
    if keyword is not None:
        env["SKB_CYTHON_PREBUILT_KEYWORD"] = keyword
    if cache is not None:
        env["SKB_CYTHON_PREBUILT_CACHE"] = cache
    if hide_cython:
        env["SKB_HIDE_CYTHON"] = "1"
    if cython_flags is not None:
        env["SKB_CYTHON_FLAGS"] = cython_flags

    with push_env(**env):
        with execute_setup_py(project, ["build"]):
            yield project


def _assert_built_extension_works(project: Path) -> None:
    candidates = [p for p in project.glob(f"build/**/_hello{get_ext_suffix()}") if "CMakeFiles" not in p.parts]
    # The staged copy under build/lib.* is the final artifact; prefer it.
    assert candidates, "no compiled extension found in the build directory"
    staged = [p for p in candidates if "lib." in str(p)]
    extension = (staged or candidates)[0]

    # Loaded in a child process because the PyInit symbol is named after the
    # extension (_hello), not the loader module name used in this test.
    check = (
        "import importlib.util, os; "
        "spec = importlib.util.spec_from_file_location('_hello', os.environ['SKB_EXT']); "
        "module = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(module); "
        "assert module.size() == 21463; "
        "module.hello('World')"
    )
    result = subprocess.run(
        [sys.executable, "-c", check],
        env={**os.environ, "SKB_EXT": str(extension)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_require_takes_over_verified_source_without_cython(tmp_path, shipped_prebuilt, capfd):
    with _prepared_project(tmp_path, shipped_prebuilt, keyword="REQUIRE", hide_cython=True) as project:
        _assert_built_extension_works(project)

    out, err = capfd.readouterr()
    assert "taking over verified pre-generated source" in out + err
    # The "generator" resolves to the no-op executable, yet the extension
    # built and ran: the verified source-tree copy was taken over.
    assert ".skb-noop" in out + err


def test_prefer_takes_over_valid_source(tmp_path, shipped_prebuilt, capfd):
    with _prepared_project(tmp_path, shipped_prebuilt, cache="PREFER") as project:
        _assert_built_extension_works(project)

    out, err = capfd.readouterr()
    assert "taking over verified pre-generated source" in out + err


def test_require_without_credential_fails(tmp_path, shipped_prebuilt, capfd):
    with pytest.raises(FailedLiveProcessError) as exc_info:
        with _prepared_project(
            tmp_path,
            shipped_prebuilt,
            keyword="REQUIRE",
            hide_cython=True,
            mutate=_drop_credential,
        ):
            pass

    assert "CMake configuration failed" in str(exc_info.value)
    _, err = capfd.readouterr()
    assert "provenance credential not found" in err
    # The unverified source-tree copy must not have been compiled instead.
    assert "PREBUILT REQUIRE" in err


def test_require_with_changed_input_fails(tmp_path, shipped_prebuilt, capfd):
    with pytest.raises(FailedLiveProcessError) as exc_info:
        with _prepared_project(
            tmp_path,
            shipped_prebuilt,
            keyword="REQUIRE",
            hide_cython=True,
            mutate=_change_pyx,
        ):
            pass

    assert "CMake configuration failed" in str(exc_info.value)
    _, err = capfd.readouterr()
    assert "changed since the credential was written" in err


def test_require_with_tampered_output_fails(tmp_path, shipped_prebuilt, capfd):
    with pytest.raises(FailedLiveProcessError) as exc_info:
        with _prepared_project(
            tmp_path,
            shipped_prebuilt,
            keyword="REQUIRE",
            hide_cython=True,
            mutate=_tamper_candidate,
        ):
            pass

    assert "CMake configuration failed" in str(exc_info.value)
    _, err = capfd.readouterr()
    # The message is line-wrapped by CMake, so stay within one rendered line.
    assert "was modified after the credential" in err
    assert "content hash mismatch" in err


def test_require_with_different_options_fails(tmp_path, shipped_prebuilt, capfd):
    with pytest.raises(FailedLiveProcessError) as exc_info:
        with _prepared_project(
            tmp_path,
            shipped_prebuilt,
            keyword="REQUIRE",
            hide_cython=True,
            cython_flags="-X c_string_encoding=default",
        ):
            pass

    assert "CMake configuration failed" in str(exc_info.value)
    _, err = capfd.readouterr()
    assert "generation options mismatch" in err


def test_prefer_regenerates_when_input_changed(tmp_path, shipped_prebuilt, capfd):
    with _prepared_project(tmp_path, shipped_prebuilt, cache="PREFER", mutate=_change_pyx) as project:
        _assert_built_extension_works(project)

    out, err = capfd.readouterr()
    # The stale source-tree copy is reported and never used directly.
    assert "regenerating with Cython" in out + err


def test_prefer_regenerates_without_credential(tmp_path, shipped_prebuilt, capfd):
    with _prepared_project(tmp_path, shipped_prebuilt, cache="PREFER", mutate=_drop_credential) as project:
        _assert_built_extension_works(project)

    out, err = capfd.readouterr()
    assert "provenance credential not found" in out + err


@pytest.mark.parametrize("mode", [None, "REGENERATE"])
def test_regenerate_never_compiles_source_tree_copy(tmp_path, shipped_prebuilt, mode):
    # A valid prebuilt source is shipped, Cython is unavailable, and the mode
    # asks for regeneration: the build must fail rather than silently
    # compiling the copy sitting in the source tree.
    with pytest.raises(FailedLiveProcessError) as exc_info:
        with _prepared_project(tmp_path, shipped_prebuilt, cache=mode, hide_cython=True):
            pass

    assert "CMake build failed" in str(exc_info.value)

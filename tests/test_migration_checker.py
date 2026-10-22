"""test_migration_checker
----------------------------------

Tests for the static scikit-build 0.x -> 1.0 migration checker. The checker
must never import or execute the scanned project, so these tests only create
text files in temporary directories -- no CMake build is involved.
"""

from __future__ import annotations

import json
import textwrap
from collections import Counter
from pathlib import Path

import pytest

from skbuild.migration_checker import Status, check_project, format_json, format_report, main


def write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def status_counts(findings):
    return Counter(f.status for f in findings)


def codes(findings, status=None):
    if status is not None:
        findings = [f for f in findings if f.status == status]
    return {f.code for f in findings}


SAMPLES = Path(__file__).parent / "samples"


# ---------------------------------------------------------------------------
# setup.py
# ---------------------------------------------------------------------------


def test_clean_modern_project(tmp_path):
    write(
        tmp_path,
        "setup.py",
        """
        from skbuild import setup

        setup(
            name="hello",
            packages=["hello"],
            cmake_args=["-DFEATURE:BOOL=ON"],
            cmake_source_dir=".",
            cmake_install_dir="",
            cmake_install_target="install",
            cmake_process_manifest_hook=lambda files: files,
        )
        """,
    )
    write(
        tmp_path,
        "CMakeLists.txt",
        """
        cmake_minimum_required(VERSION 3.15)
        project(hello)
        find_package(PythonExtensions REQUIRED)
        if(SKBUILD)
          message(STATUS "building under scikit-build ${SKBUILD}")
        endif()
        add_library(_hello MODULE hello/_hello.cxx)
        python_extension_module(_hello)
        install(TARGETS _hello LIBRARY DESTINATION hello)
        set(inc "${PYTHON_PREFIX}")
        """,
    )
    write(
        tmp_path,
        "pyproject.toml",
        """
        [build-system]
        requires = ["scikit-build>=1", "cmake", "ninja"]
        build-backend = "scikit_build_core.setuptools.build_meta"

        [tool.scikit-build]
        editable.mode = "inplace"
        """,
    )
    write(tmp_path, "MANIFEST.in", "global-include CMakeLists.txt *.cmake\n")

    findings = check_project(tmp_path)
    counts = status_counts(findings)
    assert counts[Status.REMOVED] == 0
    assert counts[Status.IGNORED] == 0
    assert counts[Status.UNKNOWN] == 0
    assert "setup-arg" in codes(findings, Status.COMPATIBLE)
    assert "cmake-module" in codes(findings, Status.COMPATIBLE)
    assert "cmake-command" in codes(findings, Status.COMPATIBLE)
    assert "cmake-sklbuild-variable" in codes(findings, Status.COMPATIBLE)
    assert "cmake-variable" in codes(findings, Status.COMPATIBLE)
    assert "pyproject-backend" in codes(findings, Status.COMPATIBLE)


def test_removed_python_api(tmp_path):
    write(
        tmp_path,
        "setup.py",
        """
        import sys
        from skbuild import setup
        from skbuild.command.build_ext import build_ext
        from skbuild.platform_specifics import get_platform
        from skbuild.cmaker import CMaker
        from skbuild.constants import CMAKE_BUILD_DIR
        from skbuild.utils import mkdir_p
        from skbuild.setuptools_wrap import setup as old_setup
        from skbuild.exceptions import SKBuildGeneratorNotFoundError

        sys.argv.append("--force-cmake")

        setup(
            name="old",
            cmake_with_sdist=True,
            script_args=["--skip-cmake", "-G", "--"],
        )
        """,
    )
    write(tmp_path, "CMakeLists.txt", "project(old)\n")

    findings = check_project(tmp_path)
    removed = codes(findings, Status.REMOVED)
    assert "py-import-removed" in removed  # all removed imports share the code
    assert "setup-arg-removed" in removed  # cmake_with_sdist=True
    assert "setup-cli-option-removed" in removed  # --force-cmake / --skip-cmake / -G / --
    messages = " ".join(f.message for f in findings)
    assert "command.build_ext" in messages
    assert "platform_specifics" in messages
    assert "CMaker" in messages
    assert "CMAKE_BUILD_DIR" in messages
    assert "mkdir_p" in messages
    assert "setuptools_wrap" in messages
    assert "SKBuildGeneratorNotFoundError" in messages
    # Manifest finding is expected too; every finding must carry a suggestion.
    assert all(f.suggestion for f in findings if f.status == Status.REMOVED)


def test_ignored_and_dynamic_usage(tmp_path):
    write(
        tmp_path,
        "setup.py",
        """
        import skbuild.utils
        from skbuild import cmaker
        from skbuild import setup

        options = {"cmake_with_sdist": maybe}

        setup(
            name="dyn",
            cmake_languages=["C"],
            cmake_minimum_required_version="3.15",
            cmake_with_sdist=some_flag,
            **options,
        )
        """,
    )
    write(tmp_path, "CMakeLists.txt", "project(dyn)\n")

    findings = check_project(tmp_path)
    ignored = codes(findings, Status.IGNORED)
    unknown = codes(findings, Status.UNKNOWN)
    assert "py-import-shim" in ignored  # skbuild.utils / cmaker shims
    assert "setup-arg-ignored" in ignored  # cmake_languages / minimum version
    assert "setup-arg-unknown" in unknown  # dynamic cmake_with_sdist
    assert "setup-dynamic-kwargs" in unknown  # **options


def test_compatible_shims(tmp_path):
    write(
        tmp_path,
        "setup.py",
        """
        import skbuild
        from skbuild import setup
        from skbuild.constants import CMAKE_INSTALL_DIR, skbuild_plat_name
        from skbuild.cmaker import get_cmake_version
        from skbuild.exceptions import SKBuildError
        """,
    )
    findings = check_project(tmp_path)
    compatible = codes(findings, Status.COMPATIBLE)
    assert "py-import" in compatible
    assert "py-import-setup" in compatible
    assert status_counts(findings)[Status.REMOVED] == 0


def test_attribute_access_classification(tmp_path):
    write(
        tmp_path,
        "setup.py",
        """
        import skbuild
        from skbuild import constants, exceptions

        skbuild.platform_specifics.get_platform()
        x = constants.SKBUILD_DIR()
        y = constants.CMAKE_INSTALL_DIR()
        z = exceptions.SKBuildInvalidFileInstallationError
        ok = skbuild.setup
        version = skbuild.__version__
        """,
    )
    findings = check_project(tmp_path)
    messages = [f.message for f in findings if f.status == Status.REMOVED]
    assert any("platform_specifics" in m for m in messages)
    assert any("SKBUILD_DIR" in m for m in messages)
    assert any("SKBuildInvalidFileInstallationError" in m for m in messages)
    assert not any("CMAKE_INSTALL_DIR" in m for m in messages)
    assert any("__version__" in f.message for f in findings if f.status == Status.COMPATIBLE)


def test_cmake_install_prefix_in_args_is_removed(tmp_path):
    write(
        tmp_path,
        "setup.py",
        """
        from skbuild import setup
        setup(name="p", cmake_args=["-DCMAKE_INSTALL_PREFIX:PATH=/tmp/x"])
        """,
    )
    findings = check_project(tmp_path)
    assert "setup-cmake-install-prefix" in codes(findings, Status.REMOVED)


def test_syntax_error_is_unknown_not_executed(tmp_path):
    write(
        tmp_path,
        "setup.py",
        """
        from skbuild import setup
        setup(name="broken",,,
        """,
    )
    findings = check_project(tmp_path)
    assert "py-parse-error" in codes(findings, Status.UNKNOWN)


# ---------------------------------------------------------------------------
# CMake files
# ---------------------------------------------------------------------------


def test_cmake_classification(tmp_path):
    write(
        tmp_path,
        "CMakeLists.txt",
        r"""
        find_package(Cython REQUIRED)
        include(UseCython)
        find_package(NumPy)
        add_cython_target(_mod.pyx C)
        set(numpy_inc "${NumPy_INCLUDE_DIRS}")
        set(cy "${CYTHON_C_EXTENSION}")
        # if(SKBUILD STREQUAL "TRUE") -- commented out, must not be flagged
        if("${SKBUILD}" STREQUAL "TRUE")
          message(FATAL_ERROR "old exact value check")
        endif()
        if(NOT SKBUILD)
          message(STATUS "standalone")
        endif()
        if(NOT DEFINED SKBUILD)
          message(FATAL_ERROR "SKBUILD is not set")
        endif()
        project(fail_unless_skbuild_set NONE)
        """,
    )
    findings = check_project(tmp_path)
    removed = codes(findings, Status.REMOVED)
    compatible = codes(findings, Status.COMPATIBLE)
    assert "cmake-sklbuild-value" in removed
    assert "cmake-module" in compatible
    assert "cmake-command" in compatible
    assert "cmake-variable" in compatible
    assert "cmake-sklbuild-variable" in compatible
    # The comment line must not have produced a finding.
    assert all(f.line != 7 for f in findings)
    # 'DEFINED SKBUILD' and a project name merely containing the word are fine.
    assert not any(f.status == Status.REMOVED and f.line in {14, 17} for f in findings)
    assert any(f.code == "cmake-sklbuild-variable" and f.line == 14 for f in findings)


def test_cmake_hardcoded_old_layout_removed(tmp_path):
    write(
        tmp_path,
        "CMakeLists.txt",
        """
        set(old "_skbuild/linux-x86_64-3.10/cmake-install/include")
        set(mods "${PYTHON_PREFIX}/lib/python3.10/site-packages/skbuild/resources/cmake")
        """,
    )
    findings = check_project(tmp_path)
    assert "cmake-hardcoded-path" in codes(findings, Status.REMOVED)


# ---------------------------------------------------------------------------
# pyproject.toml / MANIFEST.in / editable installs
# ---------------------------------------------------------------------------


def test_old_version_pin_blocks_upgrade(tmp_path):
    write(
        tmp_path,
        "pyproject.toml",
        """
        [build-system]
        requires = ["setuptools", "scikit-build<1"]
        build-backend = "setuptools.build_meta"
        """,
    )
    findings = check_project(tmp_path)
    assert "pyproject-old-pin" in codes(findings, Status.REMOVED)


def test_missing_build_system_is_unknown(tmp_path):
    write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    findings = check_project(tmp_path)
    assert "pyproject-no-build-system" in codes(findings, Status.UNKNOWN)


def test_editable_requires_inplace(tmp_path):
    write(tmp_path, "setup.py", "from skbuild import setup\nsetup(name='x')\n")
    write(
        tmp_path,
        ".github/workflows/ci.yml",
        """
        jobs:
          dev:
            run: pip install -e .
        """,
    )
    assert "pyproject-editable" in codes(check_project(tmp_path), Status.REMOVED)

    write(
        tmp_path,
        "pyproject.toml",
        """
        [build-system]
        requires = ["scikit-build>=1"]
        [tool.scikit-build]
        editable.mode = "inplace"
        """,
    )
    assert "pyproject-editable" not in codes(check_project(tmp_path), Status.REMOVED)


def test_manifest_git_behavior(tmp_path):
    write(tmp_path, "CMakeLists.txt", "project(x)\n")
    assert "manifest-git" in codes(check_project(tmp_path), Status.REMOVED)

    write(tmp_path, "MANIFEST.in", "include README.md\n")
    assert "manifest-git" in codes(check_project(tmp_path), Status.REMOVED)

    (tmp_path / "MANIFEST.in").write_text("global-include *.cmake\n", encoding="utf-8")
    assert "manifest-git" not in codes(check_project(tmp_path), Status.REMOVED)


def test_pure_project_without_manifest_not_flagged(tmp_path):
    write(tmp_path, "setup.py", "from skbuild import setup\nsetup(name='pure', packages=['p'])\n")
    assert "manifest-git" not in codes(check_project(tmp_path), Status.REMOVED)


# ---------------------------------------------------------------------------
# Build / CI scripts
# ---------------------------------------------------------------------------


def test_removed_cli_options_in_makefile(tmp_path):
    write(
        tmp_path,
        "Makefile",
        """
        wheel:
        \tpython setup.py bdist_wheel --build-type Release --generator Ninja -j 4 -- -DFOO=ON

        other:
        \tmake -j4 all
        """,
    )
    findings = check_project(tmp_path)
    removed = [f for f in findings if f.code == "cli-option-removed"]
    assert len(removed) >= 3  # --build-type, --generator/-G handling, -j, sections
    make_only = [f for f in removed if f.line == 6]
    assert make_only == []  # plain 'make -j4' is not a setup.py invocation


def test_pip_global_option_scanned(tmp_path):
    write(
        tmp_path,
        "tox.ini",
        "[testenv]\ncommands = pip wheel . --global-option=--skip-cmake\n",
    )
    assert "cli-option-removed" in codes(check_project(tmp_path), Status.REMOVED)


# ---------------------------------------------------------------------------
# No-execution guarantee, reporting and CLI
# ---------------------------------------------------------------------------


def test_checker_never_executes_setup_py(tmp_path):
    write(
        tmp_path,
        "setup.py",
        """
        from skbuild import setup

        open("SENTINEL", "w").write("executed")
        raise RuntimeError("setup.py must never be executed by the checker")

        setup(name="do-not-run", cmake_with_sdist=True)
        """,
    )
    write(tmp_path, "CMakeLists.txt", "project(x)\n")
    rc = main([str(tmp_path)])
    assert rc == 1  # removed usage found
    assert not (tmp_path / "SENTINEL").exists()


def test_reports_and_exit_codes(tmp_path, capsys):
    write(
        tmp_path,
        "setup.py",
        "from skbuild import setup\nsetup(name='x')\n",
    )
    write(tmp_path, "CMakeLists.txt", "project(x)\n")
    write(tmp_path, "MANIFEST.in", "global-include CMakeLists.txt\n")
    write(
        tmp_path,
        "pyproject.toml",
        '[build-system]\nrequires = ["scikit-build>=1"]\nbuild-backend = "setuptools.build_meta"\n',
    )

    rc = main([str(tmp_path), "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["removed"] == 0
    assert isinstance(payload["findings"], list)

    rc = main([str(tmp_path)])
    assert rc == 0
    text = capsys.readouterr().out
    assert "scikit-build 1.0 migration check" in text
    assert "Summary:" in text

    findings = check_project(tmp_path)
    assert json.loads(format_json(tmp_path, findings))["project"] == str(tmp_path)
    assert "Still compatible" in format_report(tmp_path, findings)

    with pytest.raises(SystemExit):
        main([str(tmp_path / "does-not-exist")])


def test_scan_hello_cpp_sample():
    findings = check_project(SAMPLES / "hello-cpp")
    counts = status_counts(findings)
    assert counts[Status.REMOVED] == 0
    assert counts[Status.IGNORED] == 0
    # The sample predates PEP 517 and ships no pyproject.toml: that is advisory.
    assert "pyproject-no-build-system" in codes(findings, Status.UNKNOWN)
    compatible_codes = codes(findings, Status.COMPATIBLE)
    assert "py-import-setup" in compatible_codes
    assert "cmake-module" in compatible_codes
    assert "cmake-command" in compatible_codes
    # The sample ships a MANIFEST.in including its CMakeLists.txt files.
    assert "manifest-git" not in codes(findings, Status.REMOVED)

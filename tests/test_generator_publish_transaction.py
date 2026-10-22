"""test_generator_publish_transaction
-------------------------------------

Cython and F2PY generate a set of related files in one invocation. The
``add_cython_target`` and ``add_f2py_target`` CMake helpers must therefore
generate into a private staging directory and only replace the build outputs
once the complete output set has been produced; on failure, the previously
generated outputs must be left untouched.

These tests exercise ``PublishGeneratedOutputs.cmake`` directly and drive the
two helpers through small standalone CMake projects (generation only; no
compiler-built extension is needed).
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DIR = Path(__file__).parent.resolve()
BASE = DIR.parent
CMAKE_MODULES = BASE / "skbuild" / "resources" / "cmake"
PUBLISH_SCRIPT = CMAKE_MODULES / "PublishGeneratedOutputs.cmake"

# F2PY refuses to generate a Python module for a raw Fortran subroutine
# without this input; it aborts before writing any output.
BROKEN_F2PY_INPUT = "subroutine 1bad-name(\n"


def _cmake_executable() -> str | None:
    for name in ("cmake3", "cmake"):
        executable = shutil.which(name)
        if executable is not None:
            return executable
    try:
        cmake_module = importlib.import_module("cmake")
    except ImportError:
        return None
    cmake_name = "cmake.exe" if os.name == "nt" else "cmake"
    return str(Path(cmake_module.CMAKE_BIN_DIR) / cmake_name)


def _tool_env() -> dict[str, str]:
    """Environment with the active Python's scripts directory first on PATH so
    the CMake find modules locate cython/f2py/ninja from the same env."""
    env = os.environ.copy()
    scripts_dir = Path(sys.executable).parent
    env["PATH"] = f"{scripts_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def _generator() -> tuple[str, str] | None:
    """Return (generator name, build tool) for the fastest available one."""
    scripts_dir = Path(sys.executable).parent
    ninja_exe = "ninja.exe" if os.name == "nt" else "ninja"
    if shutil.which("ninja") or (scripts_dir / ninja_exe).exists():
        return "Ninja", "ninja"
    if os.name != "nt" and shutil.which("make"):
        return "Unix Makefiles", "make"
    return None


def _run_cmake(
    *args: str,
    cwd: Path,
    env: dict[str, str],
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmake_executable = _cmake_executable()
    assert cmake_executable is not None  # callers skip the test when cmake is missing
    result = subprocess.run(
        [cmake_executable, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        pytest.fail(f"cmake {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result


def test_publish_script_replaces_outputs_on_success(tmp_path: Path) -> None:
    if _cmake_executable() is None:
        pytest.skip("cmake executable not available")

    staging = tmp_path / "staging"
    final = tmp_path / "final"
    staging.mkdir()
    final.mkdir()

    (staging / "a.txt").write_text("new-a\n")
    (staging / "b.txt").write_text("new-b\n")
    (final / "a.txt").write_text("old-a\n")
    (final / "b.txt").write_text("old-b\n")
    (final / "c.txt").write_text("old-optional\n")

    result = _run_cmake(
        "-DGENERATOR_NAME=Tester",
        f"-DSTAGING_DIR={staging}",
        f"-DOUTPUT_FILES={final / 'a.txt'};{final / 'b.txt'}",
        f"-DOPTIONAL_OUTPUT_FILES={final / 'c.txt'};{final / 'd.txt'}",
        "-P",
        str(PUBLISH_SCRIPT),
        cwd=tmp_path,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr

    assert (final / "a.txt").read_text() == "new-a\n"
    assert (final / "b.txt").read_text() == "new-b\n"
    # Missing optional outputs are replaced by empty placeholders.
    assert (final / "c.txt").read_text().startswith("! Empty output placeholder")
    assert (final / "d.txt").read_text().startswith("! Empty output placeholder")
    assert not staging.exists()


def test_publish_script_keeps_previous_outputs_on_failure(tmp_path: Path) -> None:
    if _cmake_executable() is None:
        pytest.skip("cmake executable not available")

    staging = tmp_path / "staging"
    final = tmp_path / "final"
    staging.mkdir()
    final.mkdir()

    (staging / "a.txt").write_text("new-a\n")
    (final / "a.txt").write_text("old-a\n")
    (final / "b.txt").write_text("old-b\n")

    result = _run_cmake(
        "-DGENERATOR_NAME=Tester",
        f"-DSTAGING_DIR={staging}",
        f"-DOUTPUT_FILES={final / 'a.txt'};{final / 'b.txt'}",
        "-P",
        str(PUBLISH_SCRIPT),
        cwd=tmp_path,
        env=os.environ.copy(),
    )
    assert result.returncode != 0
    assert "missing" in result.stderr.lower()

    # Nothing was published: the previous complete result is intact ...
    assert (final / "a.txt").read_text() == "old-a\n"
    assert (final / "b.txt").read_text() == "old-b\n"
    # ... and the staging area is kept for inspection.
    assert (staging / "a.txt").read_text() == "new-a\n"


def test_cython_transaction(tmp_path: Path) -> None:
    if _cmake_executable() is None:
        pytest.skip("cmake executable not available")
    generator = _generator()
    if generator is None:
        pytest.skip("neither ninja nor make available")
    env = _tool_env()
    if shutil.which("cython", path=env["PATH"]) is None:
        pytest.skip("cython executable not available")

    project = tmp_path / "project"
    build = tmp_path / "build"
    project.mkdir()
    (project / "_hello.pyx").write_text('cpdef void hello(str arg):\n    print("CYTHON_OUTPUT_V1".format(arg))\n')
    (project / "CMakeLists.txt").write_text(
        """
cmake_minimum_required(VERSION 3.15)
project(cy_transaction CXX)
find_package(Cython REQUIRED)
add_cython_target(_hello CXX)
add_custom_target(gen ALL DEPENDS ${_hello})
"""
    )

    _run_cmake(
        "-S",
        str(project),
        "-B",
        str(build),
        "-G",
        generator[0],
        f"-DCMAKE_MODULE_PATH={CMAKE_MODULES}",
        "-DCYTHON_ANNOTATE=ON",
        f"-DPython_EXECUTABLE={sys.executable}",
        cwd=tmp_path,
        env=env,
        check=True,
    )
    build_result = _run_cmake("--build", str(build), cwd=tmp_path, env=env, check=True)
    assert build_result.returncode == 0

    generated_cxx = build / "_hello.cxx"
    generated_html = build / "_hello.html"
    staging_dir = build / "._hello.cython-staging"
    assert generated_cxx.is_file()
    assert generated_cxx.stat().st_size > 0
    assert generated_html.is_file()
    assert not staging_dir.exists()
    assert b"CYTHON_OUTPUT_V1" in generated_cxx.read_bytes()

    cxx_before = generated_cxx.read_bytes()
    html_before = generated_html.read_bytes()

    # A broken pyx makes Cython fail; the previous outputs must survive.
    (project / "_hello.pyx").write_text("cpdef void broken(:\n")
    failed_build = _run_cmake("--build", str(build), cwd=tmp_path, env=env)
    assert failed_build.returncode != 0
    assert generated_cxx.read_bytes() == cxx_before
    assert generated_html.read_bytes() == html_before
    assert b"CYTHON_OUTPUT_V1" in generated_cxx.read_bytes()

    # Once fixed, the new complete output set replaces the previous one.
    (project / "_hello.pyx").write_text('cpdef void hello(str arg):\n    print("CYTHON_OUTPUT_V2".format(arg))\n')
    _run_cmake("--build", str(build), cwd=tmp_path, env=env, check=True)
    assert b"CYTHON_OUTPUT_V2" in generated_cxx.read_bytes()
    assert b"CYTHON_OUTPUT_V1" not in generated_cxx.read_bytes()
    assert not staging_dir.exists()


def test_f2py_transaction(tmp_path: Path) -> None:
    pytest.importorskip("numpy")
    if _cmake_executable() is None:
        pytest.skip("cmake executable not available")
    generator = _generator()
    if generator is None:
        pytest.skip("neither ninja nor make available")
    env = _tool_env()
    f2py = shutil.which("f2py", path=env["PATH"])
    if f2py is None:
        pytest.skip("f2py executable not available")

    good_pyf = (BASE / "tests" / "samples" / "hello-fortran" / "bonjour" / "_bonjour.pyf").read_text()

    project = tmp_path / "project"
    build = tmp_path / "build"
    project.mkdir()
    (project / "_bonjour.pyf").write_text(good_pyf)
    (project / "CMakeLists.txt").write_text(
        """
cmake_minimum_required(VERSION 3.15)
project(f2py_transaction C)
find_package(F2PY REQUIRED)
add_f2py_target(_bonjour.pyf OUTPUT_VAR f2py_outputs)
add_custom_target(gen DEPENDS ${f2py_outputs})
"""
    )

    _run_cmake(
        "-S",
        str(project),
        "-B",
        str(build),
        "-G",
        generator[0],
        f"-DCMAKE_MODULE_PATH={CMAKE_MODULES}",
        f"-DPYTHON_EXECUTABLE={sys.executable}",
        f"-DPython_EXECUTABLE={sys.executable}",
        cwd=tmp_path,
        env=env,
        check=True,
    )
    build_result = _run_cmake("--build", str(build), "--target", "gen", cwd=tmp_path, env=env)
    assert build_result.returncode == 0, build_result.stderr

    generated_module = build / "_bonjourmodule.c"
    wrappers = (build / "_bonjour-f2pywrappers.f", build / "_bonjour-f2pywrappers2.f90")
    staging_dir = build / "._bonjour.f2py-staging"
    assert generated_module.is_file()
    assert generated_module.stat().st_size > 0
    # All declared outputs exist (as generated files or empty placeholders).
    for wrapper in wrappers:
        assert wrapper.is_file()
    assert not staging_dir.exists()

    module_before = generated_module.read_bytes()
    wrappers_before = [wrapper.read_bytes() for wrapper in wrappers]

    # When F2PY fails, none of the previous outputs may change.
    (project / "_bonjour.pyf").write_text(BROKEN_F2PY_INPUT)
    failed_build = _run_cmake("--build", str(build), "--target", "gen", cwd=tmp_path, env=env)
    assert failed_build.returncode != 0
    assert generated_module.read_bytes() == module_before
    for wrapper, before in zip(wrappers, wrappers_before, strict=True):
        assert wrapper.read_bytes() == before

    # Once fixed, a successful run publishes the new complete output set.
    (project / "_bonjour.pyf").write_text(good_pyf)
    _run_cmake("--build", str(build), "--target", "gen", cwd=tmp_path, env=env, check=True)
    assert not staging_dir.exists()
    assert generated_module.stat().st_size > 0
    for wrapper in wrappers:
        assert wrapper.is_file()

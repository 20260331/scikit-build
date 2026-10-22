"""Tests for the relocatable CMake module bundle export/verify API."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile
from importlib import metadata

import pytest

import skbuild
from skbuild.exceptions import (
    BundleError,
    BundleExistsError,
    BundleVerificationError,
    InvalidModuleNameError,
    ModuleNameCollisionError,
    SKBuildError,
    UnknownModuleError,
)
from skbuild.resources.cmake import (
    ENTRY_FILENAME,
    MANIFEST_FILENAME,
    build_graph,
    dependency_closure,
    export_bundle,
    main,
    parse_dependencies,
    verify_bundle,
)

CMAKE = shutil.which("cmake")
SHIPPED_MODULES = (
    "FindCython",
    "FindF2PY",
    "FindNumPy",
    "FindPythonExtensions",
    "UseCython",
    "UseF2PY",
    "UsePythonExtensions",
    "targetLinkLibrariesWithDynamicLookup",
)


# ---------------------------------------------------------------------------
# Static parsing
# ---------------------------------------------------------------------------


def test_shipped_graph_is_self_consistent():
    graph = build_graph()
    assert graph.modules == SHIPPED_MODULES
    assert not [edge for edge in graph.edges if edge.status == "unresolved"]
    assert graph.cycles() == ()


def test_internal_edge_golden():
    edges = {
        (edge.source, edge.target, edge.kind, edge.node) for edge in build_graph().edges if edge.status == "internal"
    }
    assert edges == {
        ("FindCython", "UseCython", "include", "UseCython"),
        ("FindF2PY", "NumPy", "find_package", "FindNumPy"),
        ("FindF2PY", "UseF2PY", "include", "UseF2PY"),
        (
            "FindPythonExtensions",
            "targetLinkLibrariesWithDynamicLookup",
            "include",
            "targetLinkLibrariesWithDynamicLookup",
        ),
        ("FindPythonExtensions", "UsePythonExtensions", "include", "UsePythonExtensions"),
    }


def test_external_edges_are_classified_not_bundled():
    external = {(edge.source, edge.target) for edge in build_graph().edges if edge.status == "external"}
    assert ("FindCython", "FindPackageHandleStandardArgs") in external
    assert ("FindF2PY", "FindPackageHandleStandardArgs") in external
    assert ("FindNumPy", "FindPackageHandleStandardArgs") in external
    assert ("FindPythonExtensions", "PythonInterp") in external
    assert ("FindPythonExtensions", "PythonLibs") in external


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("include(UseCython)", ("include", "UseCython", False)),
        ("find_package ( NumPy 1.0 REQUIRED )", ("find_package", "NumPy", False)),
        ('INCLUDE ( "UseF2PY" NO_POLICY_SCOPE )', ("include", "UseF2PY", False)),
        ("include(FindCython OPTIONAL RESULT_VARIABLE _v)", ("include", "FindCython", False)),
        ("include(${PLUGIN})", ("include", "${PLUGIN}", True)),
        ("find_package(${PACKAGE} REQUIRED)", ("find_package", "${PACKAGE}", True)),
        ("include($<CONFIG>)", ("include", "$<CONFIG>", True)),
        ('set(x "include(NotACall)")', None),
        ("# include(NotACall)\n", None),
        ("#[[ include(NotACall) ]]\n", None),
        ("#[[ find_package(NotACall)\ninclude(Neither) ]]\n", None),
        ("set(x [[include(NotACall)]])\n", None),
    ],
)
def test_parse_dependencies_fixtures(source, expected):
    deps = parse_dependencies(source)
    if expected is None:
        assert deps == ()
    else:
        assert len(deps) == 1
        assert (deps[0].kind, deps[0].name, deps[0].dynamic) == expected


def test_parse_handles_strings_with_parens_and_escapes():
    # The closing paren of the string must not end the command early.
    source = r"""
    string(REPLACE "\\" "/" out "${in}")
    if("${x}" MATCHES "^(a|b)$")
      include(UseCython)
    endif()
    """
    assert [d.name for d in parse_dependencies(source)] == ["UseCython"]


def test_graph_fixture_pseudo_calls_are_not_nodes(tmp_path):
    (tmp_path / "A.cmake").write_text(
        '# include(FakeComment)\nset(x "include(FakeString)")\n#[[ include(FakeBracket) ]]\ninclude(realDep)\n'
    )
    (tmp_path / "realDep.cmake").write_text("# no deps\n")
    graph = build_graph(tmp_path)
    assert graph.modules == ("A", "realDep")
    assert {(e.target, e.status) for e in graph.edges if e.source == "A"} == {
        ("realDep", "internal"),
    }


def test_graph_fixture_dynamic_unresolved_and_cycle(tmp_path):
    (tmp_path / "A.cmake").write_text("include(B)\ninclude(${DYNAMIC})\n")
    (tmp_path / "B.cmake").write_text("include(A)\nfind_package(ExternalDep REQUIRED)\n")
    graph = build_graph(tmp_path)
    by_target = {(e.source, e.target): e for e in graph.edges}
    assert by_target[("A", "${DYNAMIC}")].status == "unresolved"
    assert by_target[("B", "ExternalDep")].status == "external"
    assert graph.cycles() == (("A", "B"),)


def test_case_only_collision_rejected(tmp_path):
    (tmp_path / "Module.cmake").write_text("")
    (tmp_path / "module.cmake").write_text("")
    with pytest.raises(ModuleNameCollisionError):
        build_graph(tmp_path)


def test_unsafe_and_reserved_resource_names_rejected(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "a\\b.cmake").write_text("")
    with pytest.raises(InvalidModuleNameError):
        build_graph(bad)
    (tmp_path / ENTRY_FILENAME).write_text("")
    with pytest.raises(InvalidModuleNameError):
        build_graph(tmp_path)


# ---------------------------------------------------------------------------
# Closure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module_request", "expected"),
    [
        ("FindCython", {"FindCython", "UseCython"}),
        ("Cython", {"FindCython", "UseCython"}),
        ("FindF2PY", {"FindF2PY", "FindNumPy", "UseF2PY"}),
        ("F2PY", {"FindF2PY", "FindNumPy", "UseF2PY"}),
        (
            "FindPythonExtensions",
            {"FindPythonExtensions", "UsePythonExtensions", "targetLinkLibrariesWithDynamicLookup"},
        ),
        (
            "PythonExtensions",
            {"FindPythonExtensions", "UsePythonExtensions", "targetLinkLibrariesWithDynamicLookup"},
        ),
    ],
)
def test_closure_golden(module_request, expected):
    graph = build_graph()
    assert set(dependency_closure(graph, module_request)) == expected


def test_closure_is_deduped_and_sorted():
    graph = build_graph()
    nodes = graph.closure(["Cython", "FindCython", "UseCython"])
    assert nodes == tuple(sorted(nodes))
    assert len(nodes) == len(set(nodes)) == 2


def test_unknown_and_bad_requests_do_not_touch_destination(tmp_path):
    dest = tmp_path / "bundle"
    for name in ("Nope", "../evil", "a/b", "", "useCython"):
        with pytest.raises((UnknownModuleError, InvalidModuleNameError, ModuleNameCollisionError)):
            export_bundle([name], dest)
        assert not dest.exists()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_contents_and_manifest(tmp_path):
    dest = tmp_path / "bundle"
    export_bundle(["FindCython"], dest)
    names = {path.name for path in dest.iterdir()}
    assert names == {"FindCython.cmake", "UseCython.cmake", ENTRY_FILENAME, MANIFEST_FILENAME}

    manifest = json.loads((dest / MANIFEST_FILENAME).read_text())
    assert manifest["bundle_format"] == 1
    assert manifest["entry"] == ENTRY_FILENAME
    assert manifest["modules"] == ["FindCython", "UseCython"]
    assert manifest["cycles"] == []
    assert manifest["requested"] == ["FindCython"]

    files = {record["path"]: record["sha256"] for record in manifest["files"]}
    assert set(files) == {"FindCython.cmake", "UseCython.cmake", ENTRY_FILENAME}
    for name, digest in files.items():
        assert digest == sha256_file(dest / name)

    edges = {(e["source"], e["target"], e["status"]) for e in manifest["edges"]}
    assert ("FindCython", "UseCython", "internal") in edges
    assert ("FindCython", "FindPackageHandleStandardArgs", "external") in edges


def sha256_file(path: pathlib.Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_manifest_has_no_host_paths_or_timestamps(tmp_path):
    dest = tmp_path / "bundle"
    export_bundle(["F2PY", "PythonExtensions"], dest)
    # The build host's absolute resource directory must never be embedded;
    # generic documentation mentions of "site-packages" inside module comments
    # are not host paths.
    host_resource_dir = str(pathlib.Path(skbuild.__file__).parent / "resources" / "cmake")
    for path in dest.iterdir():
        assert host_resource_dir.encode() not in path.read_bytes()
    manifest_text = (dest / MANIFEST_FILENAME).read_text()
    manifest = json.loads(manifest_text)
    assert set(manifest) == {
        "bundle_format",
        "cycles",
        "edges",
        "entry",
        "files",
        "generator",
        "generator_version",
        "modules",
        "requested",
    }
    for record in manifest["files"]:
        assert not pathlib.PurePosixPath(record["path"]).is_absolute()
        assert ".." not in record["path"].split("/")
    assert "CMAKE_CURRENT_LIST_DIR" in (dest / ENTRY_FILENAME).read_text()


def test_repeated_export_byte_identical(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    export_bundle(["Cython", "F2PY", "PythonExtensions"], first)
    export_bundle(["Cython", "F2PY", "PythonExtensions"], second)
    first_files = {p.name: p.read_bytes() for p in first.iterdir()}
    second_files = {p.name: p.read_bytes() for p in second.iterdir()}
    assert first_files == second_files


def test_re_export_identical_is_accepted(tmp_path):
    dest = tmp_path / "bundle"
    export_bundle(["Cython"], dest)
    assert export_bundle(["Cython"], dest) == dest
    assert verify_bundle(dest).ok


@pytest.mark.parametrize("mutate", ["content", "extra", "subset"])
def test_existing_destination_is_not_overwritten(tmp_path, mutate):
    dest = tmp_path / "bundle"
    export_bundle(["F2PY"], dest)
    if mutate == "content":
        (dest / "UseF2PY.cmake").write_bytes(b"tampered")
    elif mutate == "extra":
        (dest / "extra.cmake").write_text("include(UseF2PY)")
    else:
        (dest / "UseF2PY.cmake").unlink()
    before = {p.name: p.read_bytes() if p.is_file() else None for p in dest.iterdir()}
    with pytest.raises(BundleExistsError):
        export_bundle(["F2PY"], dest)
    after = {p.name: p.read_bytes() if p.is_file() else None for p in dest.iterdir()}
    assert before == after


def test_non_bundle_directory_and_file_refused(tmp_path):
    plain_dir = tmp_path / "dir"
    plain_dir.mkdir()
    (plain_dir / "unrelated.txt").write_text("x")
    with pytest.raises(BundleExistsError):
        export_bundle(["Cython"], plain_dir)

    plain_file = tmp_path / "file"
    plain_file.write_text("x")
    with pytest.raises(BundleExistsError):
        export_bundle(["Cython"], plain_file)

    linked = tmp_path / "link"
    os.symlink(plain_dir, linked)
    with pytest.raises(BundleExistsError):
        export_bundle(["Cython"], linked)


def test_overwrite_replaces_differing_bundle(tmp_path):
    dest = tmp_path / "bundle"
    export_bundle(["Cython"], dest)
    (dest / "UseCython.cmake").write_bytes(b"tampered")
    assert not verify_bundle(dest).ok
    export_bundle(["Cython"], dest, overwrite=True)
    assert verify_bundle(dest).ok
    assert set(dest.iterdir())  # and no backup leftovers next to it
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".skbuild-bundle")]


def test_dynamic_dependency_failure_leaves_nothing(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "A.cmake").write_text("include(${PLUGIN})\n")
    dest = tmp_path / "bundle"
    with pytest.raises(BundleError):
        export_bundle(["A"], dest, source=source)
    assert not dest.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".skbuild-bundle")]


def test_read_failure_during_staging_leaves_nothing(tmp_path):
    class FlakyTraversable:
        def __init__(self, path: pathlib.Path, reads: set[str]):
            self._path = path
            self._reads = reads

        @property
        def name(self) -> str:
            return self._path.name

        def is_file(self) -> bool:
            return self._path.is_file()

        def is_dir(self) -> bool:
            return self._path.is_dir()

        def iterdir(self):
            return [FlakyTraversable(child, self._reads) for child in sorted(self._path.iterdir())]

        def read_bytes(self) -> bytes:
            if self.name in self._reads:
                msg = "simulated read failure"
                raise OSError(msg)
            self._reads.add(self.name)
            return self._path.read_bytes()

    source = tmp_path / "src"
    source.mkdir()
    (source / "A.cmake").write_text("include(B)\n")
    (source / "B.cmake").write_text("")
    reads: set[str] = set()
    dest = tmp_path / "bundle"
    with pytest.raises(OSError, match="simulated read failure"):
        export_bundle(["A"], dest, source=FlakyTraversable(source, reads))
    assert not dest.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".skbuild-bundle")]


def test_empty_request_rejected(tmp_path):
    with pytest.raises(BundleError):
        export_bundle([], tmp_path / "bundle")


def test_export_from_directory_and_unknown_source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "FindMine.cmake").write_text("include(UseMine)\n")
    (src / "UseMine.cmake").write_text("")
    dest = tmp_path / "bundle"
    export_bundle(["Mine"], dest, source=src)
    assert (dest / "FindMine.cmake").is_file()
    with pytest.raises(BundleError):
        build_graph("definitely.not.a.package.anywhere")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_codes(dest):
    return {problem.code for problem in verify_bundle(dest).problems}


def test_verify_clean_bundle(tmp_path):
    dest = tmp_path / "bundle"
    export_bundle(["Cython"], dest)
    report = verify_bundle(dest)
    assert bool(report)
    assert report.ok is True
    assert report.manifest is not None
    verify_bundle(dest, strict=True)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        ("modify", "hash_mismatch"),
        ("missing", "missing_file"),
        ("extra", "extra_file"),
        ("symlink-file", "symlink"),
        ("symlink-dir", "symlink"),
    ],
)
def test_verify_detects_tampering(tmp_path, mutate, code):
    src = tmp_path / "src"
    src.mkdir()
    (src / "FindMine.cmake").write_text("include(UseMine)\n")
    (src / "UseMine.cmake").write_text("")
    dest = tmp_path / "bundle"
    export_bundle(["Mine"], dest, source=src)
    outside = tmp_path / "outside.txt"
    outside.write_text("x")

    if mutate == "modify":
        (dest / "UseMine.cmake").write_bytes(b"changed")
    elif mutate == "missing":
        (dest / "UseMine.cmake").unlink()
    elif mutate == "extra":
        (dest / "extra.txt").write_text("x")
    elif mutate == "symlink-file":
        (dest / "UseMine.cmake").unlink()
        os.symlink(outside, dest / "UseMine.cmake")
    else:
        os.symlink(tmp_path, dest / "dirlink")

    report = verify_bundle(dest)
    assert not report.ok
    assert code in verify_codes(dest)
    with pytest.raises(BundleVerificationError):
        verify_bundle(dest, strict=True)


def test_verify_manifest_illegal_paths_and_garbage(tmp_path):
    dest = tmp_path / "bundle"
    export_bundle(["Cython"], dest)
    manifest_path = dest / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text())

    manifest["files"].append({"path": "../evil.cmake", "sha256": "a" * 64})
    manifest_path.write_text(json.dumps(manifest))
    assert "illegal_manifest_path" in verify_codes(dest)

    manifest["files"][-1]["path"] = "/abs/evil.cmake"
    manifest_path.write_text(json.dumps(manifest))
    assert "illegal_manifest_path" in verify_codes(dest)

    manifest["files"][-1] = {"path": "dup.cmake", "sha256": "a" * 64}
    manifest["files"].append({"path": "dup.cmake", "sha256": "a" * 64})
    manifest_path.write_text(json.dumps(manifest))
    codes = verify_codes(dest)
    assert "illegal_manifest_path" in codes

    manifest_path.write_text("{not json")
    assert "malformed_manifest" in verify_codes(dest)


def test_verify_missing_manifest_and_not_directory(tmp_path):
    dest = tmp_path / "bundle"
    export_bundle(["Cython"], dest)
    (dest / MANIFEST_FILENAME).unlink()
    assert "missing_manifest" in verify_codes(dest)

    plain_file = tmp_path / "plain"
    plain_file.write_text("x")
    assert verify_codes(plain_file) == {"not_a_directory"}


# ---------------------------------------------------------------------------
# zipimport
# ---------------------------------------------------------------------------


def test_export_from_zipimported_package(tmp_path, monkeypatch):
    archive = tmp_path / "modules.zip"
    with zipfile.ZipFile(archive, "w") as archive_file:
        archive_file.writestr("zippkg/__init__.py", "")
        archive_file.writestr("zippkg/ZipRoot.cmake", "# include(Fake)\ninclude(ZipDep)\n")
        archive_file.writestr("zippkg/ZipDep.cmake", 'set(x "find_package(FakeString)")\n')
        archive_file.writestr("zippkg/ignore.txt", "ignored")
    monkeypatch.syspath_prepend(str(archive))

    graph = build_graph("zippkg")
    assert graph.modules == ("ZipDep", "ZipRoot")
    assert [e.target for e in graph.edges] == ["ZipDep"]
    dest = tmp_path / "offline"
    export_bundle(["ZipRoot"], dest, source="zippkg")
    assert verify_bundle(dest).ok
    assert {p.name for p in dest.iterdir()} == {
        "ZipRoot.cmake",
        "ZipDep.cmake",
        ENTRY_FILENAME,
        MANIFEST_FILENAME,
    }


# ---------------------------------------------------------------------------
# Real CMake relocation
# ---------------------------------------------------------------------------


PROJECT_CMAKELISTS = """
cmake_minimum_required(VERSION 3.15)
project(skbuild_bundle_reloc NONE)
list(APPEND CMAKE_MODULE_PATH "/some/preexisting/path")
include(${{CMAKE_CURRENT_SOURCE_DIR}}/vendor/{entry})
include(UseCython)
include(targetLinkLibrariesWithDynamicLookup)
if(NOT COMMAND add_cython_target)
  message(FATAL_ERROR "UseCython not found from bundle")
endif()
if(NOT COMMAND target_link_libraries_with_dynamic_lookup)
  message(FATAL_ERROR "targetLinkLibrariesWithDynamicLookup not found from bundle")
endif()
# A second include must deduplicate the bundle entry.
include(${{CMAKE_CURRENT_SOURCE_DIR}}/vendor/{entry})
list(LENGTH CMAKE_MODULE_PATH _length)
list(GET CMAKE_MODULE_PATH 0 _first)
message(STATUS "SKBUNDLE_PATH=${{CMAKE_MODULE_PATH}}")
message(STATUS "SKBUNDLE_LEN=${{_length}}")
message(STATUS "SKBUNDLE_FIRST=${{_first}}")
"""


def _configure(project_dir: pathlib.Path, build_dir: pathlib.Path) -> str:
    assert CMAKE is not None
    result = subprocess.run(
        [CMAKE, "-S", str(project_dir), "-B", str(build_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    return str(result.stdout + result.stderr)


@pytest.mark.skipif(CMAKE is None, reason="cmake executable required")
def test_cmake_finds_modules_after_bundle_moves(tmp_path):
    project = tmp_path / "project"
    project.mkdir(parents=True)
    vendor = project / "vendor"
    export_bundle(["Cython", "PythonExtensions"], vendor)
    (project / "CMakeLists.txt").write_text(PROJECT_CMAKELISTS.format(entry=ENTRY_FILENAME))

    output = _configure(project, project / "build-one")
    expected_bundle = str(vendor)
    assert f"SKBUNDLE_FIRST={expected_bundle}" in output
    assert "SKBUNDLE_LEN=2" in output
    assert expected_bundle + ";/some/preexisting/path" in output

    moved_root = tmp_path / "moved"
    shutil.move(str(project), moved_root)
    moved_vendor = moved_root / "vendor"
    output = _configure(moved_root, moved_root / "build-two")
    assert f"SKBUNDLE_FIRST={moved_vendor}" in output
    assert str(vendor) not in output
    assert "UseCython not found" not in output


# ---------------------------------------------------------------------------
# CLI and packaging regression
# ---------------------------------------------------------------------------


def test_cli_export_and_verify(tmp_path, capsys):
    dest = tmp_path / "bundle"
    assert main(["export", "Cython", "-o", str(dest)]) == 0
    assert main(["verify", str(dest)]) == 0
    out = capsys.readouterr().out
    assert "OK" in out

    (dest / "UseCython.cmake").write_bytes(b"x")
    assert main(["verify", str(dest)]) == 1

    bad = tmp_path / "bad"
    assert main(["export", "DoesNotExist", "-o", str(bad)]) == 2
    assert not bad.exists()


def test_entry_point_and_top_level_exports_unchanged():
    entry_points = metadata.entry_points(group="cmake.module")
    matches = [ep for ep in entry_points if ep.name == "skbuild"]
    assert len(matches) == 1
    assert matches[0].value == "skbuild.resources.cmake"
    assert callable(skbuild.export_bundle)
    assert callable(skbuild.verify_bundle)

    resource_root = pathlib.Path(skbuild.__file__).parent / "resources" / "cmake"
    assert {(m + ".cmake") for m in SHIPPED_MODULES} <= {p.name for p in resource_root.iterdir()}


def test_exceptions_fit_skbuild_error_hierarchy():
    assert issubclass(BundleError, SKBuildError)
    assert issubclass(UnknownModuleError, SKBuildError)
    assert issubclass(BundleVerificationError, SKBuildError)


def test_python_minus_m_exports_bundle(tmp_path):
    dest = tmp_path / "bundle"
    subprocess.run(
        [sys.executable, "-m", "skbuild.resources.cmake", "export", "NumPy", "-o", str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (dest / "FindNumPy.cmake").is_file()
    assert verify_bundle(dest).ok

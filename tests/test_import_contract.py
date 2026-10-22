"""test_import_contract
----------------------------------

Tests for :mod:`skbuild.import_contract`, the explicit import contract
checker. A successful build does not prove an extension is later loaded by the
right interpreter from inside the install tree: the source directory (cwd)
and ``PYTHONPATH`` can shadow the staged copy. These tests build a fake
staging root (and the real ``hello-cpp`` sample) and verify the checker runs
in a genuinely isolated subprocess, pins every origin to the staging root,
classifies module kinds, checks declared attributes, and emits a stable
report.
"""

from __future__ import annotations

import glob
import json
import os
import pathlib
import subprocess
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager

import pytest

from skbuild import import_contract as ic
from skbuild.exceptions import SKBuildError
from skbuild.import_contract import (
    V_ATTRIBUTE_MISMATCH,
    V_IMPORT_FAILED,
    V_MISSING_ATTRIBUTE,
    V_ORIGIN_OUTSIDE_ROOT,
    V_PACKAGE_MISMATCH,
    V_WRONG_KIND,
    ContractSpec,
)


@pytest.fixture
def stage(tmp_path: pathlib.Path) -> pathlib.Path:
    """A fake install/staging tree mixing packages, subpackages and modules."""
    root = tmp_path / "stage"
    package = root / "hello_pkg"
    subpackage = package / "sub"
    subpackage.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "1.2.3"\nVALUE = 42\n')
    (subpackage / "__init__.py").write_text("ANSWER = 7\n")
    (root / "standalone.py").write_text("TOP = True\n")
    # Data directories and metadata must not be mistaken for importable code.
    data = root / "data"
    data.mkdir()
    (data / "noise.txt").write_text("not python\n")
    dist_info = root / "hello_pkg-1.2.3.dist-info"
    dist_info.mkdir()
    (dist_info / "RECORD").write_text("")
    return root


@pytest.fixture
def poisoned(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A source tree on cwd and PYTHONPATH containing a shadowing package."""
    source = tmp_path / "poison_src"
    shadow = source / "hello_pkg"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text('__version__ = "9.9.9"\n')
    monkeypatch.chdir(source)
    monkeypatch.setenv("PYTHONPATH", str(source))
    return source


def _codes(report: ic.ImportReport) -> set[str]:
    return {violation.code for violation in report.violations}


def _codes_for(report: ic.ImportReport, module: str) -> set[str]:
    return {violation.code for violation in report.violations if violation.module == module}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_modules_lists_packages_and_modules(stage: pathlib.Path) -> None:
    assert ic.discover_modules(stage) == ["hello_pkg", "standalone", "hello_pkg.sub"]


def test_discover_modules_ignores_metadata_and_data(stage: pathlib.Path) -> None:
    discovered = ic.discover_modules(stage)
    assert "data" not in discovered
    assert not any("dist-info" in name for name in discovered)


# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "package"),
    [
        ("hello", None, None),
        ("hello:source", "source", None),
        ("hello._ext:extension", "extension", None),
        ("hello:package", "source", True),
        ("json:builtin", "builtin", None),
    ],
)
def test_parse_spec_kind(text: str, kind: str | None, package: bool | None) -> None:
    spec = ic.parse_spec(text)
    assert spec.name == text.split(":", maxsplit=1)[0]
    assert spec.kind == kind
    assert spec.package == package


def test_parse_spec_attributes() -> None:
    spec = ic.parse_spec('hello:package:present,__version__="1.2.3",VALUE=42')
    assert [attr.name for attr in spec.attributes] == ["present", "__version__", "VALUE"]
    assert spec.attributes[0].value is ic._PRESENCE
    assert spec.attributes[1].value == "1.2.3"
    assert spec.attributes[2].value == 42


@pytest.mark.parametrize("text", ["0bad", "hello:wat", "a:b:c:d", "pkg:source:1attr", "pkg:x="])
def test_parse_spec_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError, match=r"invalid|unknown|JSON literal"):
        ic.parse_spec(text)


# ---------------------------------------------------------------------------
# The happy path, with the source tree and PYTHONPATH actively poisoning it
# ---------------------------------------------------------------------------


def test_contract_passes_against_isolated_subprocess(stage: pathlib.Path, poisoned: pathlib.Path) -> None:
    report = ic.check_import_contract(
        stage,
        [
            'hello_pkg:package:__version__="1.2.3",VALUE=42',
            "hello_pkg.sub:source:ANSWER=7",
            "standalone:source:TOP",
        ],
    )

    assert report.ok, report.to_text()
    assert report.staging_root == str(stage.resolve())
    assert report.executable == sys.executable

    package = report.module("hello_pkg")
    assert package.kind == "source"
    assert package.is_package
    assert package.origin == "hello_pkg/__init__.py"
    assert package.origin_in_root
    assert package.origin_absolute is not None
    assert pathlib.Path(package.origin_absolute).resolve().is_relative_to(stage.resolve())

    attributes = {attr.name: attr for attr in package.attributes}
    assert attributes["__version__"].type == "str"
    assert attributes["__version__"].repr == "'1.2.3'"
    assert attributes["__version__"].matches
    assert attributes["VALUE"].matches

    # The shadow source tree must not influence either search path nor result.
    assert report.sys_path[0] == str(stage.resolve())
    assert str(poisoned) not in report.sys_path
    assert "" not in report.sys_path
    assert not list(stage.rglob("__pycache__"))


def test_contract_objects_equivalent_to_strings(stage: pathlib.Path, poisoned: pathlib.Path) -> None:
    specs = [
        ContractSpec(
            name="hello_pkg",
            kind="source",
            package=True,
            attributes=(ic.AttributeExpectation("__version__", "1.2.3"),),
        )
    ]
    report = ic.check_import_contract(stage, specs)
    assert report.ok
    assert str(poisoned) not in report.sys_path


def test_default_specs_check_the_whole_tree(stage: pathlib.Path, poisoned: pathlib.Path) -> None:
    report = ic.check_import_contract(stage)
    assert report.ok, report.to_text()
    assert {module.name for module in report.modules} == {
        "hello_pkg",
        "standalone",
        "hello_pkg.sub",
    }
    for module in report.modules:
        assert module.kind == "source"
        assert module.origin_in_root
    assert str(poisoned) not in report.sys_path


# ---------------------------------------------------------------------------
# Contract violations
# ---------------------------------------------------------------------------


def test_origin_outside_root_is_a_violation(stage: pathlib.Path, poisoned: pathlib.Path) -> None:
    # ``json`` resolves to the interpreter stdlib, never the staging root.
    report = ic.check_import_contract(stage, ["json:source"])
    assert not report.ok
    assert _codes_for(report, "json") == {V_ORIGIN_OUTSIDE_ROOT}
    module = report.module("json")
    assert module.origin_in_root is False
    assert module.origin_absolute is not None
    assert not module.origin_absolute.startswith(str(stage.resolve()))
    assert str(poisoned) not in report.sys_path


def test_wrong_kind_and_package_violations(stage: pathlib.Path) -> None:
    report = ic.check_import_contract(stage, ["standalone:extension", "hello_pkg:source"])
    assert _codes_for(report, "standalone") == {V_WRONG_KIND}
    # hello_pkg is a package but the contract only asked for a source module,
    # so package status is not part of the contract.
    assert report.module("hello_pkg").is_package

    package_report = ic.check_import_contract(stage, ["standalone:package"])
    assert _codes_for(package_report, "standalone") == {V_PACKAGE_MISMATCH}


def test_missing_and_mismatched_attributes(stage: pathlib.Path) -> None:
    report = ic.check_import_contract(
        stage,
        ['hello_pkg:source:not_declared,VALUE="42"'],
    )
    assert _codes_for(report, "hello_pkg") == {V_MISSING_ATTRIBUTE, V_ATTRIBUTE_MISMATCH}
    attributes = {attr.name: attr for attr in report.module("hello_pkg").attributes}
    assert attributes["not_declared"].present is False
    assert attributes["VALUE"].matches is False


def test_import_failure_is_reported(stage: pathlib.Path) -> None:
    report = ic.check_import_contract(stage, ["does_not_exist"])
    assert not report.ok
    assert _codes_for(report, "does_not_exist") == {V_IMPORT_FAILED}
    module = report.module("does_not_exist")
    assert module.error is not None
    assert module.error[0] == "ModuleNotFoundError"


def test_missing_root_is_a_fatal_error(tmp_path: pathlib.Path) -> None:
    with pytest.raises(SKBuildError, match="staging root"):
        ic.check_import_contract(tmp_path / "missing", ["hello"])


# ---------------------------------------------------------------------------
# Stable reports and the command line
# ---------------------------------------------------------------------------


def test_report_is_byte_stable(stage: pathlib.Path, poisoned: pathlib.Path) -> None:
    spec = ["hello_pkg:package:__version__"]
    first = ic.check_import_contract(stage, spec)
    second = ic.check_import_contract(stage, spec)
    assert first.to_json() == second.to_json()
    assert first.to_text() == second.to_text()
    assert str(poisoned) not in first.sys_path
    json.loads(first.to_json())  # the document is valid JSON


def test_report_text_is_readable(stage: pathlib.Path) -> None:
    report = ic.check_import_contract(stage, ['hello_pkg:package:__version__="1.2.3"'])
    text = report.to_text()
    assert "IMPORT CONTRACT REPORT" in text
    assert "hello_pkg [source, package]" in text
    assert "origin: hello_pkg/__init__.py (in root)" in text
    assert "attribute __version__: str = '1.2.3'" in text
    assert text.rstrip().endswith("result: PASS")


def _run_cli(
    stage: pathlib.Path, poisoned: pathlib.Path, *spec: str, fmt: str = "json"
) -> subprocess.CompletedProcess[str]:
    # Deliberately keep PYTHONPATH pointing at the shadow source tree.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(poisoned)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "skbuild.import_contract",
            str(stage),
            *spec,
            "--format",
            fmt,
        ],
        cwd=poisoned,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_json_pass(stage: pathlib.Path, poisoned: pathlib.Path) -> None:
    result = _run_cli(stage, poisoned, 'hello_pkg:package:__version__="1.2.3"')
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["ok"] is True
    module = next(module for module in document["modules"] if module["name"] == "hello_pkg")
    assert module["origin_in_root"] is True
    assert str(poisoned) not in document["sys_path"]


def test_cli_text_fail_exit_code(stage: pathlib.Path, poisoned: pathlib.Path) -> None:
    result = _run_cli(stage, poisoned, "standalone:extension", fmt="text")
    assert result.returncode == 1
    assert "result: FAIL" in result.stdout
    assert "WRONG_KIND" in result.stdout


def test_cli_rejects_bad_spec(stage: pathlib.Path, poisoned: pathlib.Path) -> None:
    result = _run_cli(stage, poisoned, "hello:wat")
    assert result.returncode == 2
    assert "unknown module kind" in result.stderr


# ---------------------------------------------------------------------------
# Real build: the staged hello-cpp tree, with the source tree masking it
# ---------------------------------------------------------------------------


def test_hello_cpp_staging_tree_import_contract(
    project_setup_py_test: Callable[..., AbstractContextManager[pathlib.Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from . import get_ext_suffix

    with project_setup_py_test("hello-cpp", ["build"]) as project_dir:
        staged = glob.glob("build/lib.*")
        assert len(staged) == 1
        root = pathlib.Path(staged[0]).resolve()
        assert (root / "hello" / f"_hello{get_ext_suffix()}").exists()

        # cwd is the source project and PYTHONPATH points at it too; its
        # hello/ package has __init__.py but no compiled _hello, so without
        # isolation every import below would resolve to the wrong copy.
        monkeypatch.setenv("PYTHONPATH", str(project_dir))

        discovered = ic.discover_modules(root)
        assert "hello" in discovered
        assert "hello._hello" in discovered

        report = ic.check_import_contract(
            root,
            ["hello:package", "hello._hello:extension:hello,size"],
        )
        assert report.ok, report.to_text()

        extension = report.module("hello._hello")
        assert extension.kind == "extension"
        assert extension.loader == "ExtensionFileLoader"
        assert extension.origin_in_root
        assert extension.origin is not None
        assert extension.origin.startswith("hello/_hello")
        assert extension.origin.endswith(get_ext_suffix())
        assert {attr.name for attr in extension.attributes} == {"hello", "size"}

        assert report.sys_path[0] == str(root)
        assert str(project_dir) not in report.sys_path

        # Whole-tree discovery: every file-backed module must come from root,
        # including the CMake-generated hello/world.py and helloModule.py.
        whole_tree = ic.check_import_contract(root)
        assert whole_tree.ok, whole_tree.to_text()
        for module in whole_tree.modules:
            assert module.origin_in_root, module.name

"""
Explicit import contract checker for install/staging trees.

Building an extension module successfully does not prove that, after install,
the *correct* interpreter loads it from the *correct* location: a same-named
package in the source directory (``cwd``) or on ``PYTHONPATH`` can shadow the
staged copy, and an extension built for a different ABI fails only at import
time.

This module verifies the import contract of modules living under a *staging
root* (e.g. ``build/lib.*`` after ``setup.py build``, an unpacked wheel, or a
``site-packages`` prefix) from an **isolated subprocess**:

* the interpreter runs in isolated mode (``-I``: no ``PYTHON*`` environment
  variables, no :mod:`site`), starts in an empty working directory, and
  ``sys.path`` is rebuilt so that the staging root is the only non-stdlib
  entry -- neither the source tree nor ``PYTHONPATH`` can mask a failure;
* every requested module is imported and classified by type (``source``,
  ``extension``, ``builtin``, ``frozen``, ``namespace``) and package status;
* the module's resolved origin must live inside the staging root;
* declared attributes must be present, optionally with an expected value.

The subprocess emits a schema-versioned JSON document. The parent turns it into
an :class:`ImportReport` and renders a deterministic text or JSON report.

CLI::

    python -m skbuild.import_contract STAGING_ROOT [SPEC ...] [--format text|json]

Spec syntax: ``dotted.module[:kind[:attribute,...]]``; ``kind`` is one of
``source``, ``extension``, ``builtin``, ``frozen``, ``namespace`` or ``package``
(a source package). An attribute is a name (presence check) or
``name=<json-literal>`` for equality, e.g. ``__version__="1.2.3"``. With no
specs, all modules discovered under the staging root are checked.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .exceptions import SKBuildError

__all__ = [
    "SCHEMA_VERSION",
    "AttributeExpectation",
    "AttributeReport",
    "ContractSpec",
    "ImportReport",
    "ModuleReport",
    "Violation",
    "check_import_contract",
    "discover_modules",
    "main",
    "parse_spec",
]

SCHEMA_VERSION: Final = 1

KINDS: Final = frozenset({"source", "extension", "builtin", "frozen", "namespace"})

_PRESENCE: Final = object()

_DOTTED_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")

# Violation codes (kept in sync with the strings produced by _CHILD_CODE;
# the report/repr truncation limits live inside that stdlib-only script).
V_IMPORT_FAILED: Final = "IMPORT_FAILED"
V_ORIGIN_MISSING: Final = "ORIGIN_MISSING"
V_ORIGIN_OUTSIDE_ROOT: Final = "ORIGIN_OUTSIDE_ROOT"
V_WRONG_KIND: Final = "WRONG_KIND"
V_PACKAGE_MISMATCH: Final = "PACKAGE_MISMATCH"
V_MISSING_ATTRIBUTE: Final = "MISSING_ATTRIBUTE"
V_ATTRIBUTE_MISMATCH: Final = "ATTRIBUTE_MISMATCH"


@dataclass(frozen=True)
class AttributeExpectation:
    """An attribute declared by an import contract.

    ``value`` defaults to a presence-only sentinel; pass any JSON-compatible
    value to require equality.
    """

    name: str
    value: object = _PRESENCE


@dataclass(frozen=True)
class ContractSpec:
    """Import contract for one module: its name, expected kind, and attributes."""

    name: str
    kind: str | None = None
    package: bool | None = None
    attributes: tuple[AttributeExpectation, ...] = ()

    @classmethod
    def from_string(cls, text: str) -> ContractSpec:
        return parse_spec(text)


@dataclass(frozen=True)
class AttributeReport:
    """Observed state of one declared attribute."""

    name: str
    present: bool
    type: str | None
    repr: str | None
    expected: object
    matches: bool | None


@dataclass(frozen=True)
class ModuleReport:
    """Observed state of one imported module."""

    name: str
    kind: str | None
    loader: str | None
    is_package: bool
    origin: str | None
    origin_absolute: str | None
    origin_in_root: bool | None
    search_locations: tuple[str, ...]
    attributes: tuple[AttributeReport, ...]
    error: tuple[str, str] | None


@dataclass(frozen=True)
class Violation:
    """One breached contract clause."""

    module: str
    code: str
    message: str
    expected: str | None = None
    actual: str | None = None


@dataclass(frozen=True)
class ImportReport:
    """The full result of an import contract check."""

    implementation: str
    version: str
    version_info: tuple[int, int, int]
    executable: str
    staging_root: str
    sys_path: tuple[str, ...]
    modules: tuple[ModuleReport, ...]
    violations: tuple[Violation, ...]
    import_stdout: str
    import_stderr: str
    fatal: str | None

    @property
    def ok(self) -> bool:
        """True when the subprocess ran to completion without violations."""
        return self.fatal is None and not self.violations

    def module(self, name: str) -> ModuleReport:
        for reported in self.modules:
            if reported.name == name:
                return reported
        msg = f"module {name!r} is not part of the report"
        raise KeyError(msg)

    def to_dict(self) -> dict[str, object]:
        """Canonical, schema-versioned mapping (key order is part of the report)."""
        return {
            "schema": SCHEMA_VERSION,
            "ok": self.ok,
            "fatal": self.fatal,
            "python": {
                "implementation": self.implementation,
                "version": self.version,
                "version_info": list(self.version_info),
                "executable": self.executable,
            },
            "staging_root": self.staging_root,
            "sys_path": list(self.sys_path),
            "modules": [
                {
                    "name": module.name,
                    "kind": module.kind,
                    "loader": module.loader,
                    "is_package": module.is_package,
                    "origin": module.origin,
                    "origin_absolute": module.origin_absolute,
                    "origin_in_root": module.origin_in_root,
                    "search_locations": list(module.search_locations),
                    "attributes": [
                        {
                            "name": attr.name,
                            "present": attr.present,
                            "type": attr.type,
                            "repr": attr.repr,
                            "expected": None if attr.expected is _PRESENCE else attr.expected,
                            "matches": attr.matches,
                        }
                        for attr in module.attributes
                    ],
                    "error": None if module.error is None else {"type": module.error[0], "message": module.error[1]},
                }
                for module in self.modules
            ],
            "violations": [
                {
                    "module": violation.module,
                    "code": violation.code,
                    "message": violation.message,
                    "expected": violation.expected,
                    "actual": violation.actual,
                }
                for violation in self.violations
            ],
            "import_stdout": self.import_stdout,
            "import_stderr": self.import_stderr,
        }

    def to_json(self) -> str:
        """Stable JSON rendering; two checks of the same tree produce the same bytes."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=False, ensure_ascii=True)

    def to_text(self) -> str:
        """Stable, human-readable rendering."""
        lines = [
            "IMPORT CONTRACT REPORT",
            f"schema: {SCHEMA_VERSION}",
            f"python: {self.implementation} {self.version} ({self.executable})",
            f"staging_root: {self.staging_root}",
            "sys_path:",
        ]
        lines.extend(f"  - {path}" for path in self.sys_path)
        lines.append(f"modules ({len(self.modules)}):")
        for module in self.modules:
            if module.error is not None:
                lines.append(f"  {module.name} [not imported]")
                lines.append(f"    error: {module.error[0]}: {module.error[1]}")
                continue
            tags = [module.kind or "unknown"]
            if module.is_package:
                tags.append("package")
            lines.append(f"  {module.name} [{', '.join(tags)}]")
            if module.origin is not None:
                location = "in root" if module.origin_in_root else "OUTSIDE ROOT"
                lines.append(f"    origin: {module.origin} ({location})")
                if module.origin_absolute is not None and module.origin_absolute != str(
                    Path(self.staging_root) / module.origin
                ):
                    lines.append(f"    origin_absolute: {module.origin_absolute}")
            if module.search_locations:
                lines.append("    search_locations:")
                lines.extend(f"      - {location}" for location in module.search_locations)
            for attr in module.attributes:
                if not attr.present:
                    lines.append(f"    attribute {attr.name}: MISSING")
                elif attr.matches is False:
                    lines.append(
                        f"    attribute {attr.name}: {attr.type} = {attr.repr} "
                        f"(expected {json.dumps(attr.expected, ensure_ascii=True)})"
                    )
                else:
                    lines.append(f"    attribute {attr.name}: {attr.type} = {attr.repr}")
        if self.import_stdout or self.import_stderr:
            lines.append(f"import stdout: {self.import_stdout or '(empty)'}")
            lines.append(f"import stderr: {self.import_stderr or '(empty)'}")
        lines.append(f"violations ({len(self.violations)}):")
        for violation in self.violations:
            lines.append(f"  - {violation.module} {violation.code}: {violation.message}")
        lines.append(f"result: {'PASS' if self.ok else 'FAIL'}")
        return "\n".join(lines)


def parse_spec(text: str) -> ContractSpec:
    """Parse a ``name[:kind[:attr,...]]`` contract spec string."""
    parts = text.split(":")
    if len(parts) > 3:
        msg = f"invalid contract spec {text!r}: expected 'name[:kind[:attribute,...]]'"
        raise ValueError(msg)
    name = parts[0].strip()
    if not _DOTTED_NAME.fullmatch(name):
        msg = f"invalid module name in contract spec {text!r}"
        raise ValueError(msg)

    kind: str | None = None
    package: bool | None = None
    if len(parts) >= 2 and parts[1].strip():
        token = parts[1].strip()
        if token == "package":
            kind = "source"
            package = True
        elif token in KINDS:
            kind = token
        else:
            msg = f"unknown module kind {token!r} in {text!r}; expected one of {sorted(KINDS | {'package'})}"
            raise ValueError(msg)

    attributes: list[AttributeExpectation] = []
    if len(parts) == 3 and parts[2].strip():
        for raw_attr in parts[2].split(","):
            attr = raw_attr.strip()
            if not attr:
                msg = f"empty attribute in contract spec {text!r}"
                raise ValueError(msg)
            if "=" in attr:
                attr_name, raw_value = attr.split("=", 1)
                attr_name = attr_name.strip()
                if not attr_name.isidentifier():
                    msg = f"invalid attribute name {attr_name!r} in {text!r}"
                    raise ValueError(msg)
                try:
                    value: object = json.loads(raw_value)
                except json.JSONDecodeError as err:
                    msg = f"attribute {attr_name!r} value must be a JSON literal in {text!r}: {err.msg}"
                    raise ValueError(msg) from err
            else:
                attr_name = attr
                if not attr_name.isidentifier():
                    msg = f"invalid attribute name {attr_name!r} in {text!r}"
                    raise ValueError(msg)
                value = _PRESENCE
            attributes.append(AttributeExpectation(attr_name, value))

    return ContractSpec(name=name, kind=kind, package=package, attributes=tuple(attributes))


def _module_file_name(name: str) -> str | None:
    """Return the module name for a file, or None if it is not importable code."""
    if name.endswith(".py"):
        stem = name[:-3]
        return stem if stem.isidentifier() else None
    for suffix in importlib.machinery.EXTENSION_SUFFIXES:
        if name.endswith(suffix):
            stem = name[: -len(suffix)]
            return stem if stem.isidentifier() else None
    return None


def discover_modules(root: str | os.PathLike[str]) -> list[str]:
    """List importable modules and packages found under ``root``, import order.

    Regular packages (directories containing ``__init__.py``), top-level
    Python files, and extension modules are discovered recursively. Metadata
    directories (``*.dist-info``/``*.egg-info``) and ``__pycache__`` are
    skipped. Nothing is imported.
    """
    root = Path(root)
    names: list[str] = []

    def scan(directory: Path, prefix: str) -> None:
        for path in sorted(directory.iterdir(), key=lambda candidate: candidate.name):
            if path.name == "__pycache__" or path.name.endswith((".dist-info", ".egg-info")):
                continue
            if path.is_dir() and (path / "__init__.py").is_file():
                name = f"{prefix}{path.name}"
                names.append(name)
                scan(path, f"{name}.")
                continue
            if not path.is_file():
                continue
            stem = _module_file_name(path.name)
            if stem is not None and stem != "__init__":
                names.append(f"{prefix}{stem}")

    scan(root, "")
    names.sort(key=lambda name: (name.count("."), name))
    return names


def _as_specs(specs: Iterable[str | ContractSpec]) -> list[ContractSpec]:
    parsed: list[ContractSpec] = []
    for spec in specs:
        if isinstance(spec, ContractSpec):
            parsed.append(spec)
        elif isinstance(spec, str):
            parsed.append(parse_spec(spec))
        else:
            msg = f"contract specs must be str or ContractSpec, got {type(spec).__name__}"
            raise TypeError(msg)
    parsed.sort(key=lambda spec: (spec.name.count("."), spec.name))
    return parsed


def _build_payload(root: Path, specs: list[ContractSpec], allow_site_packages: bool) -> str:
    modules = [
        {
            "name": spec.name,
            "kind": spec.kind,
            "package": spec.package,
            "attributes": [
                {
                    "name": attr.name,
                    "has_value": attr.value is not _PRESENCE,
                    "value": None if attr.value is _PRESENCE else attr.value,
                }
                for attr in spec.attributes
            ],
        }
        for spec in specs
    ]
    return json.dumps(
        {"root": str(root), "allow_site": allow_site_packages, "modules": modules},
        separators=(",", ":"),
    )


# Code executed in the isolated child interpreter. It must stay stdlib-only
# and write exactly one JSON document to the real stdout. Keep the violation
# code strings in sync with the V_* constants above.
_CHILD_CODE = r"""
import importlib
import importlib.machinery
import io
import json
import os
import pathlib
import sys
import tempfile
import traceback

ATTR_REPR_LIMIT = 200
OUTPUT_LIMIT = 4000


def classify(module):
    loader = getattr(getattr(module, "__spec__", None), "loader", None)
    if isinstance(loader, importlib.machinery.ExtensionFileLoader):
        kind = "extension"
    elif isinstance(loader, importlib.machinery.SourceFileLoader):
        kind = "source"
    elif loader is importlib.machinery.BuiltinImporter:
        kind = "builtin"
    elif loader is importlib.machinery.FrozenImporter:
        kind = "frozen"
    elif loader is None or type(loader).__name__ == "NamespaceLoader":
        kind = "namespace"
    else:
        kind = "other"
    return kind, type(loader).__name__ if loader is not None else None


def root_relative(path, root):
    resolved = pathlib.Path(os.path.realpath(str(path)))
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError:
        return None, str(resolved)
    return relative, str(resolved)


def main(payload):
    raw_root = str(payload.get("root") or "")
    if not raw_root.strip():
        raise RuntimeError("staging root is empty")
    root = pathlib.Path(raw_root).expanduser().resolve()
    if not root.is_dir():
        msg = "staging root does not exist or is not a directory: %s" % root
        raise RuntimeError(msg)

    # A throwaway cwd guarantees a leaked "" entry cannot resolve into a
    # source tree, even on Python versions where -I keeps cwd on sys.path.
    os.chdir(tempfile.mkdtemp(prefix="skbuild-import-contract-"))

    if payload.get("allow_site"):
        import site

        site.main()

    # Only absolute, existing interpreter paths survive; cwd ("") and any
    # relative/PYTHONPATH leftovers are dropped. The staging root wins.
    sys.path = [str(root)] + [p for p in sys.path if p and os.path.isabs(p) and os.path.exists(p)]
    # Do not litter the (possibly read-only) staging root with __pycache__.
    sys.dont_write_bytecode = True

    captured_out = io.StringIO()
    captured_err = io.StringIO()
    modules = []
    violations = []

    for requested in sorted(payload["modules"], key=lambda m: (m["name"].count("."), m["name"])):
        name = requested["name"]
        record = {
            "name": name,
            "kind": None,
            "loader": None,
            "is_package": False,
            "origin": None,
            "origin_absolute": None,
            "origin_in_root": None,
            "search_locations": [],
            "attributes": [],
            "error": None,
        }
        try:
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = captured_out, captured_err
            try:
                module = importlib.import_module(name)
            finally:
                sys.stdout, sys.stderr = old_out, old_err
        except Exception as exc:  # noqa: BLE001 - any import failure is a contract result
            record["error"] = {"type": type(exc).__name__, "message": str(exc)}
            modules.append(record)
            violations.append(
                {
                    "module": name,
                    "code": "IMPORT_FAILED",
                    "message": "%s: %s" % (type(exc).__name__, exc),
                    "expected": None,
                    "actual": None,
                }
            )
            continue

        kind, loader_name = classify(module)
        spec = getattr(module, "__spec__", None)
        is_package = bool(getattr(spec, "submodule_search_locations", None))
        record["kind"] = kind
        record["loader"] = loader_name
        record["is_package"] = is_package

        origin = getattr(module, "__file__", None) or getattr(spec, "origin", None)
        if kind in ("source", "extension", "other") and origin:
            relative, absolute = root_relative(origin, root)
            record["origin"] = relative if relative is not None else absolute
            record["origin_absolute"] = absolute
            record["origin_in_root"] = relative is not None
            if relative is None:
                violations.append(
                    {
                        "module": name,
                        "code": "ORIGIN_OUTSIDE_ROOT",
                        "message": "origin is outside the staging root: %s" % absolute,
                        "expected": str(root),
                        "actual": absolute,
                    }
                )
        elif kind in ("source", "extension", "other"):
            violations.append(
                {
                    "module": name,
                    "code": "ORIGIN_MISSING",
                    "message": "%s module %r has no origin (__file__)" % (kind, name),
                    "expected": "an origin inside the staging root",
                    "actual": None,
                }
            )
        elif kind == "namespace":
            locations = []
            outside = []
            for entry in list(getattr(spec, "submodule_search_locations", None) or []):
                relative, absolute = root_relative(entry, root)
                locations.append(relative if relative is not None else absolute)
                if relative is None:
                    outside.append(absolute)
            record["search_locations"] = locations
            record["origin_in_root"] = bool(locations) and not outside
            if outside:
                violations.append(
                    {
                        "module": name,
                        "code": "ORIGIN_OUTSIDE_ROOT",
                        "message": "namespace search path leaves the staging root: %s" % ", ".join(outside),
                        "expected": str(root),
                        "actual": "; ".join(outside),
                    }
                )

        if requested["kind"] is not None and kind != requested["kind"]:
            violations.append(
                {
                    "module": name,
                    "code": "WRONG_KIND",
                    "message": "module kind is %r, expected %r" % (kind, requested["kind"]),
                    "expected": requested["kind"],
                    "actual": kind,
                }
            )
        if requested["package"] is not None and is_package != requested["package"]:
            expected = "package" if requested["package"] else "non-package module"
            actual = "package" if is_package else "non-package module"
            violations.append(
                {
                    "module": name,
                    "code": "PACKAGE_MISMATCH",
                    "message": "expected a %s, got a %s" % (expected, actual),
                    "expected": expected,
                    "actual": actual,
                }
            )

        for expectation in requested["attributes"]:
            attr_name = expectation["name"]
            present = hasattr(module, attr_name)
            entry = {
                "name": attr_name,
                "present": present,
                "type": None,
                "repr": None,
                "has_value": expectation["has_value"],
                "expected": expectation["value"],
                "matches": None,
            }
            if present:
                value = getattr(module, attr_name)
                rendered = repr(value)
                entry["type"] = type(value).__name__
                if len(rendered) <= ATTR_REPR_LIMIT:
                    entry["repr"] = rendered
                else:
                    entry["repr"] = rendered[: ATTR_REPR_LIMIT - 3] + "..."
                if expectation["has_value"]:
                    entry["matches"] = value == expectation["value"]
            record["attributes"].append(entry)
            if not present:
                violations.append(
                    {
                        "module": name,
                        "code": "MISSING_ATTRIBUTE",
                        "message": "module %r does not declare %r" % (name, attr_name),
                        "expected": "attribute %s" % attr_name,
                        "actual": None,
                    }
                )
            elif entry["matches"] is False:
                violations.append(
                    {
                        "module": name,
                        "code": "ATTRIBUTE_MISMATCH",
                        "message": "attribute %r is %s, expected %s"
                        % (attr_name, entry["repr"], json.dumps(expectation["value"], sort_keys=True)),
                        "expected": json.dumps(expectation["value"], sort_keys=True),
                        "actual": entry["repr"],
                    }
                )

        modules.append(record)

    violations.sort(key=lambda violation: (violation["module"], violation["code"], violation["message"]))
    info = sys.implementation.version
    return {
        "schema": 1,
        "fatal": None,
        "python": {
            "implementation": sys.implementation.name,
            "version": "%d.%d.%d" % (info.major, info.minor, info.micro),
            "version_info": [info.major, info.minor, info.micro],
            "executable": sys.executable,
        },
        "staging_root": str(root),
        "sys_path": list(sys.path),
        "modules": modules,
        "violations": violations,
        "ok": not violations,
        "import_stdout": captured_out.getvalue()[-OUTPUT_LIMIT:],
        "import_stderr": captured_err.getvalue()[-OUTPUT_LIMIT:],
    }


payload = json.loads(sys.argv[1])
try:
    report = main(payload)
except Exception as exc:  # noqa: BLE001 - the parent needs a document, not a traceback
    report = {
        "schema": 1,
        "fatal": "%s: %s" % (type(exc).__name__, exc),
        "traceback": traceback.format_exc(),
        "python": {
            "implementation": sys.implementation.name,
            "version": "%d.%d.%d" % sys.version_info[:3],
            "version_info": list(sys.version_info[:3]),
            "executable": sys.executable,
        },
        "staging_root": payload.get("root"),
        "sys_path": list(sys.path),
        "modules": [],
        "violations": [],
        "ok": False,
        "import_stdout": "",
        "import_stderr": "",
    }
sys.__stdout__.write(json.dumps(report, ensure_ascii=True))
sys.__stdout__.flush()
"""


def _parse_report(data: dict[str, Any]) -> ImportReport:
    if data.get("fatal"):
        msg = f"import contract checker failed: {data['fatal']}"
        raise SKBuildError(msg)

    modules = tuple(
        ModuleReport(
            name=module["name"],
            kind=module["kind"],
            loader=module["loader"],
            is_package=bool(module["is_package"]),
            origin=module["origin"],
            origin_absolute=module["origin_absolute"],
            origin_in_root=module["origin_in_root"],
            search_locations=tuple(module["search_locations"]),
            attributes=tuple(
                AttributeReport(
                    name=attr["name"],
                    present=bool(attr["present"]),
                    type=attr["type"],
                    repr=attr["repr"],
                    expected=_PRESENCE if not attr["has_value"] else attr["expected"],
                    matches=attr["matches"],
                )
                for attr in module["attributes"]
            ),
            error=None if module["error"] is None else (module["error"]["type"], module["error"]["message"]),
        )
        for module in data["modules"]
    )
    violations = tuple(
        Violation(
            module=violation["module"],
            code=violation["code"],
            message=violation["message"],
            expected=violation["expected"],
            actual=violation["actual"],
        )
        for violation in data["violations"]
    )
    python = data["python"]
    major, minor, micro = python["version_info"]
    return ImportReport(
        implementation=python["implementation"],
        version=python["version"],
        version_info=(major, minor, micro),
        executable=python["executable"],
        staging_root=data["staging_root"],
        sys_path=tuple(data["sys_path"]),
        modules=modules,
        violations=violations,
        import_stdout=data["import_stdout"],
        import_stderr=data["import_stderr"],
        fatal=data["fatal"],
    )


def check_import_contract(
    root: str | os.PathLike[str],
    specs: Iterable[str | ContractSpec] | None = None,
    *,
    executable: str | os.PathLike[str] | None = None,
    allow_site_packages: bool = False,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> ImportReport:
    """Verify import contracts for modules under ``root`` in an isolated subprocess.

    ``specs`` are :class:`ContractSpec` instances or ``name[:kind[:attrs]]``
    strings; ``None`` discovers and checks every module under the root.

    The subprocess runs ``executable`` (defaults to the running interpreter)
    in isolated mode with all ``PYTHON*`` variables scrubbed and an empty
    working directory, so source trees and ``PYTHONPATH`` cannot mask the
    result.
    """
    root_path = Path(root).resolve()
    parsed = _as_specs(specs if specs is not None else discover_modules(root_path))
    payload = _build_payload(root_path, parsed, allow_site_packages)

    interpreter = sys.executable if executable is None else os.fspath(executable)
    # -I: ignore PYTHON* vars, skip site, (3.11+) keep cwd off sys.path.
    # Scrubbing PYTHON* from the environment makes the isolation explicit and
    # independent of interpreter version.
    child_env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    if env:
        child_env.update(env)

    with tempfile.TemporaryDirectory(prefix="skbuild-import-contract-parent-") as work:
        result = subprocess.run(
            [interpreter, "-I", "-c", _CHILD_CODE, payload],
            cwd=work,
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    if result.returncode != 0:
        msg = (
            f"import contract checker exited with code {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        raise SKBuildError(msg)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as err:
        msg = (
            f"could not decode import contract checker output: {err}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        raise SKBuildError(msg) from err

    return _parse_report(data)


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; returns the process exit code (0 pass, 1 fail)."""
    parser = argparse.ArgumentParser(
        prog="python -m skbuild.import_contract",
        description="Verify in an isolated subprocess that modules under a staging root "
        "import from inside that root, have the expected kind, and declare attributes.",
    )
    parser.add_argument("staging_root", type=Path, help="directory containing the staged/installed modules")
    parser.add_argument(
        "specs",
        nargs="*",
        help="contract specs 'name[:kind[:attribute,...]]'; defaults to every module discovered under the root",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text", help="report format (default: text)")
    parser.add_argument(
        "--allow-site-packages",
        action="store_true",
        help="initialize site in the child interpreter (needed when modules import third-party packages)",
    )
    parser.add_argument("--python", default=None, help="interpreter executable (default: the running interpreter)")
    args = parser.parse_args(argv)

    try:
        parsed = [parse_spec(spec) for spec in args.specs]
    except ValueError as err:
        parser.error(str(err))

    report = check_import_contract(
        args.staging_root,
        parsed,
        executable=args.python,
        allow_site_packages=args.allow_site_packages,
    )
    print(report.to_json() if args.format == "json" else report.to_text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

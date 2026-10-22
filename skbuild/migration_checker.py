"""
Static migration checker for projects moving from scikit-build 0.x to 1.x.

scikit-build 1.0 replaces the classic backend with the setuptools plugin from
scikit-build-core (see the migration guide in ``docs/usage.rst``). Projects
upgrading have no easy way to tell which of their ``setup()`` and CMake usage
still works. This checker answers that without running the project:

* ``setup.py`` is parsed with :mod:`ast` -- it is never imported or executed;
* ``pyproject.toml`` is parsed with :mod:`tomllib`;
* ``CMakeLists.txt`` and ``*.cmake`` files are scanned line by line.

Every finding is classified as one of :class:`Status`:

* ``removed``    -- breaks or no longer takes effect on scikit-build 1.0;
* ``ignored``    -- still accepted (usually with a warning) but does nothing;
* ``compatible`` -- documented usage that keeps working;
* ``unknown``    -- dynamically constructed usage that needs manual review.

Run as ``python -m skbuild.migration_checker PATH`` or via the
``skbuild-migration-check`` console script.
"""

from __future__ import annotations

import argparse
import ast
import enum
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # scikit-build-core depends on tomli on Python 3.10
    import tomli as tomllib

__all__ = ["Finding", "Status", "check_project", "format_report", "main"]

# ---------------------------------------------------------------------------
# Finding model
# ---------------------------------------------------------------------------


class Status(str, enum.Enum):
    """Classification of a scanned usage."""

    REMOVED = "removed"
    IGNORED = "ignored"
    COMPATIBLE = "compatible"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Finding:
    """One observation about the scanned project.

    ``path`` is project-relative (POSIX separators); ``line`` is 1-based and
    may be ``None`` when the finding concerns a whole file.
    """

    status: Status
    path: str
    line: int | None
    code: str
    message: str
    suggestion: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "path": self.path,
            "line": self.line,
            "code": self.code,
            "message": self.message,
            "suggestion": self.suggestion,
        }


# ---------------------------------------------------------------------------
# Static knowledge about the scikit-build 0.x -> 1.x changes
# ---------------------------------------------------------------------------

#: setup() keyword arguments still honored by scikit-build-core's wrapper.
COMPATIBLE_SETUP_KWARGS: dict[str, str] = {
    "cmake_args": "list of CMake configure arguments passed through to CMake",
    "cmake_source_dir": "directory containing the project's CMakeLists.txt",
    "cmake_install_dir": "relative directory where CMake artifacts are installed",
    "cmake_install_target": "CMake target used to install artifacts into the wheel",
    "cmake_process_manifest_hook": "hook filtering the list of CMake-installed files",
}

#: Command-line options removed from ``setup.py`` in 1.0.
REMOVED_CLI_LONG_OPTIONS: frozenset[str] = frozenset(
    {
        "--build-type",
        "--generator",
        "--cmake-executable",
        "--install-target",
        "--skip-generator-test",
        "--hide-listing",
        "--force-cmake",
        "--skip-cmake",
    }
)

_REMOVED_CLI_OPTION_RE = re.compile(
    r"--(?:build-type|generator|cmake-executable|install-target|skip-generator-test|"
    r"hide-listing|force-cmake|skip-cmake)\b"
)

#: Classic modules that no longer exist at all (import raises ImportError).
REMOVED_MODULE_PREFIXES: tuple[str, ...] = ("skbuild.command", "skbuild.platform_specifics")

#: Deprecated shim modules: importable (with a warning) but exporting nothing.
EMPTY_SHIM_MODULES: frozenset[str] = frozenset({"skbuild.utils", "skbuild.setuptools_wrap"})

#: Deprecated shim modules keeping a few helpers.
_CMAKER_KEPT: dict[str, str] = {
    "get_cmake_version": "return the installed CMake version",
}
_CMAKER_REMOVED: frozenset[str] = frozenset({"CMaker", "pop_arg", "has_cmake_cache_arg", "RE_FILE_INSTALL"})

_CONSTANTS_KEPT: dict[str, str] = {
    "CMAKE_INSTALL_DIR": "CMake install staging directory used by the setuptools plugin",
    "skbuild_plat_name": "wheel platform tag",
}
_CONSTANTS_REMOVED: dict[str, str] = {
    "SKBUILD_DIR": "the _skbuild/<platform>-<pyversion>/ directory layout is gone",
    "SKBUILD_MARKER_FILE": "the _skbuild/<platform>-<pyversion>/ directory layout is gone",
    "CMAKE_BUILD_DIR": "CMake now builds under setuptools' build/temp.*/_skbuild directory",
    "CMAKE_SPEC_FILE": "the CMake spec file from the classic incremental build is gone",
    "SETUPTOOLS_INSTALL_DIR": "the classic install-classification pipeline is gone",
    "set_skbuild_plat_name": "platform names are derived by scikit-build-core",
    "CMAKE_DEFAULT_EXECUTABLE": "CMake is located by scikit-build-core",
    "get_cmake_version": "use skbuild.cmaker.get_cmake_version (shim) instead",
}

_EXCEPTIONS_KEPT: frozenset[str] = frozenset({"SKBuildError"})
_EXCEPTIONS_REMOVED: dict[str, str] = {
    "SKBuildInvalidFileInstallationError": "the classic install-classification pipeline is gone",
    "SKBuildGeneratorNotFoundError": "generator probing was removed",
}

#: CMake find modules still shipped (via scikit-build-core's cmake.module entry point).
COMPATIBLE_CMAKE_PACKAGES: dict[str, str] = {
    "PythonExtensions": "python_extension_module() and the PYTHON_* variables",
    "Cython": "add_cython_target() and the CYTHON_* variables",
    "F2PY": "add_f2py_target() and the F2PY_* variables",
    "NumPy": "the NumPy_INCLUDE_DIRS variable",
}

#: CMake include modules still shipped.
COMPATIBLE_CMAKE_INCLUDES: frozenset[str] = frozenset(
    {
        "UseCython",
        "UseF2PY",
        "UsePythonExtensions",
        "targetLinkLibrariesWithDynamicLookup",
    }
)

#: CMake functions/macros defined by the shipped modules.
COMPATIBLE_CMAKE_COMMANDS: dict[str, str] = {
    "python_extension_module": "PythonExtensions",
    "python_standalone_executable": "PythonExtensions",
    "python_modules_header": "PythonExtensions",
    "add_python_library": "UsePythonExtensions",
    "add_python_extension": "UsePythonExtensions",
    "add_cython_target": "UseCython",
    "add_f2py_target": "UseF2PY",
    "check_dynamic_lookup": "targetLinkLibrariesWithDynamicLookup",
    "target_link_libraries_with_dynamic_lookup": "targetLinkLibrariesWithDynamicLookup",
}

_CMAKE_VARIABLE_PREFIXES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"PYTHON_?\w*"), "the PythonExtensions module (and FindPythonLibs/FindPythonInterp)"),
    (re.compile(r"CYTHON_?\w*"), "the Cython module"),
    (re.compile(r"F2PY_?\w*"), "the F2PY module"),
    (re.compile(r"NumPy_?\w*"), "the NumPy module"),
)

#: Suggestions reused in several places.
_USE_NEW_SETTINGS = (
    "use the CMAKE_ARGS/CMAKE_GENERATOR environment variables, the cmake_args keyword, "
    "or settings in the [tool.scikit-build] table of pyproject.toml instead"
)

_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".tox",
        ".nox",
        "build",
        "dist",
        "_skbuild",
        ".venv",
        "venv",
        "env",
        ".eggs",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
    }
)

_SCRIPT_SUFFIXES: tuple[str, ...] = (".sh", ".bash", ".yml", ".yaml")


# ---------------------------------------------------------------------------
# setup.py scanning (AST only -- the file is never executed)
# ---------------------------------------------------------------------------


class _SetupPyScanner(ast.NodeVisitor):
    """Collect findings from a ``setup.py`` parsed with :mod:`ast`."""

    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.findings: list[Finding] = []
        #: bound name -> fully qualified skbuild module (import skbuild.x as y)
        self._module_aliases: dict[str, str] = {}
        #: bound name -> (skbuild module, attribute) (from skbuild.x import y)
        self._name_aliases: dict[str, tuple[str, str]] = {}
        self._skbuild_seen = False

    def scan(self, tree: ast.AST) -> list[Finding]:
        # First pass: build the import alias maps and classify imports.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self._visit_import(node)
            elif isinstance(node, ast.ImportFrom):
                self._visit_import_from(node)
        # Second pass: attribute usage, setup() calls and string literals.
        self.visit(tree)
        return self.findings

    # -- helpers -----------------------------------------------------------

    def _finding(
        self,
        status: Status,
        line: int | None,
        code: str,
        message: str,
        suggestion: str | None = None,
    ) -> None:
        self.findings.append(Finding(status, self.rel_path, line, code, message, suggestion))

    @staticmethod
    def _under_skbuild(module: str) -> bool:
        return module == "skbuild" or module.startswith("skbuild.")

    @staticmethod
    def _matches_prefix(module: str, prefixes: tuple[str, ...]) -> bool:
        return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)

    # -- import classification ---------------------------------------------

    def _visit_import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.name
            if not self._under_skbuild(name):
                continue
            # ``import a.b.c`` binds the root component (``a``); an explicit
            # ``import a.b as x`` binds the full dotted path to ``x``.
            bound = alias.asname or name.split(".", 1)[0]
            self._module_aliases[bound] = name if alias.asname else bound
            if name == "skbuild":
                if not self._skbuild_seen:
                    self._finding(
                        Status.COMPATIBLE,
                        node.lineno,
                        "py-import",
                        "import skbuild: the package still provides skbuild.setup()",
                    )
                    self._skbuild_seen = True
            elif self._matches_prefix(name, REMOVED_MODULE_PREFIXES):
                self._finding(
                    Status.REMOVED,
                    node.lineno,
                    "py-import-removed",
                    f"{name} was removed in scikit-build 1.0 and cannot be imported",
                    "the internal Python API is gone; port to scikit-build-core APIs",
                )
            elif name in EMPTY_SHIM_MODULES:
                self._finding(
                    Status.IGNORED,
                    node.lineno,
                    "py-import-shim",
                    f"{name} is a deprecated shim that imports (with a warning) but exports nothing",
                    "remove the import and port to scikit-build-core APIs",
                )
            elif name in ("skbuild.cmaker", "skbuild.constants"):
                self._finding(
                    Status.IGNORED,
                    node.lineno,
                    "py-import-shim",
                    f"{name} is a deprecated shim kept for a few downstream helpers",
                    "port to scikit-build-core APIs",
                )
            elif name == "skbuild.exceptions":
                self._finding(
                    Status.COMPATIBLE,
                    node.lineno,
                    "py-import",
                    "skbuild.exceptions.SKBuildError is still available",
                )
            else:
                # e.g. import skbuild.resources.cmake: internal, not a public module
                self._finding(
                    Status.UNKNOWN,
                    node.lineno,
                    "py-import-internal",
                    f"{name} is an internal module with no compatibility guarantee in scikit-build 1.0",
                    "check whether scikit-build-core provides an equivalent API",
                )

    def _visit_import_from(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level != 0 or not self._under_skbuild(module):
            return

        for alias in node.names:
            name = alias.name
            bound = alias.asname or name
            line = node.lineno

            if name == "*":
                self._finding(
                    Status.UNKNOWN,
                    line,
                    "py-star-import",
                    f"star import from {module} cannot be resolved statically",
                )
                continue

            if module == "skbuild":
                self._classify_root_import(name, bound, line)
                continue

            if self._matches_prefix(module, REMOVED_MODULE_PREFIXES):
                self._finding(
                    Status.REMOVED,
                    line,
                    "py-import-removed",
                    f"{module}.{name} was removed with the {module.split('.')[1]} package in scikit-build 1.0",
                    "the internal Python API is gone; port to scikit-build-core APIs",
                )
                continue

            if module in EMPTY_SHIM_MODULES:
                self._finding(
                    Status.REMOVED,
                    line,
                    "py-import-removed",
                    f"cannot import {name} from {module}: the shim exports nothing",
                    "port to scikit-build-core APIs",
                )
                continue

            if module == "skbuild.cmaker":
                self._name_aliases[bound] = (module, name)
                if name in _CMAKER_KEPT:
                    self._finding(
                        Status.COMPATIBLE,
                        line,
                        "py-import",
                        f"skbuild.cmaker.{name} is still provided by the deprecated cmaker shim",
                        "prefer scikit-build-core APIs for new code",
                    )
                elif name in _CMAKER_REMOVED:
                    self._finding(
                        Status.REMOVED,
                        line,
                        "py-import-removed",
                        f"skbuild.cmaker.{name} was removed in scikit-build 1.0",
                        "port to scikit-build-core (or invoke cmake directly)",
                    )
                else:
                    self._finding(
                        Status.REMOVED,
                        line,
                        "py-import-removed",
                        f"skbuild.cmaker.{name} is not exported by the 1.0 shim (only get_cmake_version remains)",
                        "port to scikit-build-core APIs",
                    )
                continue

            if module == "skbuild.constants":
                self._name_aliases[bound] = (module, name)
                if name in _CONSTANTS_KEPT:
                    self._finding(
                        Status.COMPATIBLE,
                        line,
                        "py-import",
                        f"skbuild.constants.{name} is still provided by the deprecated constants shim",
                        "prefer scikit-build-core APIs for new code",
                    )
                else:
                    reason = _CONSTANTS_REMOVED.get(name, "the classic constants module was removed")
                    self._finding(
                        Status.REMOVED,
                        line,
                        "py-import-removed",
                        f"skbuild.constants.{name} was removed in scikit-build 1.0 ({reason})",
                        "only CMAKE_INSTALL_DIR() and skbuild_plat_name() remain as shims",
                    )
                continue

            if module == "skbuild.exceptions":
                self._name_aliases[bound] = (module, name)
                if name in _EXCEPTIONS_KEPT:
                    self._finding(
                        Status.COMPATIBLE,
                        line,
                        "py-import",
                        "skbuild.exceptions.SKBuildError is kept as an alias of setuptools' SetupError",
                        "note it is no longer a RuntimeError subclass",
                    )
                elif name in _EXCEPTIONS_REMOVED:
                    self._finding(
                        Status.REMOVED,
                        line,
                        "py-import-removed",
                        f"skbuild.exceptions.{name} was removed in scikit-build 1.0",
                        "catch SKBuildError (setuptools SetupError) instead",
                    )
                else:
                    self._finding(
                        Status.REMOVED,
                        line,
                        "py-import-removed",
                        f"skbuild.exceptions.{name} is not exported in scikit-build 1.0",
                    )
                continue

            # Other internal skbuild.* submodules (resources, _compat, ...).
            self._finding(
                Status.UNKNOWN,
                line,
                "py-import-internal",
                f"importing {module}.{name} relies on an internal module with no 1.0 compatibility guarantee",
                "check whether scikit-build-core provides an equivalent API",
            )

    def _classify_root_import(self, name: str, bound: str, line: int) -> None:
        if name == "setup":
            self._name_aliases[bound] = ("skbuild", "setup")
            self._finding(
                Status.COMPATIBLE,
                line,
                "py-import-setup",
                "from skbuild import setup: still the supported entry point",
            )
        elif name == "__version__":
            self._name_aliases[bound] = ("skbuild", "__version__")
            self._finding(Status.COMPATIBLE, line, "py-import", "skbuild.__version__ is still available")
        elif name in ("command", "platform_specifics"):
            self._finding(
                Status.REMOVED,
                line,
                "py-import-removed",
                f"skbuild.{name} was removed in scikit-build 1.0",
                "the internal Python API is gone; port to scikit-build-core APIs",
            )
        elif name in ("cmaker", "constants", "utils", "setuptools_wrap", "exceptions"):
            submodule = f"skbuild.{name}"
            self._module_aliases[bound] = submodule
            if name in ("cmaker", "constants", "utils", "setuptools_wrap"):
                self._finding(
                    Status.IGNORED,
                    line,
                    "py-import-shim",
                    f"skbuild.{name} is a deprecated shim that warns on import",
                )
            else:
                self._finding(Status.COMPATIBLE, line, "py-import", "skbuild.exceptions remains available")
        else:
            self._finding(
                Status.REMOVED,
                line,
                "py-import-removed",
                f"skbuild.{name} is not exported by scikit-build 1.0",
            )

    # -- attribute usage ----------------------------------------------------

    def _resolve(self, node: ast.Attribute) -> tuple[str, str] | None:
        parts: list[str] = [node.attr]
        current: ast.AST = node.value
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if not isinstance(current, ast.Name):
            return None
        base = self._module_aliases.get(current.id)
        if base is None:
            return None
        chain = base.split(".") + list(reversed(parts))
        # Walk down: attribute access on skbuild.* can reach deeper submodules.
        if len(chain) < 2:
            return None
        return ".".join(chain[:-1]), chain[-1]

    def visit_Attribute(self, node: ast.Attribute) -> None:
        resolved = self._resolve(node)
        if resolved is not None:
            module, attr = resolved
            self._classify_attribute(module, attr, node.lineno)
        self.generic_visit(node)

    def _classify_attribute(self, module: str, attr: str, line: int) -> None:
        if self._matches_prefix(module, REMOVED_MODULE_PREFIXES) or (
            module == "skbuild" and attr in {"command", "platform_specifics"}
        ):
            self._finding(
                Status.REMOVED,
                line,
                "py-attr-removed",
                f"{module}.{attr} is part of the internal API removed in scikit-build 1.0",
                "port to scikit-build-core APIs",
            )
            return

        if module in EMPTY_SHIM_MODULES:
            self._finding(
                Status.REMOVED,
                line,
                "py-attr-removed",
                f"{module}.{attr} is unavailable: the shim exports nothing",
                "port to scikit-build-core APIs",
            )
            return

        if module == "skbuild.cmaker":
            if attr in _CMAKER_KEPT:
                self._finding(
                    Status.COMPATIBLE,
                    line,
                    "py-attr",
                    f"skbuild.cmaker.{attr} is still provided by the deprecated shim",
                )
            else:
                self._finding(
                    Status.REMOVED,
                    line,
                    "py-attr-removed",
                    f"skbuild.cmaker.{attr} is not available in 1.0 (only get_cmake_version remains)",
                    "port to scikit-build-core APIs",
                )
            return

        if module == "skbuild.constants":
            if attr in _CONSTANTS_KEPT:
                self._finding(
                    Status.COMPATIBLE,
                    line,
                    "py-attr",
                    f"skbuild.constants.{attr} is still provided by the deprecated shim",
                )
            else:
                reason = _CONSTANTS_REMOVED.get(attr, "the classic constants module was removed")
                self._finding(
                    Status.REMOVED,
                    line,
                    "py-attr-removed",
                    f"skbuild.constants.{attr} is not available in 1.0 ({reason})",
                )
            return

        if module == "skbuild.exceptions":
            if attr in _EXCEPTIONS_KEPT:
                self._finding(
                    Status.COMPATIBLE,
                    line,
                    "py-attr",
                    "SKBuildError is kept (now an alias of setuptools SetupError, not RuntimeError)",
                )
            else:
                self._finding(
                    Status.REMOVED,
                    line,
                    "py-attr-removed",
                    f"skbuild.exceptions.{attr} was removed in scikit-build 1.0",
                )
            return

        if module == "skbuild" and attr in {"setup", "__version__", "__author__", "__email__"}:
            # setup is classified as a call in visit_Call; the metadata dunder
            # attributes remain exported by scikit-build 1.0.
            if attr != "setup":
                self._finding(
                    Status.COMPATIBLE,
                    line,
                    "py-attr",
                    f"skbuild.{attr} is still exported",
                )
            return

        if self._under_skbuild(module):
            self._finding(
                Status.UNKNOWN,
                line,
                "py-attr-internal",
                f"{module}.{attr} has no documented 1.0 status; verify manually",
            )

    # -- setup() call -------------------------------------------------------

    def _is_setup_call(self, node: ast.Call) -> bool:
        func = node.func
        if isinstance(func, ast.Name):
            return self._name_aliases.get(func.id) == ("skbuild", "setup")
        if isinstance(func, ast.Attribute):
            resolved = self._resolve(func)
            return resolved == ("skbuild", "setup")
        return False

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_setup_call(node):
            self._classify_setup_call(node)
        self.generic_visit(node)

    def _classify_setup_call(self, node: ast.Call) -> None:
        line = node.lineno
        if node.args:
            self._finding(
                Status.UNKNOWN,
                line,
                "setup-positional-args",
                "skbuild.setup() takes keyword arguments only; positional arguments could not be classified",
            )

        for keyword in node.keywords:
            if keyword.arg is None:
                self._finding(
                    Status.UNKNOWN,
                    keyword.value.lineno,
                    "setup-dynamic-kwargs",
                    "keyword arguments are forwarded with **<mapping> and cannot be classified statically",
                    "expand the mapping or review its contents against the scikit-build 1.0 migration guide",
                )
                continue
            self._classify_setup_keyword(keyword)

    def _classify_setup_keyword(self, keyword: ast.keyword) -> None:
        name = keyword.arg
        value = keyword.value
        line = getattr(value, "lineno", None)

        if name in COMPATIBLE_SETUP_KWARGS:
            self._finding(
                Status.COMPATIBLE,
                line,
                "setup-arg",
                f"setup({name}=...): still supported ({COMPATIBLE_SETUP_KWARGS[name]})",
            )
            if name == "cmake_args":
                self._check_cmake_args(value, line)
            return

        if name == "cmake_with_sdist":
            self._check_cmake_with_sdist(value, line)
            return

        if name in {"cmake_languages", "cmake_minimum_required_version"}:
            self._check_ignored_keyword(name, value, line)
            return

        if name == "script_args":
            self._check_script_args(value, line)
            return

        # All other keywords are standard setuptools options (name, packages,
        # cmdclass, ...): scikit-build 1.0 no longer intercepts them.

    @staticmethod
    def _literal(value: ast.AST) -> tuple[bool, Any]:
        try:
            return True, ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return False, None

    def _check_cmake_args(self, value: ast.AST, line: int | None) -> None:
        ok, literal = self._literal(value)
        if not ok:
            return  # the kwarg itself is compatible; contents just cannot be inspected
        if isinstance(literal, str):
            self._finding(
                Status.REMOVED,
                line,
                "setup-arg-type",
                "cmake_args must be a list of strings, not a single string (raises TypeError)",
            )
            return
        if not isinstance(literal, (list, tuple)):
            return
        for item in literal:
            if isinstance(item, str) and "CMAKE_INSTALL_PREFIX" in item:
                self._finding(
                    Status.REMOVED,
                    line,
                    "setup-cmake-install-prefix",
                    "passing CMAKE_INSTALL_PREFIX through cmake_args is not supported "
                    "(scikit-build controls the install staging directory)",
                    "install into relative DESTINATION paths in CMake instead",
                )

    def _check_cmake_with_sdist(self, value: ast.AST, line: int | None) -> None:
        ok, literal = self._literal(value)
        if not ok:
            self._finding(
                Status.UNKNOWN,
                line,
                "setup-arg-unknown",
                "cmake_with_sdist is computed dynamically; it raises an error when truthy in scikit-build 1.0",
                "remove it and ship generated files through MANIFEST.in",
            )
            return
        if bool(literal):
            self._finding(
                Status.REMOVED,
                line,
                "setup-arg-removed",
                "cmake_with_sdist=True raises an error in scikit-build 1.0 (sdists never run CMake)",
                "commit generated files or create them in an sdist preparation step",
            )

    def _check_ignored_keyword(self, name: str, value: ast.AST, line: int | None) -> None:
        ok, literal = self._literal(value)
        if not ok:
            self._finding(
                Status.UNKNOWN,
                line,
                "setup-arg-unknown",
                f"{name} is computed dynamically; it is ignored with a warning unless it evaluates to None",
                "set cmake.version / cmake.langs in [tool.scikit-build] instead",
            )
            return
        if literal is None:
            return
        if name == "cmake_minimum_required_version":
            suggestion = "set cmake.version in the [tool.scikit-build] table of pyproject.toml"
        else:
            suggestion = "languages are enabled automatically; set cmake.langs in [tool.scikit-build] if needed"
        self._finding(
            Status.IGNORED,
            line,
            "setup-arg-ignored",
            f"{name}={literal!r} is accepted but ignored with a warning in scikit-build 1.0",
            suggestion,
        )

    def _check_script_args(self, value: ast.AST, line: int | None) -> None:
        ok, literal = self._literal(value)
        if not ok:
            self._finding(
                Status.UNKNOWN,
                line,
                "setup-arg-unknown",
                "script_args is computed dynamically and could not be checked for removed setup.py options",
            )
            return
        if not isinstance(literal, (list, tuple, set)):
            return
        for item in literal:
            if not isinstance(item, str):
                continue
            if item in REMOVED_CLI_LONG_OPTIONS:
                self._removed_cli_finding(line, item)
            elif item in {"-G", "-j", "--"}:
                detail = {
                    "-G": "generator selection (-G/--generator) was removed",
                    "-j": "the -j jobs option was removed (use CMAKE_BUILD_PARALLEL_LEVEL)",
                    "--": "the 'setup.py args -- cmake args -- build tool args' sections were removed",
                }[item]
                self._finding(
                    Status.REMOVED,
                    line,
                    "setup-cli-option-removed",
                    f"script_args contains {item!r}: {detail}",
                    _USE_NEW_SETTINGS,
                )

    def _removed_cli_finding(self, line: int | None, option: str) -> None:
        self._finding(
            Status.REMOVED,
            line,
            "setup-cli-option-removed",
            f"the scikit-build-specific setup.py option {option} was removed in 1.0",
            _USE_NEW_SETTINGS,
        )

    # -- string literals (catch sys.argv manipulation, option lists, ...) ----

    def visit_Expr(self, node: ast.Expr) -> None:
        # Do not treat docstrings as option usage.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and node.value in REMOVED_CLI_LONG_OPTIONS:
            self._removed_cli_finding(node.lineno, node.value)
        self.generic_visit(node)


def scan_setup_py(path: Path, rel_path: str) -> list[Finding]:
    """Parse ``setup.py`` with :mod:`ast` and return migration findings."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [
            Finding(
                Status.UNKNOWN,
                rel_path,
                exc.lineno,
                "py-parse-error",
                f"setup.py could not be parsed statically: {exc.msg}",
                "review this file against the migration guide manually",
            )
        ]
    except OSError as exc:
        return [
            Finding(
                Status.UNKNOWN,
                rel_path,
                None,
                "py-read-error",
                f"setup.py could not be read: {exc}",
            )
        ]
    return _SetupPyScanner(rel_path).scan(tree)


# ---------------------------------------------------------------------------
# CMake scanning
# ---------------------------------------------------------------------------

_BRACKET_COMMENT_RE = re.compile(r"#(=*)\[")
_FIND_PACKAGE_RE = re.compile(r"(?<![\w(])([Ff][Ii][Nn][Dd]_[Pp][Aa][Cc][Kk][Aa][Gg][Ee])\s*\(\s*\"?([A-Za-z0-9_]+)")
_INCLUDE_RE = re.compile(r"(?<![\w(])([Ii][Nn][Cc][Ll][Uu][Dd][Ee])\s*\(\s*\"?([A-Za-z0-9_]+)")
_IF_SKBUILD_RE = re.compile(
    r"\b(?:if|elseif)\s*\([^)]*\bSKBUILD\b",
    re.IGNORECASE,
)
_SKBUILD_COMPARE_RE = re.compile(r"\bSKBUILD\b", re.IGNORECASE)
_COMPARE_OP_RE = re.compile(
    r"\b(?:STREQUAL|STRLESS|STRGREATER|VERSION_EQUAL|VERSION_LESS|VERSION_GREATER|VERSION_COMPARE|"
    r"MATCHES|EQUAL|LESS|GREATER)\b",
    re.IGNORECASE,
)
_VARIABLE_RE = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
_HARDCODED_PATH_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"[^\s\"')]*skbuild[/\\]resources[/\\]cmake[^\s\"')]*"),
    re.compile(r"[^\s\"')]*skbuild[/\\]platform_specifics[^\s\"')]*"),
    re.compile(r"_skbuild[/\\][^\s\"')]*(?:cmake-build|cmake-install|setuptools)"),
    re.compile(r"_skbuild[/\\](?:linux|darwin|win|macos|msys|cygwin)[^\s\"')]*"),
)


def _strip_cmake_comments(text: str) -> str:
    """Replace CMake comments with spaces, preserving newlines and strings."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == '"':
            out.append(char)
            i += 1
            while i < n:
                quoted = text[i]
                out.append(quoted)
                i += 1
                if quoted == "\\" and i < n:
                    out.append(text[i])
                    i += 1
                    continue
                if quoted == '"':
                    break
            continue
        if char == "#":
            match = _BRACKET_COMMENT_RE.match(text, i)
            if match:
                equals = match.group(1)
                close = "]" + equals + "]"
                end = text.find(close, match.end())
                if end == -1:
                    end = n
                else:
                    end += len(close)
                for ch in text[i:end]:
                    out.append("\n" if ch == "\n" else " ")
                i = end
                continue
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)


def scan_cmake_file(path: Path, rel_path: str) -> list[Finding]:
    """Scan one ``CMakeLists.txt`` or ``*.cmake`` file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    sanitized = _strip_cmake_comments(text)
    findings: list[Finding] = []

    def add(status: Status, line: int, code: str, message: str, suggestion: str | None = None) -> None:
        findings.append(Finding(status, rel_path, line, code, message, suggestion))

    command_alternation = "|".join(re.escape(name) for name in COMPATIBLE_CMAKE_COMMANDS)
    command_re = re.compile(rf"(?<![\w(])(?:{command_alternation})\s*\(", re.IGNORECASE)

    for lineno, line in enumerate(sanitized.splitlines(), 1):
        # SKBUILD comparisons rely on the exact old value (TRUE).
        if _SKBUILD_COMPARE_RE.search(line) and _COMPARE_OP_RE.search(line):
            add(
                Status.REMOVED,
                lineno,
                "cmake-sklbuild-value",
                'SKBUILD is compared to an exact value: it is now "2" instead of "TRUE"',
                "test truthiness with if(SKBUILD) instead",
            )
        elif _IF_SKBUILD_RE.search(line) or re.search(r"\$\{?SKBUILD\}?", line):
            add(
                Status.COMPATIBLE,
                lineno,
                "cmake-sklbuild-variable",
                'SKBUILD is still defined when scikit-build drives CMake (now "2", still truthy)',
            )

        for match in _FIND_PACKAGE_RE.finditer(line):
            package = match.group(2)
            if package in COMPATIBLE_CMAKE_PACKAGES:
                add(
                    Status.COMPATIBLE,
                    lineno,
                    "cmake-module",
                    f"find_package({package}) is still shipped ({COMPATIBLE_CMAKE_PACKAGES[package]})",
                )
        for match in _INCLUDE_RE.finditer(line):
            module = match.group(2)
            if module in COMPATIBLE_CMAKE_INCLUDES:
                add(
                    Status.COMPATIBLE,
                    lineno,
                    "cmake-module",
                    f"include({module}) is still shipped and injected into CMAKE_MODULE_PATH",
                )
        for match in command_re.finditer(line):
            command = match.group(0).split("(", 1)[0].strip().lower()
            owner = COMPATIBLE_CMAKE_COMMANDS[command]
            add(
                Status.COMPATIBLE,
                lineno,
                "cmake-command",
                f"{command}() is still provided by the shipped {owner} CMake module",
            )
        for match in _VARIABLE_RE.finditer(line):
            variable = match.group(1)
            if variable == "SKBUILD":
                continue  # reported above
            for pattern, owner in _CMAKE_VARIABLE_PREFIXES:
                if pattern.fullmatch(variable):
                    add(
                        Status.COMPATIBLE,
                        lineno,
                        "cmake-variable",
                        f"${{{variable}}} is still provided by {owner}",
                    )
                    break
        for path_re in _HARDCODED_PATH_RES:
            for match in path_re.finditer(line):
                add(
                    Status.REMOVED,
                    lineno,
                    "cmake-hardcoded-path",
                    f"hardcoded classic scikit-build path {match.group(0)!r} no longer exists",
                    "modules are injected into CMAKE_MODULE_PATH; use the standard build/ layout for build trees",
                )
    return findings


# ---------------------------------------------------------------------------
# pyproject.toml scanning
# ---------------------------------------------------------------------------


def _line_for(text: str, needle: str) -> int | None:
    for lineno, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return lineno
    return None


def scan_pyproject(path: Path) -> tuple[list[Finding], dict[str, Any]]:
    """Scan ``pyproject.toml``; return findings and facts used by other checks."""
    facts: dict[str, Any] = {"has_pyproject": True, "editable_mode": None, "has_tool_table": False}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [], facts
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return (
            [
                Finding(
                    Status.UNKNOWN,
                    "pyproject.toml",
                    None,
                    "toml-parse-error",
                    f"pyproject.toml could not be parsed: {exc}",
                )
            ],
            facts,
        )

    findings: list[Finding] = []
    build_system = data.get("build-system")
    if not isinstance(build_system, dict):
        findings.append(
            Finding(
                Status.UNKNOWN,
                "pyproject.toml",
                None,
                "pyproject-no-build-system",
                "no [build-system] table: under PEP 517 isolated builds, scikit-build is not installed automatically",
                'add requires = ["scikit-build>=1"] (and the recommended scikit_build_core.setuptools.build_meta backend)',
            )
        )
    else:
        backend = build_system.get("build-backend")
        if isinstance(backend, str):
            line = _line_for(text, backend)
            if backend in {"setuptools.build_meta", "scikit_build_core.setuptools.build_meta"}:
                note = (
                    None
                    if backend == "scikit_build_core.setuptools.build_meta"
                    else "the classic table still works; scikit_build_core.setuptools.build_meta is recommended"
                )
                findings.append(
                    Finding(
                        Status.COMPATIBLE,
                        "pyproject.toml",
                        line,
                        "pyproject-backend",
                        f"build-backend {backend!r} still works with scikit-build 1.0",
                        note,
                    )
                )
            else:
                findings.append(
                    Finding(
                        Status.UNKNOWN,
                        "pyproject.toml",
                        line,
                        "pyproject-backend",
                        f"unknown build backend {backend!r}; verify it integrates with the scikit-build-core plugin",
                    )
                )

        requires = build_system.get("requires", [])
        if isinstance(requires, list):
            for requirement in requires:
                if not isinstance(requirement, str):
                    continue
                _classify_requirement(requirement, text, findings)

    tool_table = data.get("tool", {})
    skb_table = tool_table.get("scikit-build") if isinstance(tool_table, dict) else None
    if isinstance(skb_table, dict):
        facts["has_tool_table"] = True
        findings.append(
            Finding(
                Status.COMPATIBLE,
                "pyproject.toml",
                _line_for(text, "[tool.scikit-build]"),
                "pyproject-tool-table",
                "the [tool.scikit-build] table configures scikit-build-core's setuptools plugin",
            )
        )
        editable = skb_table.get("editable")
        if isinstance(editable, dict) and isinstance(editable.get("mode"), str):
            facts["editable_mode"] = editable["mode"]
            if editable["mode"] == "inplace":
                findings.append(
                    Finding(
                        Status.COMPATIBLE,
                        "pyproject.toml",
                        _line_for(text, "editable"),
                        "pyproject-editable",
                        'editable.mode = "inplace" opts in to editable installs with scikit-build-core',
                    )
                )
    return findings, facts


def _classify_requirement(requirement: str, text: str, findings: list[Finding]) -> None:
    match = re.match(
        r"\s*scikit-build(?P<spec>\s*(?:\[.*?\])?\s*(?P<op>[<>=!~]=?)\s*(?P<ver>[A-Za-z0-9.*!+_-]+))?",
        requirement,
    )
    if match is None or match.group("spec") is None:
        return
    operator = match.group("op") or ""
    version = match.group("ver") or ""
    line = _line_for(text, requirement.strip())
    blocks_upgrade = operator in {"<", "<=", "~=", "=="} and (
        version.startswith("0") or (operator in {"<", "<="} and version.startswith("1"))
    )
    if blocks_upgrade:
        findings.append(
            Finding(
                Status.REMOVED,
                "pyproject.toml",
                line,
                "pyproject-old-pin",
                f"build requirement {requirement.strip()!r} prevents installing scikit-build 1.0",
                'update the pin, e.g. requires = ["scikit-build>=1"]',
            )
        )
    else:
        findings.append(
            Finding(
                Status.COMPATIBLE,
                "pyproject.toml",
                line,
                "pyproject-requirement",
                f"build requirement {requirement.strip()!r} allows scikit-build 1.0",
            )
        )


# ---------------------------------------------------------------------------
# Project-level checks (MANIFEST.in, editable installs, removed CLI in scripts)
# ---------------------------------------------------------------------------

_EDITABLE_RE = re.compile(
    r"(?:uv\s+)?pip[0-9.]*\s+install\b[^\n]*(?:^|\s)(?:-e|--editable)(?:\s|=|$)",
    re.IGNORECASE,
)


def _is_script_file(name: str) -> bool:
    if name.endswith(_SCRIPT_SUFFIXES):
        return True
    return name in {"Makefile", "makefile", "GNUmakefile", "tox.ini"}


def scan_auxiliary_file(path: Path, rel_path: str) -> tuple[list[Finding], bool]:
    """Scan build/CI scripts for removed CLI options and editable-install evidence.

    Only lines that clearly drive ``setup.py`` (or pip's global/build options)
    are inspected, so generic ``make -j`` usage is not reported.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], False
    findings: list[Finding] = []
    editable_evidence = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if _EDITABLE_RE.search(line):
            editable_evidence = True
        drives_setup = "setup.py" in line or "global-option" in line or "build-option" in line
        if not drives_setup:
            continue
        for match in _REMOVED_CLI_OPTION_RE.finditer(line):
            findings.append(
                Finding(
                    Status.REMOVED,
                    rel_path,
                    lineno,
                    "cli-option-removed",
                    f"the scikit-build-specific setup.py option {match.group(0)} was removed in 1.0",
                    _USE_NEW_SETTINGS,
                )
            )
        if re.search(r"(?:^|\s)-G(?:\s|=)", line):
            findings.append(
                Finding(
                    Status.REMOVED,
                    rel_path,
                    lineno,
                    "cli-option-removed",
                    "the -G/--generator setup.py option was removed in 1.0",
                    "use the CMAKE_GENERATOR environment variable instead",
                )
            )
        if re.search(r"(?:^|\s)-j(?:\s+\d+|\s|$)", line):
            findings.append(
                Finding(
                    Status.REMOVED,
                    rel_path,
                    lineno,
                    "cli-option-removed",
                    "the -j setup.py option was removed in 1.0",
                    "use the CMAKE_BUILD_PARALLEL_LEVEL environment variable instead",
                )
            )
        if re.search(r"setup\.py\b.*\s--\s", line):
            findings.append(
                Finding(
                    Status.REMOVED,
                    rel_path,
                    lineno,
                    "cli-option-removed",
                    "the 'setup.py args -- cmake args -- build tool args' sections were removed in 1.0",
                    _USE_NEW_SETTINGS,
                )
            )
    return findings, editable_evidence


def check_manifest(root: Path, has_cmake: bool) -> list[Finding]:
    """The classic sdist manifest was generated from git; that is gone."""
    if not has_cmake:
        return []
    manifest = root / "MANIFEST.in"
    if not manifest.is_file():
        return [
            Finding(
                Status.REMOVED,
                "MANIFEST.in",
                None,
                "manifest-git",
                "no MANIFEST.in: sdists no longer auto-generate their file list from git, "
                "so CMakeLists.txt/*.cmake files would be missing from the sdist",
                "add a MANIFEST.in (e.g. 'global-include CMakeLists.txt *.cmake') or use setuptools-scm",
            )
        ]
    try:
        content = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if "cmake" not in content.lower():
        return [
            Finding(
                Status.REMOVED,
                "MANIFEST.in",
                None,
                "manifest-git",
                "MANIFEST.in does not mention CMake files; classic scikit-build added them to sdists from git",
                "include CMakeLists.txt/*.cmake explicitly (or use a global-include/setuptools-scm)",
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _iter_project_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in _EXCLUDED_DIRS and not name.endswith((".egg-info", ".egg"))
        )
        for name in sorted(filenames):
            files.append(Path(dirpath) / name)
    return files


def check_project(root: Path) -> list[Finding]:
    """Statically scan the project at ``root`` and return all findings."""
    findings: list[Finding] = []
    has_cmake = False
    editable_evidence = False
    editable_mode: str | None = None
    has_pyproject = False

    for path in _iter_project_files(root):
        rel_path = path.relative_to(root).as_posix()
        name = path.name

        if name == "setup.py":
            findings.extend(scan_setup_py(path, rel_path))
        elif name == "pyproject.toml" and path.parent == root:
            has_pyproject = True
            pyproject_findings, facts = scan_pyproject(path)
            findings.extend(pyproject_findings)
            editable_mode = facts.get("editable_mode")
        elif name == "CMakeLists.txt":
            has_cmake = True
            findings.extend(scan_cmake_file(path, rel_path))
        elif name.endswith(".cmake"):
            findings.extend(scan_cmake_file(path, rel_path))

        if _is_script_file(name) and _is_ci_or_script_path(rel_path):
            aux_findings, evidence = scan_auxiliary_file(path, rel_path)
            findings.extend(aux_findings)
            editable_evidence = editable_evidence or evidence

    findings.extend(check_manifest(root, has_cmake))

    # Without pyproject.toml, PEP 517 isolated builds never see scikit-build.
    if (root / "setup.py").is_file() and not has_pyproject:
        findings.append(
            Finding(
                Status.UNKNOWN,
                "pyproject.toml",
                None,
                "pyproject-no-build-system",
                "no pyproject.toml: under PEP 517 isolated builds scikit-build is not installed automatically",
                'add requires = ["scikit-build>=1"] and the scikit_build_core.setuptools.build_meta backend',
            )
        )

    # Editable install requirement (only reported when the project uses -e).
    if editable_evidence and editable_mode != "inplace":
        findings.append(
            Finding(
                Status.REMOVED,
                "pyproject.toml",
                None,
                "pyproject-editable",
                'editable installs (pip install -e) require editable.mode = "inplace" in [tool.scikit-build]',
                "build_ext --inplace keeps working without this setting",
            )
        )

    return _dedupe(findings)


def _is_ci_or_script_path(rel_path: str) -> bool:
    """CI/build scripts live at the root, in scripts/, .github/ or .circleci/."""
    if "/" not in rel_path:
        return True
    top = rel_path.split("/", 1)[0]
    return top in {"scripts", "ci", ".github", ".circleci", ".buildkite", "docker", "deploy"}


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[Any, ...]] = set()
    unique: list[Finding] = []
    for finding in findings:
        key = (
            finding.status,
            finding.path,
            finding.line,
            finding.code,
            finding.message,
        )
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return sorted(unique, key=lambda f: (f.path, f.line or 0, f.status.value, f.code))


# ---------------------------------------------------------------------------
# Reporting and CLI
# ---------------------------------------------------------------------------

_REPORT_SECTIONS: tuple[tuple[Status, str], ...] = (
    (Status.REMOVED, "Removed (breaks or no longer takes effect on scikit-build 1.0)"),
    (Status.IGNORED, "Ignored (accepted, but has no effect)"),
    (Status.COMPATIBLE, "Still compatible"),
    (Status.UNKNOWN, "Could not determine statically (manual review needed)"),
)


def format_report(root: Path, findings: list[Finding]) -> str:
    """Render a human-readable report."""
    lines = ["scikit-build 1.0 migration check", f"Project: {root}", ""]
    counts = dict.fromkeys(Status, 0)
    for finding in findings:
        counts[finding.status] += 1

    for status, title in _REPORT_SECTIONS:
        section = [f for f in findings if f.status == status]
        lines.append(f"{title} -- {len(section)}")
        if not section:
            lines.append("  (none)")
        for finding in section:
            location = finding.path
            if finding.line is not None:
                location += f":{finding.line}"
            lines.append(f"  {location}: {finding.message}")
            if finding.suggestion:
                lines.append(f"      -> {finding.suggestion}")
        lines.append("")

    lines.append("Summary: " + ", ".join(f"{counts[status]} {status.value}" for status in Status))
    return "\n".join(lines)


def format_json(root: Path, findings: list[Finding]) -> str:
    """Render the report as JSON."""
    summary = {status.value: sum(1 for f in findings if f.status == status) for status in Status}
    payload = {
        "project": str(root),
        "summary": summary,
        "findings": [finding.as_dict() for finding in findings],
    }
    return json.dumps(payload, indent=2)


def main(argv: list[str] | None = None) -> int:
    """Console entry point. Returns 1 on removed usage, 2 on usage errors."""
    parser = argparse.ArgumentParser(
        prog="skbuild-migration-check",
        description=(
            "Statically check a project for scikit-build 0.x -> 1.0 migration issues "
            "without executing any project code."
        ),
    )
    parser.add_argument("path", nargs="?", default=".", help="project directory (default: current directory)")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="report format")
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.is_dir():
        parser.exit(2, f"error: {root} is not a directory\n")

    findings = check_project(root)
    if args.format == "json":
        sys.stdout.write(format_json(root, findings) + "\n")
    else:
        sys.stdout.write(format_report(root, findings) + "\n")
    return 1 if any(f.status == Status.REMOVED for f in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())

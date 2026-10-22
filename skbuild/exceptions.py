"""
This module defines exceptions commonly used in scikit-build.

``SKBuildError`` is now an alias of the setuptools ``SetupError`` raised by
the scikit-build-core setuptools plugin, kept so that existing
``except SKBuildError`` clauses continue to work.
"""

from __future__ import annotations

from setuptools.errors import SetupError as SKBuildError

__all__ = [
    "BundleError",
    "BundleExistsError",
    "BundleVerificationError",
    "InvalidModuleNameError",
    "ModuleNameCollisionError",
    "SKBuildError",
    "UnknownModuleError",
]


class BundleError(SKBuildError):
    """Base class for errors raised by the CMake module bundle export/verify API."""


class UnknownModuleError(BundleError):
    """A requested module is not provided by the resource set."""


class InvalidModuleNameError(BundleError):
    """A module or manifest path is not a safe, single-component name."""


class ModuleNameCollisionError(BundleError):
    """Two module names differ only in case, or a request matches only by case."""


class BundleExistsError(BundleError):
    """The destination already holds content that must not be overwritten."""


class BundleVerificationError(BundleError):
    """A bundle failed integrity verification (raised only in strict mode)."""


def __dir__() -> list[str]:
    return __all__

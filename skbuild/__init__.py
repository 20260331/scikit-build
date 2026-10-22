"""
scikit-build is an improved build system generator for CPython C extensions.

This module provides the *glue* between the setuptools Python module and CMake.
The build backend is provided by scikit-build-core's setuptools plugin.
"""

from __future__ import annotations

from scikit_build_core.setuptools.wrapper import setup

from ._version import version as __version__
from .resources.cmake import export_bundle, verify_bundle

__author__ = "The scikit-build team"
__email__ = "scikit-build@googlegroups.com"


__all__ = ["__version__", "export_bundle", "setup", "verify_bundle"]


def __dir__() -> list[str]:
    return __all__

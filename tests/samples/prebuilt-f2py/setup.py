from __future__ import annotations

from skbuild import setup

setup(
    name="prebuilt-f2py",
    version="1.2.3",
    description="sample exercising pre-generated F2PY source takeover with provenance",
    author="The scikit-build team",
    license="MIT",
    packages=["hello"],
)

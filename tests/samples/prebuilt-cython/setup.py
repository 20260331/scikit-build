from __future__ import annotations

import os

from skbuild import setup


def _noop_executable() -> str:
    """A zero-exit, no-output executable used to hide the real Cython.

    Pointing CYTHON_EXECUTABLE at a *-NOTFOUND value does not work: CMake
    re-runs find_program() when a variable holds a NOTFOUND result.
    """
    bindir = os.path.join(os.getcwd(), ".skb-noop")
    os.makedirs(bindir, exist_ok=True)
    if os.name == "nt":
        path = os.path.join(bindir, "true.bat")
        with open(path, "w", newline="\r\n") as f:
            f.write("@exit 0\r\n")
    else:
        path = os.path.join(bindir, "true")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
    return path


cmake_args = []
if os.environ.get("SKB_HIDE_CYTHON"):
    cmake_args.append(f"-DCYTHON_EXECUTABLE:FILEPATH={_noop_executable()}")
if os.environ.get("SKB_CYTHON_FLAGS"):
    cmake_args.append(f"-DCYTHON_FLAGS={os.environ['SKB_CYTHON_FLAGS']}")

setup(
    name="prebuilt-cython",
    version="1.2.3",
    description="sample exercising pre-generated source takeover with provenance",
    author="The scikit-build team",
    license="MIT",
    packages=["prebuilt_cython"],
    package_dir={"prebuilt_cython": "hello"},
    cmake_args=cmake_args,
)

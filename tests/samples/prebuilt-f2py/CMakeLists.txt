cmake_minimum_required(VERSION 3.5...3.26)

project(prebuilt_f2py C Fortran)

find_package(PythonExtensions REQUIRED)
find_package(NumPy REQUIRED)
find_package(F2PY REQUIRED)

if(DEFINED ENV{SKB_F2PY_PREBUILT_CACHE} AND NOT "$ENV{SKB_F2PY_PREBUILT_CACHE}" STREQUAL "")
  set(F2PY_PREBUILT "$ENV{SKB_F2PY_PREBUILT_CACHE}"
      CACHE STRING "prebuilt takeover mode from the test driver" FORCE)
endif()

add_subdirectory(hello)

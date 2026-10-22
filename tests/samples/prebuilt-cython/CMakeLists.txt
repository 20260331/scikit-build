cmake_minimum_required(VERSION 3.5...3.26)

project(prebuilt_cython)

find_package(PythonExtensions REQUIRED)
# Intentionally not REQUIRED: with CYTHON_PREBUILT=REQUIRE and a valid
# provenance credential, Cython itself does not need to be installed.
find_package(Cython)

if(DEFINED ENV{SKB_CYTHON_PREBUILT_CACHE} AND NOT "$ENV{SKB_CYTHON_PREBUILT_CACHE}" STREQUAL "")
  set(CYTHON_PREBUILT "$ENV{SKB_CYTHON_PREBUILT_CACHE}"
      CACHE STRING "prebuilt takeover mode from the test driver" FORCE)
endif()

add_subdirectory(hello)

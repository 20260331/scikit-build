#.rst:
#
# Internal helpers backing the ``PREBUILT`` option of
# :cmake:command:`add_cython_target` and :cmake:command:`add_f2py_target`.
#
# A *prebuilt source* is a generator output (e.g. a Cython ``.c``/``.cxx``
# file or an F2PY ``*module.c`` file) shipped in the source tree, typically in
# a released sdist. It is only considered trustworthy when it is accompanied
# by a provenance credential (a sidecar file named ``<output>.provenance``)
# recording:
#
# * the generator that produced it (``cython`` or ``f2py``) and its version,
# * the canonical generation options,
# * the SHA256 of every generated output, and
# * the SHA256 of every generator input, keyed by a path relative to the
#   directory of the primary input (the ``.pyx``/``.pyf`` file).
#
# Credential format (version 1), one ``key: value`` pair per line::
#
#   provenance-version: 1
#   generator: cython
#   generator-version: 3.0.11
#   options: cython/1|lang=CXX|py=default|embed=0|annotate=0|debug=0|flags=
#   output: <sha256> <output-basename>
#   input: <sha256> <input-relative-path>
#
# This file is an implementation detail of UseCython.cmake / UseF2PY.cmake.
#
#=============================================================================
# Copyright 2024 The scikit-build team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#=============================================================================

if(DEFINED _SKBUILD_USE_PREBUILT_PROVENANCE_INCLUDED)
  return()
endif()
set(_SKBUILD_USE_PREBUILT_PROVENANCE_INCLUDED TRUE)

# _skbuild_prebuilt_resolve_mode(<out_var> KEYWORD <value> CACHE <cache_var>)
#
# Merge the per-call PREBUILT keyword argument with the corresponding cache
# variable and normalize to one of REQUIRE, PREFER, REGENERATE.  An empty,
# OFF, FALSE, NO or NONE value means REGENERATE (the historical behavior).
function(_skbuild_prebuilt_resolve_mode _out_var)
  set(oneValueArgs "KEYWORD;CACHE")
  cmake_parse_arguments(_mode "" "${oneValueArgs}" "" ${ARGN})

  set(_mode "${_mode_KEYWORD}")
  if(_mode STREQUAL "" AND DEFINED _mode_CACHE)
    set(_mode "${${_mode_CACHE}}")
  endif()
  string(TOUPPER "${_mode}" _mode)
  string(STRIP "${_mode}" _mode)

  if(_mode STREQUAL "" OR _mode MATCHES "^(OFF|FALSE|NO|NONE|0)$")
    set(_mode "REGENERATE")
  endif()

  set(_valid_modes REQUIRE PREFER REGENERATE)
  if(NOT _mode IN_LIST _valid_modes)
    message(FATAL_ERROR
      "PREBUILT must be one of REQUIRE, PREFER or REGENERATE (got '${_mode}')")
  endif()

  set(${_out_var} "${_mode}" PARENT_SCOPE)
endfunction()

# _skbuild_provenance_writer_script(<out_var>)
#
# Write a CMake script (re-generated at every configure, invoked by the
# generator custom command at build time) that hashes the generated outputs
# and their inputs and produces the credential file.  The script path is
# returned in <out_var>.
function(_skbuild_provenance_writer_script _out_var)
  set(_script
    "${CMAKE_CURRENT_BINARY_DIR}/CMakeFiles/skbuild-provenance-write.cmake")

  # Everything below is written verbatim; the variables it references are
  # passed with -D when the script is invoked with `cmake -P`.
  file(WRITE "${_script}" [[# scikit-build prebuilt-source provenance writer.
# Required -D arguments:
#   GENERATOR, GENERATOR_VERSION, OPTIONS, PRIMARY_DIR, CREDENTIAL_FILE,
#   OUTPUT_FILES (list), INPUT_FILES (list)
foreach(_required_var
    GENERATOR OPTIONS PRIMARY_DIR CREDENTIAL_FILE OUTPUT_FILES INPUT_FILES)
  if(NOT DEFINED ${_required_var})
    message(FATAL_ERROR
      "skbuild provenance writer: missing -D${_required_var} argument")
  endif()
endforeach()

set(_provenance "")
string(APPEND _provenance "provenance-version: 1\n")
string(APPEND _provenance "generator: ${GENERATOR}\n")
if(DEFINED GENERATOR_VERSION)
  string(APPEND _provenance "generator-version: ${GENERATOR_VERSION}\n")
else()
  string(APPEND _provenance "generator-version: \n")
endif()
string(APPEND _provenance "options: ${OPTIONS}\n")

foreach(_output IN LISTS OUTPUT_FILES)
  if(NOT EXISTS "${_output}")
    message(FATAL_ERROR
      "skbuild provenance writer: generated output missing: ${_output}")
  endif()
  file(SHA256 "${_output}" _output_hash)
  get_filename_component(_output_name "${_output}" NAME)
  string(APPEND _provenance "output: ${_output_hash} ${_output_name}\n")
endforeach()

foreach(_input IN LISTS INPUT_FILES)
  if(NOT EXISTS "${_input}")
    message(FATAL_ERROR
      "skbuild provenance writer: generator input missing: ${_input}")
  endif()
  file(SHA256 "${_input}" _input_hash)
  file(RELATIVE_PATH _input_key "${PRIMARY_DIR}" "${_input}")
  string(REPLACE "\\" "/" _input_key "${_input_key}")
  string(APPEND _provenance "input: ${_input_hash} ${_input_key}\n")
endforeach()

file(WRITE "${CREDENTIAL_FILE}" "${_provenance}")
]])

  set(${_out_var} "${_script}" PARENT_SCOPE)
endfunction()

# _skbuild_provenance_check(<valid_out_var> <reason_out_var>
#                           GENERATOR <name>
#                           [GENERATOR_VERSION <version>]
#                           OPTIONS <canonical-options>
#                           PRIMARY_DIR <dir>
#                           CREDENTIAL <file>
#                           CANDIDATES <output-file>...
#                           INPUTS <input-file>...)
#
# Validate a prebuilt source takeover.  Sets <valid_out_var> to TRUE/FALSE in
# the parent scope and, on failure, <reason_out_var> to a human-readable
# explanation.  Either every recorded output/input must exist and match, or
# the takeover is rejected; a credential can never partially match.
function(_skbuild_provenance_check _valid_out_var _reason_out_var)
  set(oneValueArgs GENERATOR GENERATOR_VERSION OPTIONS PRIMARY_DIR CREDENTIAL)
  set(multiValueArgs CANDIDATES INPUTS)
  cmake_parse_arguments(_prov "" "${oneValueArgs}" "${multiValueArgs}" ${ARGN})

  # return() inside this macro leaves the calling function, not just the
  # macro (documented CMake behavior).  The rejection reason may be given as
  # several adjacent string arguments, like message() accepts.
  macro(_skbuild_provenance_reject)
    string(JOIN "" _reject_reason ${ARGN})
    set(${_valid_out_var} FALSE PARENT_SCOPE)
    set(${_reason_out_var} "${_reject_reason}" PARENT_SCOPE)
    return()
  endmacro()

  set(${_valid_out_var} FALSE PARENT_SCOPE)
  set(${_reason_out_var} "" PARENT_SCOPE)

  if(NOT _prov_GENERATOR OR NOT _prov_PRIMARY_DIR OR NOT _prov_CREDENTIAL)
    _skbuild_provenance_reject("internal error: incomplete provenance check arguments")
  endif()

  if(NOT EXISTS "${_prov_CREDENTIAL}")
    _skbuild_provenance_reject(
      "provenance credential not found next to the pre-generated source: "
      "${_prov_CREDENTIAL}")
  endif()

  foreach(_candidate IN LISTS _prov_CANDIDATES)
    if(NOT EXISTS "${_candidate}")
      _skbuild_provenance_reject(
        "pre-generated output declared by the credential is missing: "
        "${_candidate}")
    endif()
  endforeach()

  foreach(_input IN LISTS _prov_INPUTS)
    if(NOT EXISTS "${_input}")
      _skbuild_provenance_reject(
        "generator input required for the verification is missing: ${_input}")
    endif()
  endforeach()

  set(_cred_version "")
  set(_cred_generator "")
  set(_cred_generator_version "")
  set(_cred_options "")
  set(_cred_output_names "")
  set(_cred_output_hashes "")
  set(_cred_input_keys "")
  set(_cred_input_hashes "")

  file(STRINGS "${_prov_CREDENTIAL}" _prov_lines ENCODING UTF-8)
  foreach(_line IN LISTS _prov_lines)
    if(_line MATCHES "^provenance-version: (.*)$")
      set(_cred_version "${CMAKE_MATCH_1}")
    elseif(_line MATCHES "^generator-version: (.*)$")
      set(_cred_generator_version "${CMAKE_MATCH_1}")
    elseif(_line MATCHES "^generator: (.*)$")
      set(_cred_generator "${CMAKE_MATCH_1}")
    elseif(_line MATCHES "^options: (.*)$")
      set(_cred_options "${CMAKE_MATCH_1}")
    # CMake regular expressions have only recently grown bounded "{n}"
    # repetitions, so match the hex digest by character class; the hash
    # comparison below is what enforces integrity.
    elseif(_line MATCHES "^output: ([0-9a-fA-F]+) (.*)$")
      list(APPEND _cred_output_names "${CMAKE_MATCH_2}")
      string(TOLOWER "${CMAKE_MATCH_1}" _hash_lower)
      list(APPEND _cred_output_hashes "${_hash_lower}")
    elseif(_line MATCHES "^input: ([0-9a-fA-F]+) (.*)$")
      list(APPEND _cred_input_keys "${CMAKE_MATCH_2}")
      string(TOLOWER "${CMAKE_MATCH_1}" _hash_lower)
      list(APPEND _cred_input_hashes "${_hash_lower}")
    endif()
  endforeach()

  if(NOT _cred_version STREQUAL "1")
    _skbuild_provenance_reject(
      "unsupported provenance credential format (version '${_cred_version}')")
  endif()

  if(NOT _cred_generator STREQUAL "${_prov_GENERATOR}")
    _skbuild_provenance_reject(
      "provenance credential was produced for the '${_cred_generator}' "
      "generator, expected '${_prov_GENERATOR}'")
  endif()

  # Only compare generator versions when both are known.  A REQUIREd
  # takeover may legitimately run on a machine without the generator
  # executable, in which case the version cannot be determined.
  if(DEFINED _prov_GENERATOR_VERSION
     AND NOT _prov_GENERATOR_VERSION STREQUAL ""
     AND NOT _cred_generator_version STREQUAL ""
     AND NOT _cred_generator_version STREQUAL "${_prov_GENERATOR_VERSION}")
    _skbuild_provenance_reject(
      "generator version mismatch: the pre-generated source was produced "
      "with ${_prov_GENERATOR} ${_cred_generator_version} but "
      "${_prov_GENERATOR_VERSION} is now in use")
  endif()

  if(NOT _cred_options STREQUAL "${_prov_OPTIONS}")
    _skbuild_provenance_reject(
      "generation options mismatch:\n"
      "  credential: ${_cred_options}\n"
      "  in use:     ${_prov_OPTIONS}")
  endif()

  list(LENGTH _cred_output_names _cred_output_count)
  list(LENGTH _prov_CANDIDATES _candidate_count)
  if(NOT _cred_output_count EQUAL _candidate_count)
    _skbuild_provenance_reject(
      "pre-generated output set differs: the credential lists "
      "${_cred_output_count} file(s): ${_cred_output_names}")
  endif()

  foreach(_candidate IN LISTS _prov_CANDIDATES)
    get_filename_component(_name "${_candidate}" NAME)
    list(FIND _cred_output_names "${_name}" _idx)
    if(_idx EQUAL -1)
      _skbuild_provenance_reject(
        "pre-generated output is not recorded in the credential: ${_name}")
    endif()
    list(GET _cred_output_hashes ${_idx} _expected_hash)
    file(SHA256 "${_candidate}" _actual_hash)
    if(NOT _actual_hash STREQUAL _expected_hash)
      _skbuild_provenance_reject(
        "pre-generated output '${_name}' was modified after the credential "
        "was written (content hash mismatch)")
    endif()
  endforeach()

  foreach(_input IN LISTS _prov_INPUTS)
    file(RELATIVE_PATH _key "${_prov_PRIMARY_DIR}" "${_input}")
    string(REPLACE "\\" "/" _key "${_key}")
    list(FIND _cred_input_keys "${_key}" _idx)
    if(_idx EQUAL -1)
      _skbuild_provenance_reject(
        "generator input '${_key}' is not recorded in the credential; the "
        "pre-generated source may have been produced from different inputs")
    endif()
    list(GET _cred_input_hashes ${_idx} _expected_hash)
    file(SHA256 "${_input}" _actual_hash)
    if(NOT _actual_hash STREQUAL _expected_hash)
      _skbuild_provenance_reject(
        "generator input '${_key}' changed since the credential was written "
        "(content hash mismatch)")
    endif()
  endforeach()

  list(LENGTH _cred_input_keys _cred_input_count)
  list(LENGTH _prov_INPUTS _input_count)
  if(NOT _cred_input_count EQUAL _input_count)
    _skbuild_provenance_reject(
      "generator input set differs: ${_cred_input_count} file(s) are "
      "recorded in the credential but ${_input_count} are in use now")
  endif()

  set(${_valid_out_var} TRUE PARENT_SCOPE)
  set(${_reason_out_var} "" PARENT_SCOPE)
endfunction()

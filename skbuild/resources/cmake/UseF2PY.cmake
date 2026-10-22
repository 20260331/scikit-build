#.rst:
#
# The following functions are defined:
#
# .. cmake:command:: add_f2py_target
#
# Create a custom rule to generate the source code for a Python extension module
# using f2py.
#
#   add_f2py_target(<Name> [<F2PYInput>]
#                   [PREBUILT <REQUIRE | PREFER | REGENERATE>]
#                   [OUTPUT_VAR <OutputVar>]
#                   [DEPENDS [source [source2...]]])
#
# ``<Name>`` is the name of the new target, and ``<F2PYInput>``
# is the path to a pyf source file.  Note that, despite the name, no new
# targets are created by this function.  Instead, see ``OUTPUT_VAR`` for
# retrieving the path to the generated source for subsequent targets.
#
# If only ``<Name>`` is provided, and it ends in the ".pyf" extension, then it
# is assumed to be the ``<F2PYInput>``.  The name of the input without the
# extension is used as the target name.  If only ``<Name>`` is provided, and it
# does not end in the ".pyf" extension, then the ``<F2PYInput>`` is assumed to
# be ``<Name>.pyf``.
#
#
# Options:
#
# ``PREBUILT <REQUIRE | PREFER | REGENERATE>``
#   Control the takeover of pre-generated sources shipped in the source tree,
#   next to the ``.pyf`` input (``<name>module.c``, the ``<name>-f2pywrappers*``
#   files and a sidecar ``<name>module.c.provenance`` credential).  The
#   credential records the F2PY (NumPy) version and the SHA256 of every input
#   and output, so a stale or edited copy can never be mistaken for a valid
#   one:
#
#   ``REQUIRE``
#     Do not run f2py.  The pre-generated sources must be present with a valid
#     credential, otherwise configuration fails.
#
#   ``PREFER``
#     Use the pre-generated sources when their credential is valid; fall back
#     to running f2py otherwise.  Source-tree copies without a valid
#     credential are never compiled directly.
#
#   ``REGENERATE``
#     Ignore any source-tree copies and always run f2py (the default).  The
#     regenerated sources are written in the build tree together with a fresh
#     credential that can be shipped next to the input when preparing an sdist.
#
#   Whatever the mode, compiled files always live in the build tree; a
#   pre-generated source is only ever copied there after its credential has
#   been verified.  Defaults to the ``F2PY_PREBUILT`` cache variable.
#
# ``OUTPUT_VAR <OutputVar>``
#   Set the variable ``<OutputVar>`` in the parent scope to the path to the
#   generated source file.  By default, ``<Name>`` is used as the output
#   variable name.
#
# ``DEPENDS [source [source2...]]``
#   Sources that must be generated before the F2PY command is run.
#
# Cache variables that affect the behavior include:
#
# ``F2PY_PREBUILT``
#   Default for the ``PREBUILT`` option: one of ``REQUIRE``, ``PREFER`` or
#   ``REGENERATE`` (the default).
#
# Defined variables:
#
# ``<OutputVar>``
#   The path of the generated source file.
#
# Example usage
# ^^^^^^^^^^^^^
#
# .. code-block:: cmake
#
#   find_package(F2PY)
#
#   # Note: In this case, either one of these arguments may be omitted; their
#   # value would have been inferred from that of the other.
#   add_f2py_target(f2py_code f2py_code.pyf)
#
#   add_library(f2py_code MODULE ${f2py_code})
#   target_link_libraries(f2py_code ...)
#
#=============================================================================
# Copyright 2011 Kitware, Inc.
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

get_property(languages GLOBAL PROPERTY ENABLED_LANGUAGES)

set(F2PY_PREBUILT "REGENERATE" CACHE STRING
    "Take over pre-generated F2PY sources shipped next to the .pyf input when a valid .provenance credential accompanies them: REQUIRE (fail without one), PREFER (use them, regenerate otherwise) or REGENERATE (always run f2py; the default).")
set_property(CACHE F2PY_PREBUILT PROPERTY STRINGS REQUIRE PREFER REGENERATE)
mark_as_advanced(F2PY_PREBUILT)

include(UsePrebuiltProvenance)

function(add_f2py_target _name)
  set(options )
  set(oneValueArgs OUTPUT_VAR PREBUILT)
  set(multiValueArgs DEPENDS)
  cmake_parse_arguments(_args "${options}" "${oneValueArgs}" "${multiValueArgs}" ${ARGN})

  list(GET _args_UNPARSED_ARGUMENTS 0 _arg0)

  # if provided, use _arg0 as the input file path
  if(_arg0)
    set(_source_file ${_arg0})

  # otherwise, must determine source file from name, or vice versa
  else()
    get_filename_component(_name_ext "${_name}" EXT)

    # if extension provided, _name is the source file
    if(_name_ext)
      set(_source_file ${_name})
      string(REGEX REPLACE "\\.[^.]*$" "" _name ${_source_file})

    # otherwise, assume the source file is ${_name}.pyf
    else()
      set(_source_file ${_name}.pyf)
    endif()
  endif()

  if("C" IN_LIST languages)
    set(_output_syntax "C")
  else()
    message(FATAL_ERROR "C must be enabled to use F2PY")
  endif()

  set(generated_file "${CMAKE_CURRENT_BINARY_DIR}/${_name}module.c")
  set(generated_wrappers
    "${CMAKE_CURRENT_BINARY_DIR}/${_name}-f2pywrappers.f"
    "${CMAKE_CURRENT_BINARY_DIR}/${_name}-f2pywrappers2.f90"
    )

  get_filename_component(generated_file_dir ${generated_file} DIRECTORY)

  set_source_files_properties(${generated_file} PROPERTIES GENERATED TRUE)
  set_source_files_properties(${generated_wrappers} PROPERTIES GENERATED TRUE)

  set(_output_var ${_name})
  if(_args_OUTPUT_VAR)
      set(_output_var ${_args_OUTPUT_VAR})
  endif()
  set(${_output_var} ${generated_file} ${generated_wrappers} PARENT_SCOPE)

  file(RELATIVE_PATH generated_file_relative
      ${CMAKE_BINARY_DIR} ${generated_file})

  set(comment "Generating ${_output_syntax} source ${generated_file_relative}")

  # Get the include directories.
  get_source_file_property(pyf_location ${_source_file} LOCATION)
  get_filename_component(pyf_path ${pyf_location} PATH)

  # Create the directory so that the command can cd to it
  file(MAKE_DIRECTORY ${generated_file_dir})

  # Build the provenance information.  The .pyf is always an input; user
  # DEPENDS are resolved like the primary source, falling back to the build
  # tree so that sources generated earlier in the build can be recorded.
  set(_provenance_inputs "${pyf_location}")
  foreach(_dep IN LISTS _args_DEPENDS)
    get_source_file_property(_dep_location "${_dep}" LOCATION)
    if(_dep_location AND EXISTS "${_dep_location}")
      list(APPEND _provenance_inputs "${_dep_location}")
    elseif(EXISTS "${_dep}")
      list(APPEND _provenance_inputs "${_dep}")
    elseif(IS_ABSOLUTE "${_dep}")
      list(APPEND _provenance_inputs "${_dep}")
    else()
      list(APPEND _provenance_inputs "${CMAKE_CURRENT_BINARY_DIR}/${_dep}")
    endif()
  endforeach()
  list(REMOVE_DUPLICATES _provenance_inputs)

  # add_f2py_target takes no generation flags; the generator identity is the
  # only thing that can change the outputs besides the inputs.
  set(_provenance_options "f2py/1")

  # Locate the pre-generated sources in the source tree, next to the .pyf.
  set(_prebuilt_candidates "")
  foreach(_generated IN LISTS generated_file generated_wrappers)
    get_filename_component(_generated_name "${_generated}" NAME)
    list(APPEND _prebuilt_candidates "${pyf_path}/${_generated_name}")
  endforeach()
  list(GET _prebuilt_candidates 0 _prebuilt_credential)
  set(_prebuilt_credential "${_prebuilt_credential}.provenance")

  _skbuild_prebuilt_resolve_mode(_prebuilt_mode
                                 KEYWORD "${_args_PREBUILT}"
                                 CACHE F2PY_PREBUILT)
  set(_prebuilt_takeover FALSE)
  if(NOT _prebuilt_mode STREQUAL "REGENERATE")
    _skbuild_provenance_check(_prebuilt_valid _prebuilt_reason
      GENERATOR f2py
      GENERATOR_VERSION "${F2PY_VERSION_STRING}"
      OPTIONS "${_provenance_options}"
      PRIMARY_DIR "${pyf_path}"
      CREDENTIAL "${_prebuilt_credential}"
      CANDIDATES ${_prebuilt_candidates}
      INPUTS ${_provenance_inputs})

    if(_prebuilt_valid)
      set(_prebuilt_takeover TRUE)
      message(STATUS
        "F2PY: taking over verified pre-generated sources in ${pyf_path} "
        "(${_prebuilt_mode})")
    elseif(_prebuilt_mode STREQUAL "REQUIRE")
      message(FATAL_ERROR
        "F2PY: PREBUILT REQUIRE for '${_name}', but the pre-generated "
        "sources could not be verified:\n  ${_prebuilt_reason}\n"
        "Ship freshly generated sources together with their '.provenance' "
        "credential, or set PREBUILT/F2PY_PREBUILT to PREFER or REGENERATE.")
    else()
      message(WARNING
        "F2PY: pre-generated sources in '${pyf_path}' rejected "
        "(${_prebuilt_reason}); regenerating with f2py")
    endif()
  endif()

  if(_prebuilt_takeover)
    # Only verified copies are materialized in the build tree; the
    # source-tree files themselves are never build inputs.
    list(GET _prebuilt_candidates 0 _candidate_module)
    list(GET _prebuilt_candidates 1 _candidate_wrapper)
    list(GET _prebuilt_candidates 2 _candidate_wrapper2)
    list(GET generated_wrappers 0 _generated_wrapper)
    list(GET generated_wrappers 1 _generated_wrapper2)
    add_custom_command(OUTPUT ${generated_file} ${generated_wrappers}
                       COMMAND ${CMAKE_COMMAND} -E copy_if_different
                               "${_candidate_module}" "${generated_file}"
                       COMMAND ${CMAKE_COMMAND} -E copy_if_different
                               "${_candidate_wrapper}" "${_generated_wrapper}"
                       COMMAND ${CMAKE_COMMAND} -E copy_if_different
                               "${_candidate_wrapper2}" "${_generated_wrapper2}"
                       DEPENDS ${_prebuilt_candidates}
                               "${_prebuilt_credential}"
                       COMMENT "Copying verified pre-generated F2PY sources ${generated_file_relative}"
                       VERBATIM)
  else()
    _skbuild_provenance_writer_script(_provenance_writer)
    set(_all_generated "${generated_file}" ${generated_wrappers})
    set(_provenance_credential "${generated_file}.provenance")
    string(REPLACE ";" "\\;" _provenance_inputs_escaped "${_provenance_inputs}")
    string(REPLACE ";" "\\;" _provenance_outputs_escaped "${_all_generated}")

    # VERBATIM keeps the canonical options string and paths safe from shell
    # interpretation when the credential is refreshed.
    set(_provenance_writer_args
        "-DGENERATOR=f2py"
        "-DGENERATOR_VERSION=${F2PY_VERSION_STRING}"
        "-DOPTIONS=${_provenance_options}"
        "-DPRIMARY_DIR=${pyf_path}"
        "-DCREDENTIAL_FILE=${_provenance_credential}"
        "-DOUTPUT_FILES=${_provenance_outputs_escaped}"
        "-DINPUT_FILES=${_provenance_inputs_escaped}"
        "-P" "${_provenance_writer}")

    # f2py only writes a given wrappers file when the signature file actually
    # needs Fortran 77 / 90 wrappers, while both are declared outputs above.
    # Touch them so the output set is stable (empty files compile fine) and
    # the provenance credential can cover every declared output.
    set(_f2py_touch_args -E touch)
    foreach(_wrapper IN LISTS generated_wrappers)
      list(APPEND _f2py_touch_args "${_wrapper}")
    endforeach()

    # Run the compiler, materialize any absent wrapper and refresh the
    # credential.
    add_custom_command(OUTPUT ${generated_file} ${generated_wrappers}
                              ${_provenance_credential}
                       COMMAND ${F2PY_EXECUTABLE} "${pyf_location}"
                       COMMAND ${CMAKE_COMMAND} ${_f2py_touch_args}
                       COMMAND ${CMAKE_COMMAND} ${_provenance_writer_args}
                       DEPENDS ${_source_file}
                               ${_args_DEPENDS}
                       WORKING_DIRECTORY ${generated_file_dir}
                       COMMENT "${comment}"
                       VERBATIM)
  endif()

endfunction()

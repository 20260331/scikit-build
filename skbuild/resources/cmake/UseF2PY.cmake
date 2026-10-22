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
#                   [OUTPUT_VAR <OutputVar>])
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
# ``OUTPUT_VAR <OutputVar>``
#   Set the variable ``<OutputVar>`` in the parent scope to the path to the
#   generated source file.  By default, ``<Name>`` is used as the output
#   variable name.
#
# ``DEPENDS [source [source2...]]``
#   Sources that must be generated before the F2PY command is run.
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

# Remember the directory containing the scikit-build CMake helper scripts so
# the custom command below can locate PublishGeneratedOutputs.cmake at build
# time regardless of the directory scope the function is called from.
set_property(GLOBAL PROPERTY _skbuild_cmake_module_dir "${CMAKE_CURRENT_LIST_DIR}")

function(add_f2py_target _name)
  set(options )
  set(oneValueArgs OUTPUT_VAR)
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

  # F2PY emits its outputs (module.c and, only when needed, the wrapper
  # sources) at once. Generate into a private staging directory and only
  # publish the files to their final location once the complete required
  # output set has been validated, so a failed run can never leave old and
  # new outputs mixed together. The wrapper sources are optional: the publish
  # step materializes empty placeholders when F2PY does not generate them.
  set(staging_dir "${CMAKE_CURRENT_BINARY_DIR}/.${_name}.f2py-staging")
  set(_publish_outputs ${generated_file})
  set(_optional_publish_outputs ${generated_wrappers})

  get_property(_publish_script_dir GLOBAL PROPERTY _skbuild_cmake_module_dir)
  set(publish_script "${_publish_script_dir}/PublishGeneratedOutputs.cmake")

  # Escape list separators so the output sets survive the build tool's
  # command line when passed to the publish script with -D.
  string(REPLACE ";" "\\;" _publish_outputs_arg "${_publish_outputs}")
  string(REPLACE ";" "\\;" _optional_publish_outputs_arg "${_optional_publish_outputs}")

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

  # The F2PY outputs are first generated into the staging directory (F2PY
  # always writes next to its working directory) and only moved in place by
  # the publish script once the complete required output set has been
  # verified.
  add_custom_command(OUTPUT ${generated_file} ${generated_wrappers}
                     COMMAND ${CMAKE_COMMAND} -E remove_directory "${staging_dir}"
                     COMMAND ${CMAKE_COMMAND} -E make_directory "${staging_dir}"
                     COMMAND ${CMAKE_COMMAND} -E chdir "${staging_dir}"
                             ${F2PY_EXECUTABLE} ${pyf_location}
                     COMMAND ${CMAKE_COMMAND}
                             -DGENERATOR_NAME=F2PY
                             "-DSTAGING_DIR=${staging_dir}"
                             "-DOUTPUT_FILES=${_publish_outputs_arg}"
                             "-DOPTIONAL_OUTPUT_FILES=${_optional_publish_outputs_arg}"
                             -P "${publish_script}"
                     DEPENDS ${_source_file}
                             ${_args_DEPENDS}
                     COMMENT "${comment}")

endfunction()

# PublishGeneratedOutputs.cmake
#
# Transactional publish helper for generators (such as Cython and F2PY) that
# produce a set of related files in one invocation.
#
# This script is meant to be executed in script mode (``cmake -P``) from the
# custom commands created by ``add_cython_target`` and ``add_f2py_target``.
# The generator is run first with all of its outputs redirected to a private
# staging directory, and this script runs only if the generator itself exits
# successfully. It then:
#
# 1. validates the *complete* required output set, refusing to publish and
#    leaving any previously generated files untouched if one is missing;
# 2. replaces the final output files, one by one, with the staged ones;
# 3. materializes optional outputs (as empty files when not generated);
# 4. removes the staging directory.
#
# Replacing the outputs only after the full set has been generated ensures a
# failing generator can never leave a mixture of old and new files in the
# build directory.
#
# Expected command-line variables:
#
# ``GENERATOR_NAME``
#   Human-readable name of the generator, used in error messages.
#
# ``STAGING_DIR``
#   Directory holding the freshly generated files. Every expected output is
#   looked up in this directory by its file name.
#
# ``OUTPUT_FILES``
#   Semicolon-separated list of the absolute paths of the required output
#   files. Publishing aborts (with a non-zero exit code) if any of them is
#   missing from the staging directory.
#
# ``OPTIONAL_OUTPUT_FILES``
#   Semicolon-separated list of the absolute paths of outputs the generator
#   may omit: the staged file is published when present, otherwise an empty
#   placeholder is created at the final path so declared build outputs always
#   exist after a successful generation.

if(NOT DEFINED STAGING_DIR OR STAGING_DIR STREQUAL "")
  message(FATAL_ERROR "PublishGeneratedOutputs: STAGING_DIR must be provided")
endif()

if(NOT DEFINED OUTPUT_FILES OR OUTPUT_FILES STREQUAL "")
  message(FATAL_ERROR "PublishGeneratedOutputs: OUTPUT_FILES must be provided")
endif()

if(NOT DEFINED GENERATOR_NAME OR GENERATOR_NAME STREQUAL "")
  set(GENERATOR_NAME "Generator")
endif()

# Validate the complete required output set before touching any previously
# generated file, so the last complete result is preserved on failure.
set(_missing_outputs "")
foreach(_output_file IN LISTS OUTPUT_FILES)
  get_filename_component(_output_name "${_output_file}" NAME)
  if(NOT EXISTS "${STAGING_DIR}/${_output_name}")
    list(APPEND _missing_outputs "${_output_name}")
  endif()
endforeach()

if(_missing_outputs)
  message(FATAL_ERROR
    "${GENERATOR_NAME} did not produce the complete expected output set; "
    "missing: ${_missing_outputs}. The previously generated outputs were "
    "left untouched.")
endif()

# Publish the staged outputs. file(RENAME) replaces an existing destination
# atomically, so each output is either the old file or the new file, never a
# partially written one.
foreach(_output_file IN LISTS OUTPUT_FILES)
  get_filename_component(_output_name "${_output_file}" NAME)
  get_filename_component(_output_dir "${_output_file}" DIRECTORY)
  file(MAKE_DIRECTORY "${_output_dir}")
  file(RENAME "${STAGING_DIR}/${_output_name}" "${_output_file}")
endforeach()

# Optional outputs are only generated for some inputs (e.g. the F2PY wrapper
# sources). Publish them when present, otherwise write an empty placeholder so
# anything that consumes the declared outputs keeps working. The placeholder
# is rewritten on every successful generation so its timestamp is newer than
# the generator inputs and the build tool does not re-run the edge on every
# build.
foreach(_output_file IN LISTS OPTIONAL_OUTPUT_FILES)
  get_filename_component(_output_name "${_output_file}" NAME)
  get_filename_component(_output_dir "${_output_file}" DIRECTORY)
  file(MAKE_DIRECTORY "${_output_dir}")
  file(REMOVE "${_output_file}")
  if(EXISTS "${STAGING_DIR}/${_output_name}")
    file(RENAME "${STAGING_DIR}/${_output_name}" "${_output_file}")
  else()
    file(WRITE "${_output_file}"
      "! Empty output placeholder created because ${GENERATOR_NAME} did not generate ${_output_name}\n")
  endif()
endforeach()

# Discard anything the generator may have left behind in the staging area.
file(REMOVE_RECURSE "${STAGING_DIR}")

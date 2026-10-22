set(_module_name "_hello")

set(_hello_prebuilt)
if(DEFINED ENV{SKB_F2PY_PREBUILT_KEYWORD} AND NOT "$ENV{SKB_F2PY_PREBUILT_KEYWORD}" STREQUAL "")
  set(_hello_prebuilt PREBUILT "$ENV{SKB_F2PY_PREBUILT_KEYWORD}")
endif()

add_f2py_target(_hello.pyf ${_hello_prebuilt} OUTPUT_VAR _f2py_generated)

# The generated module/wrappers are consumed directly, together with the
# Fortran implementation of the wrapped subroutine.
add_library(${_module_name} MODULE ${_f2py_generated} ${_module_name}.f90)
target_include_directories(${_module_name} PRIVATE ${F2PY_INCLUDE_DIRS})
target_link_libraries(${_module_name} ${F2PY_LIBRARIES})
python_extension_module(${_module_name})

install(TARGETS ${_module_name}
  LIBRARY DESTINATION hello
)

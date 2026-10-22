set(_hello_prebuilt)
if(DEFINED ENV{SKB_CYTHON_PREBUILT_KEYWORD} AND NOT "$ENV{SKB_CYTHON_PREBUILT_KEYWORD}" STREQUAL "")
  set(_hello_prebuilt PREBUILT "$ENV{SKB_CYTHON_PREBUILT_KEYWORD}")
endif()

add_cython_target(_hello CXX ${_hello_prebuilt})
add_library(_hello MODULE ${_hello})
python_extension_module(_hello)

install(TARGETS _hello LIBRARY DESTINATION prebuilt_cython)

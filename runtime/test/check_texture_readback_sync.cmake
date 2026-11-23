# Run with -DTOOL=<vk_texture_test> -DSPIRV=<directory built by the GPU suite>
# -DOUTPUT=<readback file> -P runtime/test/check_texture_readback_sync.cmake.
# For a cross build, pass the configured emulator as "-DRUNNER=<emulator;args>".
# Requires the Khronos validation layer and a device that supports the texture harness.
execute_process(
  COMMAND "${CMAKE_COMMAND}" -E env VK_INSTANCE_LAYERS=VK_LAYER_KHRONOS_validation
          VK_LOADER_DEBUG=layer VK_LAYER_ENABLES=VK_VALIDATION_FEATURE_ENABLE_SYNCHRONIZATION_VALIDATION_EXT
          ${RUNNER} "${TOOL}" cube "${OUTPUT}"
  WORKING_DIRECTORY "${SPIRV}" TIMEOUT 90
  RESULT_VARIABLE code OUTPUT_VARIABLE log ERROR_VARIABLE log)
if(NOT code STREQUAL "0" OR NOT log MATCHES "vk_texture_test cube: PASSED"
    OR NOT log MATCHES "Insert instance layer \"VK_LAYER_KHRONOS_validation\""
    OR log MATCHES "SYNC-HAZARD|Validation Error")
  message(FATAL_ERROR "Texture draw with synchronization validation returned ${code}\n${log}")
endif()

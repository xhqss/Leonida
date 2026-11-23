foreach(mode args no_args fail_init)
  set(args)
  if(mode STREQUAL "args")
    set(args hello "two words")
  elseif(mode STREQUAL "fail_init")
    set(args fail-init)
  endif()
  execute_process(
    COMMAND ${RUNNER} "${LOADER}" --run "${GUEST}" -- ${args}
    TIMEOUT 90
    RESULT_VARIABLE code OUTPUT_VARIABLE output ERROR_VARIABLE error)
  if(mode STREQUAL "fail_init")
    if(NOT code STREQUAL "1" OR NOT error MATCHES "DllMain returned FALSE" OR output MATCHES "running ")
      message(FATAL_ERROR "Failed attach returned ${code}\n${output}\n${error}")
    endif()
  elseif(NOT code STREQUAL "0" OR NOT output MATCHES "tiny_startup_cmdline: PASS"
      OR NOT output MATCHES "game exited with code 0")
    message(FATAL_ERROR "Startup command line (${mode}) returned ${code}\n${output}\n${error}")
  endif()
endforeach()

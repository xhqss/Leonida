# Redirect the loader's stdout so both console paths must survive as UTF-8.
execute_process(
  COMMAND ${RUNNER} "${LOADER}" --game-dir "game-Игра" --overlay "overlay-保存"
          --run "${GUEST}" -- hello 42
  TIMEOUT 90 ENCODING UTF-8
  RESULT_VARIABLE code OUTPUT_VARIABLE output ERROR_VARIABLE error)
if(NOT code STREQUAL "0" OR
   NOT output MATCHES "console paths: GAME: -> [^\n]*game-Игра, writes -> [^\n]*overlay-保存\r?\n")
  message(FATAL_ERROR "Runtime did not report both Unicode paths (exit ${code})\n${output}\n${error}")
endif()

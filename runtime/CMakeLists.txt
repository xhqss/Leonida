cmake_minimum_required(VERSION 3.24)
project(gta6rt LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
  message(FATAL_ERROR "gta6rt must be built for x64 (run from an x64 Native Tools prompt or vcvars64.bat)")
endif()

if(WIN32)
  # The d3d12_xs shim and its tests use version 1.2 root signatures (D3D12_ROOT_SIGNATURE_DESC2), which the Windows
  # SDK declares from 10.0.26100 on. Stop here with the fix instead of failing halfway through the build.
  include(CheckCXXSourceCompiles)
  check_cxx_source_compiles([=[
    #include <d3d12.h>
    int main() { D3D12_VERSIONED_ROOT_SIGNATURE_DESC d{}; d.Version = D3D_ROOT_SIGNATURE_VERSION_1_2;
                 return static_cast<int>(sizeof(d.Desc_1_2)); }
  ]=] GTA6RT_SDK_HAS_ROOT_SIGNATURE_1_2)
  if(NOT GTA6RT_SDK_HAS_ROOT_SIGNATURE_1_2)
    if(CMAKE_VS_WINDOWS_TARGET_PLATFORM_VERSION)
      set(_gta6rt_sdk "${CMAKE_VS_WINDOWS_TARGET_PLATFORM_VERSION}")
    elseif(DEFINED ENV{WindowsSDKVersion})
      string(REGEX REPLACE "[\\/]+$" "" _gta6rt_sdk "$ENV{WindowsSDKVersion}")
    else()
      set(_gta6rt_sdk "an older one")
    endif()
    message(FATAL_ERROR
      "gta6rt needs the Windows SDK 10.0.26100 or newer (version 1.2 root signatures); "
      "this build uses ${_gta6rt_sdk}. Install it with "
      "`winget install Microsoft.WindowsSDK.10.0.26100` or the Visual Studio Installer, then delete the build "
      "folder and configure again.")
  endif()

  # Root-signature flag fix, shared by the d3d12_xs shim and its tests.
  add_library(gta6rt_d3d12_xs_rootsig STATIC
    shims/d3d12_xs_rootsig.cpp
  )
  target_include_directories(gta6rt_d3d12_xs_rootsig PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/shims)
  target_compile_definitions(gta6rt_d3d12_xs_rootsig PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
  target_compile_options(gta6rt_d3d12_xs_rootsig PRIVATE /W4 /permissive- /utf-8)

  # Portable root-signature model + packer to user SGPRs (no d3d12.h, no windows.h).
  add_library(gta6rt_root_signature STATIC
    src/root_signature.cpp
  )
  target_include_directories(gta6rt_root_signature PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
  target_compile_definitions(gta6rt_root_signature PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
  target_compile_options(gta6rt_root_signature PRIVATE /W4 /permissive- /utf-8)

  # Portable contract test: model, packer, canonical codec and RTS0 hypothesis.
  # Needs neither d3d12.lib nor the game; runs on Windows CI like the rest here.
  add_executable(root_signature_test
    test/root_signature_test.cpp
  )
  target_link_libraries(root_signature_test PRIVATE gta6rt_root_signature)
  target_compile_definitions(root_signature_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
  target_compile_options(root_signature_test PRIVATE /W4 /permissive- /utf-8)
  target_link_options(root_signature_test PRIVATE /OPT:NOREF)

  # XTaskQueue/XAsync from microsoft/libHttpClient (MIT), vendored in third_party/xtaskqueue; shims/xtaskqueue/pch.h
  # stands in for libHttpClient's pch.h so only the task queue builds. Used by the xgameruntime shim.
  set(XTASKQUEUE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../third_party/xtaskqueue)
  add_library(gta6rt_xtaskqueue STATIC
    ${XTASKQUEUE_DIR}/Source/Task/TaskQueue.cpp
    ${XTASKQUEUE_DIR}/Source/Task/AsyncLib.cpp
    ${XTASKQUEUE_DIR}/Source/Task/ThreadPool_win32.cpp
    ${XTASKQUEUE_DIR}/Source/Task/WaitTimer_win32.cpp
  )
  target_include_directories(gta6rt_xtaskqueue
    PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/shims/xtaskqueue ${XTASKQUEUE_DIR}/Source/Task ${XTASKQUEUE_DIR}/Source/Common
    PUBLIC ${XTASKQUEUE_DIR}/Include)
  target_compile_definitions(gta6rt_xtaskqueue PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
  target_compile_options(gta6rt_xtaskqueue PRIVATE /utf-8)  # third-party code: its own warning level
  target_link_libraries(gta6rt_xtaskqueue PUBLIC ole32)

  # Reserves one loader-assigned module TLS index for mapped images. Loaded from the executable's directory by
  # Loader::initialize_tls; its trivial DllMain only keeps the TLS anchor in the image. Built in the same directory as
  # the executables that map images, because the loader looks for it next to the running EXE.
  add_library(gta6rt_tls_host SHARED src/tls_host.cpp)
  target_compile_options(gta6rt_tls_host PRIVATE /W4 /permissive- /utf-8 /GS-)
  target_compile_definitions(gta6rt_tls_host PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
  set_target_properties(gta6rt_tls_host PROPERTIES
    OUTPUT_NAME gta6rt_tls_host
    PREFIX ""
    MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
  )

  add_executable(gta6rt
    src/main.cpp
    src/pe_loader.cpp
    shims/registry.cpp
    shims/file_system.cpp
    shims/xmem.cpp
    shims/d3d12_xs.cpp
    shims/d3d12_xs_device.cpp
    shims/xgameruntime.cpp
    shims/library_loader.cpp
    shims/process.cpp
  )
  add_dependencies(gta6rt gta6rt_tls_host)
  target_compile_definitions(gta6rt PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE)
  target_compile_options(gta6rt PRIVATE /W4 /permissive- /utf-8)
  # Static shim tables register themselves at startup; keep the linker from dropping their objects.
  # GTA6RT_HOST_BASE keeps the loader's own image off the default x64 EXE base (0x140000000), where a mapped EXE
  # without relocations wants to go. Windows moves the EXE with ASLR anyway; Wine loads it at its preferred base.
  set(GTA6RT_HOST_BASE /BASE:0x10000000000)
  target_link_options(gta6rt PRIVATE /OPT:NOREF ${GTA6RT_HOST_BASE})
  target_link_libraries(gta6rt PRIVATE gta6rt_d3d12_xs_rootsig gta6rt_xtaskqueue psapi)

  # Root-signature forwarding test: needs a real PC d3d12.lib to serialize the synthetic descriptors.
  # find_library searches %LIB% (a Developer Prompt), not the Windows SDK the Visual Studio generator links
  # against, so also look in that SDK's um/x64 folder.
  set(_sdk_lib_hints)
  if(DEFINED ENV{WindowsSdkDir} AND DEFINED ENV{WindowsSDKLibVersion})
    list(APPEND _sdk_lib_hints "$ENV{WindowsSdkDir}Lib/$ENV{WindowsSDKLibVersion}um/x64")
  endif()
  get_filename_component(_kits_root
    "[HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows Kits\\Installed Roots;KitsRoot10]" ABSOLUTE)
  if(CMAKE_VS_WINDOWS_TARGET_PLATFORM_VERSION AND EXISTS "${_kits_root}")
    list(APPEND _sdk_lib_hints "${_kits_root}/Lib/${CMAKE_VS_WINDOWS_TARGET_PLATFORM_VERSION}/um/x64")
  endif()
  find_library(D3D12_LIBRARY NAMES d3d12 HINTS ${_sdk_lib_hints})
  find_library(DXGI_LIBRARY NAMES dxgi HINTS ${_sdk_lib_hints})
  if(D3D12_LIBRARY)
    add_executable(root_sig_test
      test/root_sig_test.cpp
      shims/registry.cpp
      shims/d3d12_xs.cpp
      shims/d3d12_xs_device.cpp
    )
    target_link_libraries(root_sig_test PRIVATE gta6rt_d3d12_xs_rootsig ${D3D12_LIBRARY})
    target_compile_definitions(root_sig_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(root_sig_test PRIVATE /W4 /permissive- /utf-8)
    target_link_options(root_sig_test PRIVATE /OPT:NOREF)

    # RTS0 parser oracle: synthetic descriptors only, serialized by the real PC
    # d3d12.dll and parsed back by rt::rootsig::parse_rts0. A layout mismatch
    # fails here with the blob bytes, never silently in a draw.
    add_executable(root_signature_oracle_test
      test/root_signature_oracle_test.cpp
    )
    target_link_libraries(root_signature_oracle_test PRIVATE gta6rt_root_signature ${D3D12_LIBRARY})
    target_compile_definitions(root_signature_oracle_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(root_signature_oracle_test PRIVATE /W4 /permissive- /utf-8)
    target_link_options(root_signature_oracle_test PRIVATE /OPT:NOREF)

    # Xbox-ABI device proxy test: WARP PC device, no game executable. Needs DXGI for the WARP adapter.
    if(DXGI_LIBRARY)
      add_executable(device_proxy_test
        test/device_proxy_test.cpp
        shims/registry.cpp
        shims/d3d12_xs.cpp
        shims/d3d12_xs_device.cpp
      )
      target_link_libraries(device_proxy_test PRIVATE gta6rt_d3d12_xs_rootsig ${D3D12_LIBRARY} ${DXGI_LIBRARY})
      target_compile_definitions(device_proxy_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
      target_compile_options(device_proxy_test PRIVATE /W4 /permissive- /utf-8)
      target_link_options(device_proxy_test PRIVATE /OPT:NOREF)
    else()
      message(STATUS "dxgi.lib not found: device_proxy_test is not built")
    endif()
    # The queue and fence proxies over synthetic COM objects: no D3D12 runtime, no GPU. Replaces operator new and
    # patches its own import table to inject failures, so the proxy source is compiled into it alone.
    add_executable(xs_proxy_mock_test
      test/xs_proxy_mock_test.cpp
      shims/d3d12_xs_device.cpp
    )
    target_compile_definitions(xs_proxy_mock_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(xs_proxy_mock_test PRIVATE /W4 /permissive- /utf-8 /EHsc)
  else()
    message(STATUS "d3d12.lib not found: root_sig_test and device_proxy_test are not built")
  endif()

  # xmem shim ABI test: resolves the registered xmem.dll exports itself, so xmem.cpp and registry.cpp are compiled
  # into this executable instead of reusing gta6rt's objects (which would duplicate their symbols).
  add_executable(xmem_test
    test/xmem_test.cpp
    shims/xmem.cpp
    shims/registry.cpp
  )
  target_include_directories(xmem_test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/shims)
  target_compile_definitions(xmem_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
  target_compile_options(xmem_test PRIVATE /W4 /permissive- /utf-8)
  target_link_options(xmem_test PRIVATE /OPT:NOREF)
  target_link_libraries(xmem_test PRIVATE psapi)

  # The process shim's command line: GetCommandLineW must parse (CommandLineToArgvW) back to its __wargv.
  add_executable(process_cmdline_test
    test/process_cmdline_test.cpp
    shims/process.cpp
    shims/registry.cpp
  )
  target_include_directories(process_cmdline_test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/shims)
  target_compile_definitions(process_cmdline_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
  target_compile_options(process_cmdline_test PRIVATE /W4 /permissive- /utf-8)
  target_link_options(process_cmdline_test PRIVATE /OPT:NOREF)
  target_link_libraries(process_cmdline_test PRIVATE shell32)

  # Console paths (GAME:) and the write overlay: the file shims resolved through the registry, over temporary folders.
  add_executable(file_system_test
    test/file_system_test.cpp
    shims/file_system.cpp
    shims/registry.cpp
  )
  target_include_directories(file_system_test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/shims)
  target_compile_definitions(file_system_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
  target_compile_options(file_system_test PRIVATE /W4 /permissive- /utf-8)
  target_link_options(file_system_test PRIVATE /OPT:NOREF)

  # Offline GDK runtime shim (XGameRuntime, XSystem, XTaskQueue, XAsync), driven through the shim registry the way
  # a title calls it at boot. Like xmem_test it compiles its own registry.cpp.
  add_executable(xgameruntime_test
    test/xgameruntime_test.cpp
    shims/xgameruntime.cpp
    shims/library_loader.cpp
    shims/registry.cpp
  )
  target_include_directories(xgameruntime_test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/shims)
  target_compile_definitions(xgameruntime_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
  target_compile_options(xgameruntime_test PRIVATE /W4 /permissive- /utf-8)
  target_link_options(xgameruntime_test PRIVATE /OPT:NOREF)
  target_link_libraries(xgameruntime_test PRIVATE gta6rt_xtaskqueue)

  # GameInput shim on SDL3 (zlib licence): any controller SDL supports, DualSense trigger effects and lightbar.
  # Nothing is downloaded unless asked. The shim is built when SDL3 is available:
  #   - an installed SDL3 3.2+ that find_package finds (SDL3_DIR / CMAKE_PREFIX_PATH), or
  #   - -DLEONIDA_SDL3_FETCH=ON: download the pinned release below once at configure time (hash-checked) and build it
  #     static, or -DFETCHCONTENT_SOURCE_DIR_SDL3=<extracted SDL3-3.4.20> to build that copy without downloading.
  # Otherwise, or with -DLEONIDA_SDL3=OFF, gta6rt is built without the shim and the loader binds GameInputCreate to the
  # host's GameInput.dll or the unimplemented thunk as before. docs/gameinput.md has the details.
  option(LEONIDA_SDL3 "Build the GameInput shim when SDL3 is available" ON)
  option(LEONIDA_SDL3_FETCH "Download the pinned SDL3 release if no installed SDL3 is found" OFF)
  if(LEONIDA_SDL3)
    find_package(SDL3 3.2 CONFIG QUIET COMPONENTS SDL3-static NO_SYSTEM_ENVIRONMENT_PATH)
    if(NOT TARGET SDL3::SDL3-static AND (LEONIDA_SDL3_FETCH OR FETCHCONTENT_SOURCE_DIR_SDL3))
      include(FetchContent)
      FetchContent_Declare(SDL3
        URL https://github.com/libsdl-org/SDL/releases/download/release-3.4.20/SDL3-3.4.20.tar.gz
        URL_HASH SHA256=f3bb06f55bed9557227a700af2ea62e49497ebdf58037732a731a5fb3ef8581f
        DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
      # Only what gamepads need: the joystick, HIDAPI, haptic, sensor and power code, in a static library.
      set(SDL_SHARED OFF CACHE BOOL "" FORCE)
      set(SDL_STATIC ON CACHE BOOL "" FORCE)
      set(SDL_TEST_LIBRARY OFF CACHE BOOL "" FORCE)
      set(SDL_TESTS OFF CACHE BOOL "" FORCE)
      set(SDL_EXAMPLES OFF CACHE BOOL "" FORCE)
      set(SDL_INSTALL OFF CACHE BOOL "" FORCE)
      set(SDL_DISABLE_INSTALL ON CACHE BOOL "" FORCE)
      foreach(_sdl_off SDL_AUDIO SDL_VIDEO SDL_GPU SDL_RENDER SDL_CAMERA SDL_DIALOG SDL_TRAY)
        set(${_sdl_off} OFF CACHE BOOL "" FORCE)
      endforeach()
      FetchContent_MakeAvailable(SDL3)
    endif()
  endif()
  if(LEONIDA_SDL3 AND TARGET SDL3::SDL3-static)
    target_sources(gta6rt PRIVATE shims/gameinput.cpp)
    target_link_libraries(gta6rt PRIVATE SDL3::SDL3-static)

    # GameInput through the shim registry on SDL virtual joysticks: no controller, no game data.
    add_executable(gameinput_test test/gameinput_test.cpp shims/gameinput.cpp shims/registry.cpp)
    # gameinput_probe: the same shim on a real controller, run by hand (not a CTest).
    add_executable(gameinput_probe test/gameinput_probe.cpp shims/gameinput.cpp shims/registry.cpp)
    foreach(_gi gameinput_test gameinput_probe)
      target_include_directories(${_gi} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/shims)
      target_compile_definitions(${_gi} PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
      target_compile_options(${_gi} PRIVATE /W4 /permissive- /utf-8)
      target_link_options(${_gi} PRIVATE /OPT:NOREF)
      target_link_libraries(${_gi} PRIVATE SDL3::SDL3-static)
    endforeach()
  else()
    if(LEONIDA_SDL3)
      message(STATUS "GameInput shim not built: no installed SDL3 found. Set SDL3_DIR, or pass -DLEONIDA_SDL3_FETCH=ON "
                     "to download the pinned SDL3 release (see docs/gameinput.md)")
    else()
      message(STATUS "GameInput shim not built (LEONIDA_SDL3=OFF)")
    endif()
  endif()

else()
  message(STATUS "gta6rt requires Windows; only Vulkan tools are available on Linux")
endif()

include(CTest)
if(BUILD_TESTING)
  add_executable(retire_tickets_test test/retire_tickets_test.cpp)
  add_test(NAME retire_tickets COMMAND retire_tickets_test)

  add_executable(pipeline_cache_file_test test/pipeline_cache_file_test.cpp)
  add_test(NAME pipeline_cache_file COMMAND pipeline_cache_file_test)
  # XDXR build descriptions (docs/xdxr.md); compared with d3d12.h where the Windows SDK provides it.
  add_executable(xdxr_descs_test test/xdxr_descs_test.cpp)
  add_test(NAME xdxr_descs COMMAND xdxr_descs_test)
  # LEONIDA_VK_DEVICE name matching (runtime/src/vulkan_device_filter.h): pure stdlib, no Vulkan instance.
  add_executable(vulkan_device_filter_test test/vulkan_device_filter_test.cpp)
  target_include_directories(vulkan_device_filter_test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
  add_test(NAME vulkan_device_filter COMMAND vulkan_device_filter_test)
  if(WIN32)
    # Compile-time ABI checks (D3D12/XMem structure sizes, alignments and member offsets against the Windows SDK).
    # The file existed but no target built it, so none of its static_asserts ever ran.
    add_executable(runtime_types_test test/test_runtime_types.cpp)
    target_include_directories(runtime_types_test PRIVATE
      ${CMAKE_CURRENT_SOURCE_DIR}/src ${CMAKE_CURRENT_SOURCE_DIR}/shims)
    add_test(NAME runtime_types COMMAND runtime_types_test)
  endif()
  if(WIN32)
    # Loader tests. They build their own PE images, so they need neither the game nor any other input.
    add_executable(loader_test
      test/loader_test.cpp
      src/pe_loader.cpp
      shims/registry.cpp
    )
    target_compile_definitions(loader_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE)
    target_compile_options(loader_test PRIVATE /W4 /permissive- /utf-8)
    target_link_options(loader_test PRIVATE /OPT:NOREF ${GTA6RT_HOST_BASE})
    add_dependencies(loader_test gta6rt_tls_host)
    add_test(NAME loader COMMAND loader_test)
    # PE loader tests use two locally built fixtures: a DLL with exports, imports and relocations, and an EXE that
    # imports it so Loader::load exercises recursive module loading. They contain no game data.
    set(TINY_FIXTURE_DIR "${CMAKE_CURRENT_BINARY_DIR}/fixtures/$<CONFIG>")

    add_library(tiny_dll SHARED test/fixtures/tiny_dll.cpp)
    target_compile_definitions(tiny_dll PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(tiny_dll PRIVATE /W4 /permissive- /utf-8 /GS-)
    # tiny_fault_caught faults inside its own __try. MSVC always catches that; clang-cl only with asynchronous
    # exceptions, otherwise the access violation escapes and the unwind-registration check fails.
    target_compile_options(tiny_dll PRIVATE $<$<CXX_COMPILER_ID:Clang>:/EHa>)
    set_target_properties(tiny_dll PROPERTIES
      OUTPUT_NAME tiny_dll
      PREFIX ""
      RUNTIME_OUTPUT_DIRECTORY "${TINY_FIXTURE_DIR}"
      ARCHIVE_OUTPUT_DIRECTORY "${TINY_FIXTURE_DIR}"
      MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
    )

    add_executable(tiny_exe test/fixtures/tiny_exe.cpp)
    target_link_libraries(tiny_exe PRIVATE tiny_dll)
    target_compile_definitions(tiny_exe PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(tiny_exe PRIVATE /W4 /permissive- /utf-8 /GS-)
    set_target_properties(tiny_exe PROPERTIES
      OUTPUT_NAME tiny_exe
      RUNTIME_OUTPUT_DIRECTORY "${TINY_FIXTURE_DIR}"
      MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
    )

    # pe_loader.cpp needs the shim registry for shims::find/make_unimplemented_thunk. The xmem and d3d12_xs tables
    # are linked too so the optional LEONIDA_GAME_DIR run classifies the shipped modules' imports exactly like
    # gta6rt does (no d3d12.lib is needed: d3d12_xs resolves its host entry points at run time).
    add_executable(pe_loader_test
      test/pe_loader_test.cpp
      src/pe_loader.cpp
      shims/registry.cpp
      shims/xmem.cpp
      shims/d3d12_xs.cpp
      shims/d3d12_xs_device.cpp
    )
    target_include_directories(pe_loader_test PRIVATE
      ${CMAKE_CURRENT_SOURCE_DIR}/src
      ${CMAKE_CURRENT_SOURCE_DIR}/shims
    )
    target_compile_definitions(pe_loader_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(pe_loader_test PRIVATE /W4 /permissive- /utf-8)
    target_link_options(pe_loader_test PRIVATE /OPT:NOREF)
    target_link_libraries(pe_loader_test PRIVATE gta6rt_d3d12_xs_rootsig psapi)
    add_dependencies(pe_loader_test tiny_dll tiny_exe gta6rt_tls_host)

    # `gta6rt --run` rehearsal: a stand-in game EXE (CRT startup, statics, heap, files, threads) that imports
    # xgameruntime.dll from a link-time stub; under --run the shim registry answers those imports instead.
    add_library(xgameruntime_stub SHARED test/fixtures/xgameruntime_stub.cpp test/fixtures/xgameruntime_stub.def)
    target_include_directories(xgameruntime_stub PUBLIC ${XTASKQUEUE_DIR}/Include)
    target_compile_definitions(xgameruntime_stub PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    set_target_properties(xgameruntime_stub PROPERTIES
      OUTPUT_NAME xgameruntime
      PREFIX ""
      RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/fixtures/game/$<CONFIG>"
      ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/fixtures/game/$<CONFIG>"
    )
    add_executable(tiny_async test/fixtures/tiny_async.cpp)
    target_link_libraries(tiny_async PRIVATE xgameruntime_stub)
    set_target_properties(tiny_async PROPERTIES
      RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/fixtures/game/$<CONFIG>"
    )
    # Reaches xgameruntime.dll through LoadLibraryW/GetProcAddress, as xgameruntime.lib's thunks do (no import).
    add_executable(tiny_gdk_load test/fixtures/tiny_gdk_load.cpp)
    target_compile_definitions(tiny_gdk_load PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(tiny_gdk_load PRIVATE /W4 /permissive- /utf-8)
    set_target_properties(tiny_gdk_load PROPERTIES
      RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/fixtures/game/$<CONFIG>"
    )
    add_executable(tiny_game test/fixtures/tiny_game.cpp)
    target_link_libraries(tiny_game PRIVATE xgameruntime_stub)
    target_compile_definitions(tiny_game PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX _CRT_SECURE_NO_WARNINGS)  # plain fopen, as games use
    target_compile_options(tiny_game PRIVATE /W4 /permissive- /utf-8 /EHsc)
    set_target_properties(tiny_game PROPERTIES
      RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/fixtures/game/$<CONFIG>"
    )
    add_executable(tiny_game_exit_code_test test/tiny_game_exit_code_test.cpp)
    target_compile_definitions(tiny_game_exit_code_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(tiny_game_exit_code_test PRIVATE /W4 /permissive- /utf-8)
    add_dependencies(tiny_game_exit_code_test gta6rt tiny_game)
    add_test(NAME pe_loader COMMAND pe_loader_test "$<TARGET_FILE:tiny_dll>" "$<TARGET_FILE:tiny_exe>")

    # Synthetic, hand-built images through Loader::load: range checks, wrapping RVAs, import-table termination and
    # base relocations (HIGHLOW included). No fixture DLL is needed; the images are written to a temporary folder.
    add_executable(test_pe_loader_bounds
      test/test_pe_loader_bounds.cpp
      src/pe_loader.cpp
      shims/registry.cpp
      shims/xmem.cpp
      shims/d3d12_xs.cpp
      shims/d3d12_xs_device.cpp
    )
    target_include_directories(test_pe_loader_bounds PRIVATE
      ${CMAKE_CURRENT_SOURCE_DIR}/src
      ${CMAKE_CURRENT_SOURCE_DIR}/shims
    )
    target_compile_definitions(test_pe_loader_bounds PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(test_pe_loader_bounds PRIVATE /W4 /permissive- /utf-8)
    target_link_options(test_pe_loader_bounds PRIVATE /OPT:NOREF ${GTA6RT_HOST_BASE})
    target_link_libraries(test_pe_loader_bounds PRIVATE gta6rt_d3d12_xs_rootsig psapi)
    add_dependencies(test_pe_loader_bounds gta6rt_tls_host)
    add_test(NAME pe_loader_bounds COMMAND test_pe_loader_bounds)

    # Static TLS fixture: a DLL with __declspec(thread) and a TLS callback. It is ordinary test code; no game data
    # is involved. loader_tls_test needs gta6rt_tls_host.dll next to its own EXE, because that is where
    # Loader::initialize_tls looks for the reserved module TLS index.
    add_library(tiny_tls SHARED test/fixtures/tiny_tls.cpp)
    target_compile_definitions(tiny_tls PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(tiny_tls PRIVATE /W4 /permissive- /utf-8 /GS-)
    set_target_properties(tiny_tls PROPERTIES
      OUTPUT_NAME tiny_tls
      PREFIX ""
      RUNTIME_OUTPUT_DIRECTORY "${TINY_FIXTURE_DIR}"
      ARCHIVE_OUTPUT_DIRECTORY "${TINY_FIXTURE_DIR}"
    )

    # pe_loader.cpp needs the shim registry for shims::find/make_unimplemented_thunk. The fixtures import only host
    # DLLs, so no shim tables are linked.
    add_executable(loader_tls_test
      test/loader_tls_test.cpp
      src/pe_loader.cpp
      shims/registry.cpp
    )
    target_include_directories(loader_tls_test PRIVATE
      ${CMAKE_CURRENT_SOURCE_DIR}/src
    )
    target_compile_definitions(loader_tls_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(loader_tls_test PRIVATE /W4 /permissive- /utf-8 /EHsc)
    add_dependencies(loader_tls_test tiny_tls gta6rt_tls_host)
    add_test(NAME loader_tls
      COMMAND loader_tls_test "$<TARGET_FILE:tiny_tls>"
    )

    # LEONIDA_UNIMPLEMENTED=log|fail|stop: builds its own image with two imports that bind to nothing, calls them in
    # process (log, fail) and in child processes (default/env parsing, stop). No game files, no GPU.
    add_executable(unimplemented_policy_test
      test/unimplemented_policy_test.cpp
      src/pe_loader.cpp
      shims/registry.cpp
    )
    # The register-preservation check needs a caller that sets rbx/rbp/rdi/rsi/r12-r15 and xmm6-15, which MSVC x64
    # cannot do inline: test/fixtures/abi_probe.asm (MASM). Without ml64 the rest of the test still runs.
    find_program(LEONIDA_ML64 ml64)  # CMake would otherwise pick the 32-bit ml with the Ninja generator
    if(LEONIDA_ML64)
      set(CMAKE_ASM_MASM_COMPILER "${LEONIDA_ML64}")
      enable_language(ASM_MASM)
      target_sources(unimplemented_policy_test PRIVATE test/fixtures/abi_probe.asm)
      target_compile_definitions(unimplemented_policy_test PRIVATE LEONIDA_HAVE_ABI_PROBE=1)
    else()
      message(STATUS "ml64 not found: unimplemented_policy_test skips the register-preservation check")
    endif()
    target_include_directories(unimplemented_policy_test PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
    target_compile_definitions(unimplemented_policy_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE _CRT_SECURE_NO_WARNINGS)
    target_compile_options(unimplemented_policy_test PRIVATE $<$<COMPILE_LANGUAGE:CXX>:/W4 /permissive- /utf-8 /EHsc>)
    target_link_options(unimplemented_policy_test PRIVATE /OPT:NOREF)
    add_dependencies(unimplemented_policy_test gta6rt_tls_host)
    add_test(NAME unimplemented_policy COMMAND unimplemented_policy_test)
  endif()

  if(TARGET device_proxy_test)
    add_test(NAME device_proxy COMMAND device_proxy_test)
    set_tests_properties(device_proxy PROPERTIES SKIP_RETURN_CODE 77)  # no WARP adapter (Wine)
  endif()
  if(TARGET xs_proxy_mock_test)
    add_test(NAME xs_proxy_mock COMMAND xs_proxy_mock_test)
    # The guard region's setup failing (one process each: the tables are built once per process).
    add_test(NAME xs_proxy_mock_no_reserve COMMAND xs_proxy_mock_test --fail-virtualalloc)
    add_test(NAME xs_proxy_mock_no_protect COMMAND xs_proxy_mock_test --fail-virtualprotect)
    add_test(NAME xs_proxy_mock_no_handler COMMAND xs_proxy_mock_test --fail-veh)
  endif()
  if(TARGET root_sig_test)
    add_test(NAME root_sig COMMAND root_sig_test)
  endif()
  add_executable(command_list_test test/command_list_test.cpp)
  add_test(NAME command_list COMMAND command_list_test)
  add_executable(resource_tiling_test test/resource_tiling_test.cpp)
  add_test(NAME resource_tiling COMMAND resource_tiling_test)
  add_executable(descriptor_table_test test/descriptor_table_test.cpp)
  add_test(NAME descriptor_table COMMAND descriptor_table_test)
  add_executable(shader_library_test test/shader_library_test.cpp)
  add_test(NAME shader_library COMMAND shader_library_test)
  if(WIN32)
    # Optional: skips unless LEONIDA_GAME_DIR and LEONIDA_MOVIE are set.
    add_executable(bink_decode_test
      test/bink_decode_test.cpp
      src/pe_loader.cpp
      shims/registry.cpp
      shims/xmem.cpp
    )
    target_compile_definitions(bink_decode_test PRIVATE
      WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE _CRT_SECURE_NO_WARNINGS)
    target_compile_options(bink_decode_test PRIVATE /W4 /permissive- /EHsc /utf-8)
    target_link_options(bink_decode_test PRIVATE /OPT:NOREF)
    target_link_libraries(bink_decode_test PRIVATE psapi)
    add_dependencies(bink_decode_test gta6rt_tls_host)
    add_test(NAME bink_decode COMMAND bink_decode_test)
    set_tests_properties(bink_decode PROPERTIES SKIP_RETURN_CODE 77)

    # Optional: skips unless LEONIDA_GAME_DIR is set. Drives the package's FSR DLL on the Xbox-ABI device proxy.
    if(D3D12_LIBRARY AND DXGI_LIBRARY)
      add_executable(fsr_dispatch_test
        test/fsr_dispatch_test.cpp
        src/pe_loader.cpp
        shims/registry.cpp
        shims/xmem.cpp
        shims/d3d12_xs.cpp
        shims/d3d12_xs_device.cpp
      )
      target_compile_definitions(fsr_dispatch_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX _CRT_SECURE_NO_WARNINGS)
      target_compile_options(fsr_dispatch_test PRIVATE /W4 /permissive- /EHsc /utf-8)
      target_link_options(fsr_dispatch_test PRIVATE /OPT:NOREF)
      target_link_libraries(fsr_dispatch_test PRIVATE gta6rt_d3d12_xs_rootsig ${D3D12_LIBRARY} ${DXGI_LIBRARY} psapi)
      add_test(NAME fsr_dispatch COMMAND fsr_dispatch_test)
      set_tests_properties(fsr_dispatch PROPERTIES SKIP_RETURN_CODE 77)

      # Optional: skips unless LEONIDA_GAME_DIR is set. Constructs the package's XboxBvhBuilder on the device proxy.
      add_executable(bvh_builder_test
        test/bvh_builder_test.cpp
        src/pe_loader.cpp
        shims/registry.cpp
        shims/xmem.cpp
        shims/d3d12_xs.cpp
        shims/d3d12_xs_device.cpp
      )
      target_compile_definitions(bvh_builder_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX _CRT_SECURE_NO_WARNINGS)
      target_compile_options(bvh_builder_test PRIVATE /W4 /permissive- /EHsc /utf-8)
      target_link_options(bvh_builder_test PRIVATE /OPT:NOREF)
      target_link_libraries(bvh_builder_test PRIVATE gta6rt_d3d12_xs_rootsig ${D3D12_LIBRARY} ${DXGI_LIBRARY} psapi)
      add_test(NAME bvh_builder COMMAND bvh_builder_test)
      set_tests_properties(bvh_builder PROPERTIES SKIP_RETURN_CODE 77)
    endif()

    # Optional: skips unless LEONIDA_GAME_DIR and LEONIDA_MOVIE_AUDIO are set.
    add_executable(bink_audio_test
      test/bink_audio_test.cpp
      src/pe_loader.cpp
      shims/registry.cpp
      shims/xmem.cpp
    )
    target_compile_definitions(bink_audio_test PRIVATE
      WIN32_LEAN_AND_MEAN NOMINMAX UNICODE _UNICODE _CRT_SECURE_NO_WARNINGS)
    target_compile_options(bink_audio_test PRIVATE /W4 /permissive- /EHsc /utf-8)
    target_link_options(bink_audio_test PRIVATE /OPT:NOREF)
    target_link_libraries(bink_audio_test PRIVATE psapi)
    add_dependencies(bink_audio_test gta6rt_tls_host)
    add_test(NAME bink_audio COMMAND bink_audio_test)
    set_tests_properties(bink_audio PROPERTIES SKIP_RETURN_CODE 77)
  endif()
  if(WIN32 AND TARGET xmem_test)
    add_test(NAME xmem COMMAND xmem_test)
  endif()
  if(WIN32 AND TARGET process_cmdline_test)
    add_test(NAME process_cmdline COMMAND process_cmdline_test)
  endif()
  if(WIN32 AND TARGET file_system_test)
    add_test(NAME file_system COMMAND file_system_test)
  endif()
  if(WIN32 AND TARGET xgameruntime_test)
    add_test(NAME xgameruntime COMMAND xgameruntime_test)
  endif()
  if(WIN32 AND TARGET gameinput_test)
    add_test(NAME gameinput COMMAND gameinput_test)
  elseif(WIN32)
    # Built without SDL3: listed as "Not Run (Disabled)" so it is visible that the shim was not tested.
    add_test(NAME gameinput COMMAND ${CMAKE_COMMAND} -E echo "built without SDL3")
    set_tests_properties(gameinput PROPERTIES DISABLED TRUE)
  endif()
  if(WIN32 AND TARGET gta6rt AND TARGET tiny_game)
    add_test(NAME run_tiny_async COMMAND ${CMAKE_COMMAND}
      "-DLOADER=$<TARGET_FILE:gta6rt>" "-DGUEST=$<TARGET_FILE:tiny_async>"
      "-DRUNNER=${CMAKE_CROSSCOMPILING_EMULATOR}" -P "${CMAKE_CURRENT_SOURCE_DIR}/test/check_loaded_async.cmake")
    add_test(NAME run_tiny_gdk_load COMMAND ${CMAKE_COMMAND}
      "-DLOADER=$<TARGET_FILE:gta6rt>" "-DGUEST=$<TARGET_FILE:tiny_gdk_load>"
      "-DRUNNER=${CMAKE_CROSSCOMPILING_EMULATOR}" -P "${CMAKE_CURRENT_SOURCE_DIR}/test/check_loaded_gdk.cmake")
    add_test(NAME run_tiny_game COMMAND gta6rt --run $<TARGET_FILE:tiny_game> -- hello 42)
    add_test(NAME run_tiny_game_exit_code COMMAND tiny_game_exit_code_test
             "$<TARGET_FILE:gta6rt>" "$<TARGET_FILE:tiny_game>")
    # Two modes at once are refused before anything is mapped (#886). The stand-in game's own arguments are passed,
    # so a gta6rt that ignored the conflict would run it, print "running" and exit 0.
    foreach(modes "dry-run;run" "dry-run;init" "init;run")
      string(REPLACE ";" "_" name "${modes}")
      list(TRANSFORM modes PREPEND "--")
      add_test(NAME gta6rt_rejects_${name} COMMAND gta6rt ${modes} $<TARGET_FILE:tiny_game> -- hello 42)
      set_tests_properties(gta6rt_rejects_${name} PROPERTIES
        PASS_REGULAR_EXPRESSION "usage: gta6rt"
        FAIL_REGULAR_EXPRESSION "running |module\\(s\\) mapped")
    endforeach()
    # The opt-in checks, one test each: thread_local on the game's threads, a C++ exception caught inside the image.
    add_test(NAME run_tiny_game_tls COMMAND gta6rt --run $<TARGET_FILE:tiny_game> -- hello 42 tls)
    add_test(NAME run_tiny_game_eh COMMAND gta6rt --run $<TARGET_FILE:tiny_game> -- hello 42 eh)
  endif()
  if(WIN32 AND TARGET root_signature_test)
    add_test(NAME root_signature COMMAND root_signature_test)
  endif()
  if(WIN32 AND TARGET root_signature_oracle_test)
    add_test(NAME root_signature_oracle COMMAND root_signature_oracle_test)
  endif()
endif()

if(WIN32)
option(LEONIDA_D3D12_SHIM_TESTS "Build the standalone D3D12 root-signature shim test" OFF)
if(LEONIDA_D3D12_SHIM_TESTS)
  enable_testing()
  add_executable(d3d12_xs_root_signature_test
    test/d3d12_xs_root_signature_test.cpp
    shims/d3d12_xs.cpp
    shims/registry.cpp
    shims/d3d12_xs_device.cpp
  )
  target_include_directories(d3d12_xs_root_signature_test PRIVATE shims)
  target_compile_definitions(d3d12_xs_root_signature_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
  target_compile_options(d3d12_xs_root_signature_test PRIVATE /W4 /permissive- /EHsc /utf-8)
  target_link_options(d3d12_xs_root_signature_test PRIVATE /OPT:NOREF)
  target_link_libraries(d3d12_xs_root_signature_test PRIVATE gta6rt_d3d12_xs_rootsig d3d12 dxgi)
  add_test(NAME d3d12_xs_root_signature COMMAND d3d12_xs_root_signature_test)
  set_tests_properties(d3d12_xs_root_signature PROPERTIES SKIP_RETURN_CODE 77)  # no WARP adapter (Wine)
endif()
endif()

# GPU test and pipeline tools load Vulkan at run time, so only the Vulkan headers are needed at build time.
# Nothing is downloaded unless asked: set VULKAN_SDK, install the Vulkan headers, pass
# -DLEONIDA_VULKAN_HEADERS_FETCH=ON to download the pinned Khronos Vulkan-Headers release once at configure time
# (hash-checked), or pass -DFETCHCONTENT_SOURCE_DIR_VULKANHEADERS=<path> to use a local copy without downloading.
option(LEONIDA_VULKAN_HEADERS_FETCH "Download the pinned Khronos Vulkan-Headers release if no Vulkan headers are found" OFF)
find_package(Vulkan QUIET)
if(NOT TARGET Vulkan::Headers)
  find_path(Vulkan_INCLUDE_DIR
    NAMES vulkan/vulkan.h
    HINTS "$ENV{VULKAN_SDK}/Include" "$ENV{VULKAN_SDK}/include"
  )
  if(Vulkan_INCLUDE_DIR AND EXISTS "${Vulkan_INCLUDE_DIR}/vulkan/vulkan.h")
    add_library(Vulkan::Headers INTERFACE IMPORTED)
    set_target_properties(Vulkan::Headers PROPERTIES
      INTERFACE_INCLUDE_DIRECTORIES "${Vulkan_INCLUDE_DIR}"
    )
  elseif(LEONIDA_VULKAN_HEADERS_FETCH OR FETCHCONTENT_SOURCE_DIR_VULKANHEADERS)
    include(FetchContent)
    FetchContent_Declare(VulkanHeaders
      # vulkan-sdk-1.4.363.0 (same commit pinned in .github/workflows/runtime.yml)
      URL https://github.com/KhronosGroup/Vulkan-Headers/archive/6802bb4733b63ed5efd3adb308a6c885ef180ea1.tar.gz
      URL_HASH SHA256=9044fbb356160e43201dc2094acb44c8a906722a63a35f48fc0f051579db9bb6
      DOWNLOAD_EXTRACT_TIMESTAMP TRUE)
    set(VULKAN_HEADERS_ENABLE_TESTS OFF CACHE BOOL "" FORCE)
    set(VULKAN_HEADERS_ENABLE_INSTALL OFF CACHE BOOL "" FORCE)
    set(VULKAN_HEADERS_ENABLE_MODULE OFF CACHE BOOL "" FORCE)
    FetchContent_MakeAvailable(VulkanHeaders)
  endif()
endif()
if(TARGET Vulkan::Headers)
  foreach(tool vk_harness vk_texture_test vk_sampler_test vk_compute_test vk_store_test vk_pipeline_check vk_cache_merge vk_command_replay_test
          vk_bindless_test vk_image_fuzz vk_descriptor_bench depth24_convert_test vk_predicate_capture_test vk_predicated_copy_test
          vk_xdxr_build_test)
    add_executable(${tool} test/${tool}.cpp)
    target_link_libraries(${tool} PRIVATE Vulkan::Headers ${CMAKE_DL_LIBS})
    if(WIN32)
      target_compile_definitions(${tool} PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    endif()
  endforeach()
  if(BUILD_TESTING)
    add_test(NAME depth24_convert COMMAND depth24_convert_test)
    set_tests_properties(depth24_convert PROPERTIES SKIP_RETURN_CODE 77)
    # The XDXR BVH builder: the GPU writes every case, then tools/xdxr_bvh.py validates and traces them.
    set(LEONIDA_XDXR_OUT "${CMAKE_CURRENT_BINARY_DIR}/xdxr_build_cases")
    file(MAKE_DIRECTORY "${LEONIDA_XDXR_OUT}")
    add_test(NAME xdxr_build COMMAND vk_xdxr_build_test "${LEONIDA_XDXR_OUT}")
    set_tests_properties(xdxr_build PROPERTIES SKIP_RETURN_CODE 77 FIXTURES_SETUP xdxr_build_cases)
    find_package(Python3 COMPONENTS Interpreter QUIET)
    if(Python3_FOUND)
      add_test(NAME xdxr_build_check COMMAND "${Python3_EXECUTABLE}" -I
               "${CMAKE_CURRENT_SOURCE_DIR}/../tools/xdxr_bvh.py" check-build "${LEONIDA_XDXR_OUT}" --rays 1000)
      set_tests_properties(xdxr_build_check PROPERTIES FIXTURES_REQUIRED xdxr_build_cases SKIP_RETURN_CODE 77)
    endif()
  endif()
  # VulkanQueue/VulkanContext query-state synchronization (remove_query_pool vs commit, cross-queue plan/commit)
  # and pending Wait() preservation across transient vkQueueSubmit failures.
  add_executable(vk_queue_query_sync_test test/vk_queue_query_sync_test.cpp)
  target_link_libraries(vk_queue_query_sync_test PRIVATE Vulkan::Headers ${CMAKE_DL_LIBS})
  if(WIN32)
    target_compile_definitions(vk_queue_query_sync_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
  else()
    find_package(Threads REQUIRED)
    target_link_libraries(vk_queue_query_sync_test PRIVATE Threads::Threads)
  endif()
  if(MSVC)
    target_compile_options(vk_queue_query_sync_test PRIVATE /W4 /permissive- /utf-8)
  endif()
  if(BUILD_TESTING)
    add_test(NAME vk_queue_query_sync COMMAND vk_queue_query_sync_test)
    set_tests_properties(vk_queue_query_sync PROPERTIES SKIP_RETURN_CODE 77)
  endif()
  # With LEONIDA_VK_VALIDATION=1: synchronization validation reports a missing barrier (and not a present one), so
  # the other GPU tests' "0 validation errors" was checked by an active layer. 77 without validation or a device.
  add_executable(vk_sync_validation_selftest test/vk_sync_validation_selftest.cpp)
  target_link_libraries(vk_sync_validation_selftest PRIVATE Vulkan::Headers ${CMAKE_DL_LIBS})
  if(WIN32)
    target_compile_definitions(vk_sync_validation_selftest PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
  endif()
  if(MSVC)
    target_compile_options(vk_sync_validation_selftest PRIVATE /W4 /permissive- /utf-8)
  endif()
  if(BUILD_TESTING)
    add_test(NAME vk_sync_validation COMMAND vk_sync_validation_selftest)
    set_tests_properties(vk_sync_validation PROPERTIES SKIP_RETURN_CODE 77)
  endif()
  if(NOT Vulkan_GLSLANG_VALIDATOR_EXECUTABLE)
    find_program(Vulkan_GLSLANG_VALIDATOR_EXECUTABLE glslangValidator HINTS "$ENV{VULKAN_SDK}/Bin" "$ENV{VULKAN_SDK}/bin")
  endif()
  if(Vulkan_GLSLANG_VALIDATOR_EXECUTABLE)
    add_executable(vk_overlapping_occlusion_test test/vk_overlapping_occlusion_test.cpp)
    target_link_libraries(vk_overlapping_occlusion_test PRIVATE Vulkan::Headers ${CMAKE_DL_LIBS})
    if(WIN32)
      target_compile_definitions(vk_overlapping_occlusion_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    endif()
    foreach(stage vert frag)
      set(source "${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/overlapping_occlusion.${stage}")
      set(output "${CMAKE_CURRENT_BINARY_DIR}/overlapping_occlusion.${stage}.spv")
      add_custom_command(OUTPUT "${output}"
                         COMMAND "${Vulkan_GLSLANG_VALIDATOR_EXECUTABLE}" -V --target-env vulkan1.0
                                 -o "${output}" "${source}"
                         DEPENDS "${source}" VERBATIM)
      target_sources(vk_overlapping_occlusion_test PRIVATE "${output}")
    endforeach()
    if(BUILD_TESTING)
      add_test(NAME vk_overlapping_occlusion COMMAND vk_overlapping_occlusion_test
               "${CMAKE_CURRENT_BINARY_DIR}/overlapping_occlusion.vert.spv"
               "${CMAKE_CURRENT_BINARY_DIR}/overlapping_occlusion.frag.spv")
      set_tests_properties(vk_overlapping_occlusion PROPERTIES SKIP_RETURN_CODE 77)
    endif()
  endif()
  # CPU-only: the ranking that makes these harnesses prefer a native ICD over Mesa's Dozen (#915). No GPU needed.
  add_executable(vk_device_pick_test test/vk_device_pick_test.cpp)
  target_link_libraries(vk_device_pick_test PRIVATE Vulkan::Headers)
  if(BUILD_TESTING)
    add_test(NAME vk_device_pick COMMAND vk_device_pick_test)
  endif()
  # CPU-only: the bindless set's array sizes from synthetic device limits.
  add_executable(bindless_sizing_test test/bindless_sizing_test.cpp)
  target_link_libraries(bindless_sizing_test PRIVATE Vulkan::Headers)
  if(BUILD_TESTING)
    add_test(NAME bindless_sizing COMMAND bindless_sizing_test)
  endif()
  if(WIN32 AND BUILD_TESTING)
    # VulkanPresenter tracker lock / failed-submit rollback and capture-buffer WAW synchronization.
    add_executable(vk_presenter_sync_test test/vk_presenter_sync_test.cpp)
    target_link_libraries(vk_presenter_sync_test PRIVATE Vulkan::Headers user32)
    target_compile_definitions(vk_presenter_sync_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    if(MSVC)
      target_compile_options(vk_presenter_sync_test PRIVATE /W4 /permissive- /utf-8)
    endif()
    add_test(NAME vk_presenter_sync COMMAND vk_presenter_sync_test)
    set_tests_properties(vk_presenter_sync PROPERTIES SKIP_RETURN_CODE 77)
  endif()
  if(NOT Vulkan_GLSLANG_VALIDATOR_EXECUTABLE)
    find_program(Vulkan_GLSLANG_VALIDATOR_EXECUTABLE glslangValidator HINTS "$ENV{VULKAN_SDK}/Bin" "$ENV{VULKAN_SDK}/bin")
  endif()
  if(Vulkan_GLSLANG_VALIDATOR_EXECUTABLE)
    add_executable(vk_binary_occlusion_test test/vk_binary_occlusion_test.cpp)
    target_link_libraries(vk_binary_occlusion_test PRIVATE Vulkan::Headers ${CMAKE_DL_LIBS})
    if(WIN32)
      target_compile_definitions(vk_binary_occlusion_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    endif()
    foreach(stage vert frag)
      set(source "${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/binary_occlusion.${stage}")
      set(output "${CMAKE_CURRENT_BINARY_DIR}/binary_occlusion.${stage}.spv")
      add_custom_command(OUTPUT "${output}"
                         COMMAND "${Vulkan_GLSLANG_VALIDATOR_EXECUTABLE}" -V --target-env vulkan1.0
                                 -o "${output}" "${source}"
                         DEPENDS "${source}" VERBATIM)
      target_sources(vk_binary_occlusion_test PRIVATE "${output}")
    endforeach()
    if(BUILD_TESTING)
      add_test(NAME vk_binary_occlusion COMMAND vk_binary_occlusion_test
               "${CMAKE_CURRENT_BINARY_DIR}/binary_occlusion.vert.spv"
               "${CMAKE_CURRENT_BINARY_DIR}/binary_occlusion.frag.spv")
      set_tests_properties(vk_binary_occlusion PROPERTIES SKIP_RETURN_CODE 77)
    endif()
  endif()
  # D3D12 COM objects over the Vulkan backend. Their method stubs are generated from the SDK's d3d12.h at build
  # time (tools/gen_d3d12_stubs.py), so nothing derived from the SDK header is checked in.
  find_package(Python3 COMPONENTS Interpreter QUIET)
  # The generator finds d3d12.h through INCLUDE, which a Developer Prompt sets. A cross build (for example
  # runtime/cmake/clang-cl-xwin.cmake) has no such prompt, so it is handed the header CMake finds.
  set(_stubs_header)
  set(_stubs_header_ok TRUE)
  if(WIN32 AND CMAKE_CROSSCOMPILING)
    find_file(LEONIDA_D3D12_HEADER d3d12.h)
    if(LEONIDA_D3D12_HEADER)
      set(_stubs_header --header "${LEONIDA_D3D12_HEADER}")
    else()
      set(_stubs_header_ok FALSE)
      message(STATUS "d3d12.h not found (set CMAKE_INCLUDE_PATH): the D3D12 COM objects are not built")
    endif()
  endif()
  if(WIN32 AND Python3_FOUND AND _stubs_header_ok)
    set(LEONIDA_STUBS ${CMAKE_CURRENT_BINARY_DIR}/generated/d3d12_stubs.h)
    add_custom_command(
      OUTPUT ${LEONIDA_STUBS}
      COMMAND "${Python3_EXECUTABLE}" -I ${CMAKE_CURRENT_SOURCE_DIR}/../tools/gen_d3d12_stubs.py --out ${LEONIDA_STUBS}
              ${_stubs_header}
              ID3D12GraphicsCommandList8 ID3D12CommandQueue ID3D12CommandAllocator ID3D12Fence1
              ID3D12Device11 ID3D12DescriptorHeap ID3D12Heap ID3D12Resource ID3D12QueryHeap ID3D12RootSignature
              ID3D12PipelineState ID3D12CommandSignature
      DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/../tools/gen_d3d12_stubs.py ${LEONIDA_D3D12_HEADER}
      COMMENT "Generating D3D12 COM stubs"
      VERBATIM)
    add_library(gta6rt_d3d12_com STATIC shims/d3d12_com.cpp shims/d3d12_vulkan_device.cpp shims/d3d12_xs_device.cpp shims/xbox_present.cpp
                shims/dxgi_com.cpp ${LEONIDA_STUBS})   # d3d12_vulkan_device.cpp reads the PC device behind the proxy (#224)
    target_include_directories(gta6rt_d3d12_com PRIVATE ${CMAKE_CURRENT_BINARY_DIR}/generated)
    target_link_libraries(gta6rt_d3d12_com PUBLIC Vulkan::Headers gta6rt_root_signature)
    target_compile_definitions(gta6rt_d3d12_com PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(gta6rt_d3d12_com PRIVATE /W4 /permissive- /utf-8)

    add_executable(d3d12_com_test test/d3d12_com_test.cpp)
    target_link_libraries(d3d12_com_test PRIVATE gta6rt_d3d12_com)
    target_compile_definitions(d3d12_com_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_com_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_com COMMAND d3d12_com_test)
    set_tests_properties(d3d12_com PROPERTIES SKIP_RETURN_CODE 77)

    # Multi-threaded concurrent submissions across two D3D12 queues + resource creation/destruction churn.
    add_executable(d3d12_multithread_test test/d3d12_multithread_test.cpp)
    target_link_libraries(d3d12_multithread_test PRIVATE gta6rt_d3d12_com)
    target_compile_definitions(d3d12_multithread_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_multithread_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_multithread COMMAND d3d12_multithread_test)
    set_tests_properties(d3d12_multithread PROPERTIES SKIP_RETURN_CODE 77 TIMEOUT 120)
    # A queued Wait must not block a Signal submitted by another D3D12 queue.
    add_executable(d3d12_queue_wait_test test/d3d12_queue_wait_test.cpp)
    target_link_libraries(d3d12_queue_wait_test PRIVATE gta6rt_d3d12_com)
    target_compile_definitions(d3d12_queue_wait_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_queue_wait_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_queue_wait COMMAND d3d12_queue_wait_test)
    set_tests_properties(d3d12_queue_wait PROPERTIES SKIP_RETURN_CODE 77 TIMEOUT 20)
    add_test(NAME d3d12_queue_wait_timeout COMMAND d3d12_queue_wait_test --timeout-control)
    set_tests_properties(d3d12_queue_wait_timeout PROPERTIES SKIP_RETURN_CODE 77 WILL_FAIL TRUE TIMEOUT 10)

    # XDXR acceleration structures through BuildRaytracingAccelerationStructure (docs/d3d12_vulkan.md 4.8): the
    # device builds them, tools/xdxr_bvh.py checks them.
    add_executable(d3d12_xdxr_rt_test test/d3d12_xdxr_rt_test.cpp)
    target_link_libraries(d3d12_xdxr_rt_test PRIVATE gta6rt_d3d12_com)
    target_compile_definitions(d3d12_xdxr_rt_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_xdxr_rt_test PRIVATE /W4 /permissive- /utf-8)
    set(LEONIDA_D3D12_XDXR_OUT "${CMAKE_CURRENT_BINARY_DIR}/d3d12_xdxr_cases")
    file(MAKE_DIRECTORY "${LEONIDA_D3D12_XDXR_OUT}")
    add_test(NAME d3d12_xdxr_rt COMMAND d3d12_xdxr_rt_test "${LEONIDA_D3D12_XDXR_OUT}")
    set_tests_properties(d3d12_xdxr_rt PROPERTIES SKIP_RETURN_CODE 77 TIMEOUT 60 FIXTURES_SETUP d3d12_xdxr_cases)
    find_package(Python3 COMPONENTS Interpreter QUIET)
    if(Python3_FOUND)
      add_test(NAME d3d12_xdxr_rt_check COMMAND "${Python3_EXECUTABLE}" -I
               "${CMAKE_CURRENT_SOURCE_DIR}/../tools/xdxr_bvh.py" check-build "${LEONIDA_D3D12_XDXR_OUT}" --rays 1000)
      set_tests_properties(d3d12_xdxr_rt_check PROPERTIES FIXTURES_REQUIRED d3d12_xdxr_cases SKIP_RETURN_CODE 77)
    endif()

    # GCL8's QueryInterface and its one method need an allocator and a list, not a Vulkan device.
    add_executable(d3d12_gcl8_test test/d3d12_gcl8_test.cpp)
    target_link_libraries(d3d12_gcl8_test PRIVATE gta6rt_d3d12_com)
    target_compile_definitions(d3d12_gcl8_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_gcl8_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_gcl8 COMMAND d3d12_gcl8_test)
    # Python runs on the host; a cross-compiled fixture runs through the emulator (Wine), which the script prepends.
    # The interpreter is named by path, because ctest would put the emulator in front of an executable target.
    set(_trace_runner)
    foreach(_part IN LISTS CMAKE_CROSSCOMPILING_EMULATOR)
      list(APPEND _trace_runner "--runner=${_part}")  # one word each, even one that starts with "-" (xvfb-run -a)
    endforeach()
    add_test(NAME d3d12_trace COMMAND "${Python3_EXECUTABLE}" -I
             "${CMAKE_CURRENT_SOURCE_DIR}/test/check_d3d12_trace.py" "$<TARGET_FILE:d3d12_gcl8_test>" ${_trace_runner})

    # The Vulkan device behind the Xbox-ABI proxy, end to end.
    add_executable(d3d12_vulkan_device_test test/d3d12_vulkan_device_test.cpp)
    target_link_libraries(d3d12_vulkan_device_test PRIVATE gta6rt_d3d12_com dxguid)  # IID_ID3D12MetaCommand (#1440)
    target_compile_definitions(d3d12_vulkan_device_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_vulkan_device_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_vulkan_device COMMAND d3d12_vulkan_device_test)
    set_tests_properties(d3d12_vulkan_device PROPERTIES SKIP_RETURN_CODE 77)

    # ClearUnorderedAccessViewUint/Float on buffers and textures, read back through D3D12.
    add_executable(d3d12_uav_clear_test test/d3d12_uav_clear_test.cpp)
    target_link_libraries(d3d12_uav_clear_test PRIVATE gta6rt_d3d12_com)
    target_compile_definitions(d3d12_uav_clear_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_uav_clear_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_uav_clear COMMAND d3d12_uav_clear_test)
    set_tests_properties(d3d12_uav_clear PROPERTIES SKIP_RETURN_CODE 77)

    # Emulation of R9G9B9E5_SHAREDEXP UAV textures as mutable R32_UINT on GPUs without E5B9G9R9 storage:
    # format support, 1D/2D/3D/reserved creation, SRV/UAV T#s, WARP differential UAV clears, cross-backing copies,
    # and set-0 storage write -> E5B9G9R9 sampled HDR round-trip.
    add_executable(d3d12_d24_support_test test/d3d12_d24_support_test.cpp)
    target_link_libraries(d3d12_d24_support_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_d24_support_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_d24_support_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_d24_support COMMAND d3d12_d24_support_test)
    set_tests_properties(d3d12_d24_support PROPERTIES SKIP_RETURN_CODE 77)
    add_executable(d3d12_r9g9b9e5_uav_test test/d3d12_r9g9b9e5_uav_test.cpp)
    target_link_libraries(d3d12_r9g9b9e5_uav_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_r9g9b9e5_uav_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_r9g9b9e5_uav_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_r9g9b9e5_uav COMMAND d3d12_r9g9b9e5_uav_test)
    set_tests_properties(d3d12_r9g9b9e5_uav PROPERTIES SKIP_RETURN_CODE 77)
    if(Vulkan_GLSLANG_VALIDATOR_EXECUTABLE)
      set(LEONIDA_TEST_CS_R9G9B9E5_UAV ${CMAKE_CURRENT_BINARY_DIR}/generated/cs_r9g9b9e5_uav.spv)
      add_custom_command(
        OUTPUT ${LEONIDA_TEST_CS_R9G9B9E5_UAV}
        COMMAND ${Vulkan_GLSLANG_VALIDATOR_EXECUTABLE} -V --target-env vulkan1.3 -o ${LEONIDA_TEST_CS_R9G9B9E5_UAV}
                ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/cs_r9g9b9e5_uav.comp
        DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/cs_r9g9b9e5_uav.comp
        COMMENT "Compiling cs_r9g9b9e5_uav.comp"
        VERBATIM)
      target_sources(d3d12_r9g9b9e5_uav_test PRIVATE ${LEONIDA_TEST_CS_R9G9B9E5_UAV})
      target_compile_definitions(d3d12_r9g9b9e5_uav_test PRIVATE
        LEONIDA_TEST_CS_R9G9B9E5_UAV_SPV="${LEONIDA_TEST_CS_R9G9B9E5_UAV}")
    endif()

    # Debug markers as VK_EXT_debug_utils labels, and private data on every object (D3D-24).
    add_executable(d3d12_debug_markers_test test/d3d12_debug_markers_test.cpp)
    target_link_libraries(d3d12_debug_markers_test PRIVATE gta6rt_d3d12_com)
    target_compile_definitions(d3d12_debug_markers_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_debug_markers_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_debug_markers COMMAND d3d12_debug_markers_test)
    set_tests_properties(d3d12_debug_markers PROPERTIES SKIP_RETURN_CODE 77)

    # Fences and timing (D3D-30): multiple-fence waits, clock calibration, adapter LUID, custom heap properties.
    add_executable(d3d12_fence_timing_test test/d3d12_fence_timing_test.cpp)
    target_link_libraries(d3d12_fence_timing_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_fence_timing_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_fence_timing_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_fence_timing COMMAND d3d12_fence_timing_test)
    set_tests_properties(d3d12_fence_timing PROPERTIES SKIP_RETURN_CODE 77)
    # Fence events the host runtime sets or refuses (released fences, no fences), compared with the host devices.
    add_executable(d3d12_fence_events_test test/d3d12_fence_events_test.cpp)
    target_link_libraries(d3d12_fence_events_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_fence_events_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_fence_events_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_fence_events COMMAND d3d12_fence_events_test)
    set_tests_properties(d3d12_fence_events PROPERTIES SKIP_RETURN_CODE 77)
    # Xbox-ABI queue and fence proxies behind the device proxy, on the host runtime (WARP, a hardware adapter) and
    # on the Vulkan device: forwarding, refusal past the public chain, fence unwrapping. 77: no device at all.
    add_executable(queue_proxy_test test/queue_proxy_test.cpp)
    target_link_libraries(queue_proxy_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(queue_proxy_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(queue_proxy_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME queue_proxy COMMAND queue_proxy_test)
    set_tests_properties(queue_proxy PROPERTIES SKIP_RETURN_CODE 77)
    # The countdown behind those event-backed waits, with no GPU: the full unsigned count, no wrap past zero.
    add_executable(d3d12_fence_waiter_test test/d3d12_fence_waiter_test.cpp)
    target_compile_definitions(d3d12_fence_waiter_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_fence_waiter_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_fence_waiter COMMAND d3d12_fence_waiter_test)
    # The 1/2/3 method variants (D3D-28) against the methods they wrap, on the Vulkan device directly.
    add_executable(d3d12_method_variants_test test/d3d12_method_variants_test.cpp)
    target_link_libraries(d3d12_method_variants_test PRIVATE gta6rt_d3d12_com)
    target_compile_definitions(d3d12_method_variants_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_method_variants_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_method_variants COMMAND d3d12_method_variants_test)
    set_tests_properties(d3d12_method_variants PROPERTIES SKIP_RETURN_CODE 77)

    # CreateSampler2 (D3D-28) against CreateSampler, and its refusals against the host's PC runtime.
    add_executable(d3d12_sampler2_test test/d3d12_sampler2_test.cpp)
    target_link_libraries(d3d12_sampler2_test PRIVATE gta6rt_d3d12_com)
    target_compile_definitions(d3d12_sampler2_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_sampler2_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_sampler2 COMMAND d3d12_sampler2_test)
    set_tests_properties(d3d12_sampler2 PROPERTIES SKIP_RETURN_CODE 77)

    # Descriptors through the proxy: copies between heaps, what texture views and samplers write into their slots,
    # and (where glslangValidator is found) a compute shader the test runs on the device's bindless set, which
    # finds a T# and an S# through the hash table and samples. The shader is GLSL, compiled here.
    add_executable(d3d12_descriptor_test test/d3d12_descriptor_test.cpp)
    target_link_libraries(d3d12_descriptor_test PRIVATE gta6rt_d3d12_com)
    target_compile_definitions(d3d12_descriptor_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_descriptor_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_descriptor COMMAND d3d12_descriptor_test)
    set_tests_properties(d3d12_descriptor PROPERTIES SKIP_RETURN_CODE 77)
    if(Vulkan_GLSLANG_VALIDATOR_EXECUTABLE)
      function(descriptor_test_shader shader definition)
        set(source ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/${shader})
        set(binary ${CMAKE_CURRENT_BINARY_DIR}/generated/${definition}.spv)
        add_custom_command(OUTPUT ${binary}
          COMMAND ${Vulkan_GLSLANG_VALIDATOR_EXECUTABLE} -V --target-env vulkan1.3 ${ARGN} -o ${binary} ${source}
          DEPENDS ${source} COMMENT "Compiling descriptor test shader ${shader}" VERBATIM)
        target_sources(d3d12_descriptor_test PRIVATE ${binary})
        target_compile_definitions(d3d12_descriptor_test PRIVATE ${definition}="${binary}")
      endfunction()
      descriptor_test_shader(cs_sample.comp LEONIDA_TEST_CS_SAMPLE_SPV)
      descriptor_test_shader(cs_sample.comp LEONIDA_TEST_MS_FLOAT_SPV -DTEST_MS_FLOAT=1)
      descriptor_test_shader(cs_sample.comp LEONIDA_TEST_MS_UINT_SPV -DTEST_MS_UINT=1)
      descriptor_test_shader(cs_sample.comp LEONIDA_TEST_MS_SINT_SPV -DTEST_MS_SINT=1)
      descriptor_test_shader(msaa_seed.vert LEONIDA_TEST_MSAA_VERT_SPV)
      descriptor_test_shader(msaa_seed.frag LEONIDA_TEST_MSAA_FRAG_SPV)
    else()
      message(STATUS "glslangValidator not found: d3d12_descriptor_test skips its shader")
    endif()

    add_executable(d3d12_predicated_copy_test test/d3d12_predicated_copy_test.cpp)
    target_link_libraries(d3d12_predicated_copy_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_predicated_copy_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_predicated_copy_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_predicated_copy COMMAND d3d12_predicated_copy_test)
    set_tests_properties(d3d12_predicated_copy PROPERTIES SKIP_RETURN_CODE 77)

    # Differential regression: run explicitly (requires Vulkan hardware and Windows WARP).
    add_executable(d3d12_clear_test test/d3d12_clear_test.cpp)
    target_link_libraries(d3d12_clear_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_clear_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_clear_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_rgb9e5_rtv COMMAND d3d12_clear_test --rgb9e5)
    set_tests_properties(d3d12_rgb9e5_rtv PROPERTIES SKIP_RETURN_CODE 77)
    # Not in ctest yet: on NVIDIA it fails on the 0.5 tie rounding #521 fixes; register it once that lands.

    # Synthetic typed-view readbacks against Windows WARP; run explicitly on a Vulkan GPU.
    add_executable(d3d12_typeless_rtv_test test/d3d12_typeless_rtv_test.cpp)
    target_link_libraries(d3d12_typeless_rtv_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_typeless_rtv_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_typeless_rtv_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_typeless_rtv COMMAND d3d12_typeless_rtv_test)

    # 96-bit R32G32B32 textures (D3D-62): footprints and every copy path against Windows WARP.
    add_executable(d3d12_rgb96_test test/d3d12_rgb96_test.cpp)
    target_link_libraries(d3d12_rgb96_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_rgb96_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_rgb96_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_rgb96 COMMAND d3d12_rgb96_test)
    set_tests_properties(d3d12_rgb96 PROPERTIES SKIP_RETURN_CODE 77)  # no Vulkan GPU or no WARP adapter
    set_tests_properties(d3d12_typeless_rtv PROPERTIES SKIP_RETURN_CODE 77)

    # Depth/stencil ResolveSubresourceRegion against the host runtime and WARP; run explicitly on a Vulkan GPU.
    add_executable(d3d12_depth_resolve_test test/d3d12_depth_resolve_test.cpp)
    target_link_libraries(d3d12_depth_resolve_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_depth_resolve_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_depth_resolve_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_depth_resolve COMMAND d3d12_depth_resolve_test)
    set_tests_properties(d3d12_depth_resolve PROPERTIES SKIP_RETURN_CODE 77)

    # ID3D12Resource::GetDesc against the host runtime and WARP; run explicitly on a Vulkan GPU.
    add_executable(d3d12_resource_desc_test test/d3d12_resource_desc_test.cpp)
    target_link_libraries(d3d12_resource_desc_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_resource_desc_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_resource_desc_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_resource_desc COMMAND d3d12_resource_desc_test)
    set_tests_properties(d3d12_resource_desc PROPERTIES SKIP_RETURN_CODE 77)

    # Milestone M0: a window, 60 cleared frames presented through the proxy on Vulkan.
    add_executable(d3d12_present_test test/d3d12_present_test.cpp)
    target_link_libraries(d3d12_present_test PRIVATE gta6rt_d3d12_com user32 gdi32 dxgi)
    target_compile_definitions(d3d12_present_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_present_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_present COMMAND d3d12_present_test)
    set_tests_properties(d3d12_present PROPERTIES SKIP_RETURN_CODE 77)

    # PresentX and the frame events on the Vulkan device (shims/xbox_present.h), checked on swapchain pixels.
    add_executable(xbox_present_test test/xbox_present_test.cpp)
    target_link_libraries(xbox_present_test PRIVATE gta6rt_d3d12_com user32)
    target_compile_definitions(xbox_present_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(xbox_present_test PRIVATE /W4 /permissive- /utf-8)
    # Needs a Vulkan GPU and a window; exits 77 (skipped) without a Vulkan device.
    add_test(NAME xbox_present COMMAND xbox_present_test)
    set_tests_properties(xbox_present PROPERTIES SKIP_RETURN_CODE 77)

    # M1.5 (D3D-53): a draw whose VS and PS are translator output. test/shaders/vs_m15.asm and ps_m15.asm are assembled
    # with llvm-mc and translated by tools/rdna2spirv.py through test/shaders/build.py, which also writes the FSSM
    # programs their index entries are keyed by. Needs llvm-mc with the AMDGPU target (LLVM_MC in the environment, as
    # for the other shader tests, or llvm-mc on PATH or in C:/msys64/ucrt64/bin) and the Vulkan SDK's spirv-as and
    # spirv-val; without them the test is not built.
    if(NOT LEONIDA_LLVM_MC)
      if(DEFINED ENV{LLVM_MC})
        set(LEONIDA_LLVM_MC "$ENV{LLVM_MC}")
      else()
        find_program(LEONIDA_LLVM_MC llvm-mc HINTS "C:/msys64/ucrt64/bin")
      endif()
    endif()
    find_program(LEONIDA_SPIRV_AS spirv-as HINTS "$ENV{VULKAN_SDK}/Bin" "$ENV{VULKAN_SDK}/bin")
    find_program(LEONIDA_SPIRV_VAL spirv-val HINTS "$ENV{VULKAN_SDK}/Bin" "$ENV{VULKAN_SDK}/bin")
    if(LEONIDA_LLVM_MC AND LEONIDA_SPIRV_AS AND LEONIDA_SPIRV_VAL)
      set(LEONIDA_M15_DIR ${CMAKE_CURRENT_BINARY_DIR}/generated/m15_shaders)
      get_filename_component(LEONIDA_SPIRV_TOOLS_DIR ${LEONIDA_SPIRV_AS} DIRECTORY)
      file(TO_NATIVE_PATH "${LEONIDA_SPIRV_TOOLS_DIR}" LEONIDA_SPIRV_TOOLS_NATIVE)
      set(LEONIDA_M15_FILES ${LEONIDA_M15_DIR}/spirv/index.json ${LEONIDA_M15_DIR}/fssm/vs_m15.fssm
                            ${LEONIDA_M15_DIR}/fssm/ps_m15.fssm)
      add_custom_command(
        OUTPUT ${LEONIDA_M15_FILES}
        COMMAND ${CMAKE_COMMAND} -E remove_directory ${LEONIDA_M15_DIR}
        COMMAND ${CMAKE_COMMAND} -E env "LLVM_MC=${LEONIDA_LLVM_MC}" "LEONIDA_SPIRV_TOOLS=${LEONIDA_SPIRV_TOOLS_NATIVE}"
                $<TARGET_FILE:Python3::Interpreter> -I ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/build.py
                ${LEONIDA_M15_DIR} vs_m15 ps_m15
        DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/build.py
                ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/compute_fixture.py
                ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/vs_m15.asm
                ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/ps_m15.asm
                ${CMAKE_CURRENT_SOURCE_DIR}/../tools/rdna2spirv.py
        COMMENT "Translating vs_m15 and ps_m15 for the translated draw test"
        VERBATIM)
      add_executable(d3d12_translated_draw_test test/d3d12_translated_draw_test.cpp shims/d3d12_xs_device.cpp
                     ${LEONIDA_M15_FILES})
      target_link_libraries(d3d12_translated_draw_test PRIVATE gta6rt_d3d12_com)
      target_compile_definitions(d3d12_translated_draw_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX
                                 LEONIDA_TEST_M15_DIR="${LEONIDA_M15_DIR}")
      target_compile_options(d3d12_translated_draw_test PRIVATE /W4 /permissive- /utf-8)
      add_test(NAME d3d12_translated_draw COMMAND d3d12_translated_draw_test)
      set_tests_properties(d3d12_translated_draw PROPERTIES SKIP_RETURN_CODE 77)

      # M2: a textured quad. vs_m2.asm and ps_m2.asm (the PS samples a T#/S# it loads from two descriptor tables) go
      # through the same build; the test creates the heaps, the SRV, the sampler and the root signature through the
      # D3D12 API only.
      set(LEONIDA_M2_DIR ${CMAKE_CURRENT_BINARY_DIR}/generated/m2_shaders)
      set(LEONIDA_M2_FILES ${LEONIDA_M2_DIR}/spirv/index.json ${LEONIDA_M2_DIR}/fssm/vs_m2.fssm
                           ${LEONIDA_M2_DIR}/fssm/ps_m2.fssm ${LEONIDA_M2_DIR}/fssm/ps_m2_uoff.fssm)
      add_custom_command(
        OUTPUT ${LEONIDA_M2_FILES}
        COMMAND ${CMAKE_COMMAND} -E remove_directory ${LEONIDA_M2_DIR}
        COMMAND ${CMAKE_COMMAND} -E env "LLVM_MC=${LEONIDA_LLVM_MC}" "LEONIDA_SPIRV_TOOLS=${LEONIDA_SPIRV_TOOLS_NATIVE}"
                $<TARGET_FILE:Python3::Interpreter> -I ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/build.py
                ${LEONIDA_M2_DIR} vs_m2 ps_m2 ps_m2_uoff
        DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/build.py
                ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/compute_fixture.py
                ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/vs_m2.asm
                ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/ps_m2.asm
                ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/ps_m2_uoff.asm
                ${CMAKE_CURRENT_SOURCE_DIR}/../tools/rdna2spirv.py
        COMMENT "Translating vs_m2 and ps_m2 for the textured draw test"
        VERBATIM)
      add_executable(d3d12_m2_textured_test test/d3d12_m2_textured_test.cpp shims/d3d12_xs_device.cpp
                     ${LEONIDA_M2_FILES})
      target_link_libraries(d3d12_m2_textured_test PRIVATE gta6rt_d3d12_com)
      target_compile_definitions(d3d12_m2_textured_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX
                                 LEONIDA_TEST_M2_DIR="${LEONIDA_M2_DIR}")
      target_compile_options(d3d12_m2_textured_test PRIVATE /W4 /permissive- /utf-8)
      add_test(NAME d3d12_m2_textured COMMAND d3d12_m2_textured_test)
      set_tests_properties(d3d12_m2_textured PROPERTIES SKIP_RETURN_CODE 77)

      # M3 (synthetic part): frames with two render targets and a depth/stencil buffer, an occlusion query, resource
      # barriers and a second pass that samples the first one's target, three frames in flight with a fence each.
      # vs_m3.asm, ps_m3.asm (pass 1) and ps_m3_post.asm (pass 2, with vs_m2.asm) go through the same build.
      set(LEONIDA_M3_DIR ${CMAKE_CURRENT_BINARY_DIR}/generated/m3_shaders)
      set(LEONIDA_M3_FILES ${LEONIDA_M3_DIR}/spirv/index.json ${LEONIDA_M3_DIR}/fssm/vs_m3.fssm
                           ${LEONIDA_M3_DIR}/fssm/ps_m3.fssm ${LEONIDA_M3_DIR}/fssm/vs_m2.fssm
                           ${LEONIDA_M3_DIR}/fssm/ps_m3_post.fssm)
      add_custom_command(
        OUTPUT ${LEONIDA_M3_FILES}
        COMMAND ${CMAKE_COMMAND} -E remove_directory ${LEONIDA_M3_DIR}
        COMMAND ${CMAKE_COMMAND} -E env "LLVM_MC=${LEONIDA_LLVM_MC}" "LEONIDA_SPIRV_TOOLS=${LEONIDA_SPIRV_TOOLS_NATIVE}"
                $<TARGET_FILE:Python3::Interpreter> -I ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/build.py
                ${LEONIDA_M3_DIR} vs_m3 ps_m3 vs_m2 ps_m3_post
        DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/build.py
                ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/compute_fixture.py
                ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/vs_m3.asm
                ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/ps_m3.asm
                ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/vs_m2.asm
                ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/ps_m3_post.asm
                ${CMAKE_CURRENT_SOURCE_DIR}/../tools/rdna2spirv.py
        COMMENT "Translating vs_m3, ps_m3, vs_m2 and ps_m3_post for the frame test"
        VERBATIM)
      add_executable(d3d12_m3_frame_test test/d3d12_m3_frame_test.cpp shims/d3d12_xs_device.cpp ${LEONIDA_M3_FILES})
      target_link_libraries(d3d12_m3_frame_test PRIVATE gta6rt_d3d12_com)
      target_compile_definitions(d3d12_m3_frame_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX
                                 LEONIDA_TEST_M3_DIR="${LEONIDA_M3_DIR}")
      target_compile_options(d3d12_m3_frame_test PRIVATE /W4 /permissive- /utf-8)
      add_test(NAME d3d12_m3_frame COMMAND d3d12_m3_frame_test)
      set_tests_properties(d3d12_m3_frame PROPERTIES SKIP_RETURN_CODE 77)
    else()
      message(STATUS "llvm-mc (set LLVM_MC) or the SDK's spirv-as/spirv-val not found: d3d12_translated_draw_test, d3d12_m2_textured_test and d3d12_m3_frame_test are not built")
    endif()
    # ID3D12Resource::GetHeapProperties and ID3D12DescriptorHeap::GetDesc (node masks, resolved heap flags) against the
    # host runtime and WARP; run explicitly on a Vulkan GPU.
    add_executable(d3d12_node_mask_test test/d3d12_node_mask_test.cpp)
    target_link_libraries(d3d12_node_mask_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_node_mask_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_node_mask_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_node_mask COMMAND d3d12_node_mask_test)
    add_executable(d3d12_indirect_zero_test test/d3d12_indirect_zero_test.cpp)
    target_link_libraries(d3d12_indirect_zero_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_indirect_zero_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_indirect_zero_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_indirect_zero COMMAND d3d12_indirect_zero_test)
    set_tests_properties(d3d12_node_mask PROPERTIES SKIP_RETURN_CODE 77)
    set_tests_properties(d3d12_indirect_zero PROPERTIES SKIP_RETURN_CODE 77)

    # CopyResource / CopyTextureRegion between BC and its uncompressed equivalent (and depth <-> colour) against the
    # host runtime and WARP; run explicitly on a Vulkan GPU.
    add_executable(d3d12_copy_xfmt_test test/d3d12_copy_xfmt_test.cpp)
    target_link_libraries(d3d12_copy_xfmt_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_copy_xfmt_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_copy_xfmt_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_copy_xfmt COMMAND d3d12_copy_xfmt_test)
    set_tests_properties(d3d12_copy_xfmt PROPERTIES SKIP_RETURN_CODE 77)

    add_executable(d3d12_stencil_copy_test test/d3d12_stencil_copy_test.cpp)
    target_link_libraries(d3d12_stencil_copy_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_stencil_copy_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_stencil_copy_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_stencil_copy COMMAND d3d12_stencil_copy_test)
    set_tests_properties(d3d12_stencil_copy PROPERTIES SKIP_RETURN_CODE 77
      ENVIRONMENT "LEONIDA_HOST_ADAPTER=warp")

    # Query heaps and query calls by list type (Close results, CreateQueryHeap node masks, timestamp heap kinds)
    # against the host runtime and WARP; run explicitly on a Vulkan GPU.
    add_executable(d3d12_query_rules_test test/d3d12_query_rules_test.cpp)
    target_link_libraries(d3d12_query_rules_test PRIVATE gta6rt_d3d12_com d3d12 dxgi)
    target_compile_definitions(d3d12_query_rules_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_query_rules_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME d3d12_query_rules COMMAND d3d12_query_rules_test)
    set_tests_properties(d3d12_query_rules PROPERTIES SKIP_RETURN_CODE 77)

    # Vulkan-backed IDXGIFactory4 / IDXGISwapChain3 presentation over a Win32 HWND.
    add_executable(dxgi_swapchain_test test/dxgi_swapchain_test.cpp)
    target_link_libraries(dxgi_swapchain_test PRIVATE gta6rt_d3d12_com)
    target_compile_definitions(dxgi_swapchain_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(dxgi_swapchain_test PRIVATE /W4 /permissive- /utf-8)
    add_test(NAME dxgi_swapchain COMMAND dxgi_swapchain_test)
    set_tests_properties(dxgi_swapchain PROPERTIES SKIP_RETURN_CODE 77)

    # ExecuteIndirect with CountBuffer (vkCmdDrawIndirectCount) + optional BLAS/TLAS/RayQueryKHR GPU test.
    add_executable(d3d12_indirect_rt_test test/d3d12_indirect_rt_test.cpp)
    target_link_libraries(d3d12_indirect_rt_test PRIVATE gta6rt_d3d12_com)
    target_compile_definitions(d3d12_indirect_rt_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    target_compile_options(d3d12_indirect_rt_test PRIVATE /W4 /permissive- /utf-8)
    if(Vulkan_GLSLANG_VALIDATOR_EXECUTABLE)
      set(LEONIDA_TEST_VS_TRI ${CMAKE_CURRENT_BINARY_DIR}/generated/vs_tri.spv)
      set(LEONIDA_TEST_FS_SOLID ${CMAKE_CURRENT_BINARY_DIR}/generated/fs_solid.spv)
      set(LEONIDA_TEST_CS_RAYQUERY ${CMAKE_CURRENT_BINARY_DIR}/generated/cs_rayquery.spv)
      add_custom_command(
        OUTPUT ${LEONIDA_TEST_VS_TRI}
        COMMAND ${Vulkan_GLSLANG_VALIDATOR_EXECUTABLE} -V --target-env vulkan1.3 -o ${LEONIDA_TEST_VS_TRI}
                ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/vs_tri.vert
        DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/vs_tri.vert
        COMMENT "Compiling vs_tri.vert"
        VERBATIM)
      add_custom_command(
        OUTPUT ${LEONIDA_TEST_FS_SOLID}
        COMMAND ${Vulkan_GLSLANG_VALIDATOR_EXECUTABLE} -V --target-env vulkan1.3 -o ${LEONIDA_TEST_FS_SOLID}
                ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/fs_solid.frag
        DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/fs_solid.frag
        COMMENT "Compiling fs_solid.frag"
        VERBATIM)
      add_custom_command(
        OUTPUT ${LEONIDA_TEST_CS_RAYQUERY}
        COMMAND ${Vulkan_GLSLANG_VALIDATOR_EXECUTABLE} -V --target-env vulkan1.3 -o ${LEONIDA_TEST_CS_RAYQUERY}
                ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/cs_rayquery.comp
        DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/cs_rayquery.comp
        COMMENT "Compiling cs_rayquery.comp"
        VERBATIM)
      target_sources(d3d12_indirect_rt_test PRIVATE
                     ${LEONIDA_TEST_VS_TRI} ${LEONIDA_TEST_FS_SOLID} ${LEONIDA_TEST_CS_RAYQUERY})
      target_compile_definitions(d3d12_indirect_rt_test PRIVATE
        LEONIDA_TEST_VS_TRI_SPV="${LEONIDA_TEST_VS_TRI}"
        LEONIDA_TEST_FS_SOLID_SPV="${LEONIDA_TEST_FS_SOLID}"
        LEONIDA_TEST_CS_RAYQUERY_SPV="${LEONIDA_TEST_CS_RAYQUERY}")
    endif()
    add_test(NAME d3d12_indirect_rt COMMAND d3d12_indirect_rt_test)
    set_tests_properties(d3d12_indirect_rt PROPERTIES SKIP_RETURN_CODE 77)

    # The compute path: root signature, compute PSO from a translator-style index, root arguments, dispatch. Its
    # shader is GLSL written to the translator's push-constant/address contract, compiled here.
    if(NOT Vulkan_GLSLANG_VALIDATOR_EXECUTABLE)
      find_program(Vulkan_GLSLANG_VALIDATOR_EXECUTABLE glslangValidator HINTS "$ENV{VULKAN_SDK}/Bin" "$ENV{VULKAN_SDK}/bin")
    endif()
    if(Vulkan_GLSLANG_VALIDATOR_EXECUTABLE)
      set(LEONIDA_TEST_CS ${CMAKE_CURRENT_BINARY_DIR}/generated/cs_user_data.spv)
      add_custom_command(
        OUTPUT ${LEONIDA_TEST_CS}
        COMMAND ${Vulkan_GLSLANG_VALIDATOR_EXECUTABLE} -V --target-env vulkan1.3 -o ${LEONIDA_TEST_CS}
                ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/cs_user_data.comp
        DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/cs_user_data.comp
        COMMENT "Compiling the compute test shader"
        VERBATIM)
      add_executable(d3d12_compute_test test/d3d12_compute_test.cpp shims/d3d12_xs_device.cpp ${LEONIDA_TEST_CS})
      target_link_libraries(d3d12_compute_test PRIVATE gta6rt_d3d12_com)
      target_compile_definitions(d3d12_compute_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX
                                 LEONIDA_TEST_CS_SPV="${LEONIDA_TEST_CS}")
      target_compile_options(d3d12_compute_test PRIVATE /W4 /permissive- /utf-8)
      add_test(NAME d3d12_compute COMMAND d3d12_compute_test)
      set_tests_properties(d3d12_compute PROPERTIES SKIP_RETURN_CODE 77)
      # Milestone M1: a VS+PS pipeline and indexed draws, shaders written to the same contract.
      set(LEONIDA_TEST_VS ${CMAKE_CURRENT_BINARY_DIR}/generated/vs_draw.spv)
      set(LEONIDA_TEST_PS ${CMAKE_CURRENT_BINARY_DIR}/generated/ps_draw.spv)
      set(LEONIDA_TEST_VS_FULLSCREEN ${CMAKE_CURRENT_BINARY_DIR}/generated/vs_fullscreen.spv)
      foreach(pair "vs_draw.vert;${LEONIDA_TEST_VS}" "ps_draw.frag;${LEONIDA_TEST_PS}"
                   "vs_fullscreen.vert;${LEONIDA_TEST_VS_FULLSCREEN}")
        list(GET pair 0 source)
        list(GET pair 1 output)
        add_custom_command(
          OUTPUT ${output}
          COMMAND ${Vulkan_GLSLANG_VALIDATOR_EXECUTABLE} -V --target-env vulkan1.3 -o ${output}
                  ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/${source}
          DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/${source}
          COMMENT "Compiling the draw test shader ${source}"
          VERBATIM)
      endforeach()
      add_executable(d3d12_draw_test test/d3d12_draw_test.cpp shims/d3d12_xs_device.cpp ${LEONIDA_TEST_VS}
                     ${LEONIDA_TEST_PS} ${LEONIDA_TEST_VS_FULLSCREEN})
      target_link_libraries(d3d12_draw_test PRIVATE gta6rt_d3d12_com)
      target_compile_definitions(d3d12_draw_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX
                                 LEONIDA_TEST_VS_SPV="${LEONIDA_TEST_VS}" LEONIDA_TEST_PS_SPV="${LEONIDA_TEST_PS}"
                                 LEONIDA_TEST_VS_FULLSCREEN_SPV="${LEONIDA_TEST_VS_FULLSCREEN}")
      target_compile_options(d3d12_draw_test PRIVATE /W4 /permissive- /utf-8)
      add_test(NAME d3d12_draw COMMAND d3d12_draw_test)
      set_tests_properties(d3d12_draw PROPERTIES SKIP_RETURN_CODE 77)
      # Set 0 from the compute bind point: a compute PSO whose shader declares set-0 bindings samples through the
      # device's bindless set, which nothing but the layer binds.
      set(LEONIDA_TEST_CS_SET0_SAMPLE ${CMAKE_CURRENT_BINARY_DIR}/generated/cs_set0_sample.spv)
      add_custom_command(
        OUTPUT ${LEONIDA_TEST_CS_SET0_SAMPLE}
        COMMAND ${Vulkan_GLSLANG_VALIDATOR_EXECUTABLE} -V --target-env vulkan1.3 -o ${LEONIDA_TEST_CS_SET0_SAMPLE}
                ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/cs_set0_sample.comp
        DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/cs_set0_sample.comp
        COMMENT "Compiling the set-0 compute test shader"
        VERBATIM)
      add_executable(d3d12_set0_compute_test test/d3d12_set0_compute_test.cpp shims/d3d12_xs_device.cpp
                     ${LEONIDA_TEST_CS_SET0_SAMPLE})
      target_link_libraries(d3d12_set0_compute_test PRIVATE gta6rt_d3d12_com)
      target_compile_definitions(d3d12_set0_compute_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX
                                 LEONIDA_TEST_CS_SET0_SAMPLE_SPV="${LEONIDA_TEST_CS_SET0_SAMPLE}")
      target_compile_options(d3d12_set0_compute_test PRIVATE /W4 /permissive- /utf-8)
      add_test(NAME d3d12_set0_compute COMMAND d3d12_set0_compute_test)
      set_tests_properties(d3d12_set0_compute PROPERTIES SKIP_RETURN_CODE 77)
    else()
      message(STATUS "glslangValidator not found: d3d12_compute_test, d3d12_draw_test and d3d12_set0_compute_test are not built")
    endif()

    # Subgroup size control in PSOs, end to end: runtime/test/shaders/cs_w64x_readlane.asm translated natively and with
    # --wave64-emulate, and cs_wqm_w32.asm, built here with tools/rdna2spirv.py through test/shaders/build.py. Needs
    # llvm-mc with the AMDGPU target (LLVM_MC in the environment, as for the other shader tests, or llvm-mc on PATH)
    # and the Vulkan SDK's spirv tools; without them the test is not built.
    if(DEFINED ENV{LLVM_MC})
      set(LEONIDA_LLVM_MC "$ENV{LLVM_MC}")
    else()
      find_program(LEONIDA_LLVM_MC llvm-mc HINTS "C:/msys64/ucrt64/bin")
    endif()
    if(LEONIDA_LLVM_MC AND Vulkan_GLSLANG_VALIDATOR_EXECUTABLE)
      set(LEONIDA_SG_DIR ${CMAKE_CURRENT_BINARY_DIR}/generated/subgroup_shaders)
      set(LEONIDA_SG_INDEXES "")
      foreach(case "w64_native;cs_w64x_readlane;0" "w64_emulated;cs_w64x_readlane;1" "w32_native;cs_wqm_w32;0"
                       "ps_w64_native;ps_w64x_readlane;0")
        list(GET case 0 folder)
        list(GET case 1 shader)
        list(GET case 2 emulate)
        set(index ${LEONIDA_SG_DIR}/${folder}/spirv/index.json)
        add_custom_command(
          OUTPUT ${index}
          COMMAND ${CMAKE_COMMAND} -E env LLVM_MC=${LEONIDA_LLVM_MC} LEONIDA_W64_EMULATE=${emulate}
                  $<TARGET_FILE:Python3::Interpreter> -I ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/build.py
                  ${LEONIDA_SG_DIR}/${folder} ${shader}
          DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/build.py
                  ${CMAKE_CURRENT_SOURCE_DIR}/test/shaders/${shader}.asm
                  ${CMAKE_CURRENT_SOURCE_DIR}/../tools/rdna2spirv.py
          COMMENT "Translating ${shader} for the subgroup size test (${folder})"
          VERBATIM)
        list(APPEND LEONIDA_SG_INDEXES ${index})
      endforeach()
      # The probe shaders that store the subgroup size the driver ran them with, and a one-triangle vertex shader.
      foreach(source cs_sg_probe.comp ps_sg_probe.frag vs_sg_fullscreen.vert)
        set(output ${LEONIDA_SG_DIR}/glsl/${source}.spv)
        add_custom_command(
          OUTPUT ${output}
          COMMAND ${CMAKE_COMMAND} -E make_directory ${LEONIDA_SG_DIR}/glsl
          COMMAND ${Vulkan_GLSLANG_VALIDATOR_EXECUTABLE} -V --target-env vulkan1.3 -o ${output}
                  ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/${source}
          DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/test/glsl/${source}
          COMMENT "Compiling ${source}"
          VERBATIM)
        list(APPEND LEONIDA_SG_INDEXES ${output})
      endforeach()
      add_executable(d3d12_subgroup_test test/d3d12_subgroup_test.cpp shims/d3d12_xs_device.cpp ${LEONIDA_SG_INDEXES})
      target_link_libraries(d3d12_subgroup_test PRIVATE gta6rt_d3d12_com)
      target_compile_definitions(d3d12_subgroup_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX
                                 LEONIDA_TEST_SUBGROUP_SHADERS="${LEONIDA_SG_DIR}")
      target_compile_options(d3d12_subgroup_test PRIVATE /W4 /permissive- /utf-8)
      add_test(NAME d3d12_subgroup COMMAND d3d12_subgroup_test)
      set_tests_properties(d3d12_subgroup PROPERTIES SKIP_RETURN_CODE 77)
    else()
      message(STATUS "llvm-mc (set LLVM_MC) or glslangValidator not found: d3d12_subgroup_test is not built")
    endif()

    # The loader can create this device instead of the host's (D3D12XboxCreateDevice, LEONIDA_D3D12=vulkan).
    if(TARGET gta6rt)
      target_link_libraries(gta6rt PRIVATE gta6rt_d3d12_com)
      target_compile_definitions(gta6rt PRIVATE LEONIDA_VULKAN_DEVICE)
    endif()
    # bvh_builder_test can put the package's XboxBvhBuilder on this device too (LEONIDA_BVH_DEVICE=vulkan).
    if(TARGET bvh_builder_test)
      target_link_libraries(bvh_builder_test PRIVATE gta6rt_d3d12_com)
      target_compile_definitions(bvh_builder_test PRIVATE LEONIDA_VULKAN_DEVICE)
    endif()
    # fsr_dispatch_test can put the package's FidelityFX DLL on this device too (LEONIDA_FSR_DEVICE=vulkan).
    if(TARGET fsr_dispatch_test)
      target_link_libraries(fsr_dispatch_test PRIVATE gta6rt_d3d12_com)
      target_compile_definitions(fsr_dispatch_test PRIVATE LEONIDA_VULKAN_DEVICE)
    endif()
  else()
    message(STATUS "Python 3, Windows or d3d12.h missing: the D3D12 COM objects and d3d12_com_test are not built")
  endif()

  # Needs only the Vulkan headers, not a GPU.
  if(BUILD_TESTING)
    add_executable(typeless_rtv_test test/typeless_rtv_test.cpp)
    target_link_libraries(typeless_rtv_test PRIVATE Vulkan::Headers)
    if(WIN32)
      target_compile_definitions(typeless_rtv_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    endif()
    add_test(NAME typeless_rtv COMMAND typeless_rtv_test)
    add_executable(pipeline_cache_snapshot_test test/pipeline_cache_snapshot_test.cpp)
    target_link_libraries(pipeline_cache_snapshot_test PRIVATE Vulkan::Headers)
    add_test(NAME pipeline_cache_snapshot COMMAND pipeline_cache_snapshot_test)
    add_executable(predicated_copy_test test/predicated_copy_test.cpp)
    target_link_libraries(predicated_copy_test PRIVATE Vulkan::Headers)
    if(WIN32)
      target_compile_definitions(predicated_copy_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    endif()
    add_test(NAME predicated_copy COMMAND predicated_copy_test)
    # CopyResource (buffer-to-buffer) and CopyTexture (including depth24_as_float) apply ResourceBinding::buffer_offset (no GPU).
    add_executable(replay_copy_offset_test test/replay_copy_offset_test.cpp)
    target_link_libraries(replay_copy_offset_test PRIVATE Vulkan::Headers)
    if(WIN32)
      target_compile_definitions(replay_copy_offset_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    endif()
    add_test(NAME replay_copy_offset COMMAND replay_copy_offset_test)
    # PreparedList::wrote_readback and readback_src_stages match the actual producer stage (depth24, rgb96, UAV clear; no GPU).
    add_executable(replay_readback_stages_test test/replay_readback_stages_test.cpp)
    target_link_libraries(replay_readback_stages_test PRIVATE Vulkan::Headers)
    if(WIN32)
      target_compile_definitions(replay_readback_stages_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    endif()
    add_test(NAME replay_readback_stages COMMAND replay_readback_stages_test)
    add_executable(clear_color_test test/clear_color_test.cpp)
    target_link_libraries(clear_color_test PRIVATE Vulkan::Headers)
    if(WIN32)
      target_compile_definitions(clear_color_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    endif()
    add_test(NAME clear_color COMMAND clear_color_test)
    # FramePacer of the Xbox presentation path on a fake clock (src/frame_pacer.h); no GPU, no Vulkan.
    add_executable(frame_pacer_test test/frame_pacer_test.cpp)
    find_package(Threads REQUIRED)
    target_link_libraries(frame_pacer_test PRIVATE Threads::Threads)
    if(MSVC)
      target_compile_options(frame_pacer_test PRIVATE /W4 /permissive- /utf-8)
    endif()
    add_test(NAME frame_pacer COMMAND frame_pacer_test)

    # Replay refuses a list whose whole-view or implicit zero-clear needs an unset vkCmdClear*Image (no GPU).
    add_executable(replay_clear_gate_test test/replay_clear_gate_test.cpp)
    target_link_libraries(replay_clear_gate_test PRIVATE Vulkan::Headers)
    if(WIN32)
      target_compile_definitions(replay_clear_gate_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    endif()
    add_test(NAME replay_clear_gate COMMAND replay_clear_gate_test)
    # WriteBufferImmediate suspends conditional rendering around vkCmdUpdateBuffer so Intel drivers do not drop it.
    add_executable(replay_write_immediate_predication_test test/replay_write_immediate_predication_test.cpp)
    target_link_libraries(replay_write_immediate_predication_test PRIVATE Vulkan::Headers)
    if(WIN32)
      target_compile_definitions(replay_write_immediate_predication_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    endif()
    add_test(NAME replay_write_immediate_predication COMMAND replay_write_immediate_predication_test)
    add_executable(d3d12_state_map_test test/d3d12_state_map_test.cpp)
    target_link_libraries(d3d12_state_map_test PRIVATE Vulkan::Headers)
    if(WIN32)
      target_compile_definitions(d3d12_state_map_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    endif()
    add_test(NAME d3d12_state_map COMMAND d3d12_state_map_test)
    # A buffer UAV clear writes where its resource starts in a shared VkBuffer (no GPU).
    add_executable(replay_uav_clear_offset_test test/replay_uav_clear_offset_test.cpp)
    target_link_libraries(replay_uav_clear_offset_test PRIVATE Vulkan::Headers)
    add_test(NAME replay_uav_clear_offset COMMAND replay_uav_clear_offset_test)
    # ResolveQueryData suspends conditional rendering around un-ended vkCmdFillBuffer runs as well as dispatches (D3D-148).
    add_executable(replay_query_predication_test test/replay_query_predication_test.cpp)
    target_link_libraries(replay_query_predication_test PRIVATE Vulkan::Headers)
    if(WIN32)
      target_compile_definitions(replay_query_predication_test PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    endif()
    add_test(NAME replay_query_predication COMMAND replay_query_predication_test)
    # GFX10 T#/S#/V# descriptor encoding and its Vulkan mappings (src/gfx10_descriptors.h).
    add_executable(gfx10_descriptors_test test/gfx10_descriptors_test.cpp)
    target_link_libraries(gfx10_descriptors_test PRIVATE Vulkan::Headers)
    add_test(NAME gfx10_descriptors COMMAND gfx10_descriptors_test)
    # CPU cost of the D3D12 -> Vulkan replay per command (record, prepare, emit) with no-op Vulkan entry points:
    # no GPU or driver. ctest runs its quick self-test; run it without arguments for the numbers.
    add_executable(replay_cpu_bench test/replay_cpu_bench.cpp)
    target_link_libraries(replay_cpu_bench PRIVATE Vulkan::Headers)
    if(WIN32)
      target_compile_definitions(replay_cpu_bench PRIVATE WIN32_LEAN_AND_MEAN NOMINMAX)
    endif()
    add_test(NAME replay_cpu_bench_selftest COMMAND replay_cpu_bench --self-test)
    add_test(NAME vk_descriptor_bench_selftest COMMAND vk_descriptor_bench --self-test)
    # The subgroup size a pipeline stage asks for, from synthetic index entries and device properties.
    add_executable(subgroup_size_test test/subgroup_size_test.cpp)
    target_link_libraries(subgroup_size_test PRIVATE Vulkan::Headers)
    add_test(NAME subgroup_size COMMAND subgroup_size_test)
  endif()
else()
  message(STATUS "Vulkan headers not found: GPU test tools, vk_pipeline_check, d3d12_state_map_test and "
                 "gfx10_descriptors_test are not built. Set VULKAN_SDK, install Vulkan headers, or pass "
                 "-DLEONIDA_VULKAN_HEADERS_FETCH=ON to download the pinned Khronos Vulkan-Headers release")
endif()

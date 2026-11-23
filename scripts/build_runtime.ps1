# Builds runtime\build\gta6rt.exe with MSVC at BelowNormal priority, on half the cores by default.
# Uses VS's CMake + Ninja when the "C++ CMake tools" component is installed, otherwise calls cl.exe directly.
# The GameInput controller shim (SDL3, docs/gameinput.md) is built only on the CMake path: with an installed SDL3, or
# with -FetchSdl3, which lets CMake download the pinned SDL3 release. The cl.exe fallback never builds it.
param(
    [switch]$Debug,
    [switch]$FetchSdl3,
    [ValidateRange(1, 2147483647)][int]$jobs = [Math]::Max(1, [int]([Environment]::ProcessorCount / 2))
)
$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent
$installer = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer"
$vs = & "$installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw "Visual Studio with the C++ x64 tools wasn't found." }
$vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"

$src = Join-Path $root "runtime"
$out = Join-Path $src "build"
New-Item -ItemType Directory -Force $out | Out-Null

$cmakeDir = Join-Path $vs "Common7\IDE\CommonExtensions\Microsoft\CMake"
$cmake = Join-Path $cmakeDir "CMake\bin\cmake.exe"
$ninja = Join-Path $cmakeDir "Ninja\ninja.exe"
$cfg = if ($Debug) { "Debug" } else { "RelWithDebInfo" }
$sdl = if ($FetchSdl3) { "-DLEONIDA_SDL3_FETCH=ON" } else { "" }
# With VULKAN_SDK set, a configure that still finds no Vulkan headers fails instead of dropping the Vulkan D3D12 device.
$vk = if ($env:VULKAN_SDK) { "-DLEONIDA_REQUIRE_VULKAN=ON" } else { "" }

if ((Test-Path $cmake) -and (Test-Path $ninja)) {
    $body = @"
"$cmake" -S "$src" -B "$out" -G Ninja "-DCMAKE_MAKE_PROGRAM=$ninja" -DCMAKE_BUILD_TYPE=$cfg $sdl $vk || exit /b 1
"$cmake" --build "$out" -j $jobs || exit /b 1
"@
} else {
    # Keep this list in step with runtime\CMakeLists.txt. Optional parts that need a dependency are left out here:
    # the GameInput shim (SDL3), so controllers go through the host's GameInput.dll, if any, as without the shim.
    Write-Warning "VS C++ CMake tools not found: building with cl.exe directly, without the GameInput shim (SDL3). Install the CMake tools component to get it."
    $xtaskqueueDir = Join-Path $root "third_party\xtaskqueue"
    $xtaskqueueFiles = "Source\Task\TaskQueue.cpp", "Source\Task\AsyncLib.cpp", "Source\Task\ThreadPool_win32.cpp", "Source\Task\WaitTimer_win32.cpp" | ForEach-Object { "`"$xtaskqueueDir\$_`"" }
    $files = "src\main.cpp", "src\pe_loader.cpp", "shims\registry.cpp", "shims\file_system.cpp", "shims\xmem.cpp", "shims\d3d12_xs.cpp", "shims\d3d12_xs_device.cpp", "shims\d3d12_xs_rootsig.cpp", "shims\xgameruntime.cpp", "shims\xgameruntime_user.cpp", "shims\xgameruntime_gamesave.cpp", "shims\xgameruntime_store.cpp", "shims\xgameruntime_offline.cpp", "shims\winhttp_offline.cpp", "shims\library_loader.cpp", "shims\process.cpp" | ForEach-Object { "`"$src\$_`"" }
    $includes = "/I `"$src\src`" /I `"$src\shims`" /I `"$src\shims\xtaskqueue`" /I `"$xtaskqueueDir\Include`" /I `"$xtaskqueueDir\Source\Task`" /I `"$xtaskqueueDir\Source\Common`""
    $opt = if ($Debug) { "/Od /MDd" } else { "/O2 /MD" }
    $body = @"
cd /d "$out" || exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /permissive- /utf-8 /Zi $opt /MP$jobs /DWIN32_LEAN_AND_MEAN /DNOMINMAX /DUNICODE /D_UNICODE $includes $($files -join ' ') $($xtaskqueueFiles -join ' ') /Fe:gta6rt.exe /link /OPT:NOREF psapi.lib ole32.lib || exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /permissive- /utf-8 /O2 /MT /DWIN32_LEAN_AND_MEAN /DNOMINMAX "$src\src\tls_host.cpp" /LD /Fe:gta6rt_tls_host.dll || exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /permissive- /utf-8 /O2 /DWIN32_LEAN_AND_MEAN /DNOMINMAX /I "$src\shims" "$src\shims\d3d12_xs_rootsig.cpp" "$src\shims\registry.cpp" "$src\shims\d3d12_xs.cpp" "$src\shims\d3d12_xs_device.cpp" "$src\test\root_sig_test.cpp" /Fe:root_sig_test.exe /link d3d12.lib || exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /permissive- /utf-8 /O2 /DWIN32_LEAN_AND_MEAN /DNOMINMAX /I "$src\shims" "$src\shims\d3d12_xs_rootsig.cpp" "$src\shims\registry.cpp" "$src\shims\d3d12_xs.cpp" "$src\shims\d3d12_xs_device.cpp" "$src\test\device_proxy_test.cpp" /Fe:device_proxy_test.exe /link d3d12.lib dxgi.lib || exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /permissive- /utf-8 /O2 /DWIN32_LEAN_AND_MEAN /DNOMINMAX /I "$src\src" "$src\test\descriptor_table_test.cpp" /Fe:descriptor_table_test.exe /link /OPT:NOREF || exit /b 1
cl /nologo /std:c++20 /EHsc /W4 /permissive- /utf-8 /O2 /DWIN32_LEAN_AND_MEAN /DNOMINMAX /I "$src\src" "$src\test\command_list_test.cpp" /Fe:command_list_test.exe /link /OPT:NOREF || exit /b 1
if defined VULKAN_SDK cl /nologo /std:c++20 /EHsc /O2 /DWIN32_LEAN_AND_MEAN /DNOMINMAX /I "%VULKAN_SDK%\Include" "$src\test\vk_pipeline_check.cpp" /Fe:vk_pipeline_check.exe || exit /b 1
if defined VULKAN_SDK cl /nologo /std:c++20 /EHsc /O2 /DWIN32_LEAN_AND_MEAN /DNOMINMAX /I "%VULKAN_SDK%\Include" /I "$src\src" "$src\test\vk_texture_test.cpp" /Fe:vk_texture_test.exe || exit /b 1
if defined VULKAN_SDK cl /nologo /std:c++20 /EHsc /O2 /DWIN32_LEAN_AND_MEAN /DNOMINMAX /I "%VULKAN_SDK%\Include" /I "$src\src" "$src\test\vk_sampler_test.cpp" /Fe:vk_sampler_test.exe || exit /b 1
if defined VULKAN_SDK cl /nologo /std:c++20 /EHsc /O2 /DWIN32_LEAN_AND_MEAN /DNOMINMAX /I "%VULKAN_SDK%\Include" /I "$src\src" "$src\test\gfx10_descriptors_test.cpp" /Fe:gfx10_descriptors_test.exe || exit /b 1
if defined VULKAN_SDK cl /nologo /std:c++20 /EHsc /O2 /DWIN32_LEAN_AND_MEAN /DNOMINMAX /I "%VULKAN_SDK%\Include" /I "$src\src" "$src\test\vk_bindless_test.cpp" /Fe:vk_bindless_test.exe || exit /b 1
if defined VULKAN_SDK cl /nologo /std:c++20 /EHsc /O2 /DWIN32_LEAN_AND_MEAN /DNOMINMAX /I "%VULKAN_SDK%\Include" /I "$src\src" "$src\test\vk_descriptor_bench.cpp" /Fe:vk_descriptor_bench.exe || exit /b 1
rem The GPU test harnesses check.py and run_gpu_tests.py require and the fuzzers run (tests\test_build_runtime_fallback.py).
if defined VULKAN_SDK cl /nologo /std:c++20 /EHsc /O2 /DWIN32_LEAN_AND_MEAN /DNOMINMAX /I "%VULKAN_SDK%\Include" /I "$src\src" "$src\test\vk_harness.cpp" /Fe:vk_harness.exe || exit /b 1
if defined VULKAN_SDK cl /nologo /std:c++20 /EHsc /O2 /DWIN32_LEAN_AND_MEAN /DNOMINMAX /I "%VULKAN_SDK%\Include" /I "$src\src" "$src\test\vk_compute_test.cpp" /Fe:vk_compute_test.exe || exit /b 1
if defined VULKAN_SDK cl /nologo /std:c++20 /EHsc /O2 /DWIN32_LEAN_AND_MEAN /DNOMINMAX /I "%VULKAN_SDK%\Include" /I "$src\src" "$src\test\vk_store_test.cpp" /Fe:vk_store_test.exe || exit /b 1
if defined VULKAN_SDK cl /nologo /std:c++20 /EHsc /O2 /DWIN32_LEAN_AND_MEAN /DNOMINMAX /I "%VULKAN_SDK%\Include" /I "$src\src" "$src\test\vk_image_fuzz.cpp" /Fe:vk_image_fuzz.exe || exit /b 1
"@
}

# A .cmd file avoids cmd.exe's quote stripping on paths with spaces.
# Switch to UTF-8 (CP 65001) before invoking CMake/Ninja/cl.exe so non-English MSVC /showIncludes prefixes
# (e.g. Spanish "Nota: inclusión del archivo:") match Ninja's msvc_deps_prefix instead of being mojibaked in OEM CP.
$bat = Join-Path $out "_build.cmd"
# Keep the paths encoded for CP 65001 and start with an ASCII command that cmd.exe can read before switching.
$batchText = "@echo off`r`nchcp 65001 >nul`r`nset `"PATH=$installer;%PATH%`"`r`ncall `"$vcvars`" >nul || exit /b 1`r`n$body`r`n"
[System.IO.File]::WriteAllText($bat, $batchText, [System.Text.UTF8Encoding]::new($false))

# /s with a second pair of quotes: cmd.exe strips only the outer pair, so a path holding & or ^ stays quoted.
$p = Start-Process cmd.exe -ArgumentList "/s", "/c", "`"`"$bat`"`"" -NoNewWindow -PassThru
$null = $p.Handle  # keeps the exit code readable after WaitForExit
try { $p.PriorityClass = "BelowNormal" } catch {}
$p.WaitForExit()
if ($p.ExitCode -ne 0) { throw "build failed ($($p.ExitCode))" }
Write-Host "built $out\gta6rt.exe"

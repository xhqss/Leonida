# Running the GPU tests

The GPU tests are small hand-written RDNA2 programs in `runtime/test/shaders`. They are translated to SPIR-V and run
on your GPU, and the results are compared with what the RDNA2 hardware gives. They need no game files, and every
result helps: post it in [#166](https://github.com/ItsNotRexed/Leonida/issues/166), one comment per GPU and driver.
If your GPU is already covered there, testing an open pull request from the review queue in the [coordination
issue][claims] helps more.

This page collects the setup steps and the problems testers have run into so far.

[claims]: https://github.com/ItsNotRexed/Leonida/issues?q=is%3Aissue%20is%3Aopen%20in%3Atitle%20%22Coordinating%20who%20works%20on%20what%22

## What you need

- **Python 3.11+.** `scripts/run_gpu_tests.py` needs at least 3.10.
- **`llvm-mc` with the AMDGPU target** (LLVM 21+). The official LLVM Windows release has no AMDGPU target. On
  Windows, use one of:
  - `scripts\build_llvm_mc.ps1`, which builds a native `llvm-mc.exe` (needs git and the Visual Studio C++ tools);
  - conda-forge's `llvm-tools` package, whose Windows build includes the AMDGPU target;
  - `llvm-21` in WSL, with `LLVM_MC` set to the whole command:
    `LLVM_MC="wsl -d Ubuntu -e /usr/lib/llvm-21/bin/llvm-mc"`.

  On Linux and macOS, the distribution's or Homebrew's `llvm-mc` on `PATH` is enough, if it is 21 or newer.

  An older `llvm-mc` is not enough for everything. Ubuntu 24.04's `llvm-18` (18.1.3) runs `run_gpu_tests.py`,
  `valu_fuzz.py --family mem` and `lds_fuzz.py`, but rejects `op_sel` on the 16-bit integer VOP3 forms that
  `valu_fuzz.py --family f16` generates (`v_ashrrev_i16_e64 ... op_sel:[0,0,0]`: "not a valid operand"), so no f16
  program assembles. 23.1.6 assembles them.

  On Windows, ROCm 7.2's bundled `llvm-mc.exe` reports LLVM 21.0.0git. It assembled the TEST-71 memory and
  LDS programs but rejected the 16-bit shift used by the f16 generator. To check an assembler directly in
  PowerShell, set `$env:LLVM_MC` to the path of its executable and run:

  ```powershell
  'v_lshlrev_b16_e64 v34, v27, v27 op_sel:[0,1,1]' | & $env:LLVM_MC -triple=amdgcn--amdpal -mcpu=gfx1013 -show-encoding
  ```

  If it reports `not a valid operand`, point `LLVM_MC` at a build that accepts this instruction. The Windows
  conda-forge `llvm-tools` 23.1.6 package assembled the same f16 programs.
- **SPIRV-Tools** (`spirv-as`, `spirv-val`): part of the Vulkan SDK, or a distribution package.
- **A C++20 compiler, CMake and the Vulkan headers** to build the test programs:
  - Windows: the full runtime needs Visual Studio 2022 with "Desktop development with C++" and Windows SDK
    10.0.26100 or newer. Install the Vulkan SDK for the backend headers. The standalone tools also compile with llvm-mingw and the Vulkan headers alone.
    Running the GPU tests needs a Vulkan loader and a compatible driver.
  - Linux: CMake 3.24+, GCC or Clang, and the Vulkan headers and loader. The headers should be recent enough
    for `VK_KHR_maintenance8`; older ones still build, but skip what needs it.
  - macOS: `brew install llvm molten-vk vulkan-headers vulkan-loader spirv-tools vulkan-tools`. The test programs
    load MoltenVK directly; set `LEONIDA_VULKAN_LOADER` to use the Vulkan loader instead.

## Build the test programs

Windows, from a "Developer PowerShell for VS 2022" or a plain shell:

```powershell
scripts\build_runtime.ps1            # CMake + Ninja into runtime\build
```

For the standalone Vulkan tools, put llvm-mingw on `PATH` and point CMake at the directory containing `vulkan/vulkan.h`:

```powershell
cmake -S runtime -B runtime/build/vulkan -G Ninja -DCMAKE_CXX_COMPILER=clang++ -DLEONIDA_BUILD_WINDOWS_RUNTIME=OFF -DVulkan_INCLUDE_DIR=C:/Vulkan-Headers/include
cmake --build runtime/build/vulkan --parallel --target vk_harness vk_texture_test vk_sampler_test vk_compute_test vk_store_test vk_bindless_test
```

Pass `--build runtime/build/vulkan` to `scripts/run_gpu_tests.py`.

If you configure with CMake's Visual Studio generator instead, the programs end up in `runtime\build\Release` (or
`RelWithDebInfo`); pass that folder to `--build` below.

Linux and macOS:

```sh
cmake -S runtime -B runtime/build/linux
cmake --build runtime/build/linux --parallel
```

On macOS, add `-DVulkan_INCLUDE_DIR=/opt/homebrew/include` to the first command.

The test programs and `scripts/run_gpu_tests.py` should come from the same commit. Some checks tolerate an older
binary (`unpinned_subgroup_skip`), but mixing them can report false failures in the compute tests: 28 `cs_*`
failures on both an RTX 5070 Ti Laptop and a Radeon 610M with the `426023a` script and `48ad23f` binaries, where
either matching pair is clean. If you cannot build locally, the `runtime-windows` artifact of the Runtime workflow
belongs to the commit that built it; check that commit out as well.

### Without a compiler: a CI-built runtime

If you cannot build the test programs locally, the Runtime workflow publishes them as the `runtime-windows`
artifact of its own commit. It holds Windows executables, so use them on Windows or under Wine as in the
section below; GitHub expires the artifact after 7 days. Not every commit on `main` has one: the workflow runs
only when a commit touches what it builds or reads (the `paths` list in `.github/workflows/runtime.yml`), so a
commit that changes only docs, tests or other tools has no Runtime run. Take the most recent run under
Actions -> Runtime, and check out the commit it built, or start a run of the commit you want on your own fork with
"Run workflow" (`workflow_dispatch`). Download and unzip `runtime-windows`, and pass the unzipped folder to `--build`:

```sh
python -I scripts/run_gpu_tests.py --build <unzipped runtime-windows>
```

`build.py` and `check.py` take the same folder as their second argument:

```sh
python -I runtime/test/shaders/build.py <out dir>
python -I runtime/test/shaders/check.py <out dir> <unzipped runtime-windows>
```

The artifact belongs to the commit that built it, so run the scripts from that commit as well (see the note above).

## Run them

The whole suite, assembled, translated and run in one go:

```sh
python -I scripts/run_gpu_tests.py --build runtime/build/linux     # Windows: the default runtime\build
```

`--filter <text>` runs only matching tests, and `--work <dir>` keeps the intermediate files.
Add `--require-pass` when verifying a specific path: an all-skipped run exits 1 instead of 0.
Skips still appear as `SKIP`, and individual skips are allowed if at least one test passes and none fail.

A GPU test program that runs longer than 120 seconds is killed and counted as a failure, so a hung driver does
not stall the suite. `LEONIDA_GPU_TEST_TIMEOUT` sets the limit in seconds for both `check.py` and
`run_gpu_tests.py`; `0` turns the limit off.
**Rebuild the test programs after pulling.** The programs carry the V# and push-constant layout the translator expects. An executable from an older commit still loads and runs every shader, so a stale `vk_compute_test` does not fail loudly: when a newer translator starts enforcing a descriptor rule the old program does not set up (the raw V# in `pc[2..3]`, for one), every compute test reports the untouched `0xEEEEEEEE` fill pattern and the run looks like a translator regression. If all the compute tests in a run return that pattern, rebuild first (`cmake --build runtime/build/linux`, or `cmake --build runtime\build` on Windows) and run again before reading anything into the words.

The two-step form, which `CONTRIBUTING.md` describes:

```sh
python -I runtime/test/shaders/build.py <out dir>
python -I runtime/test/shaders/check.py <out dir> <folder with the test programs>
```

For the standalone `v_mullit_f32` readback, build the full-screen triangle and the
hand-written semantic fixture, then run 120 draws with runtime SGPR inputs.
The driver checks every pixel against an integer product rounded to 24 bits.
It reports the input words, pixel and channel on the first mismatch.

```sh
python -I runtime/test/shaders/build.py <out-dir> vs_ids ps_mullit
python -I runtime/test/shaders/mullit_readback.py <out-dir> <harness-directory>
```

For D3D12/runtime tests, `LEONIDA_VK_VALIDATION=1` enables `VK_LAYER_KHRONOS_validation`. When running with validation, set `DISABLE_RTSS_LAYER=1` and `DISABLE_VULKAN_OBS_CAPTURE=1` if MSI Afterburner (RivaTuner) or OBS is installed: their overlay layers can add unsupported swapchain usage, and in an elevated (administrator) terminal the loader ignores `VK_LOADER_LAYERS_DISABLE=~implicit~` (see "Known problems").

The shader test programs (`vk_harness`, `vk_compute_test`, `vk_store_test`, `vk_texture_test`, `vk_bindless_test`,
`vk_sampler_test`, `vk_image_fuzz`) don't read `LEONIDA_VK_VALIDATION`. To check one, load the layer through the
loader and run it by hand, for example
`VK_INSTANCE_LAYERS=VK_LAYER_KHRONOS_validation VK_KHRONOS_VALIDATION_GPUAV_ENABLE=1 vk_compute_test x.spv 1 dump`.
The layer prints its messages to stdout and the program's exit status doesn't change, and `run_gpu_tests.py` shows
only each test's last line, so read the program's own output. GPU-assisted validation (`..._GPUAV_ENABLE=1`)
instruments the translated shaders and reports what they actually do at run time: out-of-bounds buffer accesses, and
an image read through a view whose numeric type doesn't match the shader's (a T# FORMAT that disagrees with the
bound view). It runs on llvmpipe. `VK_KHRONOS_VALIDATION_VALIDATE_SYNC=1` adds synchronization validation.

## The Windows runtime on Linux, under Wine or Proton

The Linux build above makes only the Vulkan programs. The loader (`gta6rt`), its shims and their tests are Windows
programs; `runtime/cmake/clang-cl-xwin.cmake` cross-compiles them on Linux with clang-cl and lld-link (LLVM 19+, but not 22.1.7 or 22.1.8 (see Known problems): the
MSVC STL that xwin lays out rejects older Clang; versioned names go in with `-DCMAKE_CXX_COMPILER=clang-cl-19` etc.)
against an MSVC CRT and Windows SDK that [xwin](https://github.com/Jake-Shadle/xwin) downloads from Microsoft
(running it means accepting Microsoft's licence), and `ctest` runs them through Wine:

```sh
xwin --accept-license --sdk-version 10.0.26100 splat --use-winsysroot-style --preserve-ms-arch-notation \
     --output ~/.local/share/xwin-sdk
cmake -S runtime -B runtime/build/windows --toolchain "$PWD/runtime/cmake/clang-cl-xwin.cmake" \
      -DXWIN_DIR="$HOME/.local/share/xwin-sdk"
cmake --build runtime/build/windows --parallel
WINEPREFIX="$HOME/.local/share/leonida-wine" ctest --test-dir runtime/build/windows
```

Use a recent xwin: 0.10.0 lays out SDK 10.0.26100 with the command above, while 0.6.5 stops with "unable to find
x86_64 headers for Win11SDK_10.0.26100".

On a machine without a display (a server, a container, WSL without WSLg), Wine has no graphics driver to create a
Vulkan instance with, so every Vulkan test fails with `vkCreateInstance failed (VkResult -3)` and, with
`WINEDEBUG=+vulkan`, "Failed to load Wine graphics driver supporting Vulkan". Run the tests under a virtual X server,
as CI does: `WINEPREFIX="$HOME/.local/share/leonida-wine" xvfb-run -a ctest --test-dir runtime/build/windows`
(packages `xvfb` and `xauth`, which `xvfb-run` needs). With Mesa installed, the tests then run on llvmpipe.

The build type defaults to `RelWithDebInfo`: xwin leaves out the debug CRT, so `Debug` needs `--include-debug-libs`.
`make_game.py`'s `runtime` stage makes this same build on Linux when the tools and the SDK are there (`--runtime-build
runtime/build/windows` takes a folder made by hand like a Windows build). CMake packages are
looked up only inside the SDK folder, so a Windows SDL3 for the GameInput shim is passed as `-DSDL3_DIR=<its
cmake folder>` (`CMAKE_PREFIX_PATH` is not searched here).

For Proton, start any program once with `proton run` (with `STEAM_COMPAT_DATA_PATH` and
`STEAM_COMPAT_CLIENT_INSTALL_PATH` set), so that it puts vkd3d-proton and DXVK into the prefix, then configure with
`-DCMAKE_CROSSCOMPILING_EMULATOR="<Proton>/files/bin/wine"` and run `ctest` with
`WINEPREFIX=$STEAM_COMPAT_DATA_PATH/pfx WINEDLLOVERRIDES="d3d12=n;d3d12core=n;dxgi=n"`. `proton run` itself doesn't
pass the program's output on, so the tests can't run through it.

The tests that compare with the host's D3D12 (`root_sig_test`, `root_sig`, `device_proxy`, `d3d12_vulkan_device`)
check against Wine's vkd3d or vkd3d-proton here, not Microsoft's runtime, so some of them fail for that reason.

## GPUs with 32-wide subgroups (NVIDIA, Intel, Apple)

The game's shaders are mostly wave64. A GPU that can't run 64-wide compute subgroups skips the wave64 tests
(`cs_wavemask`, `cs_wave64_ballot`, `cs_wm_*`, `cs_w64x_*`, `ms_w64x_ballot`); that's expected, not a failure.

To test the wave64 emulation that makes them exact on these GPUs, set `LEONIDA_W64_EMULATE=1`, **for both
`build.py` and `check.py`**: `build.py` translates with emulation only when it's set. If you set it only for
`check.py`, the shaders are checked against the emulated expectations without being emulated, and every wave64 test
fails.

`ps_bitmask_perm` is a wave32 pixel shader, and a pixel shader's width can't be requested on every device. Intel
drivers report subgroups of 8..32 and may run it 8 or 16 wide; its second pattern reads lanes 16-31, which then
don't run. The test skips when the frame shows that (#1318), and the translation gives 0 for a source lane that
isn't running instead of an undefined value (#618). At 16 wide the first pattern stays inside the running lanes, so
it is still checked before the skip.
The wave32 tests whose words depend on 32 lanes (`cs_dpp*`, `cs_bpermute32`, `cs_exec_const`, `cs_wqm_w32`,
`cs_w32_*`, and in `check.py` every compute test the index records at 32) ask `vk_compute_test` for 32 lanes
(`VKC_SUBGROUP_SIZE=32`). MoltenVK can't require a subgroup size (`requiredSubgroupSizeStages` is 0, sizes 4..32),
but its default is 32 and the translator's SPIR-V 1.5 doesn't allow varying sizes, so Vulkan fixes the size at 32.
`vk_compute_test` checks that with a probe shader before the test (where the device has compute ballots): same
local size and flags as the test's `main`, every subgroup 32 lanes wide by its own lane numbers and ballot, and the
lanes in invocation order, which the translator assumes. It prints `compute subgroup size: 32 (observed: ...)` and
the test is judged. A wrong width fails the test with `observed wrong` and the first wrong invocation; 32-lane
subgroups in another order print `not available`, a skip. Measured
on an Apple M3 Max (MoltenVK 1.4.2 and 1.4.3): 17 workgroup shapes from 1 to 1024 invocations, partial last
subgroups, with and without full subgroups and register pressure, all 32 lanes in order. A device whose default
isn't the requested size still prints `not available`, and those tests skip.

## Wave64 pixel, vertex and geometry shaders (`w64_graphics.py`)

Graphics stages have no emulation: on a 32-wide subgroup a wave64 VS, PS or GS runs as two 32-lane halves, and the
translator marks each one tier A, B or C by whether that can change its results (`docs/design.md`, "Wave64 graphics:
partition invariance"). `runtime/test/shaders/w64_graphics.py` checks those tiers on a GPU by running each `ps_w64_*`,
`vs_w64_*` and `gs_w64_*` fixture at wave64 and at wave32: tier A and B must give the same words both ways, tier C
must differ. CI has no GPU, and the CPU wave interpreter (`tests/semantic/test_wave64_halves.py`) runs vertex shaders
only, so this comparison needs real hardware.

Vulkan can require a subgroup size for pixel shaders (`VKH_SUBGROUP_SIZE`) but not for vertex or geometry shaders, so
those two use a RADV switch instead: on GFX10 and newer, RADV runs them as wave64 by default and as wave32 with
`RADV_PERFTEST=gewave32`. The runner sets it itself and checks the result with a probe fixture per stage
(`vs_w64_c_lane_id`, `gs_w64_c_lane_id`): the largest lane id must reach 32 at wave64 and stay below 32 at wave32. On
other drivers (AMD Windows, NVIDIA, Intel) the probe sees no change, and only the default-size checks run; tier C is
then a `SKIP`, not a failure.

Build and run it on Linux with RADV, pinned to one GPU (`LEONIDA_VK_DEVICE`, see "Choosing the GPU"):

```sh
cmake -S runtime -B runtime/build/linux -G Ninja && cmake --build runtime/build/linux --target vk_harness
python3 -I runtime/test/shaders/build.py /tmp/w64g
LEONIDA_VK_DEVICE=9070 python3 -I runtime/test/shaders/w64_graphics.py /tmp/w64g runtime/build/linux
LEONIDA_VK_DEVICE=9070 python3 -I runtime/test/shaders/w64_graphics.py /tmp/w64g runtime/build/linux gs_w64_a_ring
```

Each line prints `ok`, a `SKIP` with its reason, or what differed. To add a fixture:

1. Write `runtime/test/shaders/{ps,vs,gs}_w64_<tier>_<name>.asm` with `; mattr: +wavefrontsize64,-wavefrontsize32`
   and a header that says what every result word is. Vertex and geometry fixtures store four words per vertex or
   per triangle to the heap record at `0x1000 + 16 * i`, and every word of a tier A or B fixture is a function of
   `i` alone, never of the lane (copy the store from `vs_w64_a_skip` or `gs_w64_a_ring`).
2. Add it to `FIXTURES` in `w64_graphics.py` with its tier and a function that gives the expected words for record
   `i` (`None` for a word that may depend on the wave size).
3. Add it to `EXPECT` in `tests/test_wave64_halves_fixtures.py` with the tier, reasons and idioms the translator
   should give it, and run `python -m pytest tests/test_wave64_halves_fixtures.py`.
4. Run `w64_graphics.py` on a RADV GPU and say which GPU and Mesa version in the pull request.
## GPUs with 128 bytes of push constants (AMD RDNA2 iGPUs on Windows, some Intel Arc drivers)

The translated shaders take 144 bytes of push constants: s0..s31, then the heap base and the code base. A device whose
`maxPushConstantsSize` is 128, Vulkan's minimum, can't take that, so the texture tests and every test drawn through
`vk_harness` skip there (`unsupported: need 144 push constant bytes, device has 128`); `vulkaninfo` shows the limit.
AMD's Windows driver reports 128 on RDNA2 iGPUs (Radeon 680M, 610M, the Ryzen 7000 iGPU); RADV reports more on the
same chips.

To run them anyway, set `LEONIDA_PC_TAIL_UBO=1` **for both `build.py` and `check.py`** (or for
`run_gpu_tests.py`). The shaders are then translated with `--pc-tail-ubo`: s0..s31 stay in 128 push-constant bytes,
and the heap base and code base move to a uniform block at set 1 (`docs/design.md`, "Push-constant tail as a uniform
block"), which `vk_compute_test`, `vk_store_test`, `vk_harness`, `vk_texture_test`, `vk_sampler_test` and
`vk_bindless_test` bind (`runtime/test/vk_pc_tail.h`). If you set it
only for `check.py`, the shaders read the tail from push constants the programs no longer declare. `--mesh-compute`
(`LEONIDA_MESH_COMPUTE=1`) isn't combined with it yet: its vertex shader still reads the output address from push
constants, so those tests skip.

## GPUs with 128 bytes of push constants (AMD RDNA2 iGPUs on Windows, some Intel Arc drivers)

The translated shaders take 144 bytes of push constants: s0..s31, then the heap base and the code base. A device whose
`maxPushConstantsSize` is 128, Vulkan's minimum, can't take that, so the texture tests and every test drawn through
`vk_harness` skip there (`unsupported: need 144 push constant bytes, device has 128`); `vulkaninfo` shows the limit.
AMD's Windows driver reports 128 on RDNA2 iGPUs (Radeon 680M, 610M, the Ryzen 7000 iGPU); RADV reports more on the
same chips.

To run them anyway, set `LEONIDA_PC_TAIL_UBO=1` **for both `build.py` and `check.py`** (or for
`run_gpu_tests.py`). The shaders are then translated with `--pc-tail-ubo`: s0..s31 stay in 128 push-constant bytes,
and the heap base and code base move to a uniform block at set 1 (`docs/design.md`, "Push-constant tail as a uniform
block"), which `vk_compute_test`, `vk_store_test`, `vk_harness`, `vk_texture_test`, `vk_sampler_test` and
`vk_bindless_test` bind (`runtime/test/vk_pc_tail.h`). If you set it
only for `check.py`, the shaders read the tail from push constants the programs no longer declare. `--mesh-compute`
(`LEONIDA_MESH_COMPUTE=1`) isn't combined with it yet: its vertex shader still reads the output address from push
constants, so those tests skip.

## GPUs without mesh shaders (NVIDIA Pascal and older)

Without `VK_EXT_mesh_shader`, the `ms_*` tests are skipped; that's expected, not a failure. To test the path that
runs them anyway, set `LEONIDA_MESH_COMPUTE=1` for `run_gpu_tests.py`, together with `LEONIDA_W64_EMULATE=1` on a
32-wide GPU (every mesh shader is wave64). The mesh shaders are then translated with `--mesh-compute`, dispatched as
compute and drawn by a generated vertex shader (`docs/design.md`, "Mesh shaders as compute"), so the device needs
`bufferDeviceAddress` and `shaderInt64` instead.

## Without a GPU: Mesa llvmpipe

Mesa's llvmpipe is a software Vulkan driver (`mesa-vulkan-drivers` on Debian and Ubuntu). CI runs the suite on it
for every pull request that touches the runtime, the scripts or a tool the jobs read (`.github/workflows/runtime.yml`
lists them), and you can do the same on Linux without a GPU:

```sh
sudo apt-get install -y libvulkan-dev mesa-vulkan-drivers vulkan-tools spirv-tools llvm-18
cmake -S runtime -B runtime/build/linux && cmake --build runtime/build/linux --parallel
python -I scripts/run_gpu_tests.py --build runtime/build/linux --llvm-mc /usr/lib/llvm-18/bin/llvm-mc
LEONIDA_W64_EMULATE=1 python -I scripts/run_gpu_tests.py --build runtime/build/linux \
    --llvm-mc /usr/lib/llvm-18/bin/llvm-mc --filter cs_
LEONIDA_BARY_FALLBACK=1 python -I scripts/run_gpu_tests.py --build runtime/build/linux \
    --llvm-mc /usr/lib/llvm-18/bin/llvm-mc
```

With Mesa 25.2 (llvmpipe, LLVM 20) the first run passes 52 of 88 and skips 36, the second passes 37 of 39 and
skips 2. The skips are expected: llvmpipe has 8-wide subgroups (so every wave64 test skips without emulation, and
the 32-lane tests `ps_bitmask_perm`, `cs_wqm_w32` and `cs_w32_lane_wrap` skip in both runs), no
`VK_KHR_fragment_shader_barycentric` (`ps_bary`, `ps_uniform_branch`, `ps_scc_mask32`) and no
`VK_KHR_maintenance8` (`ps_texoff`). The third run draws those three barycentric tests through the fallback below
(with Mesa 26.0.8 it passes 123 of 189, the first run 120), and CI fails it unless every shader that got a fallback
geometry shader passes. With
`LEONIDA_W64_EMULATE=1` the mesh tests abort inside llvmpipe's compiler ("LLVM ERROR: Cannot emit physreg copy
instruction"), which is why the second run is limited to compute. llvmpipe is not a substitute for a result from a
real GPU in [#166](https://github.com/ItsNotRexed/Leonida/issues/166): it catches regressions, not hardware
differences.

## GPUs without fragment barycentrics (NVIDIA Volta, Pascal and older)

`ps_bary`, `ps_uniform_branch` and `ps_scc_mask32` read raw barycentrics, which need
`VK_KHR_fragment_shader_barycentric`. NVIDIA has it from Turing on; Volta (V100, Titan V) and older lack it, as do
llvmpipe and some other drivers. Where the device lacks it those three are skipped; that's expected, not a failure.
To test the fallback that runs them anyway, set `LEONIDA_BARY_FALLBACK=1`, **for both `build.py` and `check.py`**
(or for `run_gpu_tests.py`; CI does this on llvmpipe). The shaders are then translated with `--bary-fallback` and
drawn through a pass-through geometry shader that supplies the barycentrics (`docs/design.md`, "Barycentric
fallback"), so the device needs geometry shaders instead.

`ps_interp_f16_high` and `ms_f16_pack` read 16-bit interpolants as `PerVertexKHR` inputs, which need the extension
too and which `--bary-fallback` can't express. With `LEONIDA_F16_INTERP_FALLBACK=1` they read halves the producer
exports decoded instead (see Macs, below) and run with or without the bary fallback; `run_gpu_tests.py` turns that
on by itself where the device lacks the extension. Under the bary fallback without it they skip.

## Macs: 16-bit interpolation without PerVertexKHR

`ps_interp_f16_high` (and `ms_f16_pack`) normally read 16-bit interpolants through `PerVertexKHR` inputs, which
MoltenVK can't convert to Metal ("PerVertexKHR decoration is not supported in MSL"). `run_gpu_tests.py` asks
`tools/vk_subgroup_info.py` whether the device can take them and, on MoltenVK, translates with
`--f16-interp-fallback` by itself: the vertex or mesh shader then also exports the decoded f16 halves
(`docs/design.md`, "16-bit interpolation without `PerVertexKHR`"). `LEONIDA_F16_INTERP_FALLBACK=1` or `0` forces it
on or off; `build.py` (for `check.py`) translates with the flag only when that variable is `1`. `ms_f16_pack` also runs
on a Mac with `LEONIDA_MESH_COMPUTE=1 LEONIDA_W64_EMULATE=1`.

## GPUs without VK_KHR_maintenance8 (NVIDIA Pascal and older)

`ps_texoff` and `ps_texoff_implicit` sample with a run-time texel offset, which needs `VK_KHR_maintenance8`;
without it they're skipped, which is expected, not a failure. To test the fallback, set `LEONIDA_TEXOFF_FALLBACK=1`
**for both `build.py` and `check.py`** (or for `run_gpu_tests.py`): the offset is then folded into the coordinate
(`docs/design.md`, "Texel offsets"), and the +16/-32 sample is judged on every device.

## Fused multiply-add: GLSL Fma or OpFmaKHR

`cs_fma_fused` checks that `v_fma_f32`, `v_fmac_f32`, `v_fmaak_f32`, `v_fmamk_f32` (and one `v_fma_f16`) round once, on
operands taken from user SGPRs so the driver cannot fold them. Run it with default `--fma glsl` (GLSL.std.450 `Fma`) or
with `LEONIDA_FMA=khr` **for `build.py`** (or for `run_gpu_tests.py`), which emits `OpFmaKHR`.
That needs `VK_KHR_shader_fma` with `shaderFmaFloat32`; the test programs enable it when the device has it and exit
77 (a SKIP) for a module that needs it on a device without it. The Vulkan specification allows a driver to split GLSL
`Fma` into two roundings, which is why `khr` exists: on AMD Windows (26.11.2) and NVIDIA (617.42) GLSL `Fma` stayed
fused, whereas **Mesa RADV (26.2.6-arch3.1, AMD Radeon RX 6800, Linux)** splits 5 of the 7 f32 ops under `--fma glsl`
(reusing the live `a * a` product in `v3`) and returns 8/8 fused exact under `LEONIDA_FMA=khr`. Under default
`--fma glsl`, `check.py` and `run_gpu_tests.py` therefore accept either the fused or the exact split word for each of
fused, whereas **Mesa RADV (Linux)** splits several of the 7 f32 ops under `--fma glsl` — the exact split count varies
by GPU, Mesa/ACO version, and execution modes (4 of 7 on RX 6800 / Mesa 26.2.6-arch3.1 and RX 7600 / Mesa 27.0.0-devel
with `SignedZeroInfNanPreserve 32`, 5 of 7 on RX 6800 before float controls, 7 of 7 on Radeon Pro V540 RDNA1 / Mesa
25.2.8) — while returning 8/8 fused exact under `LEONIDA_FMA=khr` where supported. Under default `--fma glsl`,
`check.py` and `run_gpu_tests.py` therefore accept either the fused or the exact split word independently for each of
the 7 f32 outputs while still requiring exact f16 round-to-odd (`0x00007351`); under `LEONIDA_FMA=khr`, all 8 words
must be strictly fused.
operands taken from user SGPRs so the driver cannot fold them. It passes with the default translation (`--fma glsl`,
GLSL.std.450 `Fma`) on a driver that keeps `Fma` fused, and with `LEONIDA_FMA=khr` **for `build.py`** (or for
`run_gpu_tests.py`), which emits `OpFmaKHR`. That needs `VK_KHR_shader_fma` with `shaderFmaFloat32`; the test programs
enable it when the device has it and exit 77 (a SKIP) for a module that needs it on a device without it. The Vulkan
specification allows a driver to split GLSL `Fma` into two roundings, which is why `khr` exists. The AMD Windows
(26.11.2) and NVIDIA (617.42) drivers we tried don't. Two do, failing `cs_fma_fused` with `glsl` in words 0, 2, 3 and 5
(`v_fma_f32`, `v_fmaak_f32`, `v_fmac_f32`), which get the split value:

- RADV (Mesa 26.3.1, RX 6700 XT, measured in #3004's review). It has `VK_KHR_shader_fma` since Mesa 26.2 (#1364), so
  `khr` fixes it, and so does `exact`.
- The Intel UHD Graphics driver (Xe-LP, Windows 101.7088). It has no `VK_KHR_shader_fma`, so only `exact` fixes it.

`LEONIDA_FMA=exact` (`--fma exact`) builds the single rounding from plain f32 operations and needs no extension; with
it `cs_fma_fused` passes on both of those, on an RTX 3050 Laptop (617.42), an RTX 5060 Laptop (621.01), an RTX 5070 Ti
(621.24) and a Radeon 780M (26.11.2).

## Hardware oracle (optional, AMD)

`valu_fuzz.py`, `lds_fuzz.py`, `f32_fuzz.py` and `image_fuzz.py` take `--oracle hw`: the same programs run as native
compute kernels on an AMD GPU (`tools/hw_oracle/`; setup in [shader_validation.md](shader_validation.md), "Hardware
oracle"). It needs AMD clang with the amdgcn target (`LEONIDA_AMD_CLANG`), the AMD driver's HIP runtime and a host C
compiler on Windows, or the libdrm launcher on Linux. Without them a run prints `hardware oracle unavailable: ...` and
exits 77, so CI skips it. `image_fuzz.py --oracle hw` loads a linear texture through a T# built for the GPU's family
(GFX11 on gfx11xx, GFX10 on gfx103x; `tests/test_image_fuzz_hw.py` has the exact words) and compares every texel with
its CPU model; add `--negative-control` to check that wrong models are caught. A gfx1150 (RDNA3.5) is not RDNA2: a
match supports the model and does not prove RDNA2. Run these one at a time: a second job on the same GPU can hang it.

## Choosing the GPU

With more than one GPU, pick one through the Vulkan loader, for example
`VK_LOADER_DRIVERS_SELECT=*amd*` or `VK_LOADER_DRIVERS_SELECT=nv-vk*` (or `VK_DRIVER_FILES=<path to the ICD json>`).
`vulkaninfo --summary` shows which devices the loader then sees.

Two GPUs of one vendor share one driver, so the loader cannot tell them apart (an RTX 5070 Ti next to an RTX 3060,
say; without a choice the runtime and every test take the same one: a native driver over Microsoft's Dozen, discrete
over integrated, the first on a tie, `vulkan_device_rank` in `runtime/src/vulkan_device_filter.h`).
`LEONIDA_VK_DEVICE` names the GPU by part of its name, without case: `LEONIDA_VK_DEVICE=5070` or
`LEONIDA_VK_DEVICE=radeon`. The D3D12 tests (`vk_test_device.h`) and the layer's standalone device read it,
and a value that matches no device fails the run instead of testing
another GPU. For the host side of the comparison tests, `LEONIDA_HOST_ADAPTER` picks the D3D12 adapter the same way.
The GPU tools of `runtime/test/shaders` (`vk_compute_test`, `vk_harness`, ...) read it the same way through
`vk_device_pick.h`, and fail with a message naming the filter when it matches no device.

## Known problems

| What you see | Cause | What to do |
|---|---|---|
| `ctest` passes but runs far fewer tests than expected (about 45 instead of 125 on Windows), no `d3d12_*` GPU tests, and `LEONIDA_D3D12=vulkan` does nothing | CMake found no Vulkan headers: `VULKAN_SDK` was not set in the environment that ran CMake (for example a user variable set after the terminal opened), and `CMakeCache.txt` keeps that failed lookup. The configure prints a `CMake Warning` "Vulkan headers not found" | Set `VULKAN_SDK` in that environment (or pass `-DLEONIDA_VULKAN_HEADERS_FETCH=ON`), delete `CMakeCache.txt` and configure again. `-DLEONIDA_REQUIRE_VULKAN=ON` makes this a configure error; CI and `scripts/build_runtime.ps1` (with `VULKAN_SDK` set) pass it |
| Windows refuses to start freshly built test programs ("An Application Control policy has blocked this file"), and the runner stops with a traceback | Smart App Control on Windows 11 | Turn Smart App Control off for the run |
| Validation errors without a VUID, or messages from a layer you didn't ask for | An implicit Vulkan layer from other software (Rockstar Social Club, ReShade, MangoHud, an overlay) | Set `VK_LOADER_LAYERS_DISABLE=~implicit~`. Overlays that hook the swapchain (RivaTuner `VK_LAYER_RTSS`, OBS `VK_LAYER_OBS_HOOK`) can stay loaded: see the next row |
| Validation errors that only your machine shows (e.g. `xbox_present`'s sRGB swapchain `STORAGE` message), with MSI Afterburner/RivaTuner or OBS installed | An overlay layer that hooks the swapchain: while RivaTuner Statistics Server is running, its `VK_LAYER_RTSS` adds `STORAGE` usage to it (OBS's `VK_LAYER_OBS_HOOK` injects `VK_IMAGE_USAGE_STORAGE_BIT` too). In an elevated (administrator) terminal the loader ignores `VK_LOADER_LAYERS_DISABLE` (`VK_LOADER_DEBUG=all` says so), so `~implicit~` leaves the layer in | Run the tests from a normal terminal, close the overlay application, or set the layer's own switch, which works elevated too: `DISABLE_RTSS_LAYER=1`, `DISABLE_VULKAN_OBS_CAPTURE=1` (OBS). Any other layer's switch is the `disable_environment` entry in its manifest (the JSON listed under `HKLM\SOFTWARE\Khronos\Vulkan\ImplicitLayers` or `HKCU\...`). `VK_LOADER_DEBUG=layer` prints the layers that were actually inserted |
| `vk_sync_validation` fails with `LEONIDA_VK_VALIDATION=1` ("the write-after-write hazard was not reported") | Synchronization validation is off although the tests ask for it: an older layer, or a layer setting such as `VK_KHRONOS_VALIDATION_VALIDATE_SYNC=false` or a `vk_layer_settings.txt` that overrides it. Every other GPU test then reports "0 validation errors" without having been checked | Remove the override or update the Vulkan SDK, then rerun `ctest -R vk_sync_validation` until it passes |
| On RADV with Mesa 25.2 (Ubuntu 24.04's default; 25.2.8 measured) or 25.3 (from the source, not run), `cs_w32_salu64` fails with word 2 = `0` (want `0xFFFFFFFF`) and `cs_signed_zero_nan` fails with word 5 = `0` (want `1`) | Two Mesa bugs, not translator bugs (#166). `cs_w32_salu64`: ACO's `combine_add_sub_b2i` reads a temporary without counting the use, so dead-code elimination removes it (`ACO ERROR: ... never defined` with `MESA_SHADER_CACHE_DISABLE=true`); `RADV_DEBUG=llvm` passes. `cs_signed_zero_nan`: NIR's range analysis (`fmul_is_a_number`) calls `0 * x` a number when `x` may be infinite, so the `x != x` check folds to false despite `SignedZeroInfNanPreserve`. The same NIR code is behind llvmpipe's word 5 (#664) | Expected with these Mesa versions. From the source, the ACO function is gone from Mesa 26.0 and the NIR code was replaced in 26.1 (neither run); RADV from Mesa 26.3.1 passes both on an 8060S (gfx1151) |
| Every test crashes inside `libnvidia-eglcore.so` | NVIDIA's 535 server driver (Ubuntu 24.04's default) | Use driver 615 or newer |
| `build.py`: 0 translated, reason `.text` | An old `build.py` with LLVM 19 | Update to current `main` |
| `vk_harness` doesn't compile: `VK_KHR_maintenance8` names unknown | Vulkan headers older than the SDK the code was written against | Update to current `main` (the code now builds without it), or use current Khronos Vulkan-Headers; conda-forge's package is too old |
| `ps_bary` and `ps_uniform_branch` fail by about 0.9 | The GPU runs pixel shaders wave64 without fragment barycentrics (GCN3 and RDNA1 on RADV), or an old AMD Windows driver (22.40) | Current `main` skips them on such devices; on Windows, update the driver |
| `cs_w32_salu64` gives eight wrong words on RDNA1 under RADV Mesa 25.2.8 (`0x1E6A2C48 0x1234 0 0x100 47 48 0xFFFF8000 0xFFFFFFFE`, where word 2 should be `0xFFFFFFFF`) | Only the high word of the `s_bfe_i64` result is wrong. The same words appear on four pinned V540s (Navi 12, PCI `1002:7360`) and on a clean upstream/main worktree, and three different valid SPIR-V sign-extension formulations give the same result while the CPU interpreter matches, so this is the driver version rather than the translator (SHADER-276) | Expected with Mesa 25.2.8; the fixture passes on NVIDIA and on AMD's Windows driver. Try a newer Mesa before treating it as a translator bug; do not weaken the fixture's expectation |
| `d3d12_typeless_rtv` fails its WARP comparison on an AMD desktop iGPU (TEST-591) | [Reported on the Ryzen 5 7600X iGPU with Windows 10 build 19045 and AMD 23.40.02](https://github.com/ItsNotRexed/Leonida/issues/166#issuecomment-7083403389). The exact cause is still open. | Retest on a newer Windows and AMD driver combination. Passing results were reported on the [Ryzen 9 7900 iGPU with AMD 25.10.30.02](https://github.com/ItsNotRexed/Leonida/issues/3232#issuecomment-7085263408), a [gfx1036 iGPU with AMD 26.5.2](https://github.com/ItsNotRexed/Leonida/issues/2891#issuecomment-7084023801), and the [Ryzen 7 9800X3D iGPU with driver 32.0.21045.29003](https://github.com/ItsNotRexed/Leonida/pull/3440). |
| `ps_interp_f16_high` fails on a Mac: `PerVertexKHR decoration is not supported in MSL`, then `vkCreateGraphicsPipelines` -3 | MoltenVK can't convert `PerVertexKHR` inputs to Metal | Current `main` translates it with `--f16-interp-fallback` on MoltenVK (see "Macs: 16-bit interpolation") |
| `ps_vcmpx_forms` fails on a Mac: word 1 reads `0xf05e8e16`, want `0xf05e8a12` | MoltenVK's fast-math makes a compare of a NaN with itself true (SHADER-234) | Current `main`'s test programs turn it off (`MVK_CONFIG_FAST_MATH_ENABLED=0`, unless you set the variable yourself); rebuild them |
| On a Mac, a translated shader's GPU time is 1.5 to 16 times what it would be without the precise-arithmetic overhead | MoltenVK's SPIRV-Cross turns every `NoContraction` float add, subtract and multiply into a call to an `[[clang::optnone]]` helper that Metal does not inline. MoltenVK 1.4.2 and `main` up to at least `5189f8de` are affected (SHADER-234, [design notes](design.md#translator-status-and-runtime-contract)) | Expected with these MoltenVK versions, and the output is still exact. Before you time shaders on a Mac, build MoltenVK with SPIRV-Cross `98cbb7c0` or newer in `ExternalRevisions/SPIRV-Cross_repo_revision` (KhronosGroup/MoltenVK#2917 moves it to `57e4efcc`), and point `LEONIDA_VULKAN_LOADER` at its `libMoltenVK.dylib` |
| `d3d12_descriptor_test` reports that the set had room for fewer than 5001 2D views and 2100 samplers, and the device logs that the bindless set holds fewer samplers than a full shader-visible sampler heap | The device's descriptor limits, or its `vkGetDescriptorSetLayoutSupport`, allow no bigger bindless set ([d3d12_vulkan.md](d3d12_vulkan.md) section 5). MoltenVK's answer is ignored, since it rejects every layout of `maxPerSetDescriptors` (1212) or more although it runs the full set | Expected where the limits are low. The test still checks the last index of each array and that the next view and sampler are refused |
| `cs_dpp_ops` fails on a Mac with MoltenVK 1.4.2: `out[0..7] = 100 100 11 ...`, want `100 10 11 ...` | MoltenVK 1.4.2's SPIRV-Cross moves the DPP lowering's subgroup ballot and shuffle into a short-circuiting `&&` and `?:`, so only some lanes run them and lane 1 sees its source lane 0 as inactive. SPIRV-Cross `5c372ffb` (in MoltenVK `main`) keeps them out of `?:`; a ballot on the right of `&&` is still moved there in SPIRV-Cross `57e4efcc` | Expected with MoltenVK 1.4.2; it passes with MoltenVK `main` (point `LEONIDA_VULKAN_LOADER` at its `libMoltenVK.dylib`) and on KosmicKrisp |
| `check.py`'s `cs_fma_fused` fails on a Mac with MoltenVK 1.4.2: word 7 reads `15360` (`0x3c00`), want `29521` (`0x7351`) | The f16 multiply-add (round to odd). The MSL of 1.4.2 and of MoltenVK `main` differ only in SPIRV-Cross's precise-arithmetic helpers (`[[clang::optnone]]` and `fma(l, r, 0)` against `math_mode(safe)` and `fma(l, r, -0.0)`, the SHADER-182 / SHADER-234 change) | Expected with MoltenVK 1.4.2; it passes with MoltenVK `main` |
| `gs_tri` and `ms_tri` fail on a Mac | MoltenVK has no geometry stage and no `VK_EXT_mesh_shader` | Current `main` reports them as skipped |
| On a Mac, `valu_fuzz.py --family f16` reads `+0` where the model wants `-0`, from a multiply whose exact product is -0 (`v_mul_f16`, `v_pk_mul_f16`) or from an FMA whose product and addend are both -0 (`v_fma_f16`, `v_fmac_f16`, `v_pk_fma_f16`) | MoltenVK computes a `NoContraction` multiply as `fma(l, r, +0)`, which drops the sign. MoltenVK 1.4.2 and `main` up to at least `5189f8de` are affected (SHADER-182, [design notes](design.md#translator-status-and-runtime-contract)) | Expected with these MoltenVK versions. To run the rest of the fuzzer clean, add `--skip-op` for each of the five ops. To see them match, install Homebrew's `mesa` and use its KosmicKrisp driver: `LEONIDA_VULKAN_LOADER=/opt/homebrew/opt/vulkan-loader/lib/libvulkan.1.dylib VK_DRIVER_FILES=/opt/homebrew/share/vulkan/icd.d/kosmickrisp_mesa_icd.aarch64.json python3 -I tools/valu_fuzz.py --family f16 --build runtime/build/linux`. Or build MoltenVK with SPIRV-Cross `300681bb` or newer in `ExternalRevisions/SPIRV-Cross_repo_revision`, and point `LEONIDA_VULKAN_LOADER` at its `libMoltenVK.dylib` |
| CMake stops with "needs the Windows SDK 10.0.26100 or newer", or MSVC reports `'Desc_1_2': is not a member of 'D3D12_VERSIONED_ROOT_SIGNATURE_DESC'` | A Windows SDK older than 10.0.26100 (10.0.22621 is the common one), which has no version 1.2 root signatures | `winget install Microsoft.WindowsSDK.10.0.26100`, or add it in the Visual Studio Installer, then delete `runtime/build` and configure again |
| `ctest` lists `fsr_dispatch` and `bvh_builder` as failed (`Not Run`) after clang-cl hits an internal compiler error in `fsr_dispatch_test.cpp` and `bvh_builder_test.cpp` | clang-cl 22.1.8 on Fedora (Linux cross-build) and Windows, plus 22.1.7 on Windows | Use clang-cl 23.1.6 (both files compile on Windows) |
| `d3d12_compute`, `d3d12_draw` or another test that writes a shader library under the temp directory fails now and then (a PSO creation `line N: SUCCEEDED(...)`, or exit 3 with no message) but passes when run alone | Two copies running at once (`ctest -j`, or two suites sharing one Wine prefix or `TMPDIR`) used the same `leonida_<test>` folder under the temp directory, and one copy deleted the shader modules the other was loading: 6 of 40 runs failed with four copies of `d3d12_compute` at once on a Mac under Wine | Current `main` gives each process its own folder (`<temp>/leonida_<test>_<pid>`) |
| A Windows CI artifact run under Wine by hand: `d3d12_compute` and the other tests that read `LEONIDA_TEST_*_SPV` end with exit 3 and no message | The `.spv` paths are compiled in as `D:/a/Leonida/Leonida/runtime/build/generated/...`; the prefix has no `D:` drive and the test isn't run from the artifact folder | Run it from the artifact folder (the tests then find `generated/<name>.spv` there) or through `scripts/run_wine_tests.py`, which maps that drive for the run; current `main` names the file it couldn't copy (`cannot copy ...: File not found`) |
| Linux cross-build: CMake can't find `clang-cl-19` (or another versioned `clang-cl-<N>`) although `clang-19` and `lld-19` are installed | On Debian and Ubuntu, the versioned `clang-cl-<N>` link is in the `clang-tools-<N>` package, not in `clang-<N>` (`/usr/lib/llvm-<N>/bin/clang-cl` comes from it too); `lld-link-<N>`, `llvm-lib`, `llvm-rc` and `llvm-mt` come from `lld-<N>` and `llvm-<N>` | `sudo apt-get install clang-tools-19 lld-19 llvm-19` (Ubuntu 24.04 has 19 and 20), then pass the versioned names as in [the cross-build](#the-windows-runtime-on-linux-under-wine-or-proton) |
| Under Ubuntu's Wine 9.0, `wineboot --init` (and every Wine program after it) stops at once with `free(): invalid pointer` or a segmentation fault, before printing anything else; the prefix holds only a `wineserver` file | That Wine opens `/run/user/<uid>` for its server directory, whether `XDG_RUNTIME_DIR` is set or not, and aborts when the folder doesn't exist. The login manager makes it for a desktop session; a container or a shell started without a login session (a root shell in Docker, a CI runner image) has none | Create it before starting Wine: `mkdir -p /run/user/$(id -u) && chmod 700 /run/user/$(id -u)` (setting `XDG_RUNTIME_DIR` alone doesn't help), delete the half-made prefix, and run `wineboot --init` again |
| `run_gpu_tests.py` fails on a `dict \| None` annotation | Python older than 3.10 | Use Python 3.11+ |
| `vulkaninfo` lists `Microsoft Direct3D12 (<gpu>)` next to the real GPU and the tests run on it | The Windows "OpenCL™, OpenGL® and Vulkan® Compatibility Pack" installs Mesa's Dozen, a Vulkan-on-D3D12 layer that reports `DISCRETE_GPU` like the native card | Current `main` prefers a native ICD over Microsoft's vendor (`0x1414`) in the runtime and every harness; set `LEONIDA_VK_DEVICE=<part of the real GPU name>` to select explicitly. See #915. |
| A GPU test that uses `image_load` (or lighting built from real shaders) reads zeros from its image; @Silliana saw every texel except pixel (0,0) come back zero in their own layer (#603, not reproduced here) | In this runtime the shader turns the T# words into an index through the descriptor hash table (`runtime/src/descriptor_table.h`: a miss gives index 0), and index 0 is a 1×1 image of zeros (`runtime/src/vulkan_bindless.h`), so a hand-built or zeroed T# that was never registered reads zeros even at (0,0). The translator does not compare coordinates with the T# size, and `cs_imgload.asm` only uses its T# words as a hash key | Register the descriptor through the runtime (for D3D12, create it with `CreateShaderResourceView`) so its T# hits the table, with the image's real width and height (stored as size − 1, see `runtime/src/gfx10_descriptors.h`) |
| `cs_sample_lz_int_d16`, `ps_sample_implicit_sint`, `ps_ssharp`, `ps_tex`, `ps_texture_mip0`, `ps_texture_sample` and `ps_texture_sample_d16` crash (exit -11 or 139) on an Intel GPU on Linux, while NVIDIA and llvmpipe pass them (TEST-399) | A bug in Mesa ANV's shader compiler: compiling these texture-sampling shaders, `nir_opt_gcm` (`nir_opt_gcm.c:430`, `gcm_schedule_early_src`) dereferences a phi source an earlier pass has freed. The SPIR-V passes `spirv-val`. Seen with Mesa 26.1.6 (Arrow Lake) and 26.2.7 (Iris Xe, ADL GT2); Mesa 26.3.1 and `main` (bb98606) pass the whole suite on the same Iris Xe | Use Mesa 26.3.1 or newer. To check a newer Mesa without installing it, build its Intel Vulkan driver and point `VK_DRIVER_FILES` at its `src/intel/vulkan/intel_devenv_icd.x86_64.json` |

## Posting your result

Use the template at the top of [#166](https://github.com/ItsNotRexed/Leonida/issues/166): GPU, driver, subgroup
range, OS, the commit you built, the tool versions and the counts each script printed. Post counts and pass/fail
lines only, never shaders or logs from the game. A failure that isn't in the table above gets its own issue, linked
from your entry.

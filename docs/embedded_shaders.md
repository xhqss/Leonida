# Shader containers embedded in the package's DLLs

**Status: measured** on the 1.0.2.1 package. This note covers the shader programs that ship outside
`shaders.rpf`: the DXBC containers inside the package's DLLs, what each one carries, and what the measurement
changes for the ray-tracing and upscaling paths. No container, shader or payload is reproduced here; the tables
hold counts, offsets, chunk names and module identifiers.

## 1. Why they matter

`make_game.py`'s `shaders` stage carves `shaders.rpf` only (`make_game.py`:655). Two DLLs in the same package
carry compute programs of their own:

* `XboxBvhBuilder.dll`, Microsoft's XDXR acceleration-structure builder. Its constructor creates compute PSOs
  through device slot 62, and the builder is on the critical path for ray tracing.
* `amd_fidelityfx_upscaler_xbox.dll`, the FidelityFX 4.1.1 upscaler. Its programs are the `ACCC`/`FSPX` format
  covered in [fspx.md](fspx.md); this note only counts them.

`tools/pe_shader_containers.py` scans a binary for DXBC containers, validates the chunk table, reads the `ILDN`
module name and reports what each container carries. `--extract` writes the containers and an `index.json` the
translator reads; extracted shaders are game data and must stay in the user's work folder.

```sh
python -I tools/pe_shader_containers.py <dll> --json <work>/containers.json --extract <work>/embedded/dxbc
```

## 2. Inventory

The 16 DLLs and the package's root files were scanned. All the containers are in two DLLs:

| image | containers | bytes | min / median / max | chunks |
|---|---:|---:|---:|---|
| `XboxBvhBuilder.dll` | 52 | 674,404 | 4,072 / 7,454 / 74,508 | `SFI0 ISG1 OSG1 PSV0 RTS0 STAT ILDN HASH DXIL` |
| `amd_fidelityfx_upscaler_xbox.dll` | 29 | 289,656 | 1,764 / 5,176 / 42,616 | `SFI0 ISG1 OSG1 PSV0 RTS0 HASH ACCC` |
| other 14 DLLs | 0 | 0 | — | — |

The 29 `ACCC` containers hold `FSPX` payloads ([fspx.md](fspx.md)); the 52 `DXIL` containers are the subject of
this note. The FSR DLL has one rejected `DXBC` hit: the bytes are an immediate in its own magic check, and the
validator rejects it (the scan reports both counts).

**Every one of the 52 containers carries two representations of its program:**

* an LLVM bitcode blob in the `DXIL` chunk (`BC C0 DE`, 1,820 to 70,276 bytes), and
* an `XDAT` directory with tag `0x0d` whose payload starts with `XBCH`: the console program. The same chunk also
  holds tag `0x07` (a second `DXIL` program header) and tag `0x12` (a 16-byte hash). The `STAT` chunk holds a
  second, different bitcode blob, not the same one.

### `STAT` and the `0x07` program header

Measured over all 52 builder containers in one local 1.0.2.1 package; no shader data is included here. The
bitcode spans below use the `DxilProgramHeader`/`DxilBitcodeHeader` offsets and sizes defined by
[DirectXShaderCompiler](https://github.com/microsoft/DirectXShaderCompiler/blob/main/include/dxc/DxilContainer/DxilContainer.h).
All spans fit inside their enclosing parts, and all 104 `STAT` and outer `DXIL` bitcode modules were accepted by
`llvm-dis` 21.1.8. The header's size-in-dwords equals its enclosing part or record length in every case.

* **`STAT`:** 52/52 contain a `DXIL` program header and a bitcode module distinct from the outer `DXIL` bitcode.
  All have `dx.entryPoints` and `dx.resources` metadata, and none defines a function in the `llvm-dis` output;
  the outer modules define functions. DirectXShaderCompiler calls this the shader-statistics part and uses a valid
  `STAT` module for reflection (see its
  [container reflection implementation](https://github.com/microsoft/DirectXShaderCompiler/blob/main/lib/HLSL/DxilContainerReflection.cpp)).
  The metadata-only shape is consistent with that role; the builder's actual consumption of `STAT` is not measured.
* **XDAT tag `0x07`:** 52/52 contain a `DXIL` program header whose declared bitcode is byte-identical to that of
  the outer `DXIL` part. None contains the `NOBC` marker. The whole `0x07` record is not the same as the whole
  outer part, which also contains `XDAT`. This differs from the `NOBC` form documented for the game's shader
  corpus in [fssm_crosscheck.md](fssm_crosscheck.md); it does not change that separate measurement.

The bitcode is what a PC D3D12 compiler consumes; the runtime path is the `XBCH` record. The constructor passes
the **whole container** to the compute-PSO call: at `0x180021330` it loads `DAT_180025700` with size `0x1134`
(4,404 bytes, the size of container 0, `CopyAndCompactBVH`) and calls `FUN_18000c470`, whose vtable call at
`+0x1f0` (slot 62) receives that pointer, its size and two zero words. `FUN_18001d400` creates the
`XDXR GetBVHCompactedSize` PSO (0x14a4 bytes; measured below). `bvh_builder_test` measures **53** PSO
creations during construction over the **52** containers ([d3d12_vulkan.md](d3d12_vulkan.md) A.2), so one
container is created twice. It is **`GetBVHCompactedSize`** (5,284 bytes = 0x14a4): creations **#47 and #53** pass
byte-identical containers (RT-182, `LEONIDA_BVH_RECORD62=1`, which now prints each creation's module from its `ILDN`
part and says when a container repeats). The test now also prints each creation's call site and the name the builder
gives the returned PSO (RT-266, WARP, default constructor flag): the builder names every one of the 53 PSOs with
`SetName`, `XDXR <module>` (for example `XDXR InitPartitionedHeaders`; some modules are shortened, as
`XDXR CopyAndCompact`, or numbered, as `XDXR CalculateMortonCodes 0`/`1`). **#47 and #53 both get `XDXR
GetBVHCompactedSize`**, from the same call site in `FUN_18001d400` (return address `+0x1d503`, called through
`FUN_18000c470` at `+0xc4e4`); `FUN_18001d400` is called twice, once from `FUN_18000d850` (#47) and once from
`FUN_180021330` directly (#53). So the repeat is one PSO created twice under one name, not one container under two
names, and no creation is named `XDXR PostBuildInfoQuery` in this run (default flag, WARP; the flag-true
constructor was not run). The creation order, by module (it is not the table
order of section 3; `CopyAndCompactBVH` is #52, not the first):

| creations | modules, in the order of the `CreateComputePipelineState` calls |
|---|---|
| #1-#13 | `InitPartitionedHeaders`, `PT_WriteOfflinePartition`, `PT_WritePartitions`, `PT_WriteInstances`, `PT_LoadInstances`, `PT_BeginPass`, `ResetCounterValues`, `InitHeaders`, `CopyForBVHUpdate`, `ResetToIndexBuffer`, `CopyCounters`, `CalculateMortonCodes`, `CalculateMortonCodes64` |
| #14-#29 | `BitonicPreSortCS`, `BitonicOuterSortCS`, `BitonicInnerSortCS`, `BitonicPreSort64CS`, `BitonicOuterSort64CS`, `BitonicInnerSort64CS`, `RadixReset`, `RadixCount`, `PrefixSum64`, `PrefixSum128`, `PrefixSum256`, `PrefixSum512`, `PrefixSum1024`, `PrefixSum2048`, `PrefixSum4096`, `RadixAlloc` |
| #30-#46 | `RearrangeTriangles`, `RearrangeBVHs`, `LoadInstances`, `SplitInstances`, `LoadTriangles`, `LoadProceduralGeometry`, `ReloadTriangles`, `ReloadProcedurals`, `PT_ConstructBVH4`, `ConstructBVH4`, `RefitBVH4`, `BuildBVHSplits`, `BuildBVHSplits64`, `FindTreelets`, `ResetKDOP`, `BuildKDOP`, `BuildApexPointMap` |
| #47-#53 | `GetBVHCompactedSize`, `DecompressHeader`, `LeafRehydration`, `Box16Rehydration`, `Box32Rehydration`, `CopyAndCompactBVH`, `GetBVHCompactedSize` (again) |

The test also prints each creation's container with its `ILDN` module name and the first bytes of its digest
(RT-182): creations 1 to 51 are 51 distinct containers, `GetBVHCompactedSize` (5,284 bytes, 0x14a4) among them at 47;
creation 52 is `CopyAndCompactBVH` (4,404 bytes, 0x1134); creation 53 is `GetBVHCompactedSize` again, with the
same digest and size. So the 0x14a4-byte container `FUN_18001d400` passes is the `GetBVHCompactedSize` module, and
the PSO for it is created twice (under one name, `XDXR GetBVHCompactedSize`, per the SetName record above). Measured on 1.0.2.1 with an RTX 5080 (driver
621.01): all 53 creations return `S_OK`.

## 3. The 52 modules

The `ILDN` PDB path gives one module per container, all under
`X:\americas\src\dev\rage\3rdparty\Microsoft\XboxBvhBuilder\Build\Release\Output\XboxBvhBuilder\` (Microsoft's
component integrated into RAGE, not Rockstar shader code). Grouped by phase:

| phase | modules |
|---|---|
| sort (11) | `BitonicPreSortCS`, `BitonicPreSort64CS`, `BitonicInnerSortCS`, `BitonicInnerSort64CS`, `BitonicOuterSortCS`, `BitonicOuterSort64CS`, `RadixCount`, `RadixAlloc`, `RadixReset`, `CalculateMortonCodes`, `CalculateMortonCodes64` |
| prefix (7) | `PrefixSum64`, `PrefixSum128`, `PrefixSum256`, `PrefixSum512`, `PrefixSum1024`, `PrefixSum2048`, `PrefixSum4096` |
| kdop (3) | `BuildKDOP`, `ResetKDOP`, `BuildApexPointMap` |
| build (7) | `ConstructBVH4`, `BuildBVHSplits`, `BuildBVHSplits64`, `SplitInstances`, `FindTreelets`, `RearrangeBVHs`, `RearrangeTriangles` |
| refit (2) | `RefitBVH4`, `CopyForBVHUpdate` |
| load (6) | `LoadTriangles`, `ReloadTriangles`, `LoadInstances`, `LoadProceduralGeometry`, `ReloadProcedurals`, `ResetToIndexBuffer` |
| path tracing (6) | `PT_ConstructBVH4`, `PT_BeginPass`, `PT_LoadInstances`, `PT_WriteInstances`, `PT_WritePartitions`, `PT_WriteOfflinePartition` |
| rehydration (3) | `Box16Rehydration`, `Box32Rehydration`, `LeafRehydration` |
| headers/util (7) | `DecompressHeader`, `InitHeaders`, `InitPartitionedHeaders`, `GetBVHCompactedSize`, `CopyCounters`, `ResetCounterValues`, `CopyAndCompactBVH` |

## 4. They are console FSSM, and the existing translator already takes them

The `XBCH` record has the same shape as the game's own shaders: `XBCH` + stored/unpacked sizes + a zlib stream
that expands to `FSSM` ([fssm_crosscheck.md](fssm_crosscheck.md)). On all 52 containers:

| step | result |
|---|---|
| `tools/fssm_unpack.py` | **52/52** decompress to a valid `FSSM` blob (716 B to 101,320 B, median 2,200 B) |
| `tools/shader_disasm.py --mcpu gfx1013` | **52/52** disassemble; 42 with no invalid word, 10 with invalid words in the code |
| `tools/rdna2spirv.py` | **51** translate and pass `spirv-val` (`main` `49475796`) |

When this page was first written, 42 translated and 10 stopped at four rules. Three of those rules have landed since
(`427c1376`, from #1968), and one program still waits on a fifth rule:

| module(s) | blocker then | now |
|---|---|---|
| `BuildKDOP`, `LoadInstances`, `ReloadProcedurals`, `LoadProceduralGeometry`, `LoadTriangles` | `global_atomic_fmax` | translate |
| `PT_LoadInstances` | `global_atomic_fmin` | translates |
| `Box32Rehydration`, `ReloadTriangles` | `buffer_load_ushort` | translate |
| `LeafRehydration` | `tbuffer_load_format_x GFX10_FORMAT_8_UINT` | translates |
| `Box16Rehydration` | `buffer_load_ushort` | stops at `buffer_store_short` (#1864) |

The disassembly count is unchanged: the same 42 programs have no invalid word, and 10 have invalid words in the
code. All 10 of those translate; `Box16Rehydration` is one of the 42 clean ones.

On current `main`, 51 of the 52 programs translate and pass `spirv-val`. The four earlier blockers were
implemented in #1871 and #1968 (`427c1376`):

* `tbuffer_load_format_x GFX10_FORMAT_8_UINT` (#1871) unblocked `LeafRehydration`.
* `global_atomic_fmax` (#1968) unblocked `BuildKDOP`, `LoadInstances`, `ReloadProcedurals`, `LoadProceduralGeometry`, and `LoadTriangles`.
* `global_atomic_fmin` (#1968) unblocked `PT_LoadInstances`.
* `buffer_load_ushort` (#1968) unblocked `Box32Rehydration`, `ReloadTriangles`, and the load half of `Box16Rehydration`.

The single remaining untranslated program stops at one rule:

| module(s) | blocker |
|---|---|
| `Box16Rehydration` | `buffer_store_short` (#1864) |

Reproduction, after `--extract` above:

```sh
python -I tools/fssm_unpack.py <work>/embedded/dxbc
python -I tools/shader_disasm.py <work>/embedded/dxbc/fssm --out <work>/embedded/dxbc/disasm --mcpu gfx1013
python -I tools/rdna2spirv.py <work>/embedded/dxbc/disasm --out <work>/embedded/spirv
```

## 5. What this changes

* **The RT builder does not need a DXIL path for the console code.** Its programs are RDNA2 `FSSM`, and 51 of
  52 already pass `spirv-val` with the translator the project uses for the game's 26,503 shaders. The bitcode in
  the same container is a second representation (the one the PC-compiler experiment measured); it is not what
  the console runtime executes.
* **The missing rules were small, and only one is left.** `global_atomic_fmax`/`fmin` (6 programs),
  `buffer_load_ushort` (3) and `tbuffer_load_format_x` with format 8 (1) were ordinary translator rules, not a new
  compiler backend, and they are on `main`. The one left is `buffer_store_short` (#1864), for `Box16Rehydration`.
* **The rehydration programs are the ones closest to the shipped data.** `Box16Rehydration`,
  `Box32Rehydration` and `LeafRehydration` are exactly the programs that would turn the dehydrated BVH packs
  into runtime trees ([bvh.md](bvh.md) §3.1). `LeafRehydration` translates since #1871, `Box32Rehydration`
  translates since #1968 (`427c1376`), and only `Box16Rehydration` awaits #1864 (`buffer_store_short`).
* **What is still open.** None of the 52 has been executed on a GPU; translation and `spirv-val` do not prove the
  output. Which of them the game actually creates is a call-site question (RT-5). The module-to-pipeline order
  above comes from `ILDN`, not from a runtime trace. The `STAT` metadata-only shape is consistent with reflection,
  but whether the builder consumes it has not been measured.
* **They run on a GPU (TEST-594).** `tools/bvh_gpu_smoke.py` translates them from the package and runs each through
  `vk_compute_test`. Each program reads its root arguments from a table at `s[0:1]`, the heap base there, which the
  tool seeds with `VKC_HEAP_INIT`: every table word points past the table into zeros. **All 49 that aren't
  rehydration programs pass**: exit 0, no Khronos validation error, and the same heap bytes on a second run (AMD
  Radeon RX 9070, Windows 11, `--max-shared-memory 32768`, package 1.0.2.1). With these inputs only seven change any
  heap word (`InitHeaders`, `InitPartitionedHeaders`, `LoadInstances`, `LoadProceduralGeometry`, `LoadTriangles`,
  `PT_BeginPass`, `PT_WriteInstances`); the others leave early. So this alone shows that the translated programs
  run cleanly, not that their output is right. **Six are checked byte for byte** (`--checked`), the ones whose effect
  reads straight off their code. `RadixReset`, `ResetKDOP`, `ResetToIndexBuffer`, `CopyCounters` and
  `ResetCounterValues` get their root-table fields (an output address, a count of 20, a value or a source), and the
  tool compares all 20 entries they write and the bytes after them, which must stay untouched. `PrefixSum64` scans 64
  words in place into their exclusive prefix sum through five `ds_swizzle_b32` broadcast steps and stores the group's
  total; the input wraps past 2^32. All six match exactly. Negative controls: with the translator made to skip
  `v_add_nc_u32`, `ResetToIndexBuffer` fails (entries hold `base+4i` and zeros); with it skipping `ds_swizzle_b32`,
  `PrefixSum64` fails from element 2 on. So the check sees a wrong translation. Two things found on the way:
  * the wave64 bitonic sorts (`BitonicPreSort64CS`, `BitonicInnerSort64CS`) declare 32,780 bytes of workgroup memory
    with the translator's default cap, 12 over the 32 KiB AMD's Windows driver allows
    (`VUID-RuntimeSpirv-Workgroup-06530`). Their highest LDS offset is 8192, and `--max-shared-memory 32768` shrinks
    the array to what they use;
  * with every table word 0 instead, the descriptors point at the table itself, and `RadixCount` then writes into the
    table that other waves are reading, so its output differs between runs. That is the test input, not the
    translator; the tool's default avoids it.
* **What is still open.** The other programs' outputs (the sorts, prefix sums, builders, refit and loaders) are not
  checked against a reference yet; the rehydration trio has one in TEST-376 (`tools/bvh_rehydrate.py`); `LeafRehydration` does not finish within 60 s with either table described
  above. Which of them the game actually creates is a call-site question (RT-5). The module-to-pipeline order
  above comes from `ILDN`, not from a runtime trace. The `STAT` bitcode and the `0x07 NOBC` record are not
  explained.

## 6. Tool and tests

`tools/bvh_gpu_smoke.py --game-dir <package> [--max-shared-memory N] [--checked] [--json report.json]` reproduces the
GPU run above: the extraction and translation of §4, then two dispatches per program, and with `--checked` the six
byte-exact cases. Its report holds module names, statuses
and counts only. `tests/test_bvh_gpu_smoke.py` checks the heap image, the word count and the judging of runs with a
fake `vk_compute_test`, and the checked cases' inputs and judging (a fake that does what each case describes passes,
one wrong byte fails), without a GPU or game files.

`tools/pe_shader_containers.py` is standard library only and reads the file, never runs it. It accepts a
container only when the chunk table is sorted, 4-byte aligned, entirely inside the declared total and its last
chunk ends exactly at that total; that is what rejects the FSR DLL's `DXBC` immediate. The synthetic tests in
`tests/test_pe_shader_containers.py` build containers by hand (no game data) and cover the accepted layout, a
bare magic, a truncated chunk, an `ACCC`/`FSPX` container, the `ILDN` name and the extraction index.

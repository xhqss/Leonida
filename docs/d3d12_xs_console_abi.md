# The D3D12.X console ABI: vtable slots, object fields and record encodings

Status: measured-facts note, sourced. This page collects the Xbox Series (`d3d12_xs`) ABI facts a
replacement runtime must reproduce — vtable slot numbers, object field layouts and the inline
record encodings the console SDK writes into a command-list object — for the layers that are still
missing: command lists, root arguments, descriptor heaps and present. It complements
[d3d12_vulkan.md](d3d12_vulkan.md) (the Vulkan layer plan) and
[d3d12_command_objects.md](d3d12_command_objects.md) (what this package's own DLLs measure).

Provenance rules for this page, stated once:

- **Sibling layer** — measured by a sibling open-source layer on retail Series titles and
  published as findings. Not independently reproduced here; treat every such claim as unverified
  on this package.
- **Package** — measured on this package's shipped DLLs (`XboxBvhBuilder.dll`,
  `amd_fidelityfx_upscaler_xbox.dll`); see
  [d3d12_command_objects.md](d3d12_command_objects.md) §§3.3–3.5a.
- **SDK** — the public Windows SDK `d3d12.h` order, for contrast.
- **Unconfirmed** — consistent with the surrounding facts but not yet measured anywhere we can
  check.

All slot numbers below are absolute vtable indices including `IUnknown` (0–2) and the
`ID3D12Object`/`ID3D12DeviceChild`/`ID3D12Pageable` base slots, as in
[d3d12_command_objects.md](d3d12_command_objects.md) §2.

## 1. Device (`ID3D12DeviceX` over the PC `ID3D12Device` layout)

The console device keeps the PC slot order where measured, with extra console methods placed in
slots the PC chain uses for newer interfaces, plus a tail of console-only methods. Slots marked
**Package** were called by the shipped upscaler or BVH-builder DLLs with the stated shapes; the
rest are **Sibling layer** measurements.

| Slot | Method | Evidence |
|---:|---|---|
| 8, 48 | `CreateCommandQueue` variants | Sibling layer |
| 9 | `CreateCommandAllocator` | Sibling layer |
| 10, 60 | `CreateGraphicsPipelineState` variants | Sibling layer |
| 12 | `CreateCommandList` | Sibling layer |
| 14 | `CreateDescriptorHeap` | Package + sibling layer |
| 15 | `GetDescriptorHandleIncrementSize` | Package + sibling layer |
| 17, 18, 19 | `CreateConstantBufferView` / `CreateShaderResourceView` / `CreateUnorderedAccessView` | Package + sibling layer |
| 22 | `CreateSampler` | Sibling layer |
| 23, 24 | `CopyDescriptors` / `CopyDescriptorsSimple` | Package + sibling layer |
| 25 | `GetResourceAllocationInfo` | Sibling layer |
| 29, 44 | `CreatePlacedResource` variants | Sibling layer |
| 38 | `GetCopyableFootprints` | Package + sibling layer |
| 41 | `CreateCommandSignature` | Sibling layer |
| 52 | `SetDebugFlags` | Sibling layer |
| 55 | derived pipeline state | Sibling layer |
| 56, 57, 66, 67 | serialize / deserialize pipeline state | Sibling layer; the package's upscaler calls 67 as `DeserializeComputePipelineStateX` (console desc, `IID_ID3D12PipelineState`) — Package |
| 61 | `CreateSamplerX` | Sibling layer |
| 62 | `CreateComputePipelineState` with two extra args: `(desc, NULL, NULL, riid, ppv)`; the PC interface presents `CreateStateObject` at this slot | **Confirmed here**: Package ([d3d12_vulkan.md](d3d12_vulkan.md) §7) + sibling layer |
| 73 | `CreateCommandListX` | Sibling layer |
| 74, 75 | register / unregister page pool | Sibling layer |
| 76 | `CreateCommittedResource`, same signature family as PC slot 27 | **Confirmed here**: Package ([d3d12_vulkan.md](d3d12_vulkan.md) §7) + sibling layer |
| 78–81 | frame pacing: `SetFrameIntervalX`, `ScheduleFrameEventX`, `WaitFrameEventX`, `GetFrameStatisticsX` | Sibling layer |
| 86 | `CreatePipelineStateX` | Sibling layer |

The slot-76 console resource descriptor is **larger than 52 bytes** (sibling layer); the package's
upscaler DLL writes a 3 at offset +52 (Package). The `Layout` field sits at +44; value `0x101`
denotes a 2D texture ([d3d12_vulkan.md](d3d12_vulkan.md) §7; a sibling layer measured the same on
retail titles).

### 1.1 Two runtime layouts

Two console runtime layouts exist: an older game-OS layout and a newer one in which the queue and
list slots shift **+2** (sibling layer). Detection rule (sibling layer): if a fence object is
passed to queue slot 16/17, the layout is newer — on the older layout those slots are
`GetTimestampFrequency`/`GetClockCalibration`. Which layout this package's titles were built
against is **unverified here**.

## 2. Descriptor heap

| Slot | Fact | Evidence |
|---:|---|---|
| 9, 10 | `GetCPUDescriptorHandleForHeapStart` / `GetGPUDescriptorHandleForHeapStart` return the 8-byte handle in `rax` on console; the PC convention is a hidden out-pointer | **Confirmed here**: Package + sibling layer |

Descriptor contents (sibling layer): CBV/SRV/UAV heaps use **32-byte slots** in unified memory — a
T# is 8 dwords, a V# 4 dwords, inside the 32-byte slot; sampler heaps use **16-byte slots**. The
CPU handle, GPU handle and the address in title memory are the same value.

## 3. Resource

| Slot | Fact | Evidence |
|---:|---|---|
| 11 | `GetGPUVirtualAddress` returns the title-memory address | Sibling layer; consistent with the package measurement that a CPU pointer from list slot 148 is passed unchanged as a root SRV GPU address ([d3d12_command_objects.md](d3d12_command_objects.md) §3.3a) |

## 4. Command queue

Measured queue slots (sibling layer); the queue follows the two-layout rule of §1.1:

| Method | Older layout | Newer layout |
|---|---:|---:|
| `Signal` | 14 | 16 |
| `Wait` | 15 | 17 |
| `GetTimestampFrequency` | 16 | 18 |
| `GetClockCalibration` | 17 | — (shifted) |
| `GetDesc` | 18 | — (shifted) |
| `SignalX` | 23 | 25 |
| `PresentX` | 30 | 32 |

Console-only queue methods the layer does not model should return `S_OK` and log the call:
title frame pacing flows through some of them (sibling layer).

## 5. Command list object

Measured by a sibling layer with a fake list — a 600-slot vtable of logging thunks — that let the
console SDK's inline writers record into the object (sibling layer; the package's BVH builder and
upscaler independently measured `+0x18`, `+0x28`, `+0x30`, `+0x140`, `+0x141`, `+0x344` and
`+0x544` — Package, [d3d12_command_objects.md](d3d12_command_objects.md) §§3.4–3.5a).

### 5.1 Object fields

| Offset | Field | Evidence |
|---:|---|---|
| +0x00 | vtable pointer | Package + sibling layer |
| +0x18 | inline write pointer | Package + sibling layer |
| +0x28 | dispatch-record limit | Package + sibling layer |
| +0x30 | buffer end | Package + sibling layer |
| +0x140 | dirty flag byte, set when record header bits 31:30 are 0 | Package + sibling layer |
| +0x141 | pipeline flag byte, taken from bit 20 of the pipeline's record word at +0x20 | Package + sibling layer |
| +0x143 | flag byte; read by titles, written only by the runtime | Package (read 31+1 sites, never written) + sibling layer |
| +0x344 + 4·i | header word of root parameter *i* | Package + sibling layer |
| +0x544 | current pipeline record, 16 bytes | Package + sibling layer |

### 5.2 Inline record encodings

The console SDK's inline writers emit a record only while the write pointer is below the buffer
end; on a full buffer they call the vtable method the record stands for (Package + sibling layer).

| Record | Encoding | Evidence |
|---|---|---|
| Pipeline | 28 B: `[PSO ptr][D3D12Core ptr][8 B flags][4 B]` | Package + sibling layer |
| Root parameter *i* | header at +0x344+4·i = `0xC0000000 \| i<<10` + const-offset·4 (bits 9:2) + `(count-1)<<16` | Sibling layer; header table itself confirmed Package |
| Root descriptor | header + 64-bit address = 12 B | Package + sibling layer |
| Root constants | one constant = 8 B; *n* constants = 4+4·*n* B | Package + sibling layer |
| Descriptor table | header + one 32-bit word = the 32-bit GPU address of the first descriptor | Sibling layer |
| `Dispatch` | `0x4002C400` (or `(0x1000BA00 \| flag) << 2`) followed by x, y, z = 16 B | Package + sibling layer |
| Indirect dispatch | `0x4003CC00`, 20 B total | Package (five-word form with W `0x8020`/`0x20`) + sibling layer |
| `IASetPrimitiveTopology` | `0xC000B600 \| topology` | Sibling layer |

### 5.3 Method slots

| Slot | Method | Evidence |
|---:|---|---|
| 9 | `Close` | Sibling layer |
| 10 | `Reset` | Sibling layer |
| 12 | `DrawInstanced` | Sibling layer |
| 13 | `DrawIndexedInstanced` | Sibling layer |
| 14 | `Dispatch` | Package + sibling layer |
| 15 | `CopyBufferRegion` | Package + sibling layer |
| 16 | `CopyTextureRegion` | Sibling layer |
| 17 | `CopyResource` | Sibling layer |
| 19 | `ResolveSubresource` | Sibling layer |
| 20 | `IASetPrimitiveTopology` | Sibling layer |
| 25 | `SetPipelineState` | Package + sibling layer |
| 26 | `ResourceBarrier` | Package + sibling layer |
| 32 | `SetDescriptorHeaps` | Package + sibling layer |
| 33 | `SetComputeRootSignature` | Package + sibling layer |
| 37, 39 | `Set*RootConstant(s)` | Package + sibling layer |
| 41, 43, 45 | `Set*RootDescriptor*` / table | Package + sibling layer |
| 48 | `IASetVertexBuffers` | Sibling layer |
| 51 | `ClearDepthStencilView` | Sibling layer |
| 80 | `ExecuteIndirect` | Sibling layer (note: the package measured slot 80 as a console-only call, a `Dispatch`-with-extra-word shape; slot assignments past 61 differ between the two sources — unconfirmed) |
| 83 | `CopyTextureRegionX` (older layout) | Sibling layer; the package measured a `CopyTextureRegion`-shaped call at slot 85 — consistent with the newer layout (+2) |
| 108 | `ResolveSubresourceRegion` | Sibling layer |
| 146 | enhanced `Barrier` | **Confirmed here**: Package (console slot 146 takes one `D3D12_BARRIER_GROUP`; PC equivalent is GCL7 slot 80) |
| 148 | `AllocateUploadMemory` `(size, alignment)`; the returned CPU pointer is the GPU address in unified memory | Package + sibling layer |

Layout shift on the list tail (sibling layer): from `CopyTextureRegionX` on, methods sit two slots
later in the newer layout — newer→older pairs 85→83, 86→84, 87→85, 90→88, 91→89, 92→90, 95→93,
96→94.

The shim hands out console-shaped command lists by default: `GetCommandList`-shaped queries build
the proxy with the console vtable above. `LEONIDA_D3D12_LISTS=pc` makes it hand out PC-shaped lists
instead, for guests compiled against the PC headers (the `tiny_frame` test fixture calls through PC
vtable indices; without the variable its `ClearRenderTargetView` at PC slot 48 lands on the
console's `IASetVertexBuffers` and `Close` never runs). The device, queue, heap and fence stay
console-shaped either way.

### 5.4 DMA command list

| Slot | Method | Evidence |
|---:|---|---|
| 9 | `Close` | Sibling layer |
| 10 | `Reset` | Sibling layer |
| 11 | `CopyBufferRegion` | Sibling layer |
| 12 | `CopyTextureRegion` | Sibling layer |
| 13 | `CopyResource` | Sibling layer |

## 6. Root arguments

Sibling-layer measurements on retail titles:

- **Compute.** The root arguments are a packed argument array in title memory; `io[0:1]` of the
  dispatch record carries its address.
- **Graphics.** VS and PS receive root arguments in user SGPRs `s8..s31` — `s0..s7` are
  driver-reserved; `s[4:5]` of a VS holds the fetch shader — 24 user SGPRs in total. Arguments
  past those go through a table whose address is in `s[0:1]`.
- **Descriptor tables** take 4 bytes in the packed array: the record is the parameter header plus
  one 32-bit word, the 32-bit GPU address of the first descriptor.
- **Root CBV.** The shader builds its own V#; a measured example sets `DST_SEL xyzw` plus the
  format word from `s11`.

## 7. Resource barriers (list slot 26)

Sibling layer: console aliasing barriers behave as "any resource" barriers — both resource
pointers are null — where the PC API only allows those between placed or reserved resources.
Console-only state bits must be dropped before forwarding, or `Close` fails `E_INVALIDARG`.

## 8. Unified memory

Titles expect graphics memory where the CPU address equals the GPU address (sibling layer;
consistent with the package's slot-148 measurement in §3).

- The sibling layer backs this with a fully-committed sub-4 GiB region at a 4 GiB-aligned virtual
  address, opened via `ID3D12Device3::OpenExistingHeapFromAddress`. Measured limits: a partial
  commit is rejected; a full 4 GiB is rejected; 4095 MiB works.
- For reservations above 4 GiB the measured technique is: reserve with `VirtualAlloc2`
  `MEM_RESERVE_PLACEHOLDER`, split chunks with `VirtualFree` `MEM_PRESERVE_PLACEHOLDER`, back each
  chunk with a `SEC_COMMIT` section mapped via `MapViewOfFile3` `MEM_REPLACE_PLACEHOLDER`. Each
  chunk opens as its own heap with GPU/CPU coherence at the title's address. `SEC_RESERVE`
  sections are refused even when fully committed.
- Buffers in unified memory are placed resources at 64 KiB alignment. A buffer at a
  non-64 KiB-aligned console address is placed from the 64 KiB boundary below it and the copy
  paths add the delta.
- `Map` of a unified-memory resource returns its console address.
- GPU-owned textures (resources with RT/DS/UAV flags) go into "arenas" — default heaps standing
  for ranges of title addresses — because titles alias several textures at one address.

## 9. `PresentX`

Sibling layer: one frame is presented per console queue method call (`PresentX`, §4). The
presented back buffer is detected by tracking render-target binds (RTV creation and
`OMSetRenderTargets`) and `ExecuteCommandLists`.

## 10. Shader-interface facts

Measured on real console shaders by a sibling layer; where the claims overlap this repository's
translator docs they are mostly confirmed here.

- An FSSM stage descriptor at +0x110 looks like `RSRC2`: `USER_SGPR` sits in bits 5:1 (e.g.
  `0x18` → 12 SGPRs). A PS header carries an export-variant patch table from about +0x25C.
- User SGPRs (a compute example with 14 per `RSRC2`): `s4` = descriptor table 0, `s5` = table 1,
  `s[6:7]` and `s[8:9]` = root CBVs; `s14`/`s15` = thread-group id x/y.
- Mesh-shader interface: `s3[27:24]` = wave index in the group, `v5` = group·128 + thread,
  `s16..s24` = user data.
- Console GS runs on-chip: the ES writes vertices to LDS and `s_setpc`s to the GS. The GS writes
  vertices to LDS at 64 B per vertex, base = `s2 + ([s8 + 0xB8] << 8)`, and uses `s_sendmsg`
  emit/cut/done. No copy shader is present in the pack.
- `IASetPrimitiveTopology` value 17 is rectangle lists, drawn as two triangles on PC.
- Derivatives: console code computes cross-quad derivatives manually (DPP `quad_perm`,
  `ds_swizzle`) and reads helper lanes. On PC, reads across quads need quad-aware lane reads:
  `WaveReadLaneAt` leaves helper lanes out before SM 6.7, and enabling
  `WaveOpsIncludeHelperLanes` caused a device hang in the sibling layer — whole-wave ballots then
  never let helper lanes exit.
- A pixel shader doing `v_readfirstlane` after `s_wqm` can read a helper lane's value; the read
  must take the value with a quad read inside its own quad plus a wave read from a live lane of
  that quad.
- Memory model: a shared "mem" buffer used for flat accesses must be coherent
  (`globallycoherent` on D3D12). Bottom-up BVH builds with atomics produce wrong child pointers
  otherwise; console code relies on GLC/cache-invalidation semantics.
- ISG1 inputs a VS never reads get no fetch-shader VGPRs.

## 11. Unverified here

The sibling-layer measurements above were made on retail Series titles and that project's own
host runtime, not on this package or this layer. Still to be measured on this package:

- **which runtime layout this title uses** — the older/newer detection rule of §1.1 needs a queue
  call trace;
- **draw-record encodings for graphics** — only compute dispatches and copies were measured; the
  graphics draw records and their sizes are unknown;
- the `PresentX` signature and plane struct (no signature is recovered anywhere;
  [d3d12_command_objects.md](d3d12_command_objects.md) §4);
- queue/fence/allocator slots and the device creation slots 8/9/12/36 for the layout this title
  uses ([d3d12_command_objects.md](d3d12_command_objects.md) §5.3);
- the exact newer→older slot pairs on the command-list tail beyond the measured set.

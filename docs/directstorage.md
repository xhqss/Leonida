# DirectStorage

The console title reads its streamed data through Xbox DirectStorage: `DStorageGetFactory` hands out a factory, the
factory opens files and creates queues, and a queue reads file or memory ranges into memory and reports completion
through status arrays, `ID3D12Fence` signals and events. `runtime/shims/dstorage.cpp` serves that API on the PC:
files open through the runtime's `GAME:` mapping and write overlay, and a worker thread per queue does the reads.
This page records which DLL and ABI the console title is expected to use, how sure that is, and what the shim does.

## Which DLL and which ABI: the evidence

The game executable is not in the package available here, so its import table is unmeasured
([runtime_imports.md](runtime_imports.md)). What is known:

| Evidence | Source | What it says |
|---|---|---|
| None of the 16 shipped DLLs imports DirectStorage | [runtime_imports.md](runtime_imports.md) (6 Xbox-specific imports, all `xmem`/`d3d12_xs`), [textures.md](textures.md) "The shipped binaries" (no `DStorage` import, export or string) | DirectStorage, if used, is called by the executable itself |
| The texture chunks carry the `DSTORAGE_BCPACK_MODE` values 1-7 and mix zlib and BCPack chunks | [textures.md](textures.md) "What BCPack is" | The data is laid out for console DirectStorage's decompression (DEFLATE, then BCPack, then swizzle) |
| A sibling console layer's export inventory names `dstorage_xs.dll` as console DirectStorage | [d3d12_vulkan.md §7](d3d12_vulkan.md#7-prior-art-and-measured-xbox-abi) | The console module name, measured elsewhere, not here |
| A retail title run by a sibling layer has `xcrdapi` in its console-only library set | [d3d12_vulkan.md §7](d3d12_vulkan.md#7-prior-art-and-measured-xbox-abi) | Only the name. Nothing in this repository says what `xcrdapi.dll` exports |
| The GDK reference: `DStorageGetFactory` is in header `dstorage_xs.h`, library `dstorage_xs.lib`; the interface pages give library `dstorage.lib`; all say "Xbox Series consoles" | [GDK DStorage reference](https://learn.microsoft.com/en-us/gaming/gdk/docs/reference/system/dstorage/dstorage_members) | The console API has its own names (`IDStorageFactoryX`, `IDStorageQueueX`, `IDStorageFileX`, `IDStorageStatusArrayX`) and structures that differ from the PC API |
| `registry.cpp` classified `dstorage.dll`/`dstorage_xs.dll` `DStorageGetFactory` as returning an HRESULT | `runtime/shims/registry.cpp` | It was an unimplemented import; `xcrdapi.dll` was not named |

**Chosen ABI: the console one** (the GDK's `IDStorageFactoryX2`/`IDStorageQueueX2`/`IDStorageFileX`/
`IDStorageStatusArrayX` and its `DSTORAGE_REQUEST`), served as `DStorageGetFactory` under all three names:

| Module | Confidence | Why |
|---|---|---|
| `dstorage_xs.dll` | medium | The reference's `dstorage_xs.lib` and the sibling inventory agree; not measured in this title |
| `xcrdapi.dll` | low | Only a name in a library list. Owning the module also means a title's `LoadLibrary("xcrdapi.dll")` gets the shims' pseudo-handle, and `GetProcAddress` for any other name returns `NULL` (logged). Static imports of other `xcrdapi` names are unchanged |
| `dstorage.dll` | low | The interface pages name `dstorage.lib`. Every caller of `gta6rt`'s import table is a console build, so this name serves the console ABI too, not the PC one |

The PC DirectStorage ABI (`dstorage.h`: `IDStorageFactory` with `DSTORAGE_REQUEST` source and destination unions,
`ID3D12Resource` destinations) is **not served**. A caller that asks for a PC interface id gets `E_NOINTERFACE` and a
log line, never an object of the wrong shape. The one exception is `IDStorageStatusArray`, whose two methods are the
console interface's in the same order (console D3D12 binaries reuse the PC SDK's ids), so a status array accepts its
id.

Owning a module has a run-time side: `LoadLibrary` of `xcrdapi.dll` or `dstorage.dll` (or `dstorage_xs.dll`) gets
the shims' pseudo-handle even when the package ships its own copy of that DLL, so the shim shadows a packaged copy
at run time (`library_loader.cpp` asks the registry before the bundled loader). None of the 16 shipped DLLs is one
of these, so nothing is shadowed today.

### What is not public, and the choice made

| Unknown | Choice | Confidence | Where to change it |
|---|---|---|---|
| Vtable order of the console interfaces (the reference lists methods alphabetically) | The PC header's order for the methods both APIs have; console-only methods where the overview lists them: factory `CreateQueue, OpenFile, CreateStatusArray, SetCpuAffinity, SetDebugFlags, SetStagingBufferSize`, then `GetStats` (X1), `CreateQueue1` (X2); queue `EnqueueRequest, EnqueueStatus, EnqueueSignal, Submit, CancelRequestsWithTag, Close, GetErrorEvent, RetrieveErrorRecord, Query`, then `EnqueueSetEvent` (X1), `Query1` (X2); status array `IsComplete, GetHResult`; file `Close, GetHandle` | queue, status array: medium; file: medium; factory: low (the `SetCpuAffinity` slot is a guess) | `runtime/shims/dstorage_xs_abi.h`, one declaration per interface |
| Interface ids of the X interfaces | A creating call (`DStorageGetFactory`, `CreateQueue*`, `OpenFile`, `CreateStatusArray`) accepts an id it has not seen for the kind of object it hands out and logs it once (`[rt] dstorage: ... asked for {...}: serving the ... interface`), so the first boot records them. It refuses an id seen for another kind, a PC DirectStorage id, and the system COM interfaces (the `{xxxxxxxx-0000-0000-C000-000000000046}` range with `IMarshal`, `IClientSecurity`, ..., and `IAgileObject`, `INoMarshal`, `IInspectable` and a few more). `QueryInterface` follows [d3d12_vulkan.md](d3d12_vulkan.md)'s rule (an unknown id is `E_NOINTERFACE`, logged): it accepts only `IUnknown`, the declared `IDStorageStatusArray` id for a status array, and the ids a creating call accepted for that kind of object | n/a (a policy, not a fact) | `IidPolicy` in `runtime/shims/dstorage.cpp` |
| `DSTORAGE_MIN_QUEUE_CAPACITY`, `DSTORAGE_MAX_QUEUE_CAPACITY`, `DSTORAGE_REQUEST_MAX_NAME` | The PC header's values: `0x80`, `0x2000`, 64 | medium | `dstorage_xs_abi.h` |
| `E_DSTORAGE_*` values | The PC `dstorageerr.h` values (`FACILITY_GAME`, `0x8924xxxx`). That header also defines the console-only failures (zlib, BCPack, swizzle, XVD), so the space is shared | medium-high | `dstorage_xs_abi.h` |
| Struct layouts | The compiler's x64 layout of the reference's declarations, pinned with `static_assert` (`DSTORAGE_REQUEST` 72 bytes, `DSTORAGE_QUEUE_DESC` 24, `DSTORAGE_QUEUE_DESC1` 32); `DSTORAGE_REQUEST_OPTIONS` is decoded from `AsUINT64` by explicit bit positions (zlib bit 0, BCPack 1-4, swizzle 5-20, destination pages 21, source type 22, source pages 23, reserved 24-63) | high for member order and types, medium for the bit allocation (MSVC allocates bit-fields from bit 0 up) | `dstorage_xs_abi.h` |

The first boot with the executable settles most of this: the dry-run report shows which module it imports
`DStorageGetFactory` from, and the log lines give the interface ids.

## What the shim does

- **Factory**: one object for the process, never freed (its reference count does not drop below one).
  `SetDebugFlags` and `SetCpuAffinity` are stored only (a console core mask means nothing on the host).
  `SetStagingBufferSize` accepts 0, 16, 20, 24, 28 and 32 MiB, fails with `E_DSTORAGE_INVALID_STAGING_BUFFER_SIZE`
  otherwise and with `E_DSTORAGE_STAGING_BUFFER_LOCKED` while a queue or file exists; there is no staging buffer.
  `GetStats` reports the counts the shim keeps (queues per priority, requests, bytes, files, errors). As
  `DSTORAGE_PRIORITY_STATS` defines them, `TotalRequestCount` and the byte totals grow when a request is submitted
  (not when it is enqueued), and `PendingRequestCount` counts the requests the worker is processing; status and fence
  entries are counted when they are enqueued. A bad
  argument (a null pointer, an invalid queue desc or staging size) is logged once per method and kind, so a wrong
  vtable guess shows up in the log as arguments that make no sense for the method.
- **Files**: `OpenFile` goes through the registry's `kernel32.dll!CreateFileW`, the shim in `file_system.cpp`, so a
  `GAME:` path reads the overlay copy first, then the package, and a path that climbs out is refused (a host path is
  passed through). Read access, `FILE_SHARE_READ`, as the overview says; a failed open returns the Win32 error as an
  HRESULT. `GetHandle` returns a handle of its own (`ReOpenFile`, read access, `FILE_SHARE_READ`) that the caller
  closes; it has its own file position, which the shim's reads do not move. `Close` waits for the submitted requests
  that read the file; a request processed after it fails with `E_DSTORAGE_FILE_NOT_OPEN`. A request holds a reference
  on its file, so the last `Release` cannot close a file a request still reads.
- **Queues**: capacity `0x80`..`0x2000`, priority `LOW`..`REALTIME`, a memory-sourced queue must be `REALTIME`
  (`E_DSTORAGE_INVALID_QUEUE_CAPACITY`, `_INVALID_QUEUE_PRIORITY`, `_INVALID_MEMORY_QUEUE_PRIORITY`). A queue
  holds `capacity - 1` entries; `Query` reports the empty slots and the entries left before the automatic submit,
  which happens when the entries enqueued since the last submit reach half the capacity (unless
  `DSTORAGE_QUEUE_OPTIONS::AutoSubmitDisabled`). An enqueue into a full queue waits for space, as the reference
  says; with auto-submit disabled only a `Submit` from another thread frees it. One worker thread per queue runs
  the submitted entries in order, so every notification follows all the entries before it.
- **Requests**: copied at enqueue. A file request reads `SourceSize` bytes at `FileOffset` into `Destination`; a
  memory request copies `Source` to `Destination` (`memmove`, so an overlapping pair is safe). Checked in this
  order when the worker reaches the request:
  1. cancelled: a success, whatever else is wrong with it;
  2. found at enqueue: a null request pointer (`E_INVALIDARG`) or a file object the shim did not make
     (`E_DSTORAGE_INVALID_FILE_HANDLE`);
  3. reserved option bits (`E_DSTORAGE_RESERVED_FIELDS`), then the source type against the queue's
     (`E_DSTORAGE_INVALID_SOURCE_TYPE`);
  4. BCPack mode above 7, swizzle mode above 4 (`_INVALID_BCPACK_MODE`, `_INVALID_SWIZZLE_MODE`), then zlib, BCPack
     or swizzle, then physical pages (`E_NOTIMPL`, logged once: later PRs);
  5. an `IntermediateSize` without zlib and BCPack (`_INVALID_INTERMEDIATE_SIZE`), `DestinationSize` other than
     `SourceSize` (`_INVALID_DESTINATION_SIZE`), more than 1 GiB in total, then a buffer over 32 MiB
     (`_REQUEST_TOO_LARGE` for both: `DSTORAGE_REQUEST` allows a buffer over 32 MiB only with 64 KiB or 2 MiB pages,
     which are not implemented yet);
  6. a null destination, a null source (memory) or a null file (`E_INVALIDARG`), a closed file (`_FILE_NOT_OPEN`);
  7. a zero size: a success that touches no memory (an assumption: the reference does not say);
  8. a range past the end of the file (`E_DSTORAGE_END_OF_FILE`), then a destination or source the process cannot
     access (`E_DSTORAGE_ACCESS_VIOLATION`; the worker survives it). This needs asynchronous exceptions under
     clang-cl, so `runtime/CMakeLists.txt` builds `dstorage.cpp` with `/EHa` there; MSVC always catches it.
- **Cancel**: `CancelRequestsWithTag(mask, value)` marks every request enqueued before the call and not yet started
  whose `(CancellationTag & mask) == value`. A cancelled request completes as a success without reading and is not
  counted as a failure, as the overview and the reference say, even when it also had an error found at enqueue.
- **Notifications**: `EnqueueStatus` sets the entry to `E_PENDING` at once and, when reached, to the first failure
  since the previous status or signal (else `S_OK`); `IsComplete` is `GetHResult != E_PENDING`; an index past the
  array is `E_DSTORAGE_INDEX_BOUND`, a foreign or null array `E_DSTORAGE_INVALID_STATUS_ARRAY`. `EnqueueSignal` calls
  `ID3D12Fence::Signal` (PC slot 10, which the `d3d12_xs` fence proxies forward) on the worker thread; a null fence is
  `E_DSTORAGE_INVALID_FENCE`. `EnqueueSetEvent` duplicates the handle at once and sets the duplicate, then closes it,
  so an event the title closes in the meantime (and a handle value reused for another object) is never the one set; a
  null or invalid handle is recorded as a failure. Each notification holds a reference on its array or fence until it
  has run.
- **Errors**: a failure goes into the status entry, the queue's error record (failure count and the first failure
  since the last `RetrieveErrorRecord`, with the file name the title opened) and sets the error event.
  `GetErrorEvent` returns a duplicate handle (the reference says the caller closes it). `RetrieveErrorRecord`
  clears the record and resets the event; with nothing recorded it returns `DSTORAGE_COMMAND_TYPE_NONE`. A failed
  `SetEvent` is recorded with command type 3 (the PC API's `EVENT`; the console enum stops at 2).
- **Close and release**: `Close` submits what is enqueued, waits until the worker has run it all and stops the
  worker; later enqueues are dropped and recorded as `E_DSTORAGE_QUEUE_CLOSED`. The last `Release` closes the same
  way, so a queue released with requests in flight finishes them first.

## Not done yet (later PRs)

- **zlib** (`ZlibDecompress`, RFC 1950), then **BCPack** (`BcpackMode`; `tools/bcpack.py` is the reference
  decoder), then **swizzle** (`SwizzleMode`). The decode order is DEFLATE, BCPack, swizzle; with both zlib and BCPack
  `IntermediateSize` is the size between them, and `FileOffset` must then be 16-byte aligned. Each becomes a stage
  in the worker between the read and the destination write, so the request checks above stay where they are.
- **Physical pages** (`SourceIsPhysicalPages`, `DestinationIsPhysicalPages`): arrays of 64 KiB page numbers from
  `XMemAllocatePhysicalPages`; needs the `xmem` shim to translate a page number to the address it mapped.
- **GPU memory**: the console API has no GPU destinations of its own (memory is unified, so a destination is an
  address). An address inside memory the Vulkan layer backs will need its write routed to that memory. The destination
  is written in two places, both in `execute_request` (`read_file_range` for a file source, `guarded_move` for a
  memory source), so that change stays inside the request path.
- **Parallel reads**: the worker reads one request at a time. Overlapped reads with in-order retirement of
  notifications would keep the order guarantee.

## Tests

`runtime/test/dstorage_test.cpp` (ctest `dstorage`) runs on synthetic files in `%TEMP%` through the registry entries
under all three module names: `GAME:` reads with an overlay copy winning, offsets, zero size, a range past the end,
cancellation, auto-submit, a full queue, status arrays, fences (a CPU fence object that checks the data is in place
when it is signalled), events, error records, file and queue close with requests in flight, a destination the
process cannot write, four threads enqueueing at once, the interface-id policy (unknown and system ids refused by
`QueryInterface`, learned by creating calls), a `GetHandle` handle with its own file position, an event closed (and
its handle value reused) before it is set, and cancelled requests that also had an enqueue error.

## Sources and licences

| Source | Facts used | Licence / use |
|---|---|---|
| [GDK DStorage reference](https://learn.microsoft.com/en-us/gaming/gdk/docs/reference/system/dstorage/dstorage_members) and its function, interface, structure and enumeration pages | Names, method signatures, structure members, enum values, requirements rows | [Microsoft Learn Terms of Use](https://learn.microsoft.com/en-us/legal/termsofuse); facts paraphrased, no text copied |
| [DirectStorage overview (GDK)](https://learn.microsoft.com/en-us/gaming/gdk/docs/features/console/storage/directstorage/directstorage-overview) | Queue, auto-submit, cancellation, notification order, file sharing, decode order | Same terms; paraphrased |
| `dstorage.h`, `dstorageerr.h` in the public `Microsoft.Direct3D.DirectStorage` 1.3.0 NuGet package | PC method order, interface ids (to refuse them), capacity and name-length constants, `E_DSTORAGE_*` values | Read for facts only; not vendored. `runtime/shims/dstorage_xs_abi.h` is Leonida's own declaration |

// xmem.dll: the GDK's tracked allocator. Mapped onto the CRT's aligned heap and VirtualQueryEx.
//
// The measured ABI (docs from the clean-room spec) shows four imports:
//   XMemAlloc(size, attributes)
//   XMemFree(ptr, attributes)
//   XMemVirtualQuery(process, address, out64)
//   XMemGetWorkingSetStatistics(kind, out)   (kind 0x20000001 at all seven FSR call sites)
//
// ASSUMPTION: the attribute word is not decoded by the measured call sites (one passes 0x5e50000). Until a call
// site shows the alignment/type fields, every block is 64-byte aligned (covers cache lines and SIMD) and a size of
// zero is rounded up to one byte so the result is a stable, non-null pointer that XMemFree can release.
#include <windows.h>
#include <intrin.h>
#include <malloc.h>
#include <psapi.h>

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <limits>
#include <mutex>
#include <set>
#include <tuple>
#include <utility>

#include "shims.h"
#include "startup_trace.h"

namespace {

constexpr size_t kAlign = 64;

// gta6rt --trace FILE: the first XMemAlloc and the first XMemFree for each distinct raw attribute word go to the
// startup trace as an "xmem_attributes" event (api, the word, the size for an allocation, the caller as module+RVA). The
// bits of the attribute word are not known yet; this records them from a real run without logging every allocation,
// which would fill the file. Off: one relaxed load per call.
void note_attributes(bool is_alloc, uint64_t attributes, size_t size, const void* caller) {
    if (!rt::startup_trace::enabled()) return;
    static std::mutex mutex;
    static std::set<std::pair<bool, uint64_t>> seen;
    {
        const std::lock_guard lock(mutex);
        if (!seen.insert({is_alloc, attributes}).second) return;
    }
    std::string fields = std::string("\"api\":\"") + (is_alloc ? "XMemAlloc" : "XMemFree") +
                         "\",\"attributes\":" + rt::startup_trace::quote(rt::startup_trace::hex(attributes));
    if (is_alloc) fields += ",\"size\":" + std::to_string(size);
    fields += ",\"caller\":" +
              rt::startup_trace::quote(rt::startup_trace::where(reinterpret_cast<uint64_t>(caller)));
    rt::startup_trace::event("xmem_attributes", fields);
}

// XMemAlloc hands out memory the console has just committed, so a fresh block is all zeros there. The CRT's aligned
// heap recycles blocks a previous title allocation freed, which is not: the game's first run put an uninitialised
// container slot (a garbage "this" pointer, different on every start) on a vtable call 1 s into startup. Zeroing is
// what the guest's memory model implies anyway, and it costs one pass over the block.
//
// The block set is what makes XMemFree safe. A title is free to hand back an address inside memory it reserved with
// XMemVirtualAlloc, and _aligned_free on a pointer this shim never allocated corrupts the CRT heap (measured: the same
// build exits with 0xC0000374 heap corruption and, on other starts, with a fault inside the game's own container
// code, because the corruption lands somewhere else each time). Foreign addresses are therefore counted and ignored.
std::mutex g_blocks_mutex;
std::set<void*> g_blocks;

void* __stdcall XMemAlloc(size_t size, uint64_t attributes) {
    note_attributes(true, attributes, size, _ReturnAddress());
    // The console rounds an allocation to its 64 KiB page, so code that writes a few bytes past the requested size
    // stays inside the allocation there. The CRT heap gives exactly the size asked for, and the same overrun lands in
    // the next block's header (measured: the game's first worker threads corrupt the heap within a second). Rounded up
    // here as well, at page granularity.
    constexpr size_t kPage = 0x10000;
    const size_t requested = size ? size : 1;
    const size_t bytes = (requested + kPage - 1) & ~(kPage - 1);
    void* const block = _aligned_malloc(bytes, kAlign);
    if (!block) return nullptr;
    std::memset(block, 0, bytes);
    {
        const std::lock_guard lock(g_blocks_mutex);
        g_blocks.insert(block);
    }
    return block;
}

void __stdcall XMemFree(void* p, uint64_t attributes) {
    note_attributes(false, attributes, 0, _ReturnAddress());
    if (!p) return;
    bool ours = false;
    {
        const std::lock_guard lock(g_blocks_mutex);
        ours = g_blocks.erase(p) != 0;
    }
    if (!ours) {
        static std::atomic<unsigned> foreign{0};
        if (foreign.fetch_add(1, std::memory_order_relaxed) < 8)
            std::fprintf(stderr, "[rt] xmem: XMemFree(%p) of a block this shim did not allocate; ignored (caller %s)\n",
                         p, rt::startup_trace::where(reinterpret_cast<uint64_t>(_ReturnAddress())).c_str());
        return;
    }
    _aligned_free(p);
}

// xmem.h exposes the default allocator its own entry points are built on so a title can wrap or replace it. They take
// the same two arguments and follow the same contract; the game executable imports both, and an unimplemented import
// returns NULL, which is a plausible reason for a boot-time allocator to end up with no heap at all.
void* __stdcall XMemAllocDefault(size_t size, uint64_t attributes) { return XMemAlloc(size, attributes); }

void __stdcall XMemFreeDefault(void* p, uint64_t attributes) { XMemFree(p, attributes); }

// Measured call sites pass a zeroed 64-byte structure. Only the MEMORY_BASIC_INFORMATION-compatible prefix is
// known: callers read at least the base and size fields. Fill that prefix from VirtualQueryEx and zero the rest.
constexpr size_t kVirtualQueryOutSize = 64;
static_assert(sizeof(MEMORY_BASIC_INFORMATION) <= kVirtualQueryOutSize,
              "the 64-byte XMemVirtualQuery output must hold MEMORY_BASIC_INFORMATION");

// VirtualQueryEx's shape: the fourth argument is the length of the caller's buffer. The package DLLs pass 64 bytes;
// the game executable's call sites are not measured yet, and writing 64 bytes into a smaller guest structure would
// corrupt whatever follows it, so a plausible length is honoured and only an implausible one (0, or past the measured
// 64 bytes) falls back to the measured size. The length is recorded on the startup trace so the executable's own call
// sites can be read from a run instead of guessed at.
void note_virtual_query(SIZE_T length, SIZE_T capacity, const void* caller) {
    if (!rt::startup_trace::enabled()) return;
    static std::mutex mutex;
    static std::set<std::pair<uint64_t, SIZE_T>> seen;
    {
        const std::lock_guard lock(mutex);
        if (!seen.insert({reinterpret_cast<uintptr_t>(caller), length}).second) return;
    }
    rt::startup_trace::event("xmem_virtual_query",
                             "\"length\":" + std::to_string(length) + ",\"capacity\":" + std::to_string(capacity) +
                                 ",\"caller\":" + rt::startup_trace::quote(rt::startup_trace::where(
                                                      reinterpret_cast<uint64_t>(caller))));
}

// Returns the number of bytes written (the VirtualQuery convention) or 0 on failure. The exact return type and the
// meaning of every output field are not fully known; callers in the package only test for success.
SIZE_T __stdcall XMemVirtualQuery(HANDLE process, const void* address, void* out, SIZE_T length) {
    if (!out) return 0;
    const SIZE_T capacity = length >= 1 && length < kVirtualQueryOutSize ? length : kVirtualQueryOutSize;
    note_virtual_query(length, capacity, _ReturnAddress());
    std::memset(out, 0, capacity);
    if (!address) return 0;

    static std::atomic<bool> warned{false};
    if (!warned.exchange(true, std::memory_order_relaxed)) {
        std::fprintf(stderr,
                     "[rt] xmem: XMemVirtualQuery: fields after MEMORY_BASIC_INFORMATION are unknown; "
                     "the output is zero-filled there\n");
    }

    // A null handle means the current process; GetCurrentProcess() is the (HANDLE)-1 pseudo handle.
    HANDLE target = process ? process : GetCurrentProcess();
    MEMORY_BASIC_INFORMATION info{};
    SIZE_T written = VirtualQueryEx(target, address, &info, sizeof(info));
    if (!written) return 0;

    const SIZE_T copy = written < capacity ? written : capacity;
    std::memcpy(out, &info, copy);
    return copy;
}

// BOOL XMemGetWorkingSetStatistics(UINT32 kind, XMEM_WORKING_SET_STATISTICS* stats). Measured at all seven call sites
// in amd_fidelityfx_upscaler_xbox.dll (1.0.2.1; calls at RVAs 0x26b3, 0x2ea1, 0x39a9, 0x7452, 0x74d2, 0x7568,
// 0x75b1): ECX is the constant 0x20000001 (its meaning is not known) and RDX points at a stack buffer (0x80 bytes
// where the frame shows it). The caller reads a 64-bit size at +8 and treats a zero return as "no data". Taking the
// pointer from the first argument wrote to address 0x20000001.
// The measured FSR sites establish argument shape, kind and the 64-bit working-set read at +8; +0's title-total memory
// limit interpretation is inferred from other Series implementations (xcl, xbox-emulator) and title engine telemetry
// sizing memory pools (RT-220). Note that if GlobalMemoryStatusEx reports 0 physical memory, XMem falls back to the
// 12 GiB title limit, whereas XSystemGetAppMemoryUsageLimit remains 0.
BOOL __stdcall XMemGetWorkingSetStatistics(uint32_t /*kind*/, void* stats) {
    if (!stats) {
        static std::atomic<bool> warned{false};
        if (!warned.exchange(true, std::memory_order_relaxed)) {
            std::fprintf(stderr, "[rt] xmem: XMemGetWorkingSetStatistics(nullptr) -> FALSE\n");
        }
        return FALSE;
    }

    PROCESS_MEMORY_COUNTERS pmc{};
    if (!GetProcessMemoryInfo(GetCurrentProcess(), &pmc, sizeof(pmc))) return FALSE;

    MEMORYSTATUSEX ms{sizeof(ms)};
    uint64_t total_memory = 12ULL * 1024 * 1024 * 1024;
    if (GlobalMemoryStatusEx(&ms) && ms.ullTotalPhys != 0) {
        total_memory = ms.ullTotalPhys;
    }

    uint8_t* bytes = static_cast<uint8_t*>(stats);
    std::memset(bytes, 0, 16);
    std::memcpy(bytes + 0, &total_memory, sizeof(total_memory));
    uint64_t working_set = pmc.WorkingSetSize;
    std::memcpy(bytes + 8, &working_set, sizeof(working_set));
    return TRUE;
}

// XMem allocation-type page bits select the guest page granularity and must not be forwarded as Win32 flags. Measured
// words (first real run, game_scarlett_master.exe+0x1e1cf60): 0x20402000 = MEM_RESERVE | 0x20400000, the guest 64 KiB
// page mode, and 0x20003000 = MEM_RESERVE | MEM_COMMIT | 0x20000000. Both 0x20400000 and 0x20000000 are guest bits that
// the host has no use for: only the MEM_* pair and the extent decide what the host allocates.
constexpr DWORD kXmemLargePages = 0x20000000;
constexpr DWORD kXmem64kPages = 0x20400000;
constexpr DWORD kXmemPageModeMask = kXmem64kPages;
constexpr SIZE_T kXmemPageSize64k = 0x10000;
constexpr DWORD kXmemHostAllocationTypeMask = MEM_COMMIT | MEM_RESERVE | MEM_TOP_DOWN | MEM_WRITE_WATCH;
// PAGE_EXECUTE_READ (0x20) and above are the guest's executable protections; PAGE_NOACCESS/READONLY/READWRITE are not.
constexpr DWORD kXmemExecutableProtect = 0xF0;

bool round_up_size(SIZE_T value, SIZE_T alignment, SIZE_T& rounded) {
    const SIZE_T max = (std::numeric_limits<SIZE_T>::max)();
    if (alignment == 0 || (alignment & (alignment - 1)) != 0 || value > max - (alignment - 1)) return false;
    rounded = (value + alignment - 1) & ~(alignment - 1);
    return true;
}

// The two guest words the host has no counterpart for - the attribute word and the protection bits above the PAGE_*
// byte - are recorded once per distinct combination on the startup trace (gta6rt --trace FILE) rather than decoded
// from a guess, with the address the host handed back. Measured so far: (0x20402000, 0x21, 0x004) and (0x20402000,
// 0x3, 0x801) for 24 GiB, (0x20003000, 0x183, 0x22004) for 1.2 and 2.3 GiB, (0x20403000, 0x181, 0x4) for 20 MiB.
// Off: one relaxed load per call.
void note_virtual_alloc(DWORD allocation_type, uint64_t attributes, DWORD protect, size_t size, const void* caller,
                        void* result, DWORD error) {
    if (!rt::startup_trace::enabled()) return;
    static std::mutex mutex;
    static std::set<std::tuple<DWORD, uint64_t, DWORD>> seen;
    {
        const std::lock_guard lock(mutex);
        if (!seen.insert({allocation_type, attributes, protect}).second) return;
    }
    std::string fields = "\"allocation_type\":" + rt::startup_trace::quote(rt::startup_trace::hex(allocation_type)) +
                         ",\"attributes\":" + rt::startup_trace::quote(rt::startup_trace::hex(attributes)) +
                         ",\"protect\":" + rt::startup_trace::quote(rt::startup_trace::hex(protect)) +
                         ",\"size\":" + std::to_string(size) +
                         ",\"result\":" + rt::startup_trace::quote(rt::startup_trace::hex(
                                              reinterpret_cast<uintptr_t>(result))) +
                         ",\"caller\":" +
                         rt::startup_trace::quote(rt::startup_trace::where(reinterpret_cast<uint64_t>(caller)));
    if (!result) fields += ",\"error\":" + rt::startup_trace::quote(rt::startup_trace::hex(error));
    rt::startup_trace::event("xmem_virtual_alloc", fields);
}

void* WINAPI XMemVirtualAlloc(void* address, SIZE_T size, DWORD allocation_type, ULONGLONG attributes, DWORD protect) {
    if (size == 0) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return nullptr;
    }

    const DWORD unsupported = allocation_type & ~(kXmemHostAllocationTypeMask | kXmemPageModeMask);
    if (unsupported != 0) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return nullptr;
    }

    // The console separates reservation, physical-page allocation and mapping, and the title reserves 24 GiB twice
    // before it asks for anything to be backed. Committing those here is what a PC cannot do: the second start of the
    // game failed its next 23 GiB request with ERROR_COMMITMENT_LIMIT (0x5af) because the two reservations alone had
    // used up the host's commit limit. A reserve stays a reserve; the guest's own commit and mapping calls are what
    // bring pages in, and until they are served the region is address space only, exactly as on the console.
    DWORD host_type = allocation_type & kXmemHostAllocationTypeMask;
    if ((host_type & (MEM_COMMIT | MEM_RESERVE)) == 0) host_type |= MEM_RESERVE;
    const DWORD requested_protect = protect & 0xFFu;
    const DWORD host_protect =
        (requested_protect & kXmemExecutableProtect) != 0 ? requested_protect : PAGE_READWRITE;

    const DWORD page_mode = allocation_type & kXmemPageModeMask;
    SYSTEM_INFO system_info{};
    GetSystemInfo(&system_info);
    const SIZE_T page_granularity = page_mode == kXmem64kPages ? kXmemPageSize64k : system_info.dwPageSize;
    const SIZE_T base_granularity =
        page_mode == kXmem64kPages ? kXmemPageSize64k : system_info.dwAllocationGranularity;
    if (page_granularity == 0 || (page_granularity & (page_granularity - 1)) != 0 || base_granularity == 0 ||
        (base_granularity & (base_granularity - 1)) != 0) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return nullptr;
    }

    if (address != nullptr) {
        const uintptr_t requested = reinterpret_cast<uintptr_t>(address);
        const uintptr_t max = (std::numeric_limits<uintptr_t>::max)();
        if (size > max - requested) {
            SetLastError(ERROR_INVALID_PARAMETER);
            return nullptr;
        }
        const uintptr_t requested_end = requested + size;
        const uintptr_t base = requested & ~(static_cast<uintptr_t>(base_granularity) - 1);
        if (requested_end > max - (static_cast<uintptr_t>(page_granularity) - 1)) {
            SetLastError(ERROR_INVALID_PARAMETER);
            return nullptr;
        }
        const uintptr_t end = (requested_end + static_cast<uintptr_t>(page_granularity) - 1) &
                              ~(static_cast<uintptr_t>(page_granularity) - 1);
        address = reinterpret_cast<void*>(base);
        size = static_cast<SIZE_T>(end - base);
    } else if (page_mode == kXmem64kPages && !round_up_size(size, page_granularity, size)) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return nullptr;
    }

    void* const result = VirtualAlloc(address, size, host_type, host_protect);
    const DWORD error = result ? ERROR_SUCCESS : GetLastError();
    note_virtual_alloc(allocation_type, attributes, protect, size, _ReturnAddress(), result, error);
    return result;
}

UINT WINAPI XMemGetWriteWatch(DWORD flags, void* base_address, SIZE_T region_size, void** addresses, ULONG_PTR* count,
                              ULONG* granularity) {
    return GetWriteWatch(flags, base_address, region_size, addresses, count, granularity);
}

UINT WINAPI XMemResetWriteWatch(void* base_address, SIZE_T region_size) {
    return ResetWriteWatch(base_address, region_size);
}

// XMem's physical-page layer. On the console the title reserves address space with XMemVirtualAlloc and then brings
// pages in with these calls: allocate a number of physical pages, map them at a guest address, or hand a range to
// another engine (transfer / scatter). A PC process has no such split, but the calls still have to succeed - a title
// that gets zero back from the allocation step treats the pool as unavailable. The argument shapes are not measured
// yet, so the first call of each entry point is printed with its four register arguments (rcx, rdx, r8, r9) and the
// call is served the cheap way: the allocation is a committed host region of the requested size, and the mapping
// steps report success. That makes the shapes readable from one run instead of guessed twice.
void note_page_call(const char* api, uintptr_t a, uintptr_t b, uintptr_t c, uintptr_t d) {
    static std::mutex mutex;
    static std::set<std::string> seen;
    {
        const std::lock_guard lock(mutex);
        if (!seen.insert(api).second) return;
    }
    std::fprintf(stderr, "[rt] xmem: %s(0x%llx, 0x%llx, 0x%llx, 0x%llx) from %s\n", api,
                 static_cast<unsigned long long>(a), static_cast<unsigned long long>(b),
                 static_cast<unsigned long long>(c), static_cast<unsigned long long>(d),
                 rt::startup_trace::where(reinterpret_cast<uint64_t>(_ReturnAddress())).c_str());
}

void* __stdcall XMemAllocatePhysicalPages(uintptr_t size, uintptr_t b, uintptr_t c, uintptr_t d) {
    note_page_call("XMemAllocatePhysicalPages", size, b, c, d);
    if (!size) return nullptr;
    return VirtualAlloc(nullptr, size, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
}

void* __stdcall XMemMapPhysicalPages(uintptr_t a, uintptr_t b, uintptr_t c, uintptr_t d) {
    note_page_call("XMemMapPhysicalPages", a, b, c, d);
    return reinterpret_cast<void*>(1);
}

void* __stdcall XMemMapPhysicalPagesScatter(uintptr_t a, uintptr_t b, uintptr_t c, uintptr_t d) {
    note_page_call("XMemMapPhysicalPagesScatter", a, b, c, d);
    return reinterpret_cast<void*>(1);
}

void* __stdcall XMemFreePhysicalPages(uintptr_t a, uintptr_t b, uintptr_t c, uintptr_t d) {
    note_page_call("XMemFreePhysicalPages", a, b, c, d);
    return reinterpret_cast<void*>(1);
}

void* __stdcall XMemTransferMemory(uintptr_t a, uintptr_t b, uintptr_t c, uintptr_t d) {
    note_page_call("XMemTransferMemory", a, b, c, d);
    return reinterpret_cast<void*>(1);
}

// kernel32!VirtualAlloc as a title calls it. The console's allocation type has two bits of its own, MEM_GRAPHICS
// (0x10000000, memory the GPU reads) and MEM_TITLE (0x40000000, the title's own heap). On a PC the host rejects both
// with ERROR_INVALID_PARAMETER (measured on Windows 11 26200: VirtualAlloc(NULL, 64 KiB, MEM_COMMIT | MEM_RESERVE |
// 0x10000000, PAGE_READWRITE) returns NULL; so does 0x40000000), so an engine allocating its heaps this way gets
// NULL at boot. Both are hints about where the memory lives; the host has no such split, so they are dropped and
// the rest is passed through. Not done: the console protection bits, and whether graphics memory has to come from
// an arena the D3D12 layer can see (#313).
constexpr DWORD kMemGraphics = 0x10000000;
constexpr DWORD kMemTitle = 0x40000000;

LPVOID WINAPI TitleVirtualAlloc(LPVOID address, SIZE_T size, DWORD allocation_type, DWORD protect) {
    return VirtualAlloc(address, size, allocation_type & ~(kMemGraphics | kMemTitle), protect);
}

}  // namespace

#define TITLE_VIRTUAL_ALLOC(dll) {dll, "VirtualAlloc", reinterpret_cast<void*>(&TitleVirtualAlloc)}

// The classic libraries and the memory api sets a title's imports name.
SHIM_TABLE(title_memory,
    TITLE_VIRTUAL_ALLOC("kernel32.dll"),
    TITLE_VIRTUAL_ALLOC("kernelbase.dll"),
    TITLE_VIRTUAL_ALLOC("api-ms-win-core-memory-l1-1-0.dll"),
    TITLE_VIRTUAL_ALLOC("api-ms-win-core-memory-l1-1-1.dll"),
    TITLE_VIRTUAL_ALLOC("api-ms-win-core-memory-l1-1-2.dll"),
    TITLE_VIRTUAL_ALLOC("api-ms-win-core-memory-l1-1-3.dll"),
    TITLE_VIRTUAL_ALLOC("api-ms-win-core-memory-l1-1-4.dll"),
    TITLE_VIRTUAL_ALLOC("api-ms-win-core-memory-l1-1-5.dll"),
    TITLE_VIRTUAL_ALLOC("api-ms-win-core-memory-l1-1-6.dll"),
);

SHIM_TABLE(xmem,
    {"xmem.dll", "XMemAlloc", reinterpret_cast<void*>(&XMemAlloc)},
    {"xmem.dll", "XMemAllocDefault", reinterpret_cast<void*>(&XMemAllocDefault)},
    {"xmem.dll", "XMemFree", reinterpret_cast<void*>(&XMemFree)},
    {"xmem.dll", "XMemFreeDefault", reinterpret_cast<void*>(&XMemFreeDefault)},
    {"xmem.dll", "XMemVirtualQuery", reinterpret_cast<void*>(&XMemVirtualQuery)},
    {"xmem.dll", "XMemGetWorkingSetStatistics", reinterpret_cast<void*>(&XMemGetWorkingSetStatistics)},
    {"xmem.dll", "XMemVirtualAlloc", reinterpret_cast<void*>(&XMemVirtualAlloc)},
    {"xmem.dll", "XMemAllocatePhysicalPages", reinterpret_cast<void*>(&XMemAllocatePhysicalPages)},
    {"xmem.dll", "XMemMapPhysicalPages", reinterpret_cast<void*>(&XMemMapPhysicalPages)},
    {"xmem.dll", "XMemMapPhysicalPagesScatter", reinterpret_cast<void*>(&XMemMapPhysicalPagesScatter)},
    {"xmem.dll", "XMemFreePhysicalPages", reinterpret_cast<void*>(&XMemFreePhysicalPages)},
    {"xmem.dll", "XMemTransferMemory", reinterpret_cast<void*>(&XMemTransferMemory)},
    {"xmem.dll", "XMemGetWriteWatch", reinterpret_cast<void*>(&XMemGetWriteWatch)},
    {"xmem.dll", "XMemResetWriteWatch", reinterpret_cast<void*>(&XMemResetWriteWatch)},
);

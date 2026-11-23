// The audio coprocessor's calls the retail title needs to finish its audio initialization, from the call sites in
// docs/audio_hal_exe_calls.md and the boot's own trace.
//
// The title's audio stack is AcpHal (the coprocessor's HAL), xapu (the command queue) and chad (the DSP). There is no
// coprocessor here, so what these functions return is a promise rather than a device: blocks it can write into, handles
// that are non-null and stable, and an address translation that maps back. The two places that made that necessary are
// measurable in the boot:
//
//   game_scarlett_master.exe+0x43aec40   AcpHalAllocateShapeContexts(&context), context+0x10 = 0x15e
//   +0x43aec80                           memset([context+0x38], 0, [context+0x10] * 64)   <- wrote 0x20 (null base)
//   +0x43a6110                           ApuAlloc(&block, 0, 0x222e00, 0x800)
//   +0x43aed18                           the 350-entry table is filled with buffers from that block
//
// An unimplemented import returns 0 with its out parameters unset, and every one of these is an out parameter the title
// writes through immediately, so each faulted as a write to a near-null address. Nothing here produces sound; the point
// is that a title whose audio device answers can finish bringing the renderer up.
#include <windows.h>

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <utility>
#include <vector>

#include "shims.h"

namespace {

std::atomic<bool> g_logged_create{false};
std::atomic<bool> g_logged_contexts{false};
std::atomic<bool> g_logged_heap{false};
std::atomic<bool> g_logged_alloc{false};
std::atomic<bool> g_logged_map{false};

// Handles the title treats as opaque: non-null, aligned, and alive for the run. A block of the shim's own is as good as
// anything, and it keeps a title that writes through a handle away from address zero.
alignas(64) unsigned char g_heap[64];

bool first(std::atomic<bool>& flag) { return !flag.exchange(true, std::memory_order_relaxed); }

// ApuMapVirtualAddress hands the coprocessor a 32-bit address for a CPU buffer and ApuMapApuAddress turns such an
// address back into the pointer; the title does both around every buffer it shares. The host addresses are 64-bit, so
// the pair is a table: a token per buffer, allocated in its own range and never reused within a run.
std::mutex g_map_mutex;
std::vector<std::pair<std::uint32_t, void*>> g_mapped;
std::uint32_t g_next_token = 0x10000000;

// The shape-context structure: +0x10 is the count the title wants (0x15e at its first call) and +0x38 the block the HAL
// hands back, which the title then zeroes as count * 64 bytes. The block is committed for the run: the title's own
// release call is not measured, and one block per call is what a HAL stub can afford.
HRESULT __stdcall AcpHalAllocateShapeContexts(void* context) {
    if (!context) return E_POINTER;
    std::uint32_t count = 0;
    std::memcpy(&count, static_cast<const std::uint8_t*>(context) + 0x10, sizeof count);
    const std::size_t bytes = std::size_t(count) * 64u;
    void* block = nullptr;
    if (bytes && bytes <= (1u << 24))  // a count this layer cannot make sense of is refused, not multiplied out
        block = VirtualAlloc(nullptr, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (bytes && !block) return E_OUTOFMEMORY;
    std::memcpy(static_cast<std::uint8_t*>(context) + 0x38, &block, sizeof block);
    if (first(g_logged_contexts))
        std::fprintf(stderr, "[rt] audio: AcpHalAllocateShapeContexts: %u contexts, %zu bytes at %p\n", count, bytes,
                     block);
    return S_OK;
}

// AcpHalCreate(out, ...): the handle it writes is dereferenced as an object — `[handle]` is a vtable and the title calls
// its first entry with (0x1000, 0x1000) right after the call (game_scarlett_master.exe+0x43aedb7) — so it has to be a
// pointer to something whose first word is a table of callable methods, not an opaque token. Every entry answers S_OK
// and writes nothing: the coprocessor is absent, and a title that gets its HAL object acknowledged initializes the rest.
std::uint64_t __stdcall hal_method(void* self, std::uint64_t a1, std::uint64_t a2) {
    (void)self;
    (void)a1;
    (void)a2;
    return 0;  // S_OK
}

void* const g_hal_vtable[16] = {
    reinterpret_cast<void*>(&hal_method), reinterpret_cast<void*>(&hal_method),
    reinterpret_cast<void*>(&hal_method), reinterpret_cast<void*>(&hal_method),
    reinterpret_cast<void*>(&hal_method), reinterpret_cast<void*>(&hal_method),
    reinterpret_cast<void*>(&hal_method), reinterpret_cast<void*>(&hal_method),
    reinterpret_cast<void*>(&hal_method), reinterpret_cast<void*>(&hal_method),
    reinterpret_cast<void*>(&hal_method), reinterpret_cast<void*>(&hal_method),
    reinterpret_cast<void*>(&hal_method), reinterpret_cast<void*>(&hal_method),
    reinterpret_cast<void*>(&hal_method), reinterpret_cast<void*>(&hal_method),
};
void* g_hal_object = const_cast<void**>(g_hal_vtable);  // the object *is* its vtable pointer: [handle] is the table

HRESULT __stdcall AcpHalCreate(void** out, std::uint64_t a1, std::uint64_t a2, std::uint64_t a3) {
    (void)a1;
    (void)a2;
    (void)a3;
    if (!out) return E_POINTER;
    *out = &g_hal_object;
    if (first(g_logged_create))
        std::fprintf(stderr, "[rt] audio: AcpHalCreate -> HAL object %p (16 vtable entries, all S_OK)\n", &g_hal_object);
    return S_OK;
}

HRESULT __stdcall AcpHalReleaseShapeContexts(void* handle) {
    (void)handle;
    return S_OK;
}

// ApuCreateHeap(size, flags): the size the title asks for (its first call shifts a count by 20, so bytes). The handle is
// the HAL's own, not the title's; the call succeeds so the initialization after it runs.
HRESULT __stdcall ApuCreateHeap(std::uint32_t size, std::uint32_t flags) {
    (void)g_heap;
    if (first(g_logged_heap))
        std::fprintf(stderr, "[rt] audio: ApuCreateHeap(%u bytes, flags 0x%x): no coprocessor, the call succeeds\n", size,
                     flags);
    return S_OK;
}

// ApuAlloc(out, alignment, size, flags): the block the caller asked for, page-aligned, through the out parameter (the
// return value is the HRESULT; the site tests only its sign and reads the block from the stack slot it passed in).
HRESULT __stdcall ApuAlloc(void** out, std::uint32_t alignment, std::uint32_t size, std::uint32_t flags) {
    if (!out) return E_POINTER;
    *out = nullptr;
    if (!size) return S_OK;
    const std::size_t bytes = (std::size_t(size) + 0xFFFu) & ~std::size_t(0xFFFu);
    void* block = VirtualAlloc(nullptr, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!block) return E_OUTOFMEMORY;
    *out = block;
    if (first(g_logged_alloc))
        std::fprintf(stderr, "[rt] audio: ApuAlloc(%u bytes, alignment %u, flags 0x%x) -> %p\n", size, alignment, flags,
                     block);
    return S_OK;
}

HRESULT __stdcall ApuFree(void* block) {
    if (block) VirtualFree(block, 0, MEM_RELEASE);
    return S_OK;
}

// Whether the coprocessor could see a buffer: everything this layer hands out is plain host memory, so it can.
unsigned char __stdcall ApuIsVirtualAddressValid(void* address, std::uint32_t size) {
    (void)size;
    return address ? 1 : 0;
}

std::uint32_t __stdcall ApuMapVirtualAddress(void* address, std::uint32_t size) {
    if (!address) return 0;
    const std::lock_guard lock(g_map_mutex);
    for (const auto& [token, mapped] : g_mapped)
        if (mapped == address) return token;
    const std::uint32_t token = g_next_token;
    g_next_token += 0x1000;
    g_mapped.emplace_back(token, address);
    if (first(g_logged_map))
        std::fprintf(stderr, "[rt] audio: ApuMapVirtualAddress(%p, %u) -> 0x%x (a token, not a coprocessor address)\n",
                     address, size, token);
    return token;
}

void* __stdcall ApuMapApuAddress(std::uint32_t token) {
    const std::lock_guard lock(g_map_mutex);
    for (const auto& [mapped_token, address] : g_mapped)
        if (mapped_token == token) return address;
    return nullptr;
}

}  // namespace

SHIM_TABLE(acphal,
    {"acphal.dll", "AcpHalCreate", reinterpret_cast<void*>(&AcpHalCreate)},
    {"acphal.dll", "AcpHalAllocateShapeContexts", reinterpret_cast<void*>(&AcpHalAllocateShapeContexts)},
    {"acphal.dll", "AcpHalReleaseShapeContexts", reinterpret_cast<void*>(&AcpHalReleaseShapeContexts)},
    {"acphal.dll", "ApuCreateHeap", reinterpret_cast<void*>(&ApuCreateHeap)},
    {"acphal.dll", "ApuAlloc", reinterpret_cast<void*>(&ApuAlloc)},
    {"acphal.dll", "ApuFree", reinterpret_cast<void*>(&ApuFree)},
    {"acphal.dll", "ApuIsVirtualAddressValid", reinterpret_cast<void*>(&ApuIsVirtualAddressValid)},
    {"acphal.dll", "ApuMapVirtualAddress", reinterpret_cast<void*>(&ApuMapVirtualAddress)},
    {"acphal.dll", "ApuMapApuAddress", reinterpret_cast<void*>(&ApuMapApuAddress)},
);

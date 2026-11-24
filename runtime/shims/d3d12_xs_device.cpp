// Xbox-ABI proxy around a PC ID3D12Device. See d3d12_xs_device.h for the measured slots and the limits.
//
// How forwarding works: the proxy object's first word is a vtable built once from one thunk per slot. A thunk
// receives the proxy in RCX, replaces it with the PC device pointer from offset 8, and tail-calls the PC method at
// the same index through a 15-argument integer prototype. Every PC D3D12 device method takes integer/pointer
// arguments only, so the x64 registers and stack words line up exactly. The measured exceptions are IUnknown (0-2),
// slot 14 (which wraps the heap), 62 and 76, 26 (the title passes the return buffer in RCX and the device in RDX), the
// struct-returning slot 25 (same C ABI form: see "Struct-returning device methods" below), plus the heap proxy's 9/10
// conversion from the PC hidden-pointer form to the Xbox RAX form. Slots 8/75 (command queues), 36 (fences) and 32
// (a shared fence) wrap what they create in queue and fence proxies built the same way, and slots 31/34/35/45/46/50
// hand the real objects behind heap, queue and fence proxies to the real device (see the command queue and fence
// section).
//
// A thunk declares 14 argument words, but the title passes only as many as the method takes, so the caller's frame
// may end right after the four register homes. The compiler owns a function's incoming stack-argument slots and may
// write them, a tail call included. Every function a thunk hands its words to therefore takes them in the same
// positions (slot 8: desc, riid, out as a1-a3; slot 50: a1-a5; and so on). A tail call then writes back only the
// values it read from those slots. A helper that moved a word to another stack position would overwrite the title's
// frame. xs_proxy_mock_test's mock hit this in an optimized build, and its thunks now pass their words by pointer.
//
// The public HLE Freminet/ScarlettX reports five more Xbox device slots (a third-party observation, not a Microsoft
// ABI); the generic thunk routes them before it forwards anything: 48 CreateCommandQueueX, 73 CreateCommandListX,
// 78 SetFrameIntervalX, 79 ScheduleFrameEventX and 80 WaitFrameEventX. The slots can be moved with LEONIDA_XS_*_SLOT
// for a trace that disagrees. The frame-event calls answer from a per-device token counter: there is no vblank source
// behind the device before a presenter exists, so WaitFrameEventX returns at once with a fresh non-zero token.
#include "d3d12_xs_device.h"
#include "../src/guest_memory.h"
#include "../src/arena_window.h"
#include "d3d12_trace.h"
#include "d3d12_xs_alloc_size.h"
#include "d3d12_xs_desc_clean.h"
#include "../src/frame_pacer.h"
#include "../src/shader_library.h"  // leonida::fspx_payload_from_parts for slot 67

#ifdef LEONIDA_VULKAN_DEVICE
// PresentX wiring: the queue's console slots land on the Xbox presentation core, which needs the Vulkan layer's
// WindowPresenter. Other builds of this file (mock and COM tests) keep the refused-slot behaviour.
#include "xbox_present.h"
#endif

#include <windows.h>
#include <dxgi1_4.h>

#include <algorithm>
#include <atomic>
#include <functional>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iterator>
#include <limits>
#include <memory>
#include <mutex>
#include <new>
#include <utility>
#include <vector>

#include "d3d12_xs_command_list_iid.h"

namespace rt::d3d12_xs {
namespace {

// The title's highest reached slot is 133 (docs/retail_boot_chain.md §3.4), and a table shorter than that leaves a null
// pointer where the title expects a method, which is an execute fault at 0 rather than an E_NOTIMPL. 128 covered the PC
// device; 192 covers the console slots the boot reaches past it.
constexpr unsigned kMaxSlots = 192;
static_assert(kMaxSlots > 133, "the proxy table must reach the console device slot 133");
static_assert(kMaxSlots > 83, "the proxy table must cover ID3D12Device14 (83 slots)");
static_assert(sizeof(void*) == 8, "the Xbox-ABI proxy is x64-only");
static_assert(sizeof(D3D12_CPU_DESCRIPTOR_HANDLE) == 8,
              "the Xbox handle ABI is an 8-byte value in RAX; the heap thunk converts the PC hidden-pointer form");
static_assert(sizeof(D3D12_RESOURCE_DESC) == 56, "the measured Xbox description prefix is 56 bytes");

// One queue per command list type is all the console submit ever needs (DIRECT, COMPUTE, COPY, BUNDLE).
constexpr int kSubmitQueueSlots = 4;

struct DeviceProxy {
    // A queue the console submit may execute lists on, with its type (see the submit's table below).
    struct SubmitQueue {
        ID3D12CommandQueue* queue;
        D3D12_COMMAND_LIST_TYPE type;
    };

    void** vtable;          // offset 0: the caller's ID3D12Device* points here
    ID3D12Device* real;     // offset 8: the PC device every thunk forwards to
    void** real_vtable;
    unsigned slot_count;    // number of valid entries in real_vtable
    std::atomic<unsigned long> refs;
    // The PresentX state (window, presenter, XboxPresentation) the first queue PresentX builds on this device,
    // under LEONIDA_VULKAN_DEVICE; always null elsewhere. Opaque: PresentState is defined with the queue proxy.
    void* present_state = nullptr;
    // The upload-memory arena console list slot 148 (AllocateUploadMemory) carves from; always null until the
    // first call. Opaque: UploadArena is defined with the list proxy. Atomic: lists record on several threads
    // at once, so the first AllocateUploadMemory installs the arena by compare-exchange.
    std::atomic<void*> upload_state{nullptr};
    // The console runtime layout this title was built against (docs/d3d12_xs_console_abi.md §4): the queue proxy
    // writes it on detection and every object shares it — the queue/list slot tails shift +2 under the newer one.
    std::atomic<unsigned char> console_layout{0};  // 0 unknown, 1 older, 2 newer
    // Set by create_device_proxy: false hands real (PC-shaped) lists back from CreateCommandList for callers
    // compiled against the PC headers.
    bool console_lists = true;
    // The device's IDXGIDevice1 (see "IDXGIDevice1 on the device" below): a title's IDXGIDevice1* points here.
    void** dxgi_vtable;
    DxgiAdapterProvider adapter_provider;
    ResourceByVaProvider resource_provider;  // slot 70: a buffer by console VA; null leaves slot 70 on the null view
    std::atomic<int> gpu_thread_priority;
    std::atomic<unsigned> max_frame_latency;
    // The first-frame subset of SetFrameIntervalX/ScheduleFrameEventX/WaitFrameEventX. Tokens were never measured;
    // they start at 1 and only have to be fresh and non-zero for the boot sequence. Waiting returns at once: there is
    // no vblank source until a presenter exists, and a bounded zero wait cannot stall the first frame.
    std::atomic<std::uint32_t> frame_interval_us{0};
    std::atomic<std::uint32_t> frame_latency{1};
    std::atomic<bool> frame_event_scheduled{false};
    std::shared_ptr<leonida::FrameTokenSource> frame_tokens = std::make_shared<leonida::FrameTokenSource>();
    // The lists the title has recorded into since the last console submit (queue slot 35), which is what that submit
    // executes (see "The console list lifecycle" at console_begin_recording). Guarded by pending_mutex; a list removes
    // itself when it is destroyed.
    std::mutex pending_mutex;
    std::vector<void*> pending_lists;
    // The real queues the console submit (queue slot 35) can hand a list to, with their types. The console submit
    // hands every recorded list to one submit call, while a PC queue refuses a list of another type (the layer logs
    // "list 0 has type 2, the queue type 0"), so each list goes to a queue of its own type. `real_queues` are the
    // title's own queues, borrowed (an entry is dropped when the queue proxy is destroyed); `made_queues` are ones
    // this device made because the title had none, owned and released in proxy_release. Fixed arrays, no allocation:
    // xs_proxy_mock injects allocation failures and expects them at the proxy's own creation, never in bookkeeping.
    std::mutex queues_mutex;
    SubmitQueue real_queues[kSubmitQueueSlots] = {};
    int real_queue_count = 0;
    ID3D12CommandQueue* made_queues[kSubmitQueueSlots] = {};
    int made_queue_count = 0;
    // The 4 MiB windows of title memory that have been copied into a buffer the shader can read (guest_translate), as
    // GuestWindows*. Created on the first such address; freed in proxy_release.
    std::atomic<void*> guest_windows{nullptr};
};

// Freed in proxy_release; defined where the states are complete.
void destroy_present_state(void* present_state);
void destroy_upload_state(void* upload_state);

// The console's submit (queue slot 35): executes the lists recorded since the last submit. Defined with the list proxy,
// which is what it walks (see console_begin_recording).
void console_submit_batch(DeviceProxy* device, IUnknown* queue, void** queue_vtable);

// The real queues the console submit executes lists on (see console_submit_batch): released with the device, which
// holds a reference to each. Defined with the submit.
// The real queues the console submit executes lists on (see console_submit_batch): released with the device, which
// holds a reference to each. Defined with the submit.
void release_real_queues(DeviceProxy* device);
// Drops a queue the title destroyed from the submit's table: the entry is a borrow, not a reference.
void forget_real_queue(DeviceProxy* device, const void* queue);

constexpr unsigned kHeapSlots = 11;  // IUnknown + ID3D12Object + DeviceChild + Pageable + GetDesc + 2 handles

struct HeapProxy {
    void** vtable;          // offset 0: the caller's ID3D12DescriptorHeap* points here
    ID3D12DescriptorHeap* real;
    DeviceProxy* device;    // keeps the Xbox-ABI device alive; GetDevice returns it
    void** real_vtable;
    std::atomic<unsigned long> refs;
    // Non-zero when the title's XMem allocation hook supplied the heap's backing address: slot 9 returns
    // custom_cpu_start and device descriptor calls translate [custom_cpu_start, +custom_byte_size) to real_cpu_start.
    SIZE_T custom_cpu_start;
    SIZE_T custom_byte_size;
    SIZE_T real_cpu_start;
    DescriptorHeapBackingFree custom_free;
};

struct DescriptorRange {
    SIZE_T custom_start;
    SIZE_T custom_end;  // exclusive
    SIZE_T real_start;
    const DeviceProxy* device;
    const HeapProxy* heap;
    bool gpu;            // the range is the heap's console (GPU) handle space, not the title's XMem backing
    SIZE_T backing_start;  // the title's XMem backing of the same heap: what it may have written itself
};

// Immortal: late COM Release calls during process teardown may still unregister a heap or check the table.
std::mutex& descriptor_range_mutex() {
    static auto* m = new std::mutex;
    return *m;
}

std::vector<DescriptorRange>& descriptor_ranges() {
    static auto* v = new std::vector<DescriptorRange>;
    return *v;
}

std::atomic<std::size_t> g_custom_range_count{0};

std::mutex& heap_alloc_mutex() {
    static auto* m = new std::mutex;
    return *m;
}

DescriptorHeapBackingAlloc g_heap_alloc = nullptr;
DescriptorHeapBackingFree g_heap_free = nullptr;
bool g_heap_alloc_explicit = false;

bool register_descriptor_range(const DeviceProxy* device, const HeapProxy* heap, SIZE_T custom_start,
                               SIZE_T byte_size, SIZE_T real_start, bool gpu = false, SIZE_T backing_start = 0) {
    if (custom_start == 0 || byte_size == 0 || custom_start > (std::numeric_limits<SIZE_T>::max)() - byte_size)
        return false;
    try {
        std::lock_guard lock(descriptor_range_mutex());
        descriptor_ranges().push_back(
            {custom_start, custom_start + byte_size, real_start, device, heap, gpu, backing_start});
        g_custom_range_count.store(descriptor_ranges().size(), std::memory_order_release);
        return true;
    } catch (const std::bad_alloc&) {
        return false;
    }
}

void unregister_descriptor_range(const HeapProxy* heap) {
    if (g_custom_range_count.load(std::memory_order_acquire) == 0) return;
    std::lock_guard lock(descriptor_range_mutex());
    auto& ranges = descriptor_ranges();
    for (auto it = ranges.begin(); it != ranges.end();) {
        if (it->heap == heap) {
            it = ranges.erase(it);
        } else {
            ++it;
        }
    }
    g_custom_range_count.store(ranges.size(), std::memory_order_release);
}

D3D12_CPU_DESCRIPTOR_HANDLE translate_cpu_handle(const DeviceProxy* device, D3D12_CPU_DESCRIPTOR_HANDLE handle) {
    if (handle.ptr == 0 || g_custom_range_count.load(std::memory_order_acquire) == 0) return handle;
    std::lock_guard lock(descriptor_range_mutex());
    const auto& ranges = descriptor_ranges();
    for (auto it = ranges.rbegin(); it != ranges.rend(); ++it) {
        if (device && it->device != device) continue;
        if (handle.ptr >= it->custom_start && handle.ptr < it->custom_end) {
            return D3D12_CPU_DESCRIPTOR_HANDLE{it->real_start + (handle.ptr - it->custom_start)};
        }
    }
    // A handle below the arena's size is a console VA, not a host pointer: a descriptor write through it lands in
    // whatever memory that number names, and the heap slot the shader reads stays zero.
    if (std::getenv("LEONIDA_LOG_TABLE") && leonida::arena_host_base && handle.ptr < leonida::arena_host_size) {
        static std::atomic<int> logged{0};
        if (logged.fetch_add(1, std::memory_order_relaxed) < 8)
            std::fprintf(stderr, "[rt] d3d12_xs: descriptor handle 0x%llx is a console VA, not a heap backing\n",
                         static_cast<unsigned long long>(handle.ptr));
    }
    return handle;
}

// Copies the range that contains `ptr` (a CPU handle or a GPU handle, see LEONIDA_XS_GPU_HANDLE) into `out`.
bool find_descriptor_range(const DeviceProxy* device, SIZE_T ptr, DescriptorRange* out) {
    std::lock_guard lock(descriptor_range_mutex());
    const auto& ranges = descriptor_ranges();
    for (auto it = ranges.rbegin(); it != ranges.rend(); ++it) {
        if (device && it->device != device) continue;
        if (ptr >= it->custom_start && ptr < it->custom_end) {
            if (out) *out = *it;
            return true;
        }
    }
    return false;
}

std::atomic<std::uint64_t> g_hits[kMaxSlots];
std::atomic<bool> g_first[kMaxSlots];
std::atomic<bool> g_beyond_warned[kMaxSlots];
std::atomic<std::uint64_t> g_heap_hits[kHeapSlots];
std::atomic<bool> g_heap_first[kHeapSlots];
// The hit counters are for tests. They count from the first call of a *_slot_hits reader or of
// reset_device_proxy_stats, so the calls of a title, which reads no counter, write no shared counter.
std::atomic<bool> g_count_hits{false};

// For the readers and the reset. The flag is written one time only.
void start_hit_counts() {
    if (!g_count_hits.load(std::memory_order_relaxed)) g_count_hits.store(true, std::memory_order_relaxed);
}

void count_hit(std::atomic<std::uint64_t>& hits) {
    if (g_count_hits.load(std::memory_order_relaxed)) hits.fetch_add(1, std::memory_order_relaxed);
}

// True for the first call of a slot only. The flag is read first: a compare-exchange takes the cache line for
// writing also when it fails, and the flags of all slots share one or two lines.
bool first_call(std::atomic<bool>& seen) {
    if (seen.load(std::memory_order_relaxed)) return false;
    bool expected = false;
    return seen.compare_exchange_strong(expected, true, std::memory_order_relaxed);
}

bool trace_enabled() {
    static const bool enabled = [] {
        char value[8] = {};
        const DWORD length = GetEnvironmentVariableA("XS_TRACE", value, sizeof(value));
        return length > 0 && value[0] != '\0' && value[0] != '0';
    }();
    return enabled;
}

const char* slot_name(unsigned slot) {
    switch (slot) {
        case 0: return "QueryInterface";
        case 1: return "AddRef";
        case 2: return "Release";
        case 8: return "CreateCommandQueue -> queue proxy";
        case 9: return "CreateCommandAllocator (PC slot 9)";
        case 10: return "CreateGraphicsPipelineState (PC slot 10)";
        case 11: return "CreateComputePipelineState (PC slot 11)";
        case 14: return "CreateDescriptorHeap";
        case 16: return "CreateRootSignature";
        case 17: return "CreateConstantBufferView (CPU handle translated)";
        case 18: return "CreateShaderResourceView (CPU handle translated)";
        case 19: return "CreateUnorderedAccessView (CPU handle translated)";
        case 20: return "CreateRenderTargetView (CPU handle translated)";
        case 21: return "CreateDepthStencilView (CPU handle translated)";
        case 22: return "CreateSampler (CPU handle translated)";
        case 23: return "CopyDescriptors (CPU handles translated)";
        case 24: return "CopyDescriptorsSimple (CPU handles translated)";
        case 25: return "GetResourceAllocationInfo (C or PC form)";
        case 26: return "GetCustomHeapProperties (C or PC form)";
        case 27: return "CreateCommittedResource (PC slot 27)";
        case 31: return "CreateSharedHandle (proxies unwrapped)";
        case 32: return "OpenSharedHandle (a fence comes back wrapped)";
        case 34: return "MakeResident (proxies unwrapped)";
        case 35: return "Evict (proxies unwrapped)";
        case 36: return "CreateFence -> fence proxy";
        case 41: return "CreateCommandSignature (Xbox argument type 105 as DISPATCH)";
        case 43: return "GetAdapterLuid";
        case 45: return "SetEventOnMultipleFenceCompletion (fence proxies unwrapped)";
        case 46: return "SetResidencyPriority (proxies unwrapped)";
        case 48: return "Xbox CreateCommandQueueX -> queue proxy";
        case 50: return "EnqueueMakeResident (proxies unwrapped)";
        case 56: return "GetResourceAllocationInfo1 (C or PC form)";
        case 61: return "Xbox CreateSamplerX -> PC CreateSampler";
        case 62: return "Xbox CreateComputePipelineState";
        case 67: return "Xbox DeserializeComputePipelineStateX";
        case 70: return "Xbox shader-resource view from raw words";
        case 73: return "Xbox CreateCommandListX -> PC CreateCommandList";
        case 75: return "CreateCommandQueue1 -> queue proxy";
        case 76: return "Xbox resource creation";
        case 127: return "Xbox CreateCommittedResource3";
        case 78: return "Xbox SetFrameIntervalX";
        case 79: return "Xbox ScheduleFrameEventX";
        case 80: return "Xbox WaitFrameEventX";
        case 86: return "Xbox CreatePipelineState -> PC CreatePipelineState (slot 47)";
        case 101: return "Xbox CreateStateObject -> PC CreateStateObject (slot 62)";
        case 108: return "Xbox GetResourceAllocationInfo2 -> PC GetResourceAllocationInfo2 (slot 68)";
        default: return "forwarded";
    }
}

void note_slot(unsigned slot) {
    if (slot >= kMaxSlots) return;
    count_hit(g_hits[slot]);
    const bool first = first_call(g_first[slot]);
    if (first) {
        std::fprintf(stderr, "[rt] d3d12_xs: device slot %u (%s) first call\n", slot, slot_name(slot));
    } else if (slot >= 3 && trace_enabled()) {
        std::fprintf(stderr, "[rt] d3d12_xs: device slot %u (%s) call\n", slot, slot_name(slot));
    }
}

// Xbox device slots (public HLE Freminet/ScarlettX, an observation, not Microsoft documentation). The defaults are
// the HLE's; an environment variable that names another slot in 3..127 replaces one (a whole-table move for a trace
// that disagrees). A value outside that range is logged once and ignored. The table is read once, on the first
// generic slot call, so a test that wants other slots sets the variables before it creates a device.
struct XboxDeviceSlotRoutes {
    unsigned frame_interval = 78;  // SetFrameIntervalX(output, interval_us, latency, flags)
    unsigned schedule_event = 79;  // ScheduleFrameEventX(event, offset_us, reserved, flags)
    unsigned wait_event = 80;      // WaitFrameEventX(event, timeout_ms, reserved, flags, &token)
    unsigned queue_x = 48;         // CreateCommandQueueX(desc, riid, out)
    unsigned list_x = 73;          // CreateCommandListX(desc, allocator, initial_state, riid, out)
};

unsigned env_device_slot(const char* name, unsigned fallback) {
    char value[32] = {};
    const DWORD length = GetEnvironmentVariableA(name, value, sizeof(value));
    if (length == 0) return fallback;
    if (length >= sizeof(value)) {
        std::fprintf(stderr, "[rt] d3d12_xs: %s is too long for a device slot; the default is kept\n", name);
        return fallback;
    }
    errno = 0;
    char* end = nullptr;
    const unsigned long slot = std::strtoul(value, &end, 0);
    if (errno != 0 || end == value || *end != '\0' || slot < 3 || slot > 127) {
        std::fprintf(stderr, "[rt] d3d12_xs: %s=%s is not a device slot in 3..127; the default is kept\n", name,
                     value);
        return fallback;
    }
    return static_cast<unsigned>(slot);
}

const XboxDeviceSlotRoutes& xbox_device_slot_routes() {
    static const XboxDeviceSlotRoutes routes = [] {
        XboxDeviceSlotRoutes r;
        const unsigned frame_interval = env_device_slot("LEONIDA_XS_FRAME_INTERVAL_SLOT", r.frame_interval);
        const unsigned schedule_event = env_device_slot("LEONIDA_XS_SCHEDULE_EVENT_SLOT", r.schedule_event);
        const unsigned wait_event = env_device_slot("LEONIDA_XS_WAIT_EVENT_SLOT", r.wait_event);
        const unsigned queue_x = env_device_slot("LEONIDA_XS_QUEUE_X_SLOT", r.queue_x);
        const unsigned list_x = env_device_slot("LEONIDA_XS_LIST_X_SLOT", r.list_x);
        const bool moved = frame_interval != r.frame_interval || schedule_event != r.schedule_event ||
                           wait_event != r.wait_event || queue_x != r.queue_x || list_x != r.list_x;
        r.frame_interval = frame_interval;
        r.schedule_event = schedule_event;
        r.wait_event = wait_event;
        r.queue_x = queue_x;
        r.list_x = list_x;
        if (moved)
            std::fprintf(stderr,
                         "[rt] d3d12_xs: Xbox device slots: SetFrameIntervalX=%u ScheduleFrameEventX=%u "
                         "WaitFrameEventX=%u CreateCommandQueueX=%u CreateCommandListX=%u\n",
                         r.frame_interval, r.schedule_event, r.wait_event, r.queue_x, r.list_x);
        return r;
    }();
    return routes;
}

// The public HLE call shapes. SetFrameIntervalX's output and flags are unused while there is no output or pacing;
// ScheduleFrameEventX's reserved event pointer is a3 in the console call and a4 is its flags. The logged words are the
// caller's, so a trace names the slot even when an override moved it.
void log_xbox_route_once(unsigned slot, const char* name, std::uint64_t a1, std::uint64_t a2, std::uint64_t a3,
                         std::uint64_t a4, std::uint64_t a5) {
    static std::atomic<bool> logged[kMaxSlots];
    if (slot >= kMaxSlots) return;
    bool expected = false;
    if (!logged[slot].compare_exchange_strong(expected, true, std::memory_order_relaxed)) return;
    std::fprintf(stderr,
                 "[rt] d3d12_xs: device slot %u routed to %s (args 0x%llx 0x%llx 0x%llx 0x%llx 0x%llx)\n", slot,
                 name, static_cast<unsigned long long>(a1), static_cast<unsigned long long>(a2),
                 static_cast<unsigned long long>(a3), static_cast<unsigned long long>(a4),
                 static_cast<unsigned long long>(a5));
}

const char* heap_slot_name(unsigned slot) {
    switch (slot) {
        case 0: return "QueryInterface";
        case 1: return "AddRef";
        case 2: return "Release";
        case 7: return "GetDevice";
        case 8: return "GetDesc";
        case 9: return "GetCPUDescriptorHandleForHeapStart";
        case 10: return "GetGPUDescriptorHandleForHeapStart";
        default: return "forwarded";
    }
}

void note_heap_slot(unsigned slot) {
    if (slot >= kHeapSlots) return;
    count_hit(g_heap_hits[slot]);
    const bool first = first_call(g_heap_first[slot]);
    if (first) {
        std::fprintf(stderr, "[rt] d3d12_xs: heap slot %u (%s) first call\n", slot, heap_slot_name(slot));
    } else if (slot >= 3 && trace_enabled()) {
        std::fprintf(stderr, "[rt] d3d12_xs: heap slot %u (%s) call\n", slot, heap_slot_name(slot));
    }
}

std::uint64_t hr_to_u64(HRESULT hr) {
    return static_cast<std::uint64_t>(static_cast<std::uint32_t>(hr));
}

unsigned long proxy_add_ref(DeviceProxy* self) {
    note_slot(1);
    return self->refs.fetch_add(1, std::memory_order_relaxed) + 1;
}

unsigned long proxy_release(DeviceProxy* self) {
    note_slot(2);
    const unsigned long refs = self->refs.fetch_sub(1, std::memory_order_acq_rel) - 1;
    if (refs == 0) {
        destroy_present_state(self->present_state);
        destroy_upload_state(self->upload_state.load(std::memory_order_relaxed));
        release_real_queues(self);
        ID3D12Device* real = self->real;
        delete self;
        if (real) real->Release();
    }
    return refs;
}

bool is_device_iid(const IID& iid) {
    if (IsEqualIID(iid, __uuidof(ID3D12Device))) return true;
#ifdef __ID3D12Device14_INTERFACE_DEFINED__
    if (IsEqualIID(iid, __uuidof(ID3D12Device14))) return true;
#endif
#ifdef __ID3D12Device13_INTERFACE_DEFINED__
    if (IsEqualIID(iid, __uuidof(ID3D12Device13))) return true;
#endif
#ifdef __ID3D12Device12_INTERFACE_DEFINED__
    if (IsEqualIID(iid, __uuidof(ID3D12Device12))) return true;
#endif
#ifdef __ID3D12Device11_INTERFACE_DEFINED__
    if (IsEqualIID(iid, __uuidof(ID3D12Device11))) return true;
#endif
#ifdef __ID3D12Device10_INTERFACE_DEFINED__
    if (IsEqualIID(iid, __uuidof(ID3D12Device10))) return true;
#endif
#ifdef __ID3D12Device9_INTERFACE_DEFINED__
    if (IsEqualIID(iid, __uuidof(ID3D12Device9))) return true;
#endif
    if (IsEqualIID(iid, __uuidof(ID3D12Device8))) return true;
    if (IsEqualIID(iid, __uuidof(ID3D12Device7))) return true;
    if (IsEqualIID(iid, __uuidof(ID3D12Device6))) return true;
    if (IsEqualIID(iid, __uuidof(ID3D12Device5))) return true;
    if (IsEqualIID(iid, __uuidof(ID3D12Device4))) return true;
    if (IsEqualIID(iid, __uuidof(ID3D12Device3))) return true;
    if (IsEqualIID(iid, __uuidof(ID3D12Device2))) return true;
    if (IsEqualIID(iid, __uuidof(ID3D12Device1))) return true;
    return false;
}

HRESULT proxy_query_interface(DeviceProxy* self, const IID* riid, void** out) {
    note_slot(0);
    if (!out) return E_POINTER;
    *out = nullptr;
    if (!riid) return E_POINTER;
    if (IsEqualIID(*riid, __uuidof(IUnknown)) || IsEqualIID(*riid, __uuidof(ID3D12Object))) {
        *out = self;
        proxy_add_ref(self);
        return S_OK;
    }
    if (IsEqualIID(*riid, __uuidof(IDXGIObject)) || IsEqualIID(*riid, __uuidof(IDXGIDevice)) ||
        IsEqualIID(*riid, __uuidof(IDXGIDevice1))) {
        *out = &self->dxgi_vtable;
        proxy_add_ref(self);
        return S_OK;
    }
    if (!is_device_iid(*riid)) return E_NOINTERFACE;

    // Only claim an interface the PC device really implements; the proxy vtable covers its slots.
    void* supported = nullptr;
    HRESULT hr = self->real->QueryInterface(*riid, &supported);
    if (FAILED(hr)) return hr;
    if (!supported) return E_NOINTERFACE;
    static_cast<IUnknown*>(supported)->Release();
    *out = self;
    proxy_add_ref(self);
    return S_OK;
}

// ---- IDXGIDevice1 on the device ---------------------------------------------------------------------------------
//
// A GDKX title finds its output through the device: QueryInterface for IDXGIDevice1, GetAdapter, EnumOutputs(0),
// and hands that output to SetFrameIntervalX (docs/d3d12_vulkan.md section 4.5, from the public samples). A PC
// ID3D12Device has no IDXGIDevice, so the proxy answers it itself: a second vtable pointer inside the proxy object,
// with the proxy's reference count and QueryInterface, so the two pointers are one COM object. Only IDXGIDevice and
// IDXGIDevice1 are answered (what the samples ask for). The 14 slots are the public dxgi.h order; that the console
// keeps it is the same assumption as for the queue and the fence.
//
// GetAdapter and GetParent hand out the adapter of the device behind the proxy. The provider says how: the default
// asks the host's DXGI for the adapter with the device's LUID (the host backend); set_device_adapter_provider puts
// the Vulkan layer's own adapter there, so that backend never loads the host's dxgi.dll for it.
constexpr unsigned kDxgiDeviceSlots = 14;  // IUnknown 3, IDXGIObject 4, IDXGIDevice 5, IDXGIDevice1 2

DeviceProxy* dxgi_owner(void* self) {
    return reinterpret_cast<DeviceProxy*>(static_cast<char*>(self) - offsetof(DeviceProxy, dxgi_vtable));
}

HRESULT host_adapter(ID3D12Device* real, REFIID riid, void** out) {
    using CreateFactoryFn = HRESULT(WINAPI*)(REFIID, void**);
    static const CreateFactoryFn create_factory = [] {
        const HMODULE dxgi = LoadLibraryExA("dxgi.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
        return dxgi ? reinterpret_cast<CreateFactoryFn>(
                          reinterpret_cast<void*>(GetProcAddress(dxgi, "CreateDXGIFactory1")))
                    : nullptr;
    }();
    if (!create_factory) return DXGI_ERROR_NOT_FOUND;
    IDXGIFactory4* factory = nullptr;
    HRESULT hr = create_factory(__uuidof(IDXGIFactory4), reinterpret_cast<void**>(&factory));
    if (FAILED(hr) || !factory) return FAILED(hr) ? hr : DXGI_ERROR_NOT_FOUND;
    hr = factory->EnumAdapterByLuid(real->GetAdapterLuid(), riid, out);
    factory->Release();
    return hr;
}

HRESULT dxgi_adapter(DeviceProxy* self, REFIID riid, void** out) {
    if (!out) return E_POINTER;
    *out = nullptr;
    const DxgiAdapterProvider provider = self->adapter_provider ? self->adapter_provider : &host_adapter;
    const HRESULT hr = provider(self->real, riid, out);
    if (FAILED(hr)) *out = nullptr;
    return hr;
}

void note_dxgi(const char* method) {
    d3d12_trace::record(method);
}

HRESULT WINAPI dxgi_query_interface(void* self, const IID* riid, void** out) {
    return proxy_query_interface(dxgi_owner(self), riid, out);
}
ULONG WINAPI dxgi_add_ref(void* self) { return proxy_add_ref(dxgi_owner(self)); }
ULONG WINAPI dxgi_release(void* self) { return proxy_release(dxgi_owner(self)); }

// IDXGIObject's private data is the device's: ID3D12Object has the same three methods with the same arguments.
HRESULT WINAPI dxgi_set_private_data(void* self, const GUID* name, UINT size, const void* data) {
    if (!name) return E_INVALIDARG;
    return dxgi_owner(self)->real->SetPrivateData(*name, size, data);
}
HRESULT WINAPI dxgi_set_private_data_interface(void* self, const GUID* name, const IUnknown* object) {
    if (!name) return E_INVALIDARG;
    return dxgi_owner(self)->real->SetPrivateDataInterface(*name, object);
}
HRESULT WINAPI dxgi_get_private_data(void* self, const GUID* name, UINT* size, void* data) {
    if (!name) return E_INVALIDARG;
    return dxgi_owner(self)->real->GetPrivateData(*name, size, data);
}
HRESULT WINAPI dxgi_get_parent(void* self, const IID* riid, void** out) {
    note_dxgi("XboxIDXGIDevice::GetParent");
    if (!riid) return E_POINTER;
    return dxgi_adapter(dxgi_owner(self), *riid, out);
}
HRESULT WINAPI dxgi_get_adapter(void* self, IDXGIAdapter** out) {
    note_dxgi("XboxIDXGIDevice::GetAdapter");
    return dxgi_adapter(dxgi_owner(self), __uuidof(IDXGIAdapter), reinterpret_cast<void**>(out));
}
// CreateSurface and QueryResourceResidency are D3D10/11-era methods no D3D12 title has a use for; what the console
// answers is not measured, so they are refused and say so once.
HRESULT WINAPI dxgi_create_surface(void*, const DXGI_SURFACE_DESC*, UINT, DXGI_USAGE, const DXGI_SHARED_RESOURCE*,
                                   IDXGISurface** out) {
    static std::atomic<bool> logged{false};
    if (!logged.exchange(true, std::memory_order_relaxed))
        std::fprintf(stderr, "[rt] d3d12_xs: IDXGIDevice::CreateSurface is not implemented; returning E_NOTIMPL\n");
    if (out) *out = nullptr;
    return E_NOTIMPL;
}
HRESULT WINAPI dxgi_query_resource_residency(void*, IUnknown* const*, DXGI_RESIDENCY*, UINT) {
    static std::atomic<bool> logged{false};
    if (!logged.exchange(true, std::memory_order_relaxed))
        std::fprintf(stderr,
                     "[rt] d3d12_xs: IDXGIDevice::QueryResourceResidency is not implemented; returning E_NOTIMPL\n");
    return E_NOTIMPL;
}
// The priority and the frame latency are kept and read back; nothing acts on them (the Xbox presenter's latency is
// SetFrameIntervalX's). The ranges are the documented ones: -7..7, and 1..16 with 0 for the default of 3.
HRESULT WINAPI dxgi_set_gpu_thread_priority(void* self, INT priority) {
    if (priority < -7 || priority > 7) return E_INVALIDARG;
    dxgi_owner(self)->gpu_thread_priority.store(priority, std::memory_order_relaxed);
    return S_OK;
}
HRESULT WINAPI dxgi_get_gpu_thread_priority(void* self, INT* priority) {
    if (!priority) return E_POINTER;
    *priority = dxgi_owner(self)->gpu_thread_priority.load(std::memory_order_relaxed);
    return S_OK;
}
HRESULT WINAPI dxgi_set_maximum_frame_latency(void* self, UINT latency) {
    if (latency > 16) return DXGI_ERROR_INVALID_CALL;
    dxgi_owner(self)->max_frame_latency.store(latency ? latency : 3, std::memory_order_relaxed);
    return S_OK;
}
HRESULT WINAPI dxgi_get_maximum_frame_latency(void* self, UINT* latency) {
    if (!latency) return E_POINTER;
    *latency = dxgi_owner(self)->max_frame_latency.load(std::memory_order_relaxed);
    return S_OK;
}

void** get_dxgi_vtable() {
    static void* table[kDxgiDeviceSlots] = {
        reinterpret_cast<void*>(&dxgi_query_interface),          // 0
        reinterpret_cast<void*>(&dxgi_add_ref),                  // 1
        reinterpret_cast<void*>(&dxgi_release),                  // 2
        reinterpret_cast<void*>(&dxgi_set_private_data),         // 3  IDXGIObject
        reinterpret_cast<void*>(&dxgi_set_private_data_interface),
        reinterpret_cast<void*>(&dxgi_get_private_data),
        reinterpret_cast<void*>(&dxgi_get_parent),
        reinterpret_cast<void*>(&dxgi_get_adapter),              // 7  IDXGIDevice
        reinterpret_cast<void*>(&dxgi_create_surface),
        reinterpret_cast<void*>(&dxgi_query_resource_residency),
        reinterpret_cast<void*>(&dxgi_set_gpu_thread_priority),
        reinterpret_cast<void*>(&dxgi_get_gpu_thread_priority),
        reinterpret_cast<void*>(&dxgi_set_maximum_frame_latency),  // 12 IDXGIDevice1
        reinterpret_cast<void*>(&dxgi_get_maximum_frame_latency),
    };
    return table;
}

unsigned long heap_add_ref(HeapProxy* self) {
    note_heap_slot(1);
    return self->refs.fetch_add(1, std::memory_order_relaxed) + 1;
}

unsigned long heap_release(HeapProxy* self) {
    note_heap_slot(2);
    const unsigned long refs = self->refs.fetch_sub(1, std::memory_order_acq_rel) - 1;
    if (refs == 0) {
        if (self->custom_cpu_start != 0) unregister_descriptor_range(self);
        void* const backing = reinterpret_cast<void*>(self->custom_cpu_start);
        const DescriptorHeapBackingFree free_fn = self->custom_free;
        ID3D12DescriptorHeap* real = self->real;
        DeviceProxy* device = self->device;
        delete self;
        if (real) real->Release();
        if (device) proxy_release(device);
        if (backing && free_fn) free_fn(backing, kXmemDescriptorHeapAttributes);
    }
    return refs;
}

bool is_heap_iid(const IID& iid) {
    return IsEqualIID(iid, __uuidof(ID3D12DescriptorHeap)) || IsEqualIID(iid, __uuidof(ID3D12Pageable)) ||
           IsEqualIID(iid, __uuidof(ID3D12DeviceChild)) || IsEqualIID(iid, __uuidof(ID3D12Object)) ||
           IsEqualIID(iid, __uuidof(IUnknown));
}

HRESULT heap_query_interface(HeapProxy* self, const IID* riid, void** out) {
    note_heap_slot(0);
    if (!out) return E_POINTER;
    *out = nullptr;
    if (!riid) return E_POINTER;
    if (!is_heap_iid(*riid)) return E_NOINTERFACE;
    *out = self;
    heap_add_ref(self);
    return S_OK;
}

// ID3D12DeviceChild::GetDevice on the heap must hand back the Xbox-ABI device, not the raw PC device.
HRESULT heap_get_device(HeapProxy* self, const IID* riid, void** out) {
    note_heap_slot(7);
    if (!out) return E_POINTER;
    *out = nullptr;
    if (!riid) return E_POINTER;
    if (!self->device) return E_NOINTERFACE;
    return proxy_query_interface(self->device, riid, out);
}

// The PC methods write through the hidden return pointer (RDX) on the measured runtime; the Xbox ABI wants the
// 8-byte value in RAX. Call with both conventions and keep whichever produced a value, so a runtime that really
// returns in RAX also works. The extra register is harmless when the callee ignores it.
D3D12_CPU_DESCRIPTOR_HANDLE raw_heap_get_cpu_handle(ID3D12DescriptorHeap* real, void** real_vtable) {
    constexpr SIZE_T kUnset = ~static_cast<SIZE_T>(0);
    D3D12_CPU_DESCRIPTOR_HANDLE handle{};
    handle.ptr = kUnset;
    using Fn = D3D12_CPU_DESCRIPTOR_HANDLE(WINAPI*)(ID3D12DescriptorHeap*, D3D12_CPU_DESCRIPTOR_HANDLE*);
    const D3D12_CPU_DESCRIPTOR_HANDLE returned = reinterpret_cast<Fn>(real_vtable[9])(real, &handle);
    return handle.ptr != kUnset ? handle : returned;
}

D3D12_CPU_DESCRIPTOR_HANDLE heap_get_cpu_handle(HeapProxy* self) {
    note_heap_slot(9);
    if (self->custom_cpu_start != 0) return D3D12_CPU_DESCRIPTOR_HANDLE{self->custom_cpu_start};
    return raw_heap_get_cpu_handle(self->real, self->real_vtable);
}

D3D12_GPU_DESCRIPTOR_HANDLE heap_get_gpu_handle(HeapProxy* self) {
    note_heap_slot(10);
    constexpr UINT64 kUnset = ~static_cast<UINT64>(0);
    D3D12_GPU_DESCRIPTOR_HANDLE handle{};
    handle.ptr = kUnset;
    using Fn = D3D12_GPU_DESCRIPTOR_HANDLE(WINAPI*)(ID3D12DescriptorHeap*, D3D12_GPU_DESCRIPTOR_HANDLE*);
    const D3D12_GPU_DESCRIPTOR_HANDLE returned =
        reinterpret_cast<Fn>(self->real_vtable[10])(self->real, &handle);
    return handle.ptr != kUnset ? handle : returned;
}

template <unsigned Slot>
std::uint64_t WINAPI heap_slot(HeapProxy* self, std::uint64_t a1, std::uint64_t a2, std::uint64_t a3,
                               std::uint64_t a4, std::uint64_t a5, std::uint64_t a6, std::uint64_t a7,
                               std::uint64_t a8, std::uint64_t a9, std::uint64_t a10, std::uint64_t a11,
                               std::uint64_t a12, std::uint64_t a13, std::uint64_t a14) {
    if constexpr (Slot == 0) {
        return hr_to_u64(heap_query_interface(self, reinterpret_cast<const IID*>(a1),
                                              reinterpret_cast<void**>(a2)));
    } else if constexpr (Slot == 1) {
        return heap_add_ref(self);
    } else if constexpr (Slot == 2) {
        return heap_release(self);
    } else if constexpr (Slot == 7) {
        return hr_to_u64(heap_get_device(self, reinterpret_cast<const IID*>(a1),
                                         reinterpret_cast<void**>(a2)));
    } else if constexpr (Slot == 9) {
        // PR #7021: the address the title's own XMem allocation hook supplied, or the host heap's when there is none.
        return static_cast<std::uint64_t>(heap_get_cpu_handle(self).ptr);
    } else if constexpr (Slot == 10) {
        return static_cast<std::uint64_t>(heap_get_gpu_handle(self).ptr);
    } else {
        note_heap_slot(Slot);
        using ForwardFn = std::uint64_t(WINAPI*)(void*, std::uint64_t, std::uint64_t, std::uint64_t,
                                                 std::uint64_t, std::uint64_t, std::uint64_t, std::uint64_t,
                                                 std::uint64_t, std::uint64_t, std::uint64_t, std::uint64_t,
                                                 std::uint64_t, std::uint64_t, std::uint64_t);
        auto fn = reinterpret_cast<ForwardFn>(self->real_vtable[Slot]);
        return fn(self->real, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11, a12, a13, a14);
    }
}

template <std::size_t... I>
void fill_heap_vtable(void** vtable, std::index_sequence<I...>) {
    ((vtable[I] = reinterpret_cast<void*>(&heap_slot<static_cast<unsigned>(I)>) ), ...);
}

void** get_heap_vtable() {
    static void** vtable = [] {
        static void* storage[kHeapSlots];
        fill_heap_vtable(storage, std::make_index_sequence<kHeapSlots>{});
        return storage;
    }();
    return vtable;
}

HRESULT create_heap_proxy(ID3D12DescriptorHeap* real, DeviceProxy* device, REFIID riid, void** out,
                          SIZE_T custom_cpu_start = 0, SIZE_T custom_byte_size = 0, SIZE_T real_cpu_start = 0,
                          DescriptorHeapBackingFree custom_free = nullptr, SIZE_T gpu_start = 0) {
    if (!real || !device) return E_POINTER;
    if (!out) return E_POINTER;
    *out = nullptr;

    HeapProxy* proxy = new (std::nothrow) HeapProxy;
    if (!proxy) return E_OUTOFMEMORY;
    proxy->vtable = get_heap_vtable();
    proxy->real = real;
    proxy->device = device;
    proxy->real_vtable = *reinterpret_cast<void***>(real);
    proxy->refs.store(1, std::memory_order_relaxed);
    proxy->custom_cpu_start = custom_cpu_start;
    proxy->custom_byte_size = custom_byte_size;
    proxy->real_cpu_start = real_cpu_start;
    proxy->custom_free = custom_free;
    proxy_add_ref(device);  // the heap keeps its Xbox-ABI device alive for GetDevice

    if (custom_cpu_start != 0 &&
        !register_descriptor_range(device, proxy, custom_cpu_start, custom_byte_size, real_cpu_start)) {
        delete proxy;
        proxy_release(device);
        return E_OUTOFMEMORY;
    }
    // LEONIDA_XS_GPU_HANDLE: the console's CPU and GPU descriptor handles are one address (memory is unified), so a
    // title may hand a *GPU* handle to Create*View or CopyDescriptors. Unmapped, that handle is used as a host pointer:
    // the descriptor lands in whatever memory the number names, the heap slot in the arena stays zero, and the shader
    // reads a null V# from its root table (2026-11-24: the black composite frame). Map the heap's GPU handle range to
    // the layer's heap the way the XMem backing range already is.
    if (custom_cpu_start != 0 && gpu_start != 0 && std::getenv("LEONIDA_XS_GPU_HANDLE")) {
        if (!register_descriptor_range(device, proxy, gpu_start, custom_byte_size, real_cpu_start, true,
                                       custom_cpu_start) &&
            std::getenv("LEONIDA_LOG_TABLE"))
            std::fprintf(stderr, "[rt] d3d12_xs: could not map the GPU handle range of a descriptor heap\n");
        else if (std::getenv("LEONIDA_LOG_TABLE"))
            std::fprintf(stderr, "[rt] d3d12_xs: descriptor heap GPU handles 0x%llx + 0x%llx -> layer 0x%llx\n",
                         static_cast<unsigned long long>(gpu_start),
                         static_cast<unsigned long long>(custom_byte_size),
                         static_cast<unsigned long long>(real_cpu_start));
    }
    if (custom_cpu_start != 0 && std::getenv("LEONIDA_LOG_TABLE"))
        std::fprintf(stderr,
                     "[rt] d3d12_xs: descriptor heap backing: title 0x%llx + 0x%llx, layer 0x%llx (in arena 0x%llx)\n",
                     static_cast<unsigned long long>(custom_cpu_start),
                     static_cast<unsigned long long>(custom_byte_size),
                     static_cast<unsigned long long>(real_cpu_start),
                     static_cast<unsigned long long>(leonida::arena_host_base
                                                         ? real_cpu_start - reinterpret_cast<SIZE_T>(
                                                                                leonida::arena_host_base)
                                                         : 0));

    const HRESULT hr = heap_query_interface(proxy, &riid, out);
    if (FAILED(hr)) {
        // No reference escaped, and the caller still owns its references to `real` and `device`.
        if (custom_cpu_start != 0) unregister_descriptor_range(proxy);
        delete proxy;
        proxy_release(device);
        return hr;
    }
    if (!d3d12_trace::enabled())
        std::fprintf(stderr, "[rt] d3d12_xs: heap proxy %p wraps PC heap %p (device proxy %p)\n",
                     static_cast<void*>(proxy), static_cast<void*>(real), static_cast<void*>(device));
    heap_release(proxy);
    return S_OK;
}

// ---- Command queue and fence proxies --------------------------------------------------------------------------
//
// Device slots 8 (CreateCommandQueue), 75 (CreateCommandQueue1), 36 (CreateFence) and 32 (OpenSharedHandle, for a
// fence) wrap what they create, so a title never holds a PC or Vulkan-layer queue or fence. The proxies forward
// the public chain in PC order, exactly like the device proxy; on a fence a slot past the PC vtable is logged and
// refused with E_NOTIMPL. PresentX, SuspendX and ResumeX are queue methods past the public chain
// (docs/d3d12_command_objects.md §5.3). A sibling layer measured two queue layouts on retail titles
// (docs/d3d12_xs_console_abi.md §4, unverified here): the older has Signal 14, Wait 15,
// GetTimestampFrequency 16, GetClockCalibration 17, GetDesc 18, SignalX 23, PresentX 30; a newer game
// OS inserts two methods before Signal, shifting to Signal 16, Wait 17, frequency 18, SignalX 25,
// PresentX 32 — a fence passed to slot 16 or 17 marks the newer layout, and the detection is shared
// across queues through the device. PresentX is wired to the Vulkan layer's Xbox presentation,
// SignalX forwards as Signal, and the unmodelled console-only slots answer S_OK (the PresentX
// section below); the first run of the executable confirms which layout.
//
// Slot numbers are those of the Windows SDK 10.0.26100 d3d12.h: ID3D12CommandQueue has 19, ID3D12Fence 11 and
// ID3D12Fence1 12. That the console keeps these public slots, and keeps device slots 8/32/36/75, is an assumption
// the shipped binaries cannot test (§3.5: the package has no queue or fence client). Every queue and fence method
// takes integer and pointer arguments only; GetDesc returns its struct through the hidden pointer in RDX, which is
// forwarded unchanged (no Xbox variant is measured, §6).
//
// The refused range is the proxy's own table: 128 queue slots and 64 fence slots, every one a thunk. The table sits
// at the end of the first page of a 64 KiB reservation whose other pages are never committed, so a slot index past
// the table makes the vtable read itself fault, at an address that names the slot (a vectored handler logs it and
// lets the fault continue), instead of jumping through whatever memory follows. A title whose vtable read lands
// further away than that reservation is not covered.
//
// Objects cross back into the real runtime wherever a device or queue method takes them, and the real object is
// passed there: queue Signal/Wait (14/15) take a fence; device CreateSharedHandle (31) a device child,
// MakeResident/Evict (34/35), SetResidencyPriority (46) and EnqueueMakeResident (50) pageables, and
// SetEventOnMultipleFenceCompletion (45) and EnqueueMakeResident fences. The heap proxies of slot 14 are pageables
// and device children too and are unwrapped in the same places. Command lists from device slot 12 come back
// console-shaped (the list proxy below): queue ExecuteCommandLists (10) unwraps the list array, the list's own
// Reset and ExecuteBundle unwrap allocator and bundle proxies, and its SetDescriptorHeaps unwraps heap proxies —
// all through proxy_real_object, so any object kind added later unwraps in every place at once.
//
// QueryInterface answers only the interfaces the proxy models (the base chain and its own interface, Fence1 when
// the real fence has it), like the device and heap proxies, so the COM identity holds and no real object leaks to
// the title. Leonida's own code reaches the real object through queue_proxy_real_queue / fence_proxy_real_fence /
// allocator_proxy_real_allocator.
//
// Command allocators (device slot 9) are the third kind: their public chain is 9 slots (Reset at 8). Slot 9 wraps
// only an allocator that answers kLeonidaCommandAllocatorTag, because a list takes the title's allocator in
// CreateCommandList and Reset and only the Vulkan layer's lists resolve a proxy there (CommandAllocator::from);
// a PC runtime allocator is handed out unchanged until a command-list proxy unwraps it at Reset.

enum class ChildKind { queue, fence, allocator };

constexpr unsigned kQueueSlots = 128;        // the table: 19 public slots plus a refused tail
constexpr unsigned kFenceSlots = 64;         // the table: 12 public slots plus a refused tail
constexpr unsigned kQueuePublicSlots = 19;   // ID3D12CommandQueue
constexpr unsigned kFencePublicSlots = 11;   // ID3D12Fence
constexpr unsigned kFence1PublicSlots = 12;  // ID3D12Fence1
constexpr unsigned kAllocatorSlots = 64;     // the table: 9 public slots plus a refused tail
constexpr unsigned kAllocatorPublicSlots = 9;  // ID3D12CommandAllocator: the base chain and Reset (slot 8)
constexpr SIZE_T kTableRegion = 64 * 1024;   // one allocation granule: the table page plus 15 never-committed pages
static_assert(kQueueSlots > kQueuePublicSlots && kFenceSlots > kFence1PublicSlots &&
                  kAllocatorSlots > kAllocatorPublicSlots,
              "the tables need a tail");
static_assert(kQueueSlots * sizeof(void*) <= 4096 && kFenceSlots * sizeof(void*) <= 4096 &&
                  kAllocatorSlots * sizeof(void*) <= 4096,
              "a table must fit in the first page of its reservation");

constexpr unsigned kQueueSignal = 14, kQueueWait = 15;  // (ID3D12Fence*, UINT64)
constexpr unsigned kFenceSignal = 10;                    // ID3D12Fence::Signal(UINT64), from the CPU
constexpr unsigned kFenceCompletedValueSlot = 12;        // one past ID3D12Fence1: the completed value's address
static_assert(kFenceCompletedValueSlot >= kFence1PublicSlots && kFenceCompletedValueSlot < kFenceSlots,
              "fence slot 12 is past the public chain and inside the table");

struct FenceMirror;

struct ChildProxy {
    void** vtable;          // offset 0: the caller's ID3D12CommandQueue* / ID3D12Fence* points here
    IUnknown* real;         // offset 8: the PC or Vulkan-layer object every thunk forwards to
    DeviceProxy* device;    // keeps the Xbox-ABI device alive; GetDevice returns it
    void** real_vtable;
    unsigned slot_count;    // public slots the real object implements; the rest of the table is refused
    std::atomic<unsigned long> refs;
    // Queues only: the console runtime layout this title was built against (docs/d3d12_xs_console_abi.md §4),
    // detected by a fence reaching slot 16/17 or SignalX slot 25. Unused on fences and allocators. A queue is
    // used from many threads, so the cache of the device's shared detection is atomic like the shared flag
    // itself.
    static constexpr unsigned char kQueueLayoutUnknown = 0;
    static constexpr unsigned char kQueueLayoutOlder = 1;   // PC order; PresentX 30, SignalX 23
    static constexpr unsigned char kQueueLayoutNewer = 2;   // +2 shift; PresentX 32, SignalX 25
    std::atomic<unsigned char> queue_layout{kQueueLayoutUnknown};
    std::atomic<FenceMirror*> mirror{nullptr};  // fence: slot 12's completed value, made on its first call
};

template <ChildKind K>
struct ChildStats {
    static constexpr unsigned kSlots = K == ChildKind::queue   ? kQueueSlots
                                       : K == ChildKind::fence ? kFenceSlots
                                                               : kAllocatorSlots;
    static inline std::atomic<std::uint64_t> hits[kSlots];
    static inline std::atomic<bool> first[kSlots];
    static inline std::atomic<bool> beyond_warned[kSlots];
    static inline std::atomic<std::uint64_t> overruns{0};
    // Where the table and its reservation are: written once, before the table is handed out.
    static inline std::atomic<std::uintptr_t> table_begin{0}, table_end{0}, region_end{0};
};

template <ChildKind K>
constexpr const char* child_kind_name() {
    return K == ChildKind::queue ? "queue" : K == ChildKind::fence ? "fence" : "allocator";
}

template <ChildKind K>
const char* child_public_name(unsigned slot) {
    static constexpr const char* object[] = {"QueryInterface", "AddRef", "Release", "GetPrivateData",
                                             "SetPrivateData", "SetPrivateDataInterface", "SetName", "GetDevice"};
    static constexpr const char* queue[] = {"UpdateTileMappings", "CopyTileMappings", "ExecuteCommandLists",
                                            "SetMarker", "BeginEvent", "EndEvent", "Signal", "Wait",
                                            "GetTimestampFrequency", "GetClockCalibration", "GetDesc"};
    static constexpr const char* fence[] = {"GetCompletedValue", "SetEventOnCompletion", "Signal",
                                            "GetCreationFlags"};
    if (slot < 8) return object[slot];
    if constexpr (K == ChildKind::queue) {
        if (slot < kQueuePublicSlots) return queue[slot - 8];
    } else if constexpr (K == ChildKind::fence) {
        if (slot < kFence1PublicSlots) return fence[slot - 8];
    } else {
        if (slot == 8) return "Reset";
    }
    return nullptr;
}

// Fixed names for the trace channel (d3d12_trace::record takes fixed strings): one per table slot, built once, in
// the trace grammar of runtime/README.md and check_d3d12_trace.py (an identifier with `::`, no spaces):
// XboxID3D12CommandQueue::Signal, and XboxID3D12CommandQueue::Slot40 for a slot past the public chain.
template <ChildKind K>
const char* child_trace_name(unsigned slot) {
    static const auto names = [] {
        static char storage[ChildStats<K>::kSlots][64];
        const char* iface = K == ChildKind::queue   ? "XboxID3D12CommandQueue"
                            : K == ChildKind::fence ? "XboxID3D12Fence"
                                                    : "XboxID3D12CommandAllocator";
        for (unsigned s = 0; s < ChildStats<K>::kSlots; ++s) {
            const char* method = child_public_name<K>(s);
            if (method)
                std::snprintf(storage[s], sizeof(storage[s]), "%s::%s", iface, method);
            else
                std::snprintf(storage[s], sizeof(storage[s]), "%s::Slot%u", iface, s);
        }
        return &storage;
    }();
    return (*names)[slot < ChildStats<K>::kSlots ? slot : 0];
}

template <ChildKind K>
void note_child_slot(unsigned slot) {
    using S = ChildStats<K>;
    if (slot >= S::kSlots) return;
    count_hit(S::hits[slot]);
    const bool first = first_call(S::first[slot]);
    const char* method = child_public_name<K>(slot);
    if (first) {
        std::fprintf(stderr, "[rt] d3d12_xs: %s slot %u (%s) first call\n", child_kind_name<K>(), slot,
                     method ? method : "past the public chain");
    } else if (slot >= 3 && trace_enabled()) {
        std::fprintf(stderr, "[rt] d3d12_xs: %s slot %u (%s) call\n", child_kind_name<K>(), slot,
                     method ? method : "past the public chain");
    }
}

// The counters reset_device_proxy_stats clears for one kind: per-slot hits, first-call and beyond-table flags, and
// the faulted reads past the table.
template <ChildKind K>
void reset_child_stats() {
    using S = ChildStats<K>;
    for (unsigned slot = 0; slot < S::kSlots; ++slot) {
        S::hits[slot].store(0, std::memory_order_relaxed);
        S::first[slot].store(false, std::memory_order_relaxed);
        S::beyond_warned[slot].store(false, std::memory_order_relaxed);
    }
    S::overruns.store(0, std::memory_order_relaxed);
}

// ---- Fence slot 12: the completed value at an address ----
//
// Measured on the retail executable (a run past the PC chain, #6982): fence slot 12, one past ID3D12Fence1, is called
// as (value, descriptor, flags), where the descriptor is four words whose second is a code pointer, and the title
// reads the return as a pointer to the fence's 64-bit completed value. It compares that value with ~0 (device
// removed) and subtracts the value it waits for. Refused with E_NOTIMPL, the title dereferences 0x80004001.
//
// A console fence keeps that value in memory the GPU writes. Here each fence gets a mirror that stays live without
// the title calling anything. A thread-pool wait refreshes on a completion event or a timeout. A timeout can
// observe a rewind and subsequent values below the outstanding event's threshold. It does not add another
// completion event. Slot 12 and CPU Signal refresh synchronously. After device removal the callback stops waiting.
//
// The descriptor's code pointer is not called: the measured run goes on without it, and its contract (when it runs,
// on which thread, with what) is not known. The first call is logged with its arguments.
struct FenceMirror {
    alignas(8) volatile std::uint64_t completed = 0;  // what the title reads through slot 12's pointer
    ID3D12Fence* fence = nullptr;  // the real fence; the proxy holds the reference
    SRWLOCK lock = SRWLOCK_INIT;   // orders refreshes
    HANDLE event = nullptr;        // auto-reset; the real fence sets it at the armed value
    PTP_WAIT wait = nullptr;      // the thread-pool wait on the completion event
    bool stopping = false;       // guarded by lock
    std::atomic<bool> arm_failed{false};
};

std::uint64_t refresh_mirror(FenceMirror* m) {
    AcquireSRWLockExclusive(&m->lock);
    const std::uint64_t value = m->fence->GetCompletedValue();
    if (m->completed != value) m->completed = value;
    ReleaseSRWLockExclusive(&m->lock);
    return value;
}

void wait_for_fence_progress(FenceMirror* mirror) {
    constexpr LONGLONG kRewindPollInterval = -10 * 10'000;  // 10 ms in relative 100 ns units
    ULARGE_INTEGER due{};
    due.QuadPart = static_cast<ULONGLONG>(kRewindPollInterval);
    FILETIME timeout{due.LowPart, due.HighPart};
    SetThreadpoolWait(mirror->wait, mirror->event, &timeout);
}

VOID CALLBACK on_fence_progress(PTP_CALLBACK_INSTANCE, PVOID context, PTP_WAIT, TP_WAIT_RESULT result) {
    auto* mirror = static_cast<FenceMirror*>(context);
    const std::uint64_t value = refresh_mirror(mirror);
    if (value == ~std::uint64_t{0}) return;
    if (result == WAIT_OBJECT_0) {
        const HRESULT hr = mirror->fence->SetEventOnCompletion(value + 1, mirror->event);
        if (FAILED(hr) && !mirror->arm_failed.exchange(true, std::memory_order_relaxed))
            std::fprintf(stderr, "[rt] d3d12_xs: fence slot 12: SetEventOnCompletion failed (0x%08lX); the completed "
                         "value at the returned address now uses periodic refresh\n",
                         static_cast<unsigned long>(hr));
    }
    AcquireSRWLockExclusive(&mirror->lock);
    if (!mirror->stopping) wait_for_fence_progress(mirror);
    ReleaseSRWLockExclusive(&mirror->lock);
}

// The fence's mirror, made on the first slot 12 call. Null only when it cannot be allocated.
FenceMirror* fence_mirror(ChildProxy* self) {
    if (FenceMirror* m = self->mirror.load(std::memory_order_acquire)) return m;
    static SRWLOCK create_lock = SRWLOCK_INIT;
    AcquireSRWLockExclusive(&create_lock);
    FenceMirror* m = self->mirror.load(std::memory_order_relaxed);
    if (!m && (m = new (std::nothrow) FenceMirror)) {
        m->fence = reinterpret_cast<ID3D12Fence*>(self->real);
        refresh_mirror(m);
        m->event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (m->event) m->wait = CreateThreadpoolWait(on_fence_progress, m, nullptr);
        if (m->wait) {
            wait_for_fence_progress(m);
            SetEvent(m->event);  // the callback makes the first SetEventOnCompletion
        } else {
            std::fprintf(stderr, "[rt] d3d12_xs: fence slot 12: no wait for the completed value (error %lu); it "
                         "moves only on slot 12 and Signal\n", GetLastError());
            if (m->event) CloseHandle(m->event);
            m->event = nullptr;
            m->wait = nullptr;
        }
        self->mirror.store(m, std::memory_order_release);
    }
    ReleaseSRWLockExclusive(&create_lock);
    return m;
}

// Fence slot 12 (value, descriptor, flags): the address of the fence's completed value, refreshed now.
std::uint64_t fence_completed_value_address(ChildProxy* self, std::uint64_t value, std::uint64_t descriptor,
                                            std::uint64_t flags) {
    static std::atomic<bool> logged{false};
    if (!logged.exchange(true, std::memory_order_relaxed))
        std::fprintf(stderr, "[rt] d3d12_xs: fence slot 12 (value %llu, descriptor 0x%llx, flags 0x%llx): returns "
                     "the address of the fence's completed value, kept live; the descriptor is not used\n",
                     static_cast<unsigned long long>(value), static_cast<unsigned long long>(descriptor),
                     static_cast<unsigned long long>(flags));
    FenceMirror* m = fence_mirror(self);
    if (!m) {
        std::fprintf(stderr, "[rt] d3d12_xs: fence slot 12: out of memory for the completed value\n");
        return 0;
    }
    refresh_mirror(m);
    return reinterpret_cast<std::uint64_t>(&m->completed);
}

// Frees a proxy whose last reference is gone, with the real object and the device reference it held. A fence's
// mirror wait is unregistered first, waiting for a callback in flight, so none runs on a released fence.
void destroy_child(ChildProxy* self) {
    IUnknown* real = self->real;
    DeviceProxy* device = self->device;
    FenceMirror* mirror = self->mirror.load(std::memory_order_acquire);
    if (mirror && mirror->wait) {
        AcquireSRWLockExclusive(&mirror->lock);
        mirror->stopping = true;
        SetThreadpoolWait(mirror->wait, nullptr, nullptr);
        ReleaseSRWLockExclusive(&mirror->lock);
        WaitForThreadpoolWaitCallbacks(mirror->wait, TRUE);
        CloseThreadpoolWait(mirror->wait);
    }
    delete self;
    if (real) {
        forget_real_queue(device, real);  // a borrowed entry from the console submit's queue table
        real->Release();
    }
    if (mirror) {
        if (mirror->event) CloseHandle(mirror->event);
        delete mirror;
    }
    if (device) proxy_release(device);
}

template <ChildKind K>
unsigned long child_add_ref(ChildProxy* self) {
    note_child_slot<K>(1);
    return self->refs.fetch_add(1, std::memory_order_relaxed) + 1;
}

template <ChildKind K>
unsigned long child_release(ChildProxy* self) {
    note_child_slot<K>(2);
    const unsigned long refs = self->refs.fetch_sub(1, std::memory_order_acq_rel) - 1;
    if (refs == 0) destroy_child(self);
    return refs;
}

// The interfaces the proxy answers: the base chain and the object's own interface.
template <ChildKind K>
bool is_child_iid(const IID& iid) {
    if (IsEqualIID(iid, __uuidof(IUnknown)) || IsEqualIID(iid, __uuidof(ID3D12Object)) ||
        IsEqualIID(iid, __uuidof(ID3D12DeviceChild)) || IsEqualIID(iid, __uuidof(ID3D12Pageable)))
        return true;
    if constexpr (K == ChildKind::queue) {
        return IsEqualIID(iid, __uuidof(ID3D12CommandQueue));
    } else if constexpr (K == ChildKind::fence) {
        return IsEqualIID(iid, __uuidof(ID3D12Fence)) || IsEqualIID(iid, __uuidof(ID3D12Fence1));
    } else {
        return IsEqualIID(iid, __uuidof(ID3D12CommandAllocator));
    }
}

bool supports_iid(IUnknown* real, REFIID iid) {
    void* found = nullptr;
    const HRESULT hr = real->QueryInterface(iid, &found);
    if (FAILED(hr) || !found) return false;
    static_cast<IUnknown*>(found)->Release();
    return true;
}

// An interface the real object implements but the proxy does not model is refused where the PC runtime would answer:
// logged once per interface (the first 32 of them), so a title that stops after such a refusal shows what it asked
// for. Also reached from wrap_child, for the interface a title creates a queue or fence as.
void log_refused_iid(const char* kind, const IID& iid) {
    static SRWLOCK lock = SRWLOCK_INIT;
    static IID seen[32];
    static unsigned count = 0;
    AcquireSRWLockExclusive(&lock);
    bool known = false;
    for (unsigned i = 0; i < count && !known; ++i) known = IsEqualIID(seen[i], iid) != 0;
    const bool log = !known && count < std::size(seen);
    if (log) seen[count++] = iid;
    ReleaseSRWLockExclusive(&lock);
    if (!log) return;
    std::fprintf(stderr,
                 "[rt] d3d12_xs: %s proxy refuses {%08lX-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X}, which the real "
                 "object implements; E_NOINTERFACE\n",
                 kind, iid.Data1, iid.Data2, iid.Data3, iid.Data4[0], iid.Data4[1], iid.Data4[2], iid.Data4[3],
                 iid.Data4[4], iid.Data4[5], iid.Data4[6], iid.Data4[7]);
}

template <ChildKind K>
HRESULT child_query_interface(ChildProxy* self, const IID* riid, void** out) {
    note_child_slot<K>(0);
    if (!out) return E_POINTER;
    *out = nullptr;
    if (!riid) return E_POINTER;
    // Only what the proxy models, and of that only what the real object implements (ID3D12Fence1 is optional).
    if (!is_child_iid<K>(*riid)) {
        if (supports_iid(self->real, *riid)) log_refused_iid(child_kind_name<K>(), *riid);
        return E_NOINTERFACE;
    }
    if (!supports_iid(self->real, *riid)) return E_NOINTERFACE;
    *out = self;
    child_add_ref<K>(self);
    return S_OK;
}

// ID3D12DeviceChild::GetDevice hands back the Xbox-ABI device, not the real one. Without an output it is the real
// object's own answer (what the PC runtime returns for a null pointer).
template <ChildKind K>
HRESULT child_get_device(ChildProxy* self, const IID* riid, void** out) {
    note_child_slot<K>(7);
    if (out) *out = nullptr;  // _COM_Outptr_: NULL on every failure (bc59ef46)
    if (!riid) return E_POINTER;
    if (!out) {
        using Fn = HRESULT(WINAPI*)(IUnknown*, const IID*, void**);
        return reinterpret_cast<Fn>(self->real_vtable[7])(self->real, riid, nullptr);
    }
    if (!self->device) return E_NOINTERFACE;
    return proxy_query_interface(self->device, riid, out);
}

template <ChildKind K>
void** get_child_vtable();

// The command-list proxy's helpers (defined with the list proxy below): proxy_real_object checks lists through
// list_real_or_null, since the ListProxy type is complete only there.
IUnknown* list_real_or_null(const void* object);

template <ChildKind K>
bool is_child_proxy(const void* object) {
    return object && *reinterpret_cast<void* const*>(object) == reinterpret_cast<void*>(get_child_vtable<K>());
}

// The real object behind a heap, queue or fence proxy, or null when `object` is none of them.
IUnknown* proxy_real_object(const void* object) {
    if (!object) return nullptr;
    void* const vtable = *reinterpret_cast<void* const*>(object);
    if (vtable == reinterpret_cast<void*>(get_heap_vtable())) return static_cast<const HeapProxy*>(object)->real;
    if (vtable == reinterpret_cast<void*>(get_child_vtable<ChildKind::queue>()) ||
        vtable == reinterpret_cast<void*>(get_child_vtable<ChildKind::fence>()) ||
        vtable == reinterpret_cast<void*>(get_child_vtable<ChildKind::allocator>()))
        return static_cast<const ChildProxy*>(object)->real;
    if (IUnknown* real = list_real_or_null(object)) return real;
    return nullptr;
}

// `object` with a proxy replaced by its real object; anything else (a real object, null) unchanged.
std::uint64_t unwrap_object(std::uint64_t object) {
    IUnknown* real = proxy_real_object(reinterpret_cast<const void*>(object));
    return real ? reinterpret_cast<std::uint64_t>(real) : object;
}

// A copy of an array of interface pointers with every proxy replaced by its real object. Nothing here throws, since
// an exception must not leave a thunk for the title's code: up to 16 entries are kept inline (no allocation, which
// also suits a per-frame ExecuteCommandLists), a longer array is allocated with new (std::nothrow).
class UnwrappedArray {
public:
    // Copies `count` entries (a UINT in every PC prototype) of `objects`. False on allocation failure.
    bool fill(std::uint64_t objects, std::uint64_t count) {
        const auto n = static_cast<std::uint32_t>(count);
        if (!objects || !n) return true;
        std::uint64_t* copy = inline_;
        if (n > std::size(inline_)) {
            heap_.reset(new (std::nothrow) std::uint64_t[n]);
            if (!heap_) return false;
            copy = heap_.get();
        }
        const auto* in = reinterpret_cast<const std::uint64_t*>(objects);
        for (std::uint32_t i = 0; i < n; ++i) copy[i] = unwrap_object(in[i]);
        data_ = copy;
        return true;
    }
    // The copy to pass on, or 0 when there was no array (the caller's argument then goes on unchanged).
    std::uint64_t data() const { return reinterpret_cast<std::uint64_t>(data_); }

private:
    std::uint64_t inline_[16];
    std::unique_ptr<std::uint64_t[]> heap_;
    std::uint64_t* data_ = nullptr;
};

// The vtable read for a slot past a table faults in the reservation behind it; name the slot. Only reads the
// exception record and writes with WriteFile, so it is safe whatever the faulting thread holds.
template <ChildKind K>
void report_table_overrun(std::uintptr_t address) {
    using S = ChildStats<K>;
    const std::uintptr_t begin = S::table_begin.load(std::memory_order_acquire);
    const std::uintptr_t end = S::table_end.load(std::memory_order_acquire);
    const std::uintptr_t limit = S::region_end.load(std::memory_order_acquire);
    if (!end || address < end || address >= limit) return;
    S::overruns.fetch_add(1, std::memory_order_relaxed);
    char line[160];
    unsigned long long slot = (address - begin) / sizeof(void*);
    char digits[24];
    int d = 0;
    do {
        digits[d++] = static_cast<char>('0' + slot % 10);
        slot /= 10;
    } while (slot && d < 23);
    int n = 0;
    const auto put = [&](const char* text) {
        while (*text && n < static_cast<int>(sizeof(line)) - 1) line[n++] = *text++;
    };
    put("[rt] d3d12_xs: ");
    put(child_kind_name<K>());
    put(" slot ");
    while (d > 0 && n < static_cast<int>(sizeof(line)) - 1) line[n++] = digits[--d];
    put(" is past the proxy table; the vtable read faults\n");
    DWORD written = 0;
    WriteFile(GetStdHandle(STD_ERROR_HANDLE), line, static_cast<DWORD>(n), &written, nullptr);
}

LONG CALLBACK table_overrun_handler(EXCEPTION_POINTERS* info) {
    const EXCEPTION_RECORD* record = info ? info->ExceptionRecord : nullptr;
    if (record && record->ExceptionCode == EXCEPTION_ACCESS_VIOLATION && record->NumberParameters >= 2) {
        const auto address = static_cast<std::uintptr_t>(record->ExceptionInformation[1]);
        report_table_overrun<ChildKind::queue>(address);
        report_table_overrun<ChildKind::fence>(address);
        report_table_overrun<ChildKind::allocator>(address);
    }
    return EXCEPTION_CONTINUE_SEARCH;  // diagnostics only: the fault goes on to the title's handlers or the crash
}

// ---- PresentX on the queue (LEONIDA_VULKAN_DEVICE) ---------------------------------------------------------
//
// The measured console queue slots (docs/d3d12_xs_console_abi.md §4): the older runtime keeps the PC order —
// Signal 14, Wait 15, frequency 16, calibration 17, GetDesc 18 — and presents at slot 30, with SignalX at 23. A
// newer game OS inserts two methods before Signal: Signal 16, Wait 17, frequency 18, calibration 19, GetDesc 20,
// SignalX 25, PresentX 32. The layout is detected once, by a fence object reaching slot 16 or 17 (the older
// layout's frequency and calibration getters take no object argument) or by a fence reaching SignalX slot 25 —
// a title that signals only through SignalX would otherwise stay undetected and lose every signal.
//
// Every other console-only queue method answers S_OK (sibling layer: title frame pacing flows through them, and a
// refusal wedges the present loop), still recorded by note_child_slot. Slot 148's AllocateUploadMemory is a list
// method, not a queue method, so it is not part of this table.

#ifdef LEONIDA_VULKAN_DEVICE

struct PresentState {
    ~PresentState() {
        if (owns_window && window) DestroyWindow(window);  // only the window this state made dies with it
    }
    HWND window = nullptr;
    bool owns_window = false;  // false: the window is the title's, and outlives the presentation
    std::unique_ptr<rt::d3d12::XboxPresentation> xbox;
    bool tried = false;  // creation is attempted once; a null xbox means this device cannot present
};

// The window the console's presentation belongs to. PresentX takes no HWND: on the console the title's own window is
// what the picture appears in, and the retail executable makes it itself ("sgaWindowClass", title "Americas"). So the
// presenter is put on that window. A window of this layer's own would be a second, hidden window and the title's one
// would stay blank, which is exactly how a white window was seen with the presentation running at hundreds of frames.
//
// The lookup sends no message to any window. The present thread is not the title's window thread, and that thread can
// be busy (or waiting on the present thread) when the first PresentX comes: GetWindowTextW, or any call that sends a
// message across threads, would block the present thread there (measured upstream: reading the titles stopped the
// retail title at its first PresentX). Everything below reads the window manager's own state.
struct TitleWindowProbe {
    HWND window = nullptr;
    long long area = -1;
};

HWND own_present_window = nullptr;  // the fallback window this layer made, never mistaken for the title's

BOOL CALLBACK find_title_window_proc(HWND window, LPARAM param) {
    auto* probe = reinterpret_cast<TitleWindowProbe*>(param);
    DWORD process = 0;
    if (window == own_present_window || !GetWindowThreadProcessId(window, &process) || process != GetCurrentProcessId())
        return TRUE;
    if (!IsWindowVisible(window) || GetWindow(window, GW_OWNER) != nullptr) return TRUE;  // an owned window is a dialog
    if (GetWindowLongPtrW(window, GWL_EXSTYLE) & WS_EX_TOOLWINDOW) return TRUE;
    RECT client{};
    const long long area = GetClientRect(window, &client)
                               ? static_cast<long long>(client.right - client.left) * (client.bottom - client.top)
                               : 0;
    if (area > probe->area) {
        probe->window = window;
        probe->area = area;
    }
    return TRUE;
}

HWND find_title_window() {
    TitleWindowProbe probe;
    EnumWindows(&find_title_window_proc, reinterpret_cast<LPARAM>(&probe));
    return probe.window;
}

void destroy_present_state(void* present_state) {
    delete static_cast<PresentState*>(present_state);
}

LRESULT CALLBACK present_window_proc(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    return DefWindowProcW(window, message, wparam, lparam);
}

// Lazily builds the window, presenter and XboxPresentation a console PresentX needs, once per device. Returns the
// state even when `xbox` stays null (no presentation support): the failure is logged once, not retried.
PresentState* present_state_for(DeviceProxy* device) {
    if (auto* state = static_cast<PresentState*>(device->present_state)) return state;
    auto* state = new (std::nothrow) PresentState;
    if (!state) return nullptr;
    device->present_state = state;
    state->tried = true;
    if (HWND title_window = find_title_window()) {
        state->window = title_window;
        RECT client{};
        GetClientRect(title_window, &client);
        std::fprintf(stderr, "[rt] d3d12_xs: PresentX presents into the title's window %p (client %ldx%ld, visible %d)\n",
                     static_cast<void*>(title_window), client.right - client.left, client.bottom - client.top,
                     IsWindowVisible(title_window) ? 1 : 0);
    } else {
        // No window of the title's (it has not made one yet): make one and show it, so a frame it does present is
        // visible rather than going into a hidden window.
        WNDCLASSW window_class{};
        window_class.lpfnWndProc = present_window_proc;
        window_class.hInstance = GetModuleHandleW(nullptr);
        window_class.lpszClassName = L"LeonidaPresentX";
        if (!RegisterClassW(&window_class) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) {
            std::fprintf(stderr, "[rt] d3d12_xs: PresentX window class failed (error %lu)\n", GetLastError());
            return state;
        }
        state->window = CreateWindowExW(0, window_class.lpszClassName, L"Leonida", WS_OVERLAPPEDWINDOW | WS_VISIBLE,
                                        CW_USEDEFAULT, CW_USEDEFAULT, CW_USEDEFAULT, CW_USEDEFAULT, nullptr, nullptr,
                                        window_class.hInstance, nullptr);
        if (!state->window) {
            std::fprintf(stderr, "[rt] d3d12_xs: PresentX window failed (error %lu)\n", GetLastError());
            return state;
        }
        state->owns_window = true;
        own_present_window = state->window;
        ShowWindow(state->window, SW_SHOWNORMAL);
        std::fprintf(stderr, "[rt] d3d12_xs: no title window yet; PresentX uses its own window %p\n",
                     static_cast<void*>(state->window));
    }
    auto presenter = rt::d3d12::create_window_presenter(device->real, state->window);
    if (!presenter) {
        std::fprintf(stderr, "[rt] d3d12_xs: no window presenter on this device; PresentX is a no-op\n");
        return state;
    }
    // The device's frame events issue the tokens a title hands to PresentX, so the presentation's pacer must number
    // from the same source: with a counter of its own it refused every token the device had issued (E_INVALIDARG on
    // every present, and a white window that never received a frame).
    state->xbox = std::make_unique<rt::d3d12::XboxPresentation>(std::move(presenter), nullptr,
                                                                device_frame_tokens(device));
    std::fprintf(stderr, "[rt] d3d12_xs: PresentX ready (window %p)\n", static_cast<void*>(state->window));
    return state;
}

std::uint64_t queue_present_x(ChildProxy* self, std::uint64_t plane_count, std::uint64_t planes,
                              std::uint64_t params) {
    PresentState* state = self->device ? present_state_for(self->device) : nullptr;
    // Whether the title reaches its frame loop at all is the difference between "the picture is wrong" and "it never
    // presented"; one line every 60 presents says which without flooding the log (PresentX itself is not logged).
    static std::atomic<std::uint64_t> presents{0};
    const std::uint64_t present = presents.fetch_add(1, std::memory_order_relaxed) + 1;
    if (present == 1 || present % 60 == 0)
        std::fprintf(stderr, "[rt] d3d12_xs: PresentX call %llu (%llu plane(s))\n",
                     static_cast<unsigned long long>(present), static_cast<unsigned long long>(plane_count));
    // What the first present actually names: the display texture's shape, so a white window can be read as "the title
    // presents a texture this size and format" rather than guessed at.
    if (present == 1 && planes) {
        const auto* first = reinterpret_cast<const rt::d3d12::XboxPresentPlaneParameters*>(planes);
        if (first->ResourceCount && first->ppResources && first->ppResources[0]) {
            const D3D12_RESOURCE_DESC desc = first->ppResources[0]->GetDesc();
            D3D12_HEAP_PROPERTIES heap{};
            D3D12_HEAP_FLAGS heap_flags = D3D12_HEAP_FLAG_NONE;
            const HRESULT heap_hr = first->ppResources[0]->GetHeapProperties(&heap, &heap_flags);
            std::fprintf(stderr,
                         "[rt] d3d12_xs: first present plane: %llux%u format %d, %u resource(s), colour space %d, "
                         "display heap %s, flags 0x%x, resources %p\n",
                         static_cast<unsigned long long>(desc.Width), desc.Height, int(desc.Format),
                         first->ResourceCount, first->ColorSpace,
                         SUCCEEDED(heap_hr) && (heap_flags & D3D12_HEAP_FLAG_ALLOW_DISPLAY) ? "yes" : "no",
                         static_cast<unsigned>(desc.Flags), static_cast<void*>(first->ppResources[0]));
        }
    }
    if (!state || !state->xbox) return hr_to_u64(S_OK);  // a title still runs with nothing presented
    const HRESULT present_hr = state->xbox->PresentX(
        static_cast<UINT>(plane_count), reinterpret_cast<const rt::d3d12::XboxPresentPlaneParameters*>(planes),
        reinterpret_cast<const rt::d3d12::XboxPresentParameters*>(params));
    static std::atomic<std::uint64_t> failures{0};
    if (FAILED(present_hr)) {
        const std::uint64_t failed = failures.fetch_add(1, std::memory_order_relaxed) + 1;
        if (failed == 1 || failed % 600 == 0)
            std::fprintf(stderr, "[rt] d3d12_xs: PresentX failed with 0x%08lx (%llu failure(s) so far)\n",
                         static_cast<unsigned long>(present_hr), static_cast<unsigned long long>(failed));
    } else if (present == 1) {
        std::fprintf(stderr, "[rt] d3d12_xs: PresentX returned S_OK\n");
    }
    return hr_to_u64(present_hr);
}

#else  // !LEONIDA_VULKAN_DEVICE: this translation unit stands alone (mock and COM tests)

void destroy_present_state(void*) {}

std::uint64_t queue_present_x(ChildProxy*, std::uint64_t, std::uint64_t, std::uint64_t) {
    return hr_to_u64(S_OK);
}

#endif

template <ChildKind K, unsigned Slot>
std::uint64_t WINAPI child_slot(ChildProxy* self, std::uint64_t a1, std::uint64_t a2, std::uint64_t a3,
                                std::uint64_t a4, std::uint64_t a5, std::uint64_t a6, std::uint64_t a7,
                                std::uint64_t a8, std::uint64_t a9, std::uint64_t a10, std::uint64_t a11,
                                std::uint64_t a12, std::uint64_t a13, std::uint64_t a14) {
    if constexpr (Slot == 0) {
        d3d12_trace::record(child_trace_name<K>(Slot));
        return hr_to_u64(child_query_interface<K>(self, reinterpret_cast<const IID*>(a1),
                                                  reinterpret_cast<void**>(a2)));
    } else if constexpr (Slot == 1) {
        return child_add_ref<K>(self);
    } else if constexpr (Slot == 2) {
        return child_release<K>(self);
    } else if constexpr (Slot == 7) {
        d3d12_trace::record(child_trace_name<K>(Slot));
        return hr_to_u64(child_get_device<K>(self, reinterpret_cast<const IID*>(a1),
                                             reinterpret_cast<void**>(a2)));
    } else if constexpr (K == ChildKind::fence && Slot == kFenceCompletedValueSlot) {
        // Past ID3D12Fence1, so whether the real fence has Fence1 does not matter here.
        note_child_slot<K>(Slot);
        d3d12_trace::record(child_trace_name<K>(Slot));
        return fence_completed_value_address(self, a1, a2, a3);
    } else if constexpr (K == ChildKind::queue && Slot == 35) {
        // The console's submit. The title records its drawing into command lists and submits one batch per frame here;
        // the trace shows no ID3D12CommandQueue::ExecuteCommandLists call anywhere.
        //
        // The arguments do not name the batch: a1 is 1, a2 is the address of a fixed array of five list proxies whose
        // entries never change over 100,000 submits, and a3 counts up. A list drawn into is never among those five, so
        // executing a1 entries of a2 ran a list that carried no command -- the submit "succeeded" and the frame stayed
        // black. What the submit does carry is everything recorded since the last submit (console_begin_recording keeps
        // that list), which is what this executes: the close first, because the layer executes a closed list, then the
        // batch. LEONIDA_SUBMIT_35=1 enables it while the console submit is still being worked out.
        note_child_slot<K>(Slot);
        if (const char* submit = std::getenv("LEONIDA_SUBMIT_35"); submit && submit[0] == '1')
            console_submit_batch(self->device, self->real, self->real_vtable);
        return hr_to_u64(S_OK);
    } else {
        note_child_slot<K>(Slot);
        if constexpr (K == ChildKind::queue) {
            // Layout detection (the comment at the PresentX section): a fence reaching slot 16 or 17 is the newer
            // layout's Signal/Wait; anything else there is the older layout's frequency/calibration getter. A
            // fence at slot 14 or 15 is the older Signal/Wait; a non-fence argument there is one of the newer
            // layout's two inserted console methods.
            // A queue made after another one detected the layout inherits it from the device; undetected queues
            // assume the older layout, whose unshifted slots are also the PC chain. A fence reaching SignalX
            // slot 25 while the layout is still unknown is itself the newer-layout evidence: a title that
            // signals only through SignalX never reaches Signal/Wait at 16/17, and an unanswered SignalX
            // returns S_OK having signalled nothing, leaving a waiter on that fence value hanging.
            const bool newer = [&] {
                const unsigned char own = self->queue_layout.load(std::memory_order_relaxed);
                if (own == ChildProxy::kQueueLayoutNewer) return true;
                const unsigned char shared =
                    self->device->console_layout.load(std::memory_order_relaxed);
                return own == ChildProxy::kQueueLayoutUnknown && shared == ChildProxy::kQueueLayoutNewer;
            }();
            if (Slot == 16 || Slot == 17) {
                if (IUnknown* fence = proxy_real_object(reinterpret_cast<const void*>(a1))) {
                    self->queue_layout.store(ChildProxy::kQueueLayoutNewer, std::memory_order_relaxed);
                    self->device->console_layout.store(ChildProxy::kQueueLayoutNewer,
                                                       std::memory_order_relaxed);
                    using Fn = HRESULT(WINAPI*)(IUnknown*, IUnknown*, UINT64);
                    return hr_to_u64(reinterpret_cast<Fn>(self->real_vtable[kQueueSignal + (Slot - 16)])(
                        self->real, fence, a2));
                }
                if (newer) return hr_to_u64(S_OK);  // proven newer: a non-fence Signal/Wait arg is malformed,
                // not evidence of the older layout — do not downgrade the shared detection on it
                if (self->queue_layout.load(std::memory_order_relaxed) ==
                    ChildProxy::kQueueLayoutUnknown) {
                    self->queue_layout.store(ChildProxy::kQueueLayoutOlder, std::memory_order_relaxed);
                    self->device->console_layout.store(ChildProxy::kQueueLayoutOlder,
                                                       std::memory_order_relaxed);
                }
            } else if ((Slot == 14 || Slot == 15) && !proxy_real_object(reinterpret_cast<const void*>(a1)) &&
                       newer) {
                return hr_to_u64(S_OK);  // the newer layout's two inserted console methods, unmeasured
            }
            if (newer && Slot >= 18 && Slot <= 20) {
                // Newer layout: frequency 18, calibration 19, GetDesc 20 are the PC slots 16-18 shifted.
                d3d12_trace::record(child_trace_name<K>(Slot));
                using ForwardFn = std::uint64_t(WINAPI*)(void*, std::uint64_t, std::uint64_t, std::uint64_t,
                                                         std::uint64_t, std::uint64_t, std::uint64_t,
                                                         std::uint64_t, std::uint64_t, std::uint64_t,
                                                         std::uint64_t, std::uint64_t, std::uint64_t,
                                                         std::uint64_t, std::uint64_t);
                auto fn = reinterpret_cast<ForwardFn>(self->real_vtable[Slot - 2]);
                return fn(self->real, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11, a12, a13, a14);
            }
            // Slot 18 while the layout is still unknown is ambiguous: the older GetDesc (a struct-return
            // write of a whole desc) or the newer frequency getter (an 8-byte out). Answering it as GetDesc
            // writes far past the 8 bytes a frequency caller passes, so the call goes to the frequency
            // getter — the only interpretation that cannot overrun its argument, and a title often queries
            // the frequency before its first fence call. A proven-older queue keeps the generic GetDesc
            // path below; slots 19 and 20 stay no-ops, since their newer interpretations write through
            // caller pointers an unmodelled older method might not supply.
            if (Slot == 18 && !newer && self->queue_layout == ChildProxy::kQueueLayoutUnknown &&
                self->device->console_layout.load(std::memory_order_relaxed) !=
                    ChildProxy::kQueueLayoutOlder) {
                d3d12_trace::record(child_trace_name<K>(Slot));
                using Fn = HRESULT(WINAPI*)(IUnknown*, UINT64*);
                return hr_to_u64(reinterpret_cast<Fn>(self->real_vtable[16])(
                    self->real, reinterpret_cast<UINT64*>(a1)));
            }
            if (Slot == (newer ? 25u : 23u) ||
                (Slot == 25u && self->queue_layout.load(std::memory_order_relaxed) ==
                                    ChildProxy::kQueueLayoutUnknown &&
                 self->device->console_layout.load(std::memory_order_relaxed) ==
                     ChildProxy::kQueueLayoutUnknown)) {
                // SignalX: console Signal (older 23, newer 25); the extra X word is dropped. The slot-25
                // alternative is the fence itself carrying the layout evidence, per the comment above; a
                // proven-older queue still leaves slot 25 to the console-only no-op, since its fence-taking
                // methods sit at 14/15/23 there.
                if (IUnknown* fence = proxy_real_object(reinterpret_cast<const void*>(a1))) {
                    if (Slot == 25u && !newer) {
                        self->queue_layout.store(ChildProxy::kQueueLayoutNewer,
                                                 std::memory_order_relaxed);
                        self->device->console_layout.store(ChildProxy::kQueueLayoutNewer,
                                                           std::memory_order_relaxed);
                    }
                    static std::atomic<bool> logged{false};
                    if (!logged.exchange(true, std::memory_order_relaxed))
                        std::fprintf(stderr, "[rt] d3d12_xs: SignalX (queue slot %u) forwarded as Signal\n", Slot);
                    using Fn = HRESULT(WINAPI*)(IUnknown*, IUnknown*, UINT64);
                    return hr_to_u64(reinterpret_cast<Fn>(self->real_vtable[kQueueSignal])(self->real, fence, a2));
                }
                return hr_to_u64(S_OK);
            }
            if (Slot == (newer ? 32u : 30u)) return queue_present_x(self, a1, a2, a3);  // PresentX
            if (Slot >= self->slot_count || (newer && (Slot == 14 || Slot == 15))) {
                d3d12_trace::record(child_trace_name<K>(Slot), "console-only");
                return hr_to_u64(S_OK);  // unmodelled console queue methods: frame pacing flows through them
            }
        }
        if (Slot >= self->slot_count) {
            bool expected = false;
            if (ChildStats<K>::beyond_warned[Slot].compare_exchange_strong(expected, true,
                                                                            std::memory_order_relaxed)) {
                std::fprintf(stderr,
                             "[rt] d3d12_xs: %s slot %u is beyond the PC %s vtable (%u slots); returning E_NOTIMPL "
                             "(args 0x%llx 0x%llx 0x%llx 0x%llx)\n",
                             child_kind_name<K>(), Slot, child_kind_name<K>(), self->slot_count,
                             static_cast<unsigned long long>(a1), static_cast<unsigned long long>(a2),
                             static_cast<unsigned long long>(a3), static_cast<unsigned long long>(a4));
            }
            d3d12_trace::record(child_trace_name<K>(Slot), "unsupported");
            return hr_to_u64(E_NOTIMPL);
        }
        d3d12_trace::record(child_trace_name<K>(Slot));  // "called": the real object's result is not inspected
        if constexpr (K == ChildKind::queue && (Slot == kQueueSignal || Slot == kQueueWait)) a1 = unwrap_object(a1);

        // Queue slot 10: ExecuteCommandLists(count, lists). The array is copied with each list proxy replaced by
        // its real list; a bundle on the list side unwraps the same way at the console's ExecuteBundle.
        UnwrappedArray unwrapped;
        if constexpr (K == ChildKind::queue && Slot == 10) {
            if (!unwrapped.fill(a2, a1)) return hr_to_u64(E_OUTOFMEMORY);
            if (unwrapped.data()) a2 = unwrapped.data();
        }

        // The PC vtable entry with the real object as `this`; integer/pointer arguments, the hidden struct-return
        // pointer and stack arguments keep their positions, as in the device proxy.
        using ForwardFn = std::uint64_t(WINAPI*)(void*, std::uint64_t, std::uint64_t, std::uint64_t,
                                                 std::uint64_t, std::uint64_t, std::uint64_t, std::uint64_t,
                                                 std::uint64_t, std::uint64_t, std::uint64_t, std::uint64_t,
                                                 std::uint64_t, std::uint64_t, std::uint64_t);
        auto fn = reinterpret_cast<ForwardFn>(self->real_vtable[Slot]);
        if constexpr (K == ChildKind::fence && Slot == kFenceSignal) {
            const std::uint64_t result = fn(self->real, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11, a12, a13, a14);
            if (FenceMirror* m = self->mirror.load(std::memory_order_acquire)) refresh_mirror(m);
            return result;
        }
        return fn(self->real, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11, a12, a13, a14);
    }
}

template <ChildKind K, std::size_t... I>
void fill_child_vtable(void** vtable, std::index_sequence<I...>) {
    ((vtable[I] = reinterpret_cast<void*>(&child_slot<K, static_cast<unsigned>(I)>)), ...);
}

// True when the fault logger is installed (once per process; a failure is logged once).
bool install_table_overrun_handler() {
    static const bool installed = [] {
        if (AddVectoredExceptionHandler(0, &table_overrun_handler)) return true;
        std::fprintf(stderr, "[rt] d3d12_xs: AddVectoredExceptionHandler failed (error %lu); a slot past a queue or "
                     "fence proxy table faults without being named\n", GetLastError());
        return false;
    }();
    return installed;
}

template <ChildKind K>
void** get_child_vtable() {
    static void** vtable = [] {
        using S = ChildStats<K>;
        constexpr std::size_t bytes = S::kSlots * sizeof(void*);
        SYSTEM_INFO system{};
        GetSystemInfo(&system);
        const SIZE_T page = system.dwPageSize;
        if (void* base = page >= bytes ? VirtualAlloc(nullptr, kTableRegion, MEM_RESERVE, PAGE_NOACCESS) : nullptr) {
            if (VirtualAlloc(base, page, MEM_COMMIT, PAGE_READWRITE)) {
                auto* table = reinterpret_cast<void**>(static_cast<char*>(base) + page - bytes);
                fill_child_vtable<K>(table, std::make_index_sequence<S::kSlots>{});
                DWORD old = 0;
                if (!VirtualProtect(base, page, PAGE_READONLY, &old))  // the table works, but stays writable
                    std::fprintf(stderr, "[rt] d3d12_xs: %s proxy table stays writable (VirtualProtect error %lu)\n",
                                 child_kind_name<K>(), GetLastError());
                S::table_begin.store(reinterpret_cast<std::uintptr_t>(table), std::memory_order_release);
                S::region_end.store(reinterpret_cast<std::uintptr_t>(base) + kTableRegion, std::memory_order_release);
                // table_end is what "guarded" means (command_object_tables_guarded, the fault logger): set only when
                // a fault past the table will be named. Without the logger the region still faults, unnamed.
                if (install_table_overrun_handler())
                    S::table_end.store(reinterpret_cast<std::uintptr_t>(base) + page, std::memory_order_release);
                return table;
            }
            VirtualFree(base, 0, MEM_RELEASE);
        }
        // No reservation: the table still works, but a slot past it is not caught.
        std::fprintf(stderr, "[rt] d3d12_xs: %s proxy table without a guard region\n", child_kind_name<K>());
        static void* storage[S::kSlots];
        fill_child_vtable<K>(storage, std::make_index_sequence<S::kSlots>{});
        return storage;
    }();
    return vtable;
}

// Wraps `created` (one reference, owned by the proxy on success, released on failure) and returns `riid` from the
// proxy through `out`.
template <ChildKind K>
HRESULT wrap_child(IUnknown* created, DeviceProxy* device, const IID* riid, void** out) {
    if (!created) return E_FAIL;
    ChildProxy* proxy = new (std::nothrow) ChildProxy;
    if (!proxy) {
        created->Release();
        return E_OUTOFMEMORY;
    }
    proxy->vtable = get_child_vtable<K>();
    proxy->real = created;
    proxy->device = device;
    proxy->real_vtable = *reinterpret_cast<void***>(created);
    if constexpr (K == ChildKind::queue) {
        proxy->slot_count = kQueuePublicSlots;
    } else if constexpr (K == ChildKind::fence) {
        proxy->slot_count = supports_iid(created, __uuidof(ID3D12Fence1)) ? kFence1PublicSlots : kFencePublicSlots;
    } else {
        proxy->slot_count = kAllocatorPublicSlots;
    }
    proxy->refs.store(1, std::memory_order_relaxed);
    proxy_add_ref(device);  // the object keeps its Xbox-ABI device alive for GetDevice

    const HRESULT hr = child_query_interface<K>(proxy, riid, out);
    if (FAILED(hr)) {
        destroy_child(proxy);  // no reference escaped
        return hr;
    }
    // Titles create fences at run time: the first proxy of each kind is logged, every one only with XS_TRACE.
    static std::atomic<bool> logged{false};
    if (!d3d12_trace::enabled() && (!logged.exchange(true, std::memory_order_relaxed) || trace_enabled()))
        std::fprintf(stderr, "[rt] d3d12_xs: %s proxy %p wraps %p (device proxy %p, %u public slots)\n",
                     child_kind_name<K>(), static_cast<void*>(proxy), static_cast<void*>(created),
                     static_cast<void*>(device), proxy->slot_count);
    proxy->refs.fetch_sub(1, std::memory_order_relaxed);  // drop the creation reference; `out` holds the caller's
    return S_OK;
}

// ---- Command list proxy ---------------------------------------------------------------------------------
//
// Device slot 12 (CreateCommandList) hands the title a console-shaped command list: the console vtable order, a
// 160-slot table, and the object fields the console SDK's inline writers touch (docs/d3d12_xs_console_abi.md
// §5). The write cursor (+0x18) is pinned at both limits (+0x28, +0x30), so a title always finds the record
// buffer full and takes the vtable path for every command — the proxy never has to interpret inline records.
// The flag bytes (+0x140..+0x143), the root-parameter header table (+0x344) and the pipeline record (+0x544)
// stay zeroed: they are only read while writing a record, which never happens at the limit.
//
// The console slot order keeps the PC order through slot 26 (measured: 9, 10, 12-17, 19, 20, 25, 26), places
// four console-only methods at 27-30, then the PC chain resumes shifted +4 — confirmed at console 32->PC 28
// (SetDescriptorHeaps), 33->29, 41->37, 43->39, 45->41, 48->44, 51->47 and 53->49, and the same shift fills the
// block through console 59 -> PC 55. The console has no PIX markers: console 60 is EndEvent (PC 58) and 61 is
// ExecuteIndirect (PC 59, package-measured). Console 146 is the enhanced Barrier taking one D3D12_BARRIER_GROUP
// (package-confirmed; PC equivalent is GCL7 slot 80). Console-only slots — the 27-30 extras, AllocateUploadMemory
// (148), the disputed tail past 61 and the newer-layout +2 tail — are recorded with their first argument words
// and return 0 without a PC call: a wrong forward is worse than a clean refusal.

constexpr unsigned kListSlots = 160;
// ---- Upload memory (console list slot 148, AllocateUploadMemory) ----
//
// On the console the CPU and GPU share one address space: the pointer AllocateUploadMemory(size, alignment)
// returns is written by the CPU and handed to the GPU unchanged — the package measures it going straight into a
// root SRV (docs/d3d12_xs_console_abi.md §5.3, d3d12_command_objects.md §3.3a). A PC has two address spaces, so
// each arena chunk is a persistently-mapped UPLOAD-heap buffer: the title gets the CPU pointer and writes the
// very memory the GPU samples, and the root-descriptor setters translate a chunk address to the buffer's device
// address (the same backing memory) before forwarding. Translation covers the six root VA setters (console
// 41-46); VAs inside view structs (IASetVertexBuffers/IASetIndexBuffer) and ExecuteIndirect arguments are not
// rewritten yet and stay title addresses.

struct UploadChunk {
    UploadChunk* next;
    ID3D12Resource* resource;      // the mapped buffer; released with the chunk
    std::uint8_t* cpu_base;
    D3D12_GPU_VIRTUAL_ADDRESS gpu_base;
    std::size_t size;
    std::size_t used;
};

struct UploadArena {
    UploadChunk* chunks = nullptr;
    std::mutex lock;
};

constexpr std::size_t kUploadChunkMin = 8 << 20;  // one chunk serves a title's whole frame of uploads

UploadChunk* upload_chunk_new(DeviceProxy* device, std::size_t size) {
    auto* chunk = new (std::nothrow) UploadChunk{};
    if (!chunk) return nullptr;
    chunk->size = size;

    D3D12_HEAP_PROPERTIES heap = {};
    heap.Type = D3D12_HEAP_TYPE_UPLOAD;
    D3D12_RESOURCE_DESC desc = {};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = size;
    desc.Height = 1;
    desc.DepthOrArraySize = 1;
    desc.MipLevels = 1;
    desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ID3D12Resource* resource = nullptr;
    const HRESULT hr = device->real->CreateCommittedResource(
        &heap, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_GENERIC_READ, nullptr,
        __uuidof(ID3D12Resource), reinterpret_cast<void**>(&resource));
    void* cpu = nullptr;
    if (SUCCEEDED(hr) && resource && SUCCEEDED(resource->Map(0, nullptr, &cpu)) && cpu &&
        resource->GetGPUVirtualAddress() != 0) {
        chunk->resource = resource;
        chunk->cpu_base = static_cast<std::uint8_t*>(cpu);
        chunk->gpu_base = resource->GetGPUVirtualAddress();
        return chunk;
    }
    if (resource) resource->Release();
    // A device that cannot hand out a mapped buffer (a mock, or an older layer without buffer device
    // addresses) still gets memory to write; upload_translate leaves its addresses alone.
    auto* cpu_fallback = static_cast<std::uint8_t*>(
        VirtualAlloc(nullptr, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE));
    if (!cpu_fallback) {
        delete chunk;
        return nullptr;
    }
    chunk->cpu_base = cpu_fallback;
    chunk->gpu_base = reinterpret_cast<D3D12_GPU_VIRTUAL_ADDRESS>(cpu_fallback);
    return chunk;
}

std::uint64_t upload_alloc(DeviceProxy* device, std::uint64_t size, std::uint64_t alignment) {
    if (!device || !size) return 0;
    alignment = alignment < 16 ? 16 : alignment;  // every measured call asked for 16
    // Refuse what cannot be served before any arithmetic runs: only power-of-two alignments are measured,
    // chunks map at 64K granularity at best, and a size too big to add the alignment to would wrap the
    // checks below — one such call returned a pointer and left the cursor so the next request re-handed
    // the same range (daemosofchaos's report on #2490).
    if ((alignment & (alignment - 1)) != 0 || alignment > (64 << 10) ||
        size > ~static_cast<std::size_t>(0) - alignment) {
        std::fprintf(stderr, "[rt] d3d12_xs: AllocateUploadMemory(%llu, %llu) refused\n",
                     static_cast<unsigned long long>(size), static_cast<unsigned long long>(alignment));
        return 0;
    }
    auto* arena = static_cast<UploadArena*>(device->upload_state.load(std::memory_order_acquire));
    if (!arena) {
        auto* fresh = new (std::nothrow) UploadArena;
        if (!fresh) return 0;
        void* expected = nullptr;  // a concurrent first call loses its arena but keeps the winner's chunks
        if (device->upload_state.compare_exchange_strong(expected, fresh, std::memory_order_acq_rel))
            arena = fresh;
        else {
            delete fresh;
            arena = static_cast<UploadArena*>(expected);
        }
    }
    std::lock_guard<std::mutex> lock(arena->lock);
    const std::size_t need = static_cast<std::size_t>(size);
    for (UploadChunk* chunk = arena->chunks; chunk; chunk = chunk->next) {
        const std::size_t aligned = (chunk->used + (alignment - 1)) & ~(static_cast<std::size_t>(alignment) - 1);
        if (aligned >= chunk->used && aligned <= chunk->size && need <= chunk->size - aligned) {
            chunk->used = aligned + need;
            return reinterpret_cast<std::uint64_t>(chunk->cpu_base + aligned);
        }
    }
    const std::size_t chunk_size = need + alignment > kUploadChunkMin ? need + alignment : kUploadChunkMin;
    UploadChunk* chunk = upload_chunk_new(device, chunk_size);
    if (!chunk) {
        std::fprintf(stderr, "[rt] d3d12_xs: AllocateUploadMemory(%llu, %llu) failed\n",
                     static_cast<unsigned long long>(size), static_cast<unsigned long long>(alignment));
        return 0;
    }
    chunk->next = arena->chunks;
    arena->chunks = chunk;
    const std::size_t aligned = (alignment - 1) & ~(static_cast<std::size_t>(alignment) - 1);  // 0-based
    chunk->used = aligned + need;
    return reinterpret_cast<std::uint64_t>(chunk->cpu_base + aligned);
}

// An address inside an upload chunk becomes the chunk's device address plus the offset; anything else — a
// real resource VA the title already holds — passes through.
std::uint64_t upload_translate(DeviceProxy* device, std::uint64_t va) {
    auto* arena = static_cast<UploadArena*>(
        device ? device->upload_state.load(std::memory_order_acquire) : nullptr);
    if (!arena) return va;
    std::lock_guard<std::mutex> lock(arena->lock);
    for (UploadChunk* chunk = arena->chunks; chunk; chunk = chunk->next) {
        const std::uint64_t base = reinterpret_cast<std::uint64_t>(chunk->cpu_base);
        if (va >= base && va < base + chunk->size) return chunk->gpu_base + (va - base);
    }
    return va;
}

// A V# in a table the title wrote by hand can name an address outside the GPU address arena: a 48-bit field cannot
// hold a buffer whose console VA is the device address minus heap_base (a negative or >2^48 number, docs section 3.3),
// and a shader that adds heap_base to it reads unmapped memory and loses the device. Zeroing such an entry makes the
// load return 0 (the same buffer bounds check sees size 0), so one bad descriptor no longer stops the frame.
void sanitize_descriptor_table(std::uint8_t* table, std::uint64_t bytes) {
    const std::uint64_t arena = leonida::arena_host_size;
    if (!arena) return;
    for (std::uint64_t at = 0; at + 16 <= bytes; at += 16) {
        auto* words = reinterpret_cast<std::uint32_t*>(table + at);
        const unsigned type = (words[3] >> 28) & 0xf;
        if (type >= 8) {
            const std::uint64_t address =
                ((std::uint64_t(words[1] & 0xff) << 32) | words[0]) << 8;  // a T#: 256-byte units
            if (address >= arena) std::memset(words, 0, 32);
            continue;
        }
        const std::uint64_t base = (std::uint64_t(words[1] & 0xffff) << 32) | (words[0] & 0xfffffffcull);
        const unsigned stride = (words[1] >> 16) & 0x3fff;
        const std::uint64_t records = stride ? std::uint64_t(words[2]) * stride : words[2];
        // Only the base has to be an address: a title's V# can carry a record count that says nothing about the end
        // of the buffer (a raw buffer's count is a byte size, a structured one a record count), and the shader's own
        // bounds check is what stops a load. LEONIDA_XS_TABLE_STRICT also rejects an extent outside the arena.
        const bool strict = std::getenv("LEONIDA_XS_TABLE_STRICT") != nullptr;
        if (base >= arena || (strict && records > arena - base)) {
            // LEONIDA_LOG_SANITIZE: which entries the title wrote that cannot name a buffer in the arena. A table whose
            // entries are all zeroed here is not in the format the shader's 16-byte V# load expects.
            if (std::getenv("LEONIDA_LOG_SANITIZE")) {
                static std::atomic<int> shown{0};
                if (shown.fetch_add(1, std::memory_order_relaxed) < 40)
                    std::fprintf(stderr,
                                 "[rt] sanitize: +0x%llx %08x %08x %08x %08x V# base 0x%llx stride %u records %llu "
                                 "(arena 0x%llx)\n",
                                 static_cast<unsigned long long>(at), words[0], words[1], words[2], words[3],
                                 static_cast<unsigned long long>(base), stride,
                                 static_cast<unsigned long long>(records),
                                 static_cast<unsigned long long>(arena));
            }
            std::memset(words, 0, 16);
        }
    }
}


// Title memory used as GPU memory. A root descriptor or a vertex or index view can name an address of the title's own
// allocator, which no resource covers: title memory is host memory here, so the shader cannot read it as a device
// address. The 4 MiB window the address lands in is copied into an UPLOAD buffer created through the layer, whose device
// address the shader can read; the copy is refreshed on every use because the title writes it every frame. VIctory does
// the same (crates/victory-gpu/src/shadow.rs); XCL instead places title memory in UMA windows, which Vulkan on Windows
// has no equivalent of.
struct GuestWindow {
    ID3D12Resource* resource = nullptr;
    std::uint8_t* mapped = nullptr;
    std::uint64_t gpu = 0;
    std::uint64_t start = 0;
    std::uint64_t len = 0;
};

struct GuestWindows {
    std::mutex lock;
    std::vector<GuestWindow> windows;
};

constexpr std::uint64_t kGuestWindowBits = 22;  // 4 MiB, the window size VIctory measured as enough for the title
constexpr std::uint64_t kGuestWindow = std::uint64_t(1) << kGuestWindowBits;

GuestWindows* guest_windows(DeviceProxy* device) {
    if (!device) return nullptr;
    if (auto* existing = static_cast<GuestWindows*>(device->guest_windows.load(std::memory_order_acquire)))
        return existing;
    auto* fresh = new (std::nothrow) GuestWindows;
    if (!fresh) return nullptr;
    void* expected = nullptr;
    if (device->guest_windows.compare_exchange_strong(expected, fresh, std::memory_order_acq_rel))
        return fresh;
    delete fresh;
    return static_cast<GuestWindows*>(expected);
}

// A device address that reads the same bytes as the title address `va`, by copying `bytes` of it into the window's
// buffer. Zero when the window cannot be made, so the caller keeps the address it had.
std::uint64_t guest_translate(DeviceProxy* device, std::uint64_t va, std::uint64_t bytes, bool sanitize = false) {
    if (!device || !va) return 0;
    if (!leonida::is_committed(reinterpret_cast<const void*>(va))) return 0;  // already a device address
    GuestWindows* windows = guest_windows(device);
    if (!windows) return 0;
    const std::uint64_t start = va & ~(kGuestWindow - 1);
    std::uint64_t len = kGuestWindow;
    while (va + bytes > start + len) len <<= 1;
    std::lock_guard lock(windows->lock);
    GuestWindow* entry = nullptr;
    for (GuestWindow& window : windows->windows)
        if (window.start == start) {
            entry = &window;
            break;
        }
    if (!entry) {
        D3D12_HEAP_PROPERTIES heap{};
        heap.Type = D3D12_HEAP_TYPE_UPLOAD;
        D3D12_RESOURCE_DESC desc{};
        desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        desc.Width = len;
        desc.Height = 1;
        desc.DepthOrArraySize = 1;
        desc.MipLevels = 1;
        desc.SampleDesc.Count = 1;
        desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        ID3D12Resource* resource = nullptr;
        if (FAILED(device->real->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc,
                                                         D3D12_RESOURCE_STATE_GENERIC_READ, nullptr,
                                                         __uuidof(ID3D12Resource),
                                                         reinterpret_cast<void**>(&resource))) ||
            !resource)
            return 0;
        void* mapped = nullptr;
        const std::uint64_t gpu = resource->GetGPUVirtualAddress();
        if (FAILED(resource->Map(0, nullptr, &mapped)) || !mapped || !gpu) {
            resource->Release();
            return 0;
        }
        static std::atomic<int> logged{0};
        if (logged.fetch_add(1, std::memory_order_relaxed) < 8)
            std::fprintf(stderr, "[rt] d3d12_xs: title memory window 0x%llx (%llu bytes) copied for the GPU\n",
                         static_cast<unsigned long long>(start), static_cast<unsigned long long>(len));
        windows->windows.push_back(GuestWindow{resource, static_cast<std::uint8_t*>(mapped), gpu, start, len});
        entry = &windows->windows.back();
    }
    const std::uint64_t offset = va - start;
    const std::uint64_t room = entry->len - offset;
    leonida::copy_committed(entry->mapped + offset, va, std::min(bytes, room));
    if (sanitize) sanitize_descriptor_table(entry->mapped + offset, std::min(bytes, room));
    return entry->gpu + offset;
}

// The window copy's bytes for `va`, once guest_translate has made the window, or null.
std::uint8_t* guest_window_bytes(DeviceProxy* device, std::uint64_t va, std::uint64_t bytes) {
    GuestWindows* windows = guest_windows(device);
    if (!windows) return nullptr;
    const std::uint64_t start = va & ~(kGuestWindow - 1);
    std::lock_guard lock(windows->lock);
    for (GuestWindow& window : windows->windows)
        if (window.start == start) {
            const std::uint64_t offset = va - start;
            if (offset + bytes > window.len) return nullptr;
            return window.mapped + offset;
        }
    return nullptr;
}

// The shader adds heap_base to every address a descriptor names. A console title names its own memory, whose console VA
// is a host address here, so a V# base that is title memory has to become the device address of the window holding a
// copy of those bytes (guest_translate). Bases that are already console VAs into the arena stay; a base that is
// neither is zeroed. `va`'s window must exist (guest_translate ran for it) and `bytes` bytes of the table fit in it.
void rewrite_table_descriptors(DeviceProxy* device, std::uint64_t va, std::uint8_t* window, std::uint64_t bytes) {
    const std::uint64_t arena = leonida::arena_host_size;
    std::vector<std::uint32_t> table(static_cast<std::size_t>(bytes / 4), 0);
    leonida::copy_committed(reinterpret_cast<std::uint8_t*>(table.data()), va, bytes);
    for (std::uint64_t at = 0; at + 16 <= bytes; at += 16) {
        std::uint32_t* words = table.data() + at / 4;
        const unsigned type = (words[3] >> 28) & 0xf;
        std::uint64_t address = type >= 8 ? (((std::uint64_t(words[1] & 0xff) << 32) | words[0]) << 8)
                                          : ((std::uint64_t(words[1] & 0xffff) << 32) | (words[0] & 0xfffffffcull));
        if (!address) continue;
        const std::uint64_t source = address;
        // LEONIDA_XS_TABLE_INDIRECT: the entry can name a per-resource structure whose first qword is the data rather
        // than the data itself (title memory at such a base held {ptr, 0, ptr, 0} pairs). Follow one pointer first.
        if (type < 8 && std::getenv("LEONIDA_XS_TABLE_INDIRECT") &&
            leonida::is_committed(reinterpret_cast<const void*>(address))) {
            std::uint64_t inner = 0;
            leonida::copy_committed(reinterpret_cast<std::uint8_t*>(&inner), address, sizeof(inner));
            if (inner) address = inner;
        }
        // A title VA is a host address here, so title memory at that number wins over the "inside the arena" reading:
        // a console VA that merely lies in the arena's range has no data behind it, while the title's memory does.
        if (leonida::is_committed(reinterpret_cast<const void*>(address))) {
            const std::uint64_t window_va = guest_translate(device, address, 4096);
            if (window_va) {
                address = window_va;
            } else if (address >= arena) {
                std::memset(words, 0, 16);
                std::memcpy(window + at, words, 16);
                continue;
            }
        } else if (address >= arena) {
            const std::uint64_t window_va =
                leonida::is_committed(reinterpret_cast<const void*>(address)) ? guest_translate(device, address, 64) : 0;
            if (!window_va) {
                std::memset(words, 0, 16);
                std::memcpy(window + at, words, 16);
                continue;
            }
            address = window_va;
        }
        if (type >= 8) {
            words[0] = std::uint32_t(address >> 8);
            words[1] = (words[1] & ~0xffu) | std::uint32_t((address >> 40) & 0xff);
        } else {
            words[0] = std::uint32_t(address & 0xfffffffcull) | (words[0] & 3u);
            words[1] = (words[1] & 0xffff0000u) | std::uint32_t((address >> 32) & 0xffff);
            // A title's table is not a gfx10 V# (the dword a shader reads as NUM_RECORDS repeats the address), so the
            // count a shader would loop over is meaningless. Clamp it to what the window copy actually holds, which
            // keeps a rewritten table usable instead of sending the shader on an enormous loop (a GPU hang).
            const unsigned stride = (words[1] >> 16) & 0x3fff;
            const std::uint64_t records = words[2];
            const std::uint64_t limit = 4096;
            words[2] = std::uint32_t(stride ? std::min<std::uint64_t>(records, limit / stride)
                                            : std::min<std::uint64_t>(records, limit));
        }
        std::memcpy(window + at, words, type >= 8 ? 32 : 16);
        // LEONIDA_LOG_TABLE: what the rewrite made of the first records, and the first bytes of the data they name.
        if (std::getenv("LEONIDA_LOG_TABLE") && at <= 32) {
            std::uint32_t sample[4] = {};
            leonida::copy_committed(reinterpret_cast<std::uint8_t*>(sample), source, 16);
            std::fprintf(stderr,
                         "[rt] rewrite +0x%llx: %08x %08x %08x %08x from 0x%llx; data %08x %08x %08x %08x\n",
                         static_cast<unsigned long long>(at), words[0], words[1], words[2], words[3],
                         static_cast<unsigned long long>(source), sample[0], sample[1], sample[2], sample[3]);
        }
    }
}

// The device address a shader must read for the title address `va`: the upload arena's, the title-memory window's, or
// the address itself when it already is one.
std::uint64_t shader_address(DeviceProxy* device, std::uint64_t va, std::uint64_t bytes) {
    const std::uint64_t translated = upload_translate(device, va);
    if (translated != va) return translated;
    const std::uint64_t guest = guest_translate(device, va, bytes);
    return guest ? guest : va;
}

void destroy_upload_state(void* upload_state) {
    auto* arena = static_cast<UploadArena*>(upload_state);
    if (!arena) return;
    for (UploadChunk* chunk = arena->chunks; chunk;) {
        UploadChunk* next = chunk->next;
        if (chunk->resource) {
            chunk->resource->Unmap(0, nullptr);
            chunk->resource->Release();
        } else {
            VirtualFree(chunk->cpu_base, 0, MEM_RELEASE);
        }
        delete chunk;
        chunk = next;
    }
    delete arena;
}

constexpr unsigned kListNotForwarded = ~0u;
constexpr unsigned kConsoleSetDescriptorHeaps = 32;
constexpr unsigned kConsoleExecuteBundle = 31;
constexpr unsigned kConsoleBarrier = 146;
constexpr unsigned kConsoleAllocateUpload = 148;
// The newer console layout's Reset: the PC Reset (10) two slots on per console method, which is 139 for the method the
// title takes a pooled list from. It is also the layout evidence a command list can see for itself (see list_slot).
constexpr unsigned kConsoleResetNewer = 139;

constexpr unsigned console_to_pc_list_slot(unsigned slot) {
    if (slot <= 26) return slot;               // PC order, measured
    if (slot <= 30) return kListNotForwarded;  // console-only extras
    if (slot <= 55) return slot - 4;           // PC chain 28-51, shifted +4
    // 56-59 keep their PC numbers: the console's four extras moved PC 52-55 to 28-31, it did not shift the block above
    // them (sibling layer: ExecuteIndirect is console 59 in the older layout, and PC slot 59 is ExecuteIndirect). The
    // comment this replaces filed them under the same -4 as the block below, which would have forwarded a title's
    // ExecuteIndirect to PC 55 (SetPredication).
    if (slot <= 59) return slot;
    if (slot == 60) return 58;                 // EndEvent (newer layout; the console has no SetMarker/BeginEvent)
    if (slot == 61) return 59;                 // ExecuteIndirect (newer layout: the older layout's 59, two slots on)
    if (slot == kConsoleBarrier) return 80;    // D3D12_BARRIER_GROUP (Package; PC GCL7)
    return kListNotForwarded;
}

struct ListProxy {
    void** vtable;                      // +0x00
    ID3D12GraphicsCommandList* real;    // +0x08
    DeviceProxy* device;                // +0x10: keeps the Xbox-ABI device alive; GetDevice returns it
    // +0x18..: the console object body a title's inline writers read. +0x18 holds the write cursor, +0x28 and
    // +0x30 the record limits; the rest is room for the measured flags, header table and pipeline record.
    alignas(8) std::uint8_t console_fields[0x600];
    std::atomic<unsigned long> refs;    // past the console body
    unsigned pc_slot_count;             // slots the real list implements; mapped slots past it are refused
    // The allocator CreateCommandList was given, kept for the Reset the console never asks for (console_begin_recording)
    // and for teardown.
    ID3D12CommandAllocator* real_allocator;
    // The type the title created the list with. The console submit hands every recorded list to one submit call, and a
    // PC queue refuses a list of another type, so the submit routes each list to a queue of its own type.
    D3D12_COMMAND_LIST_TYPE type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    // The console Close (slot 9) was forwarded: the real list is closed until the next recording call reopens it.
    std::atomic<bool> closed_by_title;
    // This list is in its device's pending_lists.
    std::atomic<bool> pending;
    std::vector<D3D12_VERTEX_BUFFER_VIEW> vb_scratch;  // slot-48 views with their VAs translated
    std::vector<D3D12_STREAM_OUTPUT_BUFFER_VIEW> so_scratch;  // slot-49 views, same treatment

    void pin_cursor_at_limit() {
        const std::uint64_t at_limit = static_cast<std::uint64_t>(
            reinterpret_cast<std::uintptr_t>(this) + 0x18 + 0x500);
        for (const size_t offset : {0x18, 0x28, 0x30})
            std::memcpy(reinterpret_cast<std::uint8_t*>(this) + offset, &at_limit, sizeof(at_limit));
    }
};
static_assert(offsetof(ListProxy, console_fields) == 0x18, "the console fields start at +0x18");

struct ListStats {
    static inline std::atomic<std::uint64_t> hits[kListSlots];
    static inline std::atomic<bool> first[kListSlots];
};

// LEONIDA_LOG_SLOTS=1: a cumulative console list slot histogram on stderr every two seconds. Which console methods
// the title really calls decides which console-only ones are worth modelling — the newer console layout moves
// Write{32,64}BitValue* / the fills / Reset onto different slots (docs/d3d12_xs_console_abi.md 5.3), and those are
// the writes a GPU-side counter needs before an indirect draw has anything to dispatch. The counters below exist
// for the tests (list_slot_hits); this only prints them.
void dump_list_slot_histogram() {
    std::fprintf(stderr, "[rt] d3d12_xs: console list slot histogram\n");
    for (unsigned slot = 0; slot < kListSlots; ++slot) {
        const std::uint64_t hits = ListStats::hits[slot].load(std::memory_order_relaxed);
        if (hits) std::fprintf(stderr, "[rt]   slot %u: %llu\n", slot, static_cast<unsigned long long>(hits));
    }
}

bool list_slot_histogram_due() {
    static std::atomic<unsigned long long> next_ms{0};
    const unsigned long long now = GetTickCount64();
    unsigned long long expected = next_ms.load(std::memory_order_relaxed);
    if (now < expected) return false;
    return next_ms.compare_exchange_strong(expected, now + 2000, std::memory_order_relaxed);
}

// The list tail (D3D-445). From CopyTextureRegionX on, the newer console layout sits two slots later than the older
// one (docs/d3d12_xs_console_abi.md 5.3). The table is keyed by the older-layout slot. Only CopyTextureRegionX has a
// measured name; the other measured pairs are named by their older slot, so that both layouts print the same text
// for the same method.
struct ListTailName {
    unsigned older_slot;
    const char* name;
};
constexpr ListTailName kListTailNames[] = {
    {83, "CopyTextureRegionX"},         {84, "tail method, older slot 84"}, {85, "tail method, older slot 85"},
    {88, "tail method, older slot 88"}, {89, "tail method, older slot 89"}, {90, "tail method, older slot 90"},
    {93, "tail method, older slot 93"}, {94, "tail method, older slot 94"},
};
constexpr unsigned kListTailShift = 2;
static_assert(kConsoleLayoutOlder == ChildProxy::kQueueLayoutOlder &&
                  kConsoleLayoutNewer == ChildProxy::kQueueLayoutNewer,
              "the list tail names use the queue proxy's layout values");

// Null while the layout is unknown, and for a slot that is not a measured tail slot of that layout: a slot number
// never gets the other layout's name.
const char* list_tail_name(unsigned slot, unsigned char console_layout) {
    if (console_layout != kConsoleLayoutOlder && console_layout != kConsoleLayoutNewer) return nullptr;
    const unsigned shift = console_layout == kConsoleLayoutNewer ? kListTailShift : 0;
    if (slot < shift) return nullptr;
    for (const ListTailName& entry : kListTailNames)
        if (entry.older_slot == slot - shift) return entry.name;
    return nullptr;
}

void note_list_slot(unsigned slot, std::uint64_t a1, std::uint64_t a2, std::uint64_t a3, std::uint64_t a4,
                    unsigned char console_layout = 0) {
    if (slot >= kListSlots) return;
    ListStats::hits[slot].fetch_add(1, std::memory_order_relaxed);
    static const bool slot_histogram = std::getenv("LEONIDA_LOG_SLOTS") != nullptr;
    if (slot_histogram && list_slot_histogram_due()) dump_list_slot_histogram();
    bool expected = false;
    if (ListStats::first[slot].compare_exchange_strong(expected, true, std::memory_order_relaxed)) {
        const char* name = list_tail_name(slot, console_layout);
        std::fprintf(stderr, "[rt] d3d12_xs: list slot %u%s%s%s first call (args 0x%llx 0x%llx 0x%llx 0x%llx)\n", slot,
                     name ? " (" : "", name ? name : "", name ? ")" : "", static_cast<unsigned long long>(a1),
                     static_cast<unsigned long long>(a2), static_cast<unsigned long long>(a3),
                     static_cast<unsigned long long>(a4));
    }
}

// The interfaces the console list answers: the base chain, ID3D12CommandList and every graphics-command-list
// interface the real list implements — on the console the list is a single object, so any of them returns the
// proxy.
bool is_list_iid(const IID& iid) {
    return IsEqualIID(iid, __uuidof(IUnknown)) || IsEqualIID(iid, __uuidof(ID3D12Object)) ||
           IsEqualIID(iid, __uuidof(ID3D12DeviceChild)) || IsEqualIID(iid, __uuidof(ID3D12CommandList)) ||
           IsEqualIID(iid, __uuidof(ID3D12GraphicsCommandList)) ||
           IsEqualIID(iid, __uuidof(ID3D12GraphicsCommandList1)) ||
           IsEqualIID(iid, __uuidof(ID3D12GraphicsCommandList2)) ||
           IsEqualIID(iid, __uuidof(ID3D12GraphicsCommandList3)) ||
           IsEqualIID(iid, __uuidof(ID3D12GraphicsCommandList4)) ||
           IsEqualIID(iid, __uuidof(ID3D12GraphicsCommandList5)) ||
           IsEqualIID(iid, __uuidof(ID3D12GraphicsCommandList6)) ||
           IsEqualIID(iid, __uuidof(ID3D12GraphicsCommandList7)) ||
           IsEqualIID(iid, __uuidof(ID3D12GraphicsCommandList8));
}

unsigned long list_add_ref(ListProxy* self) { return self->refs.fetch_add(1, std::memory_order_relaxed) + 1; }

void destroy_list(ListProxy* self) {
    IUnknown* real = self->real;
    DeviceProxy* device = self->device;
    if (device && self->pending.load(std::memory_order_relaxed)) {
        std::lock_guard lock(device->pending_mutex);
        auto& pending = device->pending_lists;
        pending.erase(std::remove(pending.begin(), pending.end(), static_cast<void*>(self)), pending.end());
    }
    ID3D12CommandAllocator* allocator = self->real_allocator;
    delete self;
    if (allocator) allocator->Release();
    if (real) real->Release();
    if (device) proxy_release(device);
}

unsigned long list_release(ListProxy* self) {
    const unsigned long refs = self->refs.fetch_sub(1, std::memory_order_acq_rel) - 1;
    if (refs == 0) destroy_list(self);
    return refs;
}

// The console list lifecycle. The title records a frame into a list, closes it (slot 9), submits the batch through the
// queue's console slot 35, and then records the next frame into the SAME list: it never calls Reset, so the console
// driver must be reopening the list when recording starts again. Before this, every command after the first frame was
// dropped by CommandList::append (`closed_`), so every list closed with zero commands, the submit executed an empty
// batch, and the window stayed black however much the title drew. Two pieces replay that: the first recording call
// after a close resets the list, and the submit executes every list recorded since the last submit.
void console_note_recording(ListProxy* self) {
    if (self->pending.load(std::memory_order_relaxed)) return;
    self->pending.store(true, std::memory_order_relaxed);
    DeviceProxy* device = self->device;
    if (!device) return;
    std::lock_guard lock(device->pending_mutex);
    device->pending_lists.push_back(self);
}

// Reopens a list the title closed, if it did, so this recording call can append. False when the list stays closed (no
// allocator to reset it with, or the reset failed): the call is then forwarded anyway and refused by the layer.
bool console_begin_recording(ListProxy* self) {
    if (!self->closed_by_title.load(std::memory_order_relaxed)) return true;
    if (!self->real || !self->real_allocator) {
        if (std::getenv("LEONIDA_LOG_REOPEN")) {
            static std::atomic<int> logged{0};
            if (logged.fetch_add(1, std::memory_order_relaxed) < 8)
                std::fprintf(stderr, "[rt] reopen: list %p has no allocator (real %p)\n", static_cast<void*>(self),
                             static_cast<void*>(self->real));
        }
        return false;
    }
    using ResetFn = HRESULT(WINAPI*)(IUnknown*, ID3D12CommandAllocator*, ID3D12PipelineState*);
    auto** const vtable = *reinterpret_cast<void***>(self->real);
    const HRESULT hr = reinterpret_cast<ResetFn>(vtable[10])(self->real, self->real_allocator, nullptr);
    if (FAILED(hr)) {
        if (std::getenv("LEONIDA_LOG_REOPEN")) {
            static std::atomic<int> logged{0};
            if (logged.fetch_add(1, std::memory_order_relaxed) < 8)
                std::fprintf(stderr, "[rt] reopen: list %p reset failed 0x%08lx\n", static_cast<void*>(self),
                             static_cast<unsigned long>(hr));
        }
        return false;
    }
    self->closed_by_title.store(false, std::memory_order_relaxed);
    return true;
}

// Closes a list whose commands the submit is about to execute. A list the title already closed answers E_FAIL, which is
// not an error here: it is closed, which is what the layer wants.
void console_close_for_submit(ListProxy* self) {
    if (!self->real) return;
    if (self->closed_by_title.load(std::memory_order_relaxed)) return;
    using CloseFn = HRESULT(WINAPI*)(IUnknown*);
    auto** const vtable = *reinterpret_cast<void***>(self->real);
    const HRESULT hr = reinterpret_cast<CloseFn>(vtable[9])(self->real);
    if (SUCCEEDED(hr)) self->closed_by_title.store(true, std::memory_order_relaxed);
}

// The queue a list of `type` must be executed on: one the title created with that type, or one made here. The console
// submit runs every recorded list through a single submit call, while a PC queue refuses a list of another type (the
// layer logs "list 0 has type 2, the queue type 0" and executes nothing), which is how the title's DMA uploads were
// never run. A title queue is borrowed, never referenced: the mock test counts the title objects' references, and the
// entry is dropped when the queue proxy dies (destroy_child). Only a queue made here is owned.
void record_real_queue(DeviceProxy* device, ID3D12CommandQueue* queue, D3D12_COMMAND_LIST_TYPE type) {
    if (!device || !queue) return;
    std::lock_guard lock(device->queues_mutex);
    for (int at = 0; at < device->real_queue_count; ++at)
        if (device->real_queues[at].queue == queue) return;
    if (device->real_queue_count >= kSubmitQueueSlots) return;
    device->real_queues[device->real_queue_count++] = DeviceProxy::SubmitQueue{queue, type};
}

void forget_real_queue(DeviceProxy* device, const void* queue) {
    if (!device || !queue) return;
    std::lock_guard lock(device->queues_mutex);
    for (int at = 0; at < device->real_queue_count; ++at) {
        if (device->real_queues[at].queue != queue) continue;
        device->real_queues[at] = device->real_queues[--device->real_queue_count];
        device->real_queues[device->real_queue_count] = DeviceProxy::SubmitQueue{};
        return;
    }
}

ID3D12CommandQueue* real_queue_for(DeviceProxy* device, D3D12_COMMAND_LIST_TYPE type) {
    if (!device) return nullptr;
    std::lock_guard lock(device->queues_mutex);
    for (int at = 0; at < device->real_queue_count; ++at)
        if (device->real_queues[at].type == type) return device->real_queues[at].queue;
    D3D12_COMMAND_QUEUE_DESC desc{};
    desc.Type = type;
    desc.Priority = D3D12_COMMAND_QUEUE_PRIORITY_NORMAL;
    ID3D12CommandQueue* queue = nullptr;
    if (FAILED(device->real->CreateCommandQueue(&desc, __uuidof(ID3D12CommandQueue),
                                               reinterpret_cast<void**>(&queue))) ||
        !queue)
        return nullptr;
    if (device->made_queue_count < kSubmitQueueSlots) device->made_queues[device->made_queue_count++] = queue;
    if (device->real_queue_count < kSubmitQueueSlots)
        device->real_queues[device->real_queue_count++] = DeviceProxy::SubmitQueue{queue, type};
    static std::atomic<int> logged{0};
    if (logged.fetch_add(1, std::memory_order_relaxed) < 4)
        std::fprintf(stderr, "[rt] d3d12_xs: console submit made a queue of type %d (the title had none)\n",
                     static_cast<int>(type));
    return queue;
}

void release_real_queues(DeviceProxy* device) {
    if (!device) return;
    ID3D12CommandQueue* made[kSubmitQueueSlots] = {};
    int count = 0;
    {
        std::lock_guard lock(device->queues_mutex);
        for (int at = 0; at < device->made_queue_count; ++at) made[count++] = device->made_queues[at];
        device->made_queue_count = 0;
        device->real_queue_count = 0;
    }
    for (int at = 0; at < count; ++at) made[at]->Release();
}

// Queue slot 35 with LEONIDA_SUBMIT_35=1: the batch is everything the title has closed since the last submit, not the
// lists in the slot's arguments (see the comment at the slot). A list the title closed is finished; closing one here
// instead raced with the thread still recording into it, which then saw a closed list and lost its frame. Each finished
// list goes to the queue in its own ExecuteCommandLists call: the layer refuses a whole batch for one unknown resource
// ("unknown resource; nothing was submitted"), and the first batch after startup holds every list recorded so far,
// including commands whose resources no longer resolve.
void console_submit_batch(DeviceProxy* device, IUnknown* queue, void** queue_vtable) {
    if (!device || !queue || !queue_vtable) return;
    std::vector<void*> batch;
    {
        std::lock_guard lock(device->pending_mutex);
        batch.swap(device->pending_lists);
    }
    using ExecuteFn = void(WINAPI*)(IUnknown*, UINT, ID3D12CommandList* const*);
    std::vector<void*> unfinished;
    std::size_t submitted = 0;
    for (void* entry : batch) {
        auto* list = static_cast<ListProxy*>(entry);
        if (!list->real || !list->closed_by_title.load(std::memory_order_relaxed)) {
            list->pending.store(false, std::memory_order_relaxed);
            unfinished.push_back(entry);
            continue;
        }
        list->pending.store(false, std::memory_order_relaxed);
        // Each list goes to a queue of its own type: a PC queue refuses a list of another type outright.
        ID3D12CommandQueue* target = real_queue_for(device, list->type);
        if (!target) target = static_cast<ID3D12CommandQueue*>(queue);
        auto** const target_vtable = *reinterpret_cast<void***>(target);
        auto execute = reinterpret_cast<ExecuteFn>(target_vtable[10]);
        ID3D12CommandList* real = list->real;
        execute(target, 1, &real);
        ++submitted;
    }
    static std::atomic<int> submits{0};
    const int nth = submits.fetch_add(1, std::memory_order_relaxed) + 1;
    if (!unfinished.empty()) {  // still being recorded: the next submit picks them up
        std::lock_guard lock(device->pending_mutex);
        for (void* entry : unfinished) {
            auto* list = static_cast<ListProxy*>(entry);
            list->pending.store(true, std::memory_order_relaxed);
            device->pending_lists.push_back(entry);
        }
    }
    if (nth == 1 && submitted == 0)
        std::fprintf(stderr, "[rt] d3d12_xs: the first console submit has no finished list\n");
    if (nth <= 3 || nth % 600 == 0)
        std::fprintf(stderr, "[rt] d3d12_xs: console submit #%d executed %zu of %zu list(s)\n", nth, submitted,
                     batch.size());
}

// ---- Console-only list methods ---------------------------------------------------------------------------------
//
// Past the PC chain the console list holds methods a PC has no equivalent of (docs/d3d12_xs_console_abi.md 5.3). The
// ones this title records every frame are the memory and fence protocol a GPU-driven frame needs: values written into
// unified memory (Write{32,64}BitValue*OfPipeX), waits for values (Wait{32,64}BitValueX), fills, copies, and a Reset
// through slot 139. Dropping them left the title's own counters unwritten and its waits unanswered; xcl measured the
// same title and found "the title's indirect draws and dispatches never ran" while these slots were misrouted
// (crates/xcl/src/hle/d3d12x/real.rs).
//
// The newer console layout puts every one of them two slots later (older 60->62, 69->71, 70->72, 72->74, 78->80,
// 81->83), so a slot is mapped to the OLDER layout's numbering before it is handled.
enum ConsoleListExtension : unsigned {
    kExtNone = ~0u,
    kExtCopyMemoryToMemory = 60,
    kExtFill32 = 61,
    kExtFill64 = 62,
    kExtFill128 = 63,
    kExtDepthBounds = 68,
    kExtWrite32Bottom = 69,
    kExtWrite64Bottom = 70,
    kExtWait32 = 71,
    kExtWait64 = 72,
    kExtDispatchX = 78,
    kExtDrawIndexedX = 81,
    kExtCopyResourceX = 84,
    kExtWrite32Top = 88,
    kExtWrite64Top = 89,
    kExtWrite32BottomOfShader = 90,
    kExtKickoffX = 93,
    kExtSetDepthBias = 94,
};

// The extension `slot` names, in the older layout's numbering, or kExtNone. An unknown layout answers kExtNone: the
// same slot means different methods in the two layouts, and taking the wrong one writes a value into whatever address
// the other layout's method had in its first argument.
unsigned console_list_extension(unsigned slot, unsigned char layout) {
    if (layout == kConsoleLayoutNewer) {
        static const struct {
            unsigned slot;
            unsigned extension;
        } kNewerPairs[] = {
            {62, kExtCopyMemoryToMemory}, {63, kExtFill32},       {64, kExtFill64},
            {65, kExtFill128},            {70, kExtDepthBounds},  {71, kExtWrite32Bottom},
            {72, kExtWrite64Bottom},      {73, kExtWait32},       {74, kExtWait64},
            {80, kExtDispatchX},          {83, kExtDrawIndexedX}, {86, kExtCopyResourceX},
            {90, kExtWrite32Top},         {91, kExtWrite64Top},   {92, kExtWrite32BottomOfShader},
            {95, kExtKickoffX},           {96, kExtSetDepthBias},
        };
        for (const auto& pair : kNewerPairs)
            if (pair.slot == slot) return pair.extension;
        return kExtNone;
    }
    if (layout != kConsoleLayoutOlder || slot < 60) return kExtNone;
    switch (slot) {
        case kExtCopyMemoryToMemory:
        case kExtFill32:
        case kExtFill64:
        case kExtFill128:
        case kExtDepthBounds:
        case kExtWrite32Bottom:
        case kExtWrite64Bottom:
        case kExtWait32:
        case kExtWait64:
        case kExtDispatchX:
        case kExtDrawIndexedX:
        case kExtCopyResourceX:
        case kExtWrite32Top:
        case kExtWrite64Top:
        case kExtWrite32BottomOfShader:
        case kExtKickoffX:
        case kExtSetDepthBias: return slot;
        default: return kExtNone;
    }
}

// List slot 139 in the newer layout: Reset with the allocator, the call the title makes on each list it takes from its
// pool before recording ((allocator, NULL, 0), sibling layer). Without it the real list stays closed and the layer
// refuses everything recorded into it.
HRESULT console_reset_from_title(ListProxy* self, std::uint64_t allocator_word, std::uint64_t initial_pso) {
    if (!self->real) return E_FAIL;
    ID3D12CommandAllocator* allocator =
        allocator_proxy_real_allocator(reinterpret_cast<const void*>(allocator_word));
    if (!allocator) allocator = self->real_allocator;
    if (!allocator) return E_FAIL;
    using ResetFn = HRESULT(WINAPI*)(IUnknown*, ID3D12CommandAllocator*, ID3D12PipelineState*);
    auto** const vtable = *reinterpret_cast<void***>(self->real);
    const HRESULT hr = reinterpret_cast<ResetFn>(vtable[10])(
        self->real, allocator, reinterpret_cast<ID3D12PipelineState*>(static_cast<std::uintptr_t>(initial_pso)));
    if (SUCCEEDED(hr)) self->closed_by_title.store(false, std::memory_order_relaxed);
    return hr;
}

// A value the console runtime writes into unified memory after the commands recorded so far. The address is this
// process's memory, which is the very bytes the title reads back, so the write happens there; an address that already
// is a device address goes through the real list's WriteBufferImmediate. A timestamp flag (bottom 1/2, top 4/8) means
// the runtime writes the clock, not the caller's value.
void console_write_value(ListProxy* self, std::uint64_t va, std::uint64_t value, unsigned width, bool timestamp) {
    if (!va) return;
    if (timestamp) value = static_cast<std::uint64_t>(GetTickCount64());
    if (leonida::is_committed(reinterpret_cast<const void*>(va))) {
        std::uint8_t bytes[8] = {};
        std::memcpy(bytes, &value, sizeof(bytes));
        leonida::write_committed(va, bytes, width);
        return;
    }
    if (!self->real) return;
    D3D12_WRITEBUFFERIMMEDIATE_PARAMETER params[2] = {};
    params[0].Dest = va;
    params[0].Value = static_cast<UINT>(value);
    UINT count = 1;
    if (width == 8) {
        params[1].Dest = va + 4;
        params[1].Value = static_cast<UINT>(value >> 32);
        count = 2;
    }
    using Fn = void(WINAPI*)(IUnknown*, UINT, const D3D12_WRITEBUFFERIMMEDIATE_PARAMETER*,
                             const D3D12_WRITEBUFFERIMMEDIATE_MODE*);
    auto** const vtable = *reinterpret_cast<void***>(self->real);
    reinterpret_cast<Fn>(vtable[66])(self->real, count, params, nullptr);
}

// A fill or a copy the console does in unified memory: both ends are title memory here, so it is a memory operation
// rather than a command.
void console_fill_value(std::uint64_t va, std::uint64_t bytes, std::uint64_t value, unsigned width) {
    if (!va || !bytes) return;
    std::uint8_t pattern[16] = {};
    std::memcpy(pattern, &value, sizeof(pattern));
    const std::uint64_t pattern_bytes = width;
    std::uint8_t chunk[4096];
    for (std::size_t at = 0; at < sizeof(chunk); at += sizeof(pattern)) std::memcpy(chunk + at, pattern, sizeof(pattern));
    std::uint64_t done = 0;
    while (done < bytes) {
        const std::uint64_t step = std::min<std::uint64_t>(sizeof(chunk), bytes - done);
        const std::uint64_t stop = done + step - (step % pattern_bytes);  // whole patterns only
        if (stop <= done) break;
        leonida::write_committed(va + done, chunk, stop - done);
        done = stop;
    }
}

void console_copy_memory(std::uint64_t dst, std::uint64_t src, std::uint64_t bytes) {
    if (!dst || !src || !bytes) return;
    std::uint8_t chunk[4096];
    std::uint64_t done = 0;
    while (done < bytes) {
        const std::uint64_t step = std::min<std::uint64_t>(sizeof(chunk), bytes - done);
        leonida::copy_committed(chunk, src + done, step);
        leonida::write_committed(dst + done, chunk, step);
        done += step;
    }
}

// One console-only list method, in the older layout's numbering.
HRESULT console_extension_call(ListProxy* self, unsigned extension, std::uint64_t a1, std::uint64_t a2,
                               std::uint64_t a3, std::uint64_t a4) {
    switch (extension) {
        case kExtWrite32Bottom:  // (address, value, flush, flags)
            console_write_value(self, a1, a2, 4, (a4 & 0x3u) != 0);
            return S_OK;
        case kExtWrite64Bottom:
            console_write_value(self, a1, a2, 8, (a4 & 0x3u) != 0);
            return S_OK;
        case kExtWrite32Top:  // (address, value, flags)
        case kExtWrite32BottomOfShader:
            console_write_value(self, a1, a2, 4, (a3 & 0xCu) != 0);
            return S_OK;
        case kExtWrite64Top:
            console_write_value(self, a1, a2, 8, (a3 & 0xCu) != 0);
            return S_OK;
        case kExtWait32:  // (address, value, ...)
        case kExtWait64: {
            // The value is one the title itself writes (console_write_value above) or one the GPU would write. One
            // queue runs the lists in submission order, so the ordering the wait asks for is already what a PC does;
            // a real GPU-side write into title memory has no zero-copy form in Vulkan on Windows (see GuestWindow), so
            // this answers as if the value had arrived rather than hanging the frame. The held value is printed with
            // the wanted one: a wait whose value never becomes right is a frame that will spin on the title's side.
            static std::atomic<int> logged{0};
            if (logged.fetch_add(1, std::memory_order_relaxed) < 12) {
                const unsigned width = extension == kExtWait32 ? 4u : 8u;
                std::uint8_t bytes[8] = {};
                std::uint64_t holds = 0;
                leonida::copy_committed(bytes, a1, width);
                std::memcpy(&holds, bytes, sizeof(holds));
                std::fprintf(stderr,
                             "[rt] d3d12_xs: list wait on 0x%llx for 0x%llx, holds 0x%llx (answered, no GPU wait)\n",
                             static_cast<unsigned long long>(a1), static_cast<unsigned long long>(a2),
                             static_cast<unsigned long long>(holds));
            }
            // LEONIDA_SATISFY_WAITS=1: put the awaited value there instead of only answering, so a title that reads
            // the cell itself (rather than trusting the wait) sees the step the GPU would have taken. Diagnostic.
            if (const char* satisfy = std::getenv("LEONIDA_SATISFY_WAITS"); satisfy && satisfy[0] == '1') {
                const unsigned width = extension == kExtWait32 ? 4u : 8u;
                std::uint8_t bytes[8] = {};
                std::memcpy(bytes, &a2, sizeof(bytes));
                leonida::write_committed(a1, bytes, width);
            }
            return S_OK;
        }
        case kExtFill32:  // (address, size, value)
            console_fill_value(a1, a2, a3, 4);
            return S_OK;
        case kExtFill64:
            console_fill_value(a1, a2, a3, 8);
            return S_OK;
        case kExtFill128:
            console_fill_value(a1, a2, a3, 16);
            return S_OK;
        case kExtCopyMemoryToMemory:  // (destination, source, size, flags)
            console_copy_memory(a1, a2, a3);
            return S_OK;
        case kExtDepthBounds: {  // the two bounds, as float bits
            if (self->pc_slot_count <= 62 || !self->real) return S_OK;
            FLOAT bounds[2];
            const std::uint32_t words[2] = {static_cast<std::uint32_t>(a1), static_cast<std::uint32_t>(a2)};
            std::memcpy(bounds, words, sizeof(bounds));
            using Fn = void(WINAPI*)(IUnknown*, FLOAT, FLOAT);
            auto** const vtable = *reinterpret_cast<void***>(self->real);
            reinterpret_cast<Fn>(vtable[62])(self->real, bounds[0], bounds[1]);
            return S_OK;
        }
        case kExtDispatchX: {  // (x, y, z, flags): Dispatch with an extra word
            if (self->pc_slot_count <= 14 || !self->real) return S_OK;
            using DispatchFn = void(WINAPI*)(IUnknown*, UINT, UINT, UINT);
            auto** const vtable = *reinterpret_cast<void***>(self->real);
            reinterpret_cast<DispatchFn>(vtable[14])(self->real, static_cast<UINT>(a1), static_cast<UINT>(a2),
                                                     static_cast<UINT>(a3));
            return S_OK;
        }
        case kExtDrawIndexedX: {  // (index address, format, count, flags): the index buffer for one draw
            if (self->pc_slot_count <= 43 || !self->real) return S_OK;
            const auto format = static_cast<DXGI_FORMAT>(static_cast<std::uint32_t>(a2));
            const std::uint64_t count = a3;
            const std::uint64_t stride = format == DXGI_FORMAT_R32_UINT ? 4 : 2;
            D3D12_INDEX_BUFFER_VIEW view{};
            view.BufferLocation = shader_address(self->device, a1, count * stride);
            view.SizeInBytes = static_cast<UINT>(count * stride);
            view.Format = format;
            auto** const vtable = *reinterpret_cast<void***>(self->real);
            using SetIndexFn = void(WINAPI*)(IUnknown*, const D3D12_INDEX_BUFFER_VIEW*);
            reinterpret_cast<SetIndexFn>(vtable[43])(self->real, &view);
            using DrawIndexedFn = void(WINAPI*)(IUnknown*, UINT, UINT, UINT, INT, UINT);
            reinterpret_cast<DrawIndexedFn>(vtable[13])(self->real, static_cast<UINT>(count), 1, 0, 0, 0);
            return S_OK;
        }
        case kExtCopyResourceX: {  // (destination, source): plain resources on both sides
            if (self->pc_slot_count <= 17 || !self->real) return S_OK;
            using CopyFn = void(WINAPI*)(IUnknown*, IUnknown*, IUnknown*);
            auto** const vtable = *reinterpret_cast<void***>(self->real);
            reinterpret_cast<CopyFn>(vtable[17])(self->real, reinterpret_cast<IUnknown*>(static_cast<std::uintptr_t>(a1)),
                                                 reinterpret_cast<IUnknown*>(static_cast<std::uintptr_t>(a2)));
            return S_OK;
        }
        default:  // KickoffX, RSSetDepthBiasX and anything unmeasured: recorded, no PC call
            return S_OK;
    }
}

HRESULT list_query_interface(ListProxy* self, const IID* riid, void** out) {
    note_list_slot(0, reinterpret_cast<std::uint64_t>(riid), reinterpret_cast<std::uint64_t>(out), 0, 0);
    if (!out) return E_POINTER;
    *out = nullptr;
    if (!riid) return E_POINTER;
    // A console title asks for the console command-list IID (device slot 73 forwards it): it maps to a PC IID the
    // real list answers, and the caller gets this console-shaped object back.
    const IID& pc_iid = command_list_iid_for_pc(*riid);
    if (!is_list_iid(pc_iid)) {
        if (supports_iid(self->real, pc_iid)) log_refused_iid("list", *riid);
        return E_NOINTERFACE;
    }
    if (!supports_iid(self->real, pc_iid)) return E_NOINTERFACE;
    *out = self;
    list_add_ref(self);
    return S_OK;
}

// Highest list interface the real object implements; its cumulative slot count bounds mapped forwards
// (ID3D12GraphicsCommandList8 ends at slot 81, OMSetFrontAndBackStencilRef).
unsigned detect_list_slot_count(IUnknown* real) {
    if (supports_iid(real, __uuidof(ID3D12GraphicsCommandList8))) return 82;
    if (supports_iid(real, __uuidof(ID3D12GraphicsCommandList7))) return 81;
    if (supports_iid(real, __uuidof(ID3D12GraphicsCommandList6))) return 78;
    if (supports_iid(real, __uuidof(ID3D12GraphicsCommandList5))) return 75;
    if (supports_iid(real, __uuidof(ID3D12GraphicsCommandList4))) return 69;
    if (supports_iid(real, __uuidof(ID3D12GraphicsCommandList3))) return 67;
    if (supports_iid(real, __uuidof(ID3D12GraphicsCommandList2))) return 66;
    if (supports_iid(real, __uuidof(ID3D12GraphicsCommandList1))) return 65;
    return 60;
}

template <unsigned Slot>
std::uint64_t WINAPI list_slot(ListProxy* self, std::uint64_t a1, std::uint64_t a2, std::uint64_t a3,
                               std::uint64_t a4, std::uint64_t a5, std::uint64_t a6, std::uint64_t a7,
                               std::uint64_t a8, std::uint64_t a9, std::uint64_t a10, std::uint64_t a11,
                               std::uint64_t a12, std::uint64_t a13, std::uint64_t a14) {
    if constexpr (Slot == 0) {
        return hr_to_u64(list_query_interface(self, reinterpret_cast<const IID*>(a1),
                                              reinterpret_cast<void**>(a2)));
    } else if constexpr (Slot == 1) {
        return list_add_ref(self);
    } else if constexpr (Slot == 2) {
        return list_release(self);
    } else if constexpr (Slot == 7) {  // GetDevice: the Xbox-ABI device, as on the other proxies
        note_list_slot(7, a1, a2, 0, 0);
        if (a2) *reinterpret_cast<void**>(a2) = nullptr;
        if (!a1) return hr_to_u64(E_POINTER);
        if (!a2) {  // no output: the real object's own answer
            using Fn = HRESULT(WINAPI*)(IUnknown*, const IID*, void**);
            return hr_to_u64(reinterpret_cast<Fn>((*reinterpret_cast<void***>(self->real))[7])(
                self->real, reinterpret_cast<const IID*>(a1), nullptr));
        }
        return hr_to_u64(proxy_query_interface(self->device, reinterpret_cast<const IID*>(a1),
                                               reinterpret_cast<void**>(a2)));
    } else {
        note_list_slot(Slot, a1, a2, a3, a4, self->device->console_layout.load(std::memory_order_relaxed));
        if constexpr (Slot == 9 || Slot == 12) {
            if (std::getenv("LEONIDA_LOG_PROXY")) {
                static std::atomic<int> shown{0};
                if (shown.fetch_add(1, std::memory_order_relaxed) < 24)
                    std::fprintf(stderr, "[rt] xs list slot %u: proxy %p real %p\n", Slot,
                                 static_cast<void*>(self), static_cast<void*>(self->real));
            }
        }
        constexpr unsigned pc = console_to_pc_list_slot(Slot);
        if constexpr (pc == kListNotForwarded) {
            // A slot past both layouts' modelled tails is the layout evidence a list can see for itself: the newer
            // layout's Reset sits at 139, and the older layout has no console method out there at all. Learning the
            // layout here is what lets this title's tail slots (70-75) be read as the newer layout's methods.
            if constexpr (Slot >= 128) {
                unsigned char expected = 0;
                if (self->device->console_layout.compare_exchange_strong(expected, kConsoleLayoutNewer,
                                                                         std::memory_order_relaxed))
                    std::fprintf(stderr,
                                 "[rt] d3d12_xs: the title's console list has the newer layout (list slot %u)\n",
                                 Slot);
            }
            if constexpr (Slot == kConsoleResetNewer) return hr_to_u64(console_reset_from_title(self, a1, a2));
            const unsigned extension =
                console_list_extension(Slot, self->device->console_layout.load(std::memory_order_relaxed));
            if (extension != kExtNone) {
                console_begin_recording(self);  // these are recording calls: reopen the list the title closed
                console_note_recording(self);
                return hr_to_u64(console_extension_call(self, extension, a1, a2, a3, a4));
            }
            if (std::getenv("LEONIDA_LOG_UNMAPPED")) {
                static std::atomic<int> shown{0};
                if (shown.fetch_add(1, std::memory_order_relaxed) < 240)
                    std::fprintf(stderr,
                                 "[rt] xs unmapped list slot %u: list %p, args %llx %llx %llx %llx %llx %llx\n", Slot,
                                 static_cast<void*>(self), static_cast<unsigned long long>(a1),
                                 static_cast<unsigned long long>(a2), static_cast<unsigned long long>(a3),
                                 static_cast<unsigned long long>(a4), static_cast<unsigned long long>(a5),
                                 static_cast<unsigned long long>(a6));
            }
            if constexpr (Slot == kConsoleAllocateUpload) {
                return upload_alloc(self->device, a1, a2);  // (size, alignment); the comment at UploadChunk
            }
            return 0;  // unmodelled console slot: recorded above, never a PC call
        } else {
            if (pc >= self->pc_slot_count) {
                static std::atomic<bool> beyond_warned[kListSlots];
                bool expected = false;
                if (beyond_warned[Slot].compare_exchange_strong(expected, true, std::memory_order_relaxed))
                    std::fprintf(stderr,
                                 "[rt] d3d12_xs: list slot %u maps to PC slot %u, beyond the real list's %u "
                                 "slots; returning E_NOTIMPL\n",
                                 Slot, pc, self->pc_slot_count);
                return hr_to_u64(E_NOTIMPL);
            }
            using ForwardFn = std::uint64_t(WINAPI*)(void*, std::uint64_t, std::uint64_t, std::uint64_t,
                                                     std::uint64_t, std::uint64_t, std::uint64_t,
                                                     std::uint64_t, std::uint64_t, std::uint64_t,
                                                     std::uint64_t, std::uint64_t, std::uint64_t,
                                                     std::uint64_t, std::uint64_t);
            auto** const real_vtable = *reinterpret_cast<void***>(self->real);
            // The console list lifecycle (console_begin_recording): a recording call reopens the list the title closed
            // after the previous frame, and joins the batch the next console submit executes.
            if constexpr (Slot != 9) console_begin_recording(self);
            console_note_recording(self);
            if constexpr (Slot == 9) {
                // Close: the real list closes, and the next recording call reopens it.
                const std::uint64_t closed = reinterpret_cast<ForwardFn>(real_vtable[pc])(
                    self->real, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11, a12, a13, a14);
                if (SUCCEEDED(static_cast<HRESULT>(static_cast<std::uint32_t>(closed))))
                    self->closed_by_title.store(true, std::memory_order_relaxed);
                return closed;
            }
            if constexpr (Slot == 10) {
                // Reset(allocator, pso): the title hands its allocator proxy; the real list takes the real one.
                a1 = unwrap_object(a1);
            } else if constexpr (Slot == kConsoleExecuteBundle) {
                a1 = unwrap_object(a1);  // the bundle is a list proxy too
            } else if constexpr (Slot >= 41 && Slot <= 46) {
                // The six root descriptor setters (index, GPU VA): an upload-arena CPU pointer becomes the
                // chunk's device address (d3d12_xs_console_abi.md §5.3 measures slots 41/43/45 for the compute
                // side; the graphics setters interleave on even slots).
                const std::uint64_t source = a2;
                a2 = shader_address(self->device, a2, 64 << 10);  // a root CBV/SRV/UAV: at most a 64 KiB buffer
                if (std::getenv("LEONIDA_LOG_ROOT")) {
                    static std::atomic<int> logged{0};
                    const int nth = logged.fetch_add(1, std::memory_order_relaxed) + 1;
                    if (nth <= 24)
                        std::fprintf(stderr, "[rt] root descriptor %u: 0x%llx -> 0x%llx\n", unsigned(Slot),
                                     static_cast<unsigned long long>(source),
                                     static_cast<unsigned long long>(a2));
                    if (nth <= 2) {
                        std::uint32_t words[32] = {};
                        leonida::copy_committed(reinterpret_cast<std::uint8_t*>(words), source, sizeof(words));
                        std::fprintf(stderr,
                                     "[rt] root descriptor %u: 0x%llx -> 0x%llx; 32 words %08x %08x %08x %08x %08x "
                                     "%08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x "
                                     "%08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x\n",
                                     unsigned(Slot), static_cast<unsigned long long>(source),
                                     static_cast<unsigned long long>(a2), words[0], words[1], words[2], words[3],
                                     words[4], words[5], words[6], words[7], words[8], words[9], words[10],
                                     words[11], words[12], words[13], words[14], words[15], words[16], words[17],
                                     words[18], words[19], words[20], words[21], words[22], words[23], words[24],
                                     words[25], words[26], words[27], words[28], words[29], words[30], words[31]);
                    }
                }
            } else if constexpr (Slot == 47) {
                // IASetIndexBuffer(pView): the view's BufferLocation is a GPU VA; translate in a copy so the
                // title's struct is not written.
                if (a1) {
                    auto view = *reinterpret_cast<const D3D12_INDEX_BUFFER_VIEW*>(a1);
                    view.BufferLocation = shader_address(self->device, view.BufferLocation, view.SizeInBytes);
                    return reinterpret_cast<ForwardFn>(real_vtable[pc])(
                        self->real, reinterpret_cast<std::uint64_t>(&view), a2, a3, a4, a5, a6, a7, a8, a9,
                        a10, a11, a12, a13, a14);
                }
            } else if constexpr (Slot == 48) {
                // IASetVertexBuffers(start, count, views): each view's BufferLocation is a GPU VA. The views are
                // translated in the proxy's scratch array for the same reason as the index view.
                if (a3 && a2) {
                    const auto* in = reinterpret_cast<const D3D12_VERTEX_BUFFER_VIEW*>(a3);
                    self->vb_scratch.assign(in, in + a2);
                    for (auto& view : self->vb_scratch)
                        view.BufferLocation = shader_address(self->device, view.BufferLocation, view.SizeInBytes);
                    if (std::getenv("LEONIDA_LOG_VB")) {
                        static std::atomic<int> logged{0};
                        if (logged.fetch_add(1, std::memory_order_relaxed) < 40) {
                            // The bytes the shader will read: the title's own memory at the view's address, so a view
                            // that resolves to a buffer full of zeros (or to the wrong memory) is visible at once.
                            std::uint32_t words[16] = {};
                            leonida::copy_committed(reinterpret_cast<std::uint8_t*>(words), in[0].BufferLocation,
                                                    sizeof(words));
                            std::fprintf(stderr,
                                         "[rt] vertex buffers: count %llu, view 0 at 0x%llx -> 0x%llx, %u bytes, "
                                         "stride %u; first words %08x %08x %08x %08x %08x %08x %08x %08x %08x %08x "
                                         "%08x %08x %08x %08x %08x %08x\n",
                                         static_cast<unsigned long long>(a2),
                                         static_cast<unsigned long long>(in[0].BufferLocation),
                                         static_cast<unsigned long long>(self->vb_scratch[0].BufferLocation),
                                         in[0].SizeInBytes, in[0].StrideInBytes, words[0], words[1], words[2],
                                         words[3], words[4], words[5], words[6], words[7], words[8], words[9],
                                         words[10], words[11], words[12], words[13], words[14], words[15]);
                        }
                    }
                    return reinterpret_cast<ForwardFn>(real_vtable[pc])(
                        self->real, a1, a2, reinterpret_cast<std::uint64_t>(self->vb_scratch.data()), a4, a5,
                        a6, a7, a8, a9, a10, a11, a12, a13, a14);
                }
            } else if constexpr (Slot == 49) {
                // SOSetTargets(start, count, views): BufferLocation and BufferFilledSizeLocation are both VAs.
                if (a3 && a2) {
                    const auto* in = reinterpret_cast<const D3D12_STREAM_OUTPUT_BUFFER_VIEW*>(a3);
                    self->so_scratch.assign(in, in + a2);
                    for (auto& view : self->so_scratch) {
                        view.BufferLocation = shader_address(self->device, view.BufferLocation, view.SizeInBytes);
                        view.BufferFilledSizeLocation =
                            shader_address(self->device, view.BufferFilledSizeLocation, 8);
                    }
                    return reinterpret_cast<ForwardFn>(real_vtable[pc])(
                        self->real, a1, a2, reinterpret_cast<std::uint64_t>(self->so_scratch.data()), a4, a5,
                        a6, a7, a8, a9, a10, a11, a12, a13, a14);
                }
            } else if constexpr (Slot == 50) {
                // OMSetRenderTargets(count, handles, single_range, depth): the handles name descriptors in the title's
                // own heap, while the layer wrote its TargetViews into the translated heap (the mapping the device
                // applies when it creates a view). Handed the raw handle, the layer read the console heap's bytes as a
                // TargetView and the lists bound resource ids 15/2/1 where the presented texture is 5725022816.
                if (a1 > 0 && a1 <= 8 && a2) {
                    const auto* handles = reinterpret_cast<const D3D12_CPU_DESCRIPTOR_HANDLE*>(a2);
                    D3D12_CPU_DESCRIPTOR_HANDLE translated[8];
                    for (std::uint64_t i = 0; i < a1; ++i) translated[i] = translate_cpu_handle(self->device, handles[i]);
                    D3D12_CPU_DESCRIPTOR_HANDLE depth{};
                    std::uint64_t depth_word = a4;
                    if (a4) {
                        depth = translate_cpu_handle(self->device,
                                                     *reinterpret_cast<const D3D12_CPU_DESCRIPTOR_HANDLE*>(a4));
                        depth_word = reinterpret_cast<std::uint64_t>(&depth);
                    }
                    return reinterpret_cast<ForwardFn>(real_vtable[pc])(
                        self->real, a1, reinterpret_cast<std::uint64_t>(translated), a3, depth_word, a5, a6, a7, a8,
                        a9, a10, a11, a12, a13, a14);
                }
            } else if constexpr (Slot == 51 || Slot == 52) {
                // ClearDepthStencilView(view, ...) at 51 and ClearRenderTargetView(view, ...) at 52: the view is a
                // descriptor handle by value, and it names a slot of the title's own heap for the same reason the
                // slot-50 handles do. Untranslated, the layer reads the console heap's bytes as a TargetView: the
                // clear then named resources 1 and 2, `clear_render_target` refused them, and every list that cleared
                // a frame's colour target closed as invalid -- 17,929 of 18,000 in a 45 s run, with the draws those
                // lists recorded never submitted. Translate it the way the device translated the view when it created it.
                if (a1) a1 = translate_cpu_handle(self->device, D3D12_CPU_DESCRIPTOR_HANDLE{a1}).ptr;
            } else if constexpr (Slot == 36) {
                // SetGraphicsRootDescriptorTable(index, table GPU VA): the title builds this table in its own pages
                // (the shaders add heap_base to every VA they read), so the VA needs the treatment the root descriptor
                // setters below get, not the raw value.
                // LEONIDA_XS_TABLE_VA: translate the table VA to a window over the title's pages, as the root
                // descriptor setters below do. Off by default: with it on the title's menu draws do read their real
                // console T#s (LEONIDA_LOG_ROOT shows them), but a shader then reads out of bounds and the device is
                // lost before the second frame (2026-11-24); the faulting read is the next thing to find.
                const std::uint64_t table_va = a2;
                // LEONIDA_XS_HEAP_VA: on the console CPU and GPU share one address space, so a descriptor-table GPU VA
                // can be the *CPU* address of the heap backing the title allocated (its own pages), not the GPU handle
                // the layer reported. translate_cpu_handle maps a backing address to the layer's heap in the arena, so
                // the shader reads the descriptors Create*View wrote instead of a copy of unrelated title memory.
                const D3D12_CPU_DESCRIPTOR_HANDLE in_layer =
                    translate_cpu_handle(self->device, D3D12_CPU_DESCRIPTOR_HANDLE{a2});
                const bool is_backing = in_layer.ptr != a2 && leonida::arena_host_base &&
                                        in_layer.ptr >= reinterpret_cast<SIZE_T>(leonida::arena_host_base) &&
                                        in_layer.ptr - reinterpret_cast<SIZE_T>(leonida::arena_host_base) <
                                            leonida::arena_host_size;
                const std::uint64_t heap_va =
                    is_backing ? in_layer.ptr - reinterpret_cast<SIZE_T>(leonida::arena_host_base) : a2;
                if (std::getenv("LEONIDA_XS_HEAP_VA")) a2 = heap_va;
                else if (a2 == heap_va) {
                    // LEONIDA_XS_TABLE_FIX: give the shader a window copy of the title's table, with each descriptor's
                    // address rewritten to the window (or arena VA) the shader's heap_base + address must reach, and
                    // zero the ones that name nothing (sanitize_descriptor_table). Without the rewrite the title's
                    // base is a host address and heap_base + it reads unrelated arena bytes (2026-11-24: the black
                    // composite frame).
                    if (std::getenv("LEONIDA_XS_TABLE_FIX")) {
                        const std::uint64_t translated = guest_translate(self->device, a2, 64 << 10, true);
                        // LEONIDA_XS_TABLE_REWRITE: additionally turn each descriptor's address into the window that
                        // holds those bytes. Off by default: the entries a title writes are not gfx10 V#s (the dword
                        // after the address repeats the address, so a rewritten record count sends the shader on an
                        // enormous loop and the GPU hangs).
                        if (translated && std::getenv("LEONIDA_XS_TABLE_REWRITE")) {
                            if (std::uint8_t* window = guest_window_bytes(self->device, a2, 64 << 10))
                                rewrite_table_descriptors(self->device, a2, window, 64 << 10);
                        }
                        if (translated) a2 = translated;
                    } else if (std::getenv("LEONIDA_XS_TABLE_VA")) {
                        a2 = shader_address(self->device, a2, 64 << 10);
                    }
                }
                if (std::getenv("LEONIDA_LOG_ROOT")) {
                    static std::atomic<int> logged{0};
                    const int nth = logged.fetch_add(1, std::memory_order_relaxed) + 1;
                    if (nth <= 40) {
                        std::uint32_t words[8] = {};
                        leonida::copy_committed(reinterpret_cast<std::uint8_t*>(words), table_va, sizeof(words));
                        std::fprintf(stderr,
                                     "[rt] root table %d: index %llu at 0x%llx -> 0x%llx; first descriptor %08x %08x "
                                     "%08x %08x %08x %08x %08x %08x\n",
                                     nth, static_cast<unsigned long long>(a1),
                                     static_cast<unsigned long long>(table_va), static_cast<unsigned long long>(a2),
                                     words[0], words[1], words[2], words[3], words[4], words[5], words[6],
                                     words[7]);
                    }
                }
                // LEONIDA_DUMP_TABLE: the table's bytes as the shader reads them, from the GPU address arena's host
                // mapping (a translated table's window is not in the arena, so this then prints nothing).
                if (std::getenv("LEONIDA_DUMP_TABLE") && leonida::arena_host_base && a2 < leonida::arena_host_size) {
                    static std::atomic<int> shown{0};
                    const int nth = shown.fetch_add(1, std::memory_order_relaxed) + 1;
                    const std::uint64_t room = leonida::arena_host_size - a2;
                    if (nth <= 40) {
                        const auto* words = reinterpret_cast<const std::uint32_t*>(leonida::arena_host_base + a2);
                        std::fprintf(stderr, "[rt] table %d: index %llu at 0x%llx:", nth,
                                     static_cast<unsigned long long>(a1), static_cast<unsigned long long>(a2));
                        for (unsigned i = 0; i < 24 && (i + 1) * 4 <= room; ++i)
                            std::fprintf(stderr, " %08x", words[i]);
                        std::fprintf(stderr, "\n");
                    }
                }
                // LEONIDA_LOG_TABLE: every 16-byte window of the table decoded as the translated shader decodes it --
                // a GFX10 V#, `base = (word1 & 0xffff) << 32 | (word0 & ~3)`, `stride = word1[29:16]`,
                // `num_records = word2` -- and flagged when `heap_base + base` leaves the GPU address arena, which is
                // the read that loses the device. Prints the table's first 0x100 bytes, the list type and the index.
                if (std::getenv("LEONIDA_LOG_TABLE")) {
                    static std::atomic<int> logged{0};
                    const int nth = logged.fetch_add(1, std::memory_order_relaxed) + 1;
                    if (nth <= 200) {
                        std::uint32_t bytes[64] = {};
                        leonida::copy_committed(reinterpret_cast<std::uint8_t*>(bytes), table_va, sizeof(bytes));
                        std::fprintf(stderr, "[rt] table %d: type %d index %llu at 0x%llx -> 0x%llx (backing %d, 0x%llx)\n",
                                     nth, int(self->type), static_cast<unsigned long long>(a1),
                                     static_cast<unsigned long long>(table_va),
                                     static_cast<unsigned long long>(a2), is_backing ? 1 : 0,
                                     static_cast<unsigned long long>(heap_va));
                        // The same offset in the GPU address arena, which is what the shader reads when the VA is not
                        // translated: equal bytes mean the table VA is a console VA into the arena; zeros there mean
                        // the table is somewhere else entirely.
                        if (leonida::arena_host_base && table_va + 32 <= leonida::arena_host_size) {
                            const auto* arena_words =
                                reinterpret_cast<const std::uint32_t*>(leonida::arena_host_base + table_va);
                            std::fprintf(stderr, "[rt]   arena bytes: %08x %08x %08x %08x %08x %08x %08x %08x\n",
                                         arena_words[0], arena_words[1], arena_words[2], arena_words[3],
                                         arena_words[4], arena_words[5], arena_words[6], arena_words[7]);
                        }
                        // The heap's own XMem backing at the same offset: a console title's CPU and GPU share memory, so
                        // a title that builds descriptors itself writes them there, and the arena slot stays zero.
                        if (is_backing) {
                            DescriptorRange range{};
                            if (find_descriptor_range(self->device, SIZE_T(table_va), &range) && range.backing_start) {
                                const std::uint64_t backing =
                                    range.backing_start + (table_va - range.custom_start);
                                std::uint32_t backing_words[16] = {};
                                leonida::copy_committed(reinterpret_cast<std::uint8_t*>(backing_words), backing,
                                                        sizeof(backing_words));
                                std::fprintf(stderr,
                                             "[rt]   backing 0x%llx: %08x %08x %08x %08x %08x %08x %08x %08x\n",
                                             static_cast<unsigned long long>(backing), backing_words[0],
                                             backing_words[1], backing_words[2], backing_words[3], backing_words[4],
                                             backing_words[5], backing_words[6], backing_words[7]);
                            }
                        }
                        for (unsigned at = 0; at + 8 <= 64; at += 4) {
                            const std::uint32_t* w = bytes + at;
                            const std::uint64_t base =
                                (std::uint64_t(w[1] & 0xffff) << 32) | (w[0] & 0xfffffffcull);
                            const unsigned stride = (w[1] >> 16) & 0x3fff;
                            const std::uint64_t records = stride ? std::uint64_t(w[2]) * stride : w[2];
                            const unsigned type = (w[3] >> 28) & 0xf;
                            const bool outside = base >= leonida::arena_host_size ||
                                                 records > leonida::arena_host_size - base;
                            std::fprintf(stderr,
                                         "[rt]   +0x%02x: %08x %08x %08x %08x  %s base 0x%llx stride %u records %llu end "
                                         "0x%llx%s\n",
                                         at, w[0], w[1], w[2], w[3],
                                         type >= 8 ? "T#" : "V#", static_cast<unsigned long long>(base), stride,
                                         static_cast<unsigned long long>(records),
                                         static_cast<unsigned long long>(base + records),
                                         outside ? " OUTSIDE THE ARENA" : "");
                        }
                        // What the shader reads at those bases: it adds heap_base to the V# base, so the bytes below are
                        // the arena's. Vertex data looks like floats; a descriptor table looks like descriptors.
                        if (leonida::arena_host_base) {
                            for (unsigned i = 0; i < 3; ++i) {
                                const std::uint32_t* r = bytes + i * 4;
                                const std::uint64_t base =
                                    (std::uint64_t(r[1] & 0xffff) << 32) | (r[0] & 0xfffffffcull);
                                if (base + 16 > leonida::arena_host_size) continue;
                                const auto* data =
                                    reinterpret_cast<const std::uint32_t*>(leonida::arena_host_base + base);
                                std::fprintf(stderr, "[rt]   base 0x%llx data: %08x %08x %08x %08x\n",
                                             static_cast<unsigned long long>(base), data[0], data[1], data[2],
                                             data[3]);
                            }
                        }
                    }
                }
            } else if constexpr (Slot == kConsoleSetDescriptorHeaps) {
                UnwrappedArray heaps;
                if (!heaps.fill(a2, a1)) return 0;  // void on the PC side; the call simply does not happen
                const std::uint64_t real_heaps = heaps.data() ? heaps.data() : a2;
                return reinterpret_cast<ForwardFn>(real_vtable[pc])(self->real, a1, real_heaps, a3, a4, a5, a6,
                                                                  a7, a8, a9, a10, a11, a12, a13, a14);
            }
            return reinterpret_cast<ForwardFn>(real_vtable[pc])(self->real, a1, a2, a3, a4, a5, a6, a7, a8, a9,
                                                              a10, a11, a12, a13, a14);
        }
    }
}

template <std::size_t... I>
void fill_list_vtable(void** vtable, std::index_sequence<I...>) {
    ((vtable[I] = reinterpret_cast<void*>(&list_slot<static_cast<unsigned>(I)>) ), ...);
}

void** get_list_vtable() {
    static void** vtable = [] {
        static void* storage[kListSlots];
        fill_list_vtable(storage, std::make_index_sequence<kListSlots>{});
        return storage;
    }();
    return vtable;
}

bool is_list_proxy_impl(const void* list) {
    return list && *reinterpret_cast<void* const*>(list) == reinterpret_cast<void*>(get_list_vtable());
}

IUnknown* list_real_or_null(const void* object) {
    return is_list_proxy_impl(object) ? static_cast<IUnknown*>(static_cast<const ListProxy*>(object)->real)
                                    : nullptr;
}

// Wraps `created` (one reference, owned by the proxy on success, released on failure) and returns `riid` from the
// proxy through `out`. `type` is the type the title created the list with: the console submit runs every recorded list
// through one submit call, and a PC queue refuses a list of another type (console_submit_batch).
HRESULT wrap_list(IUnknown* created, DeviceProxy* device, IUnknown* allocator, D3D12_COMMAND_LIST_TYPE type,
                  const IID* riid, void** out) {
    if (!created) return E_FAIL;
    ListProxy* proxy = new (std::nothrow) ListProxy();  // (): the console body starts zeroed
    if (!proxy) {
        created->Release();
        return E_OUTOFMEMORY;
    }
    proxy->vtable = get_list_vtable();
    proxy->real = static_cast<ID3D12GraphicsCommandList*>(created);
    proxy->device = device;
    proxy->refs.store(1, std::memory_order_relaxed);
    proxy->pc_slot_count = detect_list_slot_count(created);
    proxy->real_allocator = static_cast<ID3D12CommandAllocator*>(allocator);
    if (proxy->real_allocator) proxy->real_allocator->AddRef();
    proxy->type = type;
    proxy->pin_cursor_at_limit();
    proxy_add_ref(device);  // the list keeps its Xbox-ABI device alive for GetDevice

    const HRESULT hr = list_query_interface(proxy, riid, out);
    if (FAILED(hr)) {
        destroy_list(proxy);  // no reference escaped
        return hr;
    }
    static std::atomic<bool> logged{false};
    if (!d3d12_trace::enabled() && (!logged.exchange(true, std::memory_order_relaxed) || trace_enabled()))
        std::fprintf(stderr, "[rt] d3d12_xs: list proxy %p wraps %p (%u PC vtable slots)\n",
                     static_cast<void*>(proxy), static_cast<void*>(created), proxy->pc_slot_count);
    proxy->refs.fetch_sub(1, std::memory_order_relaxed);  // drop the creation reference; `out` holds the caller's
    return S_OK;
}

// Device slot 12: CreateCommandList(node_mask, type, allocator, initial_pso, riid, out). The title hands its
// allocator proxy, which the real device unwraps to the real allocator; the created list goes back console-shaped.
HRESULT proxy_create_command_list(DeviceProxy* self, std::uint64_t node_mask, std::uint64_t type,
                                  std::uint64_t allocator, std::uint64_t pso, const IID* riid, void** out) {
    note_slot(12);
    if (out) *out = nullptr;  // as slot 8
    if (!riid) return E_POINTER;
    if (!out) return self->real->CreateCommandList(static_cast<UINT>(node_mask),
                                                 static_cast<D3D12_COMMAND_LIST_TYPE>(type),
                                                 reinterpret_cast<ID3D12CommandAllocator*>(unwrap_object(allocator)),
                                                 reinterpret_cast<ID3D12PipelineState*>(pso), *riid, nullptr);
    if (!self->console_lists) {
        // PC caller (create_device_proxy's console_lists=false): the real list answers every PC slot, and the
        // console-only pieces (slot 148, the shifted tail) are simply not there for this caller to reach.
        return self->real->CreateCommandList(static_cast<UINT>(node_mask),
                                             static_cast<D3D12_COMMAND_LIST_TYPE>(type),
                                             reinterpret_cast<ID3D12CommandAllocator*>(unwrap_object(allocator)),
                                             reinterpret_cast<ID3D12PipelineState*>(pso), *riid, out);
    }
    void* created = nullptr;
    const HRESULT hr = self->real->CreateCommandList(
        static_cast<UINT>(node_mask), static_cast<D3D12_COMMAND_LIST_TYPE>(type),
        reinterpret_cast<ID3D12CommandAllocator*>(unwrap_object(allocator)),
        reinterpret_cast<ID3D12PipelineState*>(pso), __uuidof(ID3D12GraphicsCommandList), &created);
    if (FAILED(hr)) return hr;
    if (!created) return E_FAIL;
    return wrap_list(static_cast<IUnknown*>(created), self,
                     reinterpret_cast<IUnknown*>(unwrap_object(allocator)),
                     static_cast<D3D12_COMMAND_LIST_TYPE>(type), riid, out);
}

// Calls the real device's slot `Slot` with these five words in the positions the thunk received them (see "A thunk
// declares 14 argument words" at the top: no word may move to another stack position on the way).
template <unsigned Slot>
std::uint64_t forward_device(DeviceProxy* self, std::uint64_t a1, std::uint64_t a2, std::uint64_t a3,
                             std::uint64_t a4, std::uint64_t a5) {
    using Fn = std::uint64_t(WINAPI*)(void*, std::uint64_t, std::uint64_t, std::uint64_t, std::uint64_t,
                                      std::uint64_t);
    return reinterpret_cast<Fn>(self->real_vtable[Slot])(self->real, a1, a2, a3, a4, a5);
}

// A device slot past the PC device's vtable (the real device predates the interface): E_NOTIMPL, logged once per
// slot. The generic thunk and the slots the proxy handles itself (31/34/35/45/46/50, 75) log the same line; `caller` is
// the title's return address, printed raw because the mock test links this file without the shim registry that turns an
// address into module+RVA (gta6rt.exe's trace holds the image base).
HRESULT refuse_beyond_device(unsigned slot, unsigned slot_count, void* caller = nullptr) {
    bool expected = false;
    if (slot < kMaxSlots && g_beyond_warned[slot].compare_exchange_strong(expected, true, std::memory_order_relaxed))
        std::fprintf(stderr,
                     "[rt] d3d12_xs: device slot %u (%s) is beyond the PC device vtable (%u slots), called from 0x%llx; "
                     "returning E_NOTIMPL\n",
                     slot, slot_name(slot), slot_count, static_cast<unsigned long long>(
                                                          reinterpret_cast<std::uintptr_t>(caller)));
    return E_NOTIMPL;
}

// Device slot 8: CreateCommandQueue(desc, riid, out). Without an output it is the device's own capability answer
// (S_FALSE for a valid description on the PC), as #558 does for slot 14.
HRESULT proxy_create_command_queue(DeviceProxy* self, const D3D12_COMMAND_QUEUE_DESC* desc, const IID* riid,
                                   void** out) {
    note_slot(8);
    if (out) *out = nullptr;  // _COM_Outptr_: NULL on every failure, as slots 14/62/76
    if (!riid) return E_POINTER;
    // The caller's null output goes to the real device on purpose: its answer (S_FALSE, E_UNEXPECTED, ...) is the
    // one to give, although the SDK annotates the parameter as required.
#pragma warning(suppress : 6387)
    if (!out) return self->real->CreateCommandQueue(desc, *riid, nullptr);
    void* created = nullptr;
    const HRESULT hr = self->real->CreateCommandQueue(desc, __uuidof(ID3D12CommandQueue), &created);
    if (FAILED(hr)) return hr;
    if (!created) return E_FAIL;
    record_real_queue(self, static_cast<ID3D12CommandQueue*>(created), desc->Type);  // console_submit_batch routes by it
    return wrap_child<ChildKind::queue>(static_cast<IUnknown*>(created), self, riid, out);
}

// Xbox device slot 48 (public HLE): CreateCommandQueueX(desc, riid, out). `desc` starts with a PC
// D3D12_COMMAND_QUEUE_DESC and continues with Xbox words, and the PC prefix carries console values the host runtime
// refuses: measured on 1.0.2.1 the first call is a compute queue whose Flags/NodeMask words are not PC values
// (docs/retail_boot_chain.md §3.2). The flags are limited to D3D12_COMMAND_QUEUE_FLAG_DISABLE_GPU_TIMEOUT and the node
// mask to 0 before the PC device sees them; the discarded words are logged once so a trace can show them.
HRESULT proxy_create_command_queue_x(DeviceProxy* self, std::uint64_t desc_word, const IID* riid, void** out) {
    // The generic thunk counts slot 48 before it routes here, so this helper does not count it again.
    if (!desc_word) return E_INVALIDARG;
    D3D12_COMMAND_QUEUE_DESC desc{};
    std::memcpy(&desc, reinterpret_cast<const void*>(desc_word), sizeof(desc));
    const auto original_flags = static_cast<std::uint32_t>(desc.Flags);
    const auto original_node = desc.NodeMask;
    const auto pc_flags = original_flags & static_cast<std::uint32_t>(D3D12_COMMAND_QUEUE_FLAG_DISABLE_GPU_TIMEOUT);
    // A description that already carries PC values reaches the PC path as the caller's own pointer: the sanitising copy
    // exists only for the console words, and a mock in xs_proxy_mock_test checks the pointer it handed in.
    const bool passthrough = pc_flags == original_flags && original_node == 0;
    desc.Flags = static_cast<D3D12_COMMAND_QUEUE_FLAGS>(pc_flags);
    desc.NodeMask = 0;

    const HRESULT hr =
        passthrough ? proxy_create_command_queue(self, reinterpret_cast<const D3D12_COMMAND_QUEUE_DESC*>(desc_word),
                                                 riid, out)
                    : proxy_create_command_queue(self, &desc, riid, out);
    // Every call, the first eight: a refusal here shows up as a failed HRESULT in the title's own check, two frames
    // later, with nothing in the log to say which description caused it.
    static std::atomic<unsigned> calls{0};
    const unsigned n = calls.fetch_add(1, std::memory_order_relaxed);
    if (n < 8)
        std::fprintf(stderr,
                     "[rt] d3d12_xs: slot 48 call %u: type=%u priority=%d flags=0x%x node mask=0x%x -> PC flags 0x%x, "
                     "node mask 0, %s hr=0x%08x\n",
                     n, static_cast<unsigned>(desc.Type), static_cast<int>(desc.Priority), original_flags,
                     original_node, pc_flags, passthrough ? "caller pointer" : "sanitised copy",
                     static_cast<unsigned>(hr));
    return hr;
}

// Xbox device slot 78 (public HLE): SetFrameIntervalX(output, interval_us, latency, flags). The output and flags are
// not used before a presenter exists. The period is a vblank period in microseconds in the public samples (16667 for
// 60 Hz), and latency is the allowed frames in flight. S_OK even for 0: a boot must not abort here, and nothing paces.
HRESULT proxy_set_frame_interval_x(DeviceProxy* self, std::uint64_t output, std::uint64_t interval_us,
                                   std::uint64_t latency, std::uint64_t flags) {
    (void)output;
    (void)flags;
    self->frame_interval_us.store(static_cast<std::uint32_t>(interval_us), std::memory_order_relaxed);
    self->frame_latency.store(static_cast<std::uint32_t>(latency ? latency : 1), std::memory_order_relaxed);
    return S_OK;
}

// Xbox device slot 79 (public HLE): ScheduleFrameEventX(event, offset_us, pEvent, flags); our pacer has only the origin.
HRESULT proxy_schedule_frame_event_x(DeviceProxy* self, std::uint64_t event, std::uint64_t offset_us,
                                     std::uint64_t flags) {
    (void)event;
    (void)offset_us;
    (void)flags;
    self->frame_event_scheduled.store(true, std::memory_order_relaxed);
    return S_OK;
}

// Xbox device slot 80 (public HLE): WaitFrameEventX(event, timeout_ms, pEvent, flags, &token). It returns at once with
// a fresh non-zero token (the first-frame choice): there is no vblank source behind the device before a presenter, and
// a zero wait cannot stall the title. A null token pointer is E_POINTER, as a _Out_ parameter.
HRESULT proxy_wait_frame_event_x(DeviceProxy* self, std::uint64_t event, std::uint64_t timeout_ms,
                                 std::uint64_t flags, std::uint64_t token_word) {
    (void)event;
    (void)timeout_ms;
    (void)flags;
    auto* token = reinterpret_cast<std::uint64_t*>(token_word);
    if (!token) return E_POINTER;
    *token = self->frame_tokens->issue();
    return S_OK;
}

// Xbox device slot 73 (public HLE): CreateCommandListX(desc, allocator, initial_state, riid, out). The console call is
// not the PC six-argument form: the type is the first word of D3D12XBOX_COMMAND_LIST_DESC and there is no node mask
// (the HLE passes 0). The second desc word is logged, not interpreted: its public layout is not established.
HRESULT proxy_create_command_list_x(DeviceProxy* self, std::uint64_t desc_word, std::uint64_t allocator_word,
                                    std::uint64_t initial_state_word, const IID* riid, void** out) {
    if (out) *out = nullptr;
    if (!desc_word || !allocator_word) return E_INVALIDARG;
    if (!riid) return E_POINTER;
    if (!out) return E_POINTER;

    std::uint32_t words[2] = {};
    std::memcpy(words, reinterpret_cast<const void*>(desc_word), sizeof(words));
    auto type = static_cast<D3D12_COMMAND_LIST_TYPE>(words[0]);
    if (type != D3D12_COMMAND_LIST_TYPE_DIRECT && type != D3D12_COMMAND_LIST_TYPE_BUNDLE &&
        type != D3D12_COMMAND_LIST_TYPE_COMPUTE && type != D3D12_COMMAND_LIST_TYPE_COPY)
        type = D3D12_COMMAND_LIST_TYPE_DIRECT;  // as the public HLE does

    static std::atomic<bool> logged{false};
    if (first_call(logged))
        std::fprintf(stderr,
                     "[rt] d3d12_xs: slot 73 CreateCommandListX desc words 0x%08x 0x%08x -> node mask 0, type %d\n",
                     words[0], words[1], static_cast<int>(type));

    // Leonida wraps its Vulkan allocators; a PC allocator comes back unwrapped and is passed as it is. Pipeline states
    // are not wrapped by this proxy, so initial_state is already the real object (a proxy there is passed through too).
    auto* allocator = allocator_proxy_real_allocator(reinterpret_cast<void*>(allocator_word));
    if (!allocator) allocator = reinterpret_cast<ID3D12CommandAllocator*>(allocator_word);
    auto* initial_state = reinterpret_cast<ID3D12PipelineState*>(initial_state_word);
    if (!self->console_lists)
        return self->real->CreateCommandList(0, type, allocator, initial_state,
                                             command_list_iid_for_pc(*riid), out);
    void* created = nullptr;
    const HRESULT hr = self->real->CreateCommandList(0, type, allocator, initial_state,
                                                   __uuidof(ID3D12GraphicsCommandList), &created);
    if (FAILED(hr)) return hr;
    return wrap_list(static_cast<IUnknown*>(created), self, allocator, type, riid, out);
}

// Xbox device slot 70: (texture_words, view_desc, destination), no return value. The retail executable's SRV writer
// makes a view from raw descriptions and a GPU address with no resource object (docs/retail_boot_chain.md, stop 14).
// The PC slot 70 is ID3D12Device8::CreatePlacedResource1, which would take the destination handle for a resource
// description and write through it, so it is never forwarded.
//
// The one measured shape (PR 7066, eugen1763, first retail call) is a raw buffer view: texture_words starts with a
// desktop D3D12_RESOURCE_DESC (buffer), view_desc with a desktop D3D12_SHADER_RESOURCE_VIEW_DESC (R32_TYPELESS,
// BUFFER, RAW flag) followed at once by a D3D12_GPU_VIRTUAL_ADDRESS, the console VA slot 133 registered. For that shape
// and a VA inside a buffer the resource provider knows, the destination gets a raw view of that range: FirstElement
// (va - resource start) / 4, NumElements the description's Width / 4, cut to the end of the resource. Anything else
// (a texture, another dimension or format, no RAW flag, a VA no buffer holds, no provider, unreadable words) keeps the
// PC null view, which a shader reads as zeros, with one log line saying why. Texture shapes are not measured.
struct RawSrvShape {
    D3D12_RESOURCE_DESC resource;
    D3D12_SHADER_RESOURCE_VIEW_DESC view;
    D3D12_GPU_VIRTUAL_ADDRESS va;
};

bool readable(std::uint64_t address, std::size_t bytes) {
    MEMORY_BASIC_INFORMATION info{};
    if (!address || !VirtualQuery(reinterpret_cast<const void*>(address), &info, sizeof(info))) return false;
    if (info.State != MEM_COMMIT || (info.Protect & (PAGE_NOACCESS | PAGE_GUARD))) return false;
    const auto end = reinterpret_cast<std::uintptr_t>(info.BaseAddress) + info.RegionSize;
    return address + bytes <= end;
}

// Reads the measured shape from the two words into `out`. Returns null when it parsed, else why it is not that shape.
const char* parse_raw_srv(std::uint64_t texture_words, std::uint64_t view_desc, RawSrvShape& out) {
    if (!readable(texture_words, sizeof(D3D12_RESOURCE_DESC)) ||
        !readable(view_desc, sizeof(D3D12_SHADER_RESOURCE_VIEW_DESC) + sizeof(D3D12_GPU_VIRTUAL_ADDRESS)))
        return "the words are not readable";
    std::memcpy(&out.resource, reinterpret_cast<const void*>(texture_words), sizeof(out.resource));
    std::memcpy(&out.view, reinterpret_cast<const void*>(view_desc), sizeof(out.view));
    std::memcpy(&out.va, reinterpret_cast<const std::uint8_t*>(view_desc) + sizeof(out.view), sizeof(out.va));
    if (out.resource.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER) return "arg1 is not a buffer description";
    if (out.view.ViewDimension != D3D12_SRV_DIMENSION_BUFFER) return "arg2 is not a buffer view";
    if (out.view.Format != DXGI_FORMAT_R32_TYPELESS || !(out.view.Buffer.Flags & D3D12_BUFFER_SRV_FLAG_RAW))
        return "arg2 is not a raw R32_TYPELESS view";
    if (!out.resource.Width || !out.va) return "no width or no address";
    return nullptr;
}

void write_null_srv(DeviceProxy* self, std::uint64_t destination) {
    D3D12_SHADER_RESOURCE_VIEW_DESC null_view{};
    null_view.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    null_view.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    null_view.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    null_view.Texture2D.MipLevels = 1;
    self->real->CreateShaderResourceView(nullptr, &null_view, D3D12_CPU_DESCRIPTOR_HANDLE{destination});
}

void log_slot70_null_once(const char* why, std::uint64_t texture_words, std::uint64_t view_desc) {
    static std::atomic<bool> logged{false};
    if (first_call(logged))
        std::fprintf(stderr, "[rt] d3d12_xs: slot 70 writes a null view: %s (args 0x%llx 0x%llx)\n", why,
                     static_cast<unsigned long long>(texture_words), static_cast<unsigned long long>(view_desc));
}

void proxy_create_srv_from_raw_x(DeviceProxy* self, std::uint64_t texture_words, std::uint64_t view_desc,
                                 std::uint64_t destination) {
    note_slot(70);
    log_xbox_route_once(70, "CreateShaderResourceView from raw words", texture_words, view_desc, destination, 0, 0);
    if (!destination) return;
    // Hook point for the CPU destination: the console hands a CPU descriptor handle that the PC layer may not be able
    // to write as it is. Translating it (#7021) goes here, on `destination`, before either write below.

    RawSrvShape shape{};
    const char* why = parse_raw_srv(texture_words, view_desc, shape);
    if (why || !self->resource_provider) {
        log_slot70_null_once(why ? why : "no resource provider", texture_words, view_desc);
        write_null_srv(self, destination);
        return;
    }
    std::uint64_t start = 0, size = 0;
    ID3D12Resource* resource = self->resource_provider(self->real, shape.va, shape.resource.Width, &start, &size);
    if (!resource || shape.va < start || shape.va - start >= size) {
        if (resource) resource->Release();
        log_slot70_null_once("no buffer holds the address", texture_words, view_desc);
        write_null_srv(self, destination);
        return;
    }
    const std::uint64_t offset = shape.va - start;
    const std::uint64_t elements = std::min<std::uint64_t>(shape.resource.Width, size - offset) / 4;
    if (offset % 4 || !elements || elements > UINT32_MAX) {
        resource->Release();
        log_slot70_null_once("address or width is not 4-byte aligned or too large", texture_words, view_desc);
        write_null_srv(self, destination);
        return;
    }
    D3D12_SHADER_RESOURCE_VIEW_DESC view{};
    view.Format = DXGI_FORMAT_R32_TYPELESS;
    view.ViewDimension = D3D12_SRV_DIMENSION_BUFFER;
    view.Shader4ComponentMapping = shape.view.Shader4ComponentMapping;
    view.Buffer.FirstElement = offset / 4;
    view.Buffer.NumElements = static_cast<UINT>(elements);
    view.Buffer.Flags = D3D12_BUFFER_SRV_FLAG_RAW;
    self->real->CreateShaderResourceView(resource, &view, D3D12_CPU_DESCRIPTOR_HANDLE{destination});
    resource->Release();
    static std::atomic<bool> logged{false};
    if (first_call(logged))
        std::fprintf(stderr, "[rt] d3d12_xs: slot 70 wrote a raw buffer view (va 0x%llx, first element %llu, %u elements)\n",
                     static_cast<unsigned long long>(shape.va), static_cast<unsigned long long>(view.Buffer.FirstElement),
                     view.Buffer.NumElements);
}

// Device slot 75: ID3D12Device9::CreateCommandQueue1(desc, creator_id, riid, out), called by slot index.
HRESULT proxy_create_command_queue1(DeviceProxy* self, std::uint64_t desc, std::uint64_t creator_id,
                                    const IID* riid, void** out) {
    note_slot(75);
    if (out) *out = nullptr;  // as slot 8
    if (self->slot_count <= 75) return refuse_beyond_device(75, self->slot_count);  // predates ID3D12Device9
    if (!riid) return E_POINTER;
    using Fn = HRESULT(WINAPI*)(ID3D12Device*, std::uint64_t, std::uint64_t, const IID*, void**);
    const auto create = reinterpret_cast<Fn>(self->real_vtable[75]);
    if (!out) return create(self->real, desc, creator_id, riid, nullptr);
    void* created = nullptr;
    const HRESULT hr = create(self->real, desc, creator_id, &__uuidof(ID3D12CommandQueue), &created);
    if (FAILED(hr)) return hr;
    return wrap_child<ChildKind::queue>(static_cast<IUnknown*>(created), self, riid, out);
}

// Device slot 36: CreateFence(initial_value, flags, riid, out).
HRESULT proxy_create_fence(DeviceProxy* self, std::uint64_t initial_value, std::uint64_t flags, const IID* riid,
                           void** out) {
    note_slot(36);
    if (out) *out = nullptr;  // as slot 8
    if (!riid) return E_POINTER;
    const auto fence_flags = static_cast<D3D12_FENCE_FLAGS>(static_cast<std::uint32_t>(flags));
#pragma warning(suppress : 6387)  // the caller's null output, on purpose (see slot 8)
    if (!out) return self->real->CreateFence(initial_value, fence_flags, *riid, nullptr);
    void* created = nullptr;
    const HRESULT hr = self->real->CreateFence(initial_value, fence_flags, __uuidof(ID3D12Fence), &created);
    if (FAILED(hr)) return hr;
    return wrap_child<ChildKind::fence>(static_cast<IUnknown*>(created), self, riid, out);
}

// True when `allocator` is one whose consumers resolve allocator proxies: Leonida's own Vulkan-layer allocator,
// whose lists (CreateCommandList, Reset) find the real allocator through allocator_proxy_real_allocator. A PC
// runtime allocator does not answer the tag, and its lists would take a proxy for their own allocator.
bool resolves_allocator_proxies(IUnknown* allocator) {
    void* found = nullptr;
    if (FAILED(allocator->QueryInterface(kLeonidaCommandAllocatorTag, &found)) || !found) return false;
    static_cast<IUnknown*>(found)->Release();  // the implementation pointer starts with its IUnknown chain
    return true;
}

// Device slot 9: CreateCommandAllocator(type, riid, out). An allocator proxy where the list side resolves it (see
// resolves_allocator_proxies); any other allocator goes back unchanged, logged once, so the host runtime's lists
// never see a proxy. Null output and null IID as slot 8.
HRESULT proxy_create_command_allocator(DeviceProxy* self, std::uint64_t type, const IID* riid, void** out) {
    note_slot(9);
    if (out) *out = nullptr;  // as slot 8
    if (!riid) return E_POINTER;
    const auto list_type = static_cast<D3D12_COMMAND_LIST_TYPE>(static_cast<std::uint32_t>(type));
#pragma warning(suppress : 6387)  // the caller's null output, on purpose (see slot 8)
    if (!out) return self->real->CreateCommandAllocator(list_type, *riid, nullptr);
    void* created = nullptr;
    const HRESULT hr = self->real->CreateCommandAllocator(list_type, __uuidof(ID3D12CommandAllocator), &created);
    if (FAILED(hr)) return hr;
    if (!created) return E_FAIL;
    auto* allocator = static_cast<IUnknown*>(created);
    if (!resolves_allocator_proxies(allocator)) {
        static std::atomic<bool> logged{false};
        if (!logged.exchange(true, std::memory_order_relaxed))
            std::fprintf(stderr, "[rt] d3d12_xs: allocators of this device are not wrapped: its command lists "
                                 "cannot resolve an allocator proxy\n");
        const HRESULT asked = allocator->QueryInterface(*riid, out);
        allocator->Release();
        return asked;
    }
    return wrap_child<ChildKind::allocator>(allocator, self, riid, out);
}

// Device slot 32: OpenSharedHandle(handle, riid, out). The object is opened as asked; when it is a fence (whatever
// interface was asked for, IUnknown included) the title gets a fence proxy, anything else (heaps, resources) is
// returned unchanged.
HRESULT proxy_open_shared_handle(DeviceProxy* self, std::uint64_t handle, const IID* riid, void** out) {
    note_slot(32);
    if (out) *out = nullptr;  // as slot 8
    if (!riid) return E_POINTER;
    const auto shared = reinterpret_cast<HANDLE>(handle);
    if (!out) return self->real->OpenSharedHandle(shared, *riid, nullptr);
    void* opened = nullptr;
    HRESULT hr = self->real->OpenSharedHandle(shared, *riid, &opened);
    if (FAILED(hr) || !opened) return FAILED(hr) ? hr : E_FAIL;
    void* fence = nullptr;
    if (FAILED(static_cast<IUnknown*>(opened)->QueryInterface(__uuidof(ID3D12Fence), &fence)) || !fence) {
        *out = opened;  // not a fence
        return hr;
    }
    static_cast<IUnknown*>(opened)->Release();
    return wrap_child<ChildKind::fence>(static_cast<IUnknown*>(fence), self, riid, out);
}

// Device slots that take heap, queue or fence objects: the real runtime gets the real objects.
//   31 CreateSharedHandle(object, attributes, access, name, handle)
//   34 MakeResident(count, objects)      35 Evict(count, objects)
//   45 SetEventOnMultipleFenceCompletion(fences, values, count, flags, event)            (ID3D12Device1)
//   46 SetResidencyPriority(count, objects, priorities)                                  (ID3D12Device1)
//   50 EnqueueMakeResident(flags, count, objects, fence, value)                          (ID3D12Device3)
template <unsigned Slot>
HRESULT proxy_unwrapping_call(DeviceProxy* self, std::uint64_t a1, std::uint64_t a2, std::uint64_t a3,
                              std::uint64_t a4, std::uint64_t a5) {
    note_slot(Slot);
    if (Slot >= self->slot_count) return refuse_beyond_device(Slot, self->slot_count);  // predates the interface
    UnwrappedArray objects;
    std::uint64_t r1 = a1, r2 = a2, r3 = a3, r4 = a4;
    if constexpr (Slot == 31) {
        r1 = unwrap_object(a1);
    } else if constexpr (Slot == 34 || Slot == 35 || Slot == 46) {
        if (!objects.fill(a2, a1)) return E_OUTOFMEMORY;
        if (objects.data()) r2 = objects.data();
    } else if constexpr (Slot == 45) {
        if (!objects.fill(a1, a3)) return E_OUTOFMEMORY;
        if (objects.data()) r1 = objects.data();
    } else if constexpr (Slot == 50) {
        if (!objects.fill(a3, a2)) return E_OUTOFMEMORY;
        if (objects.data()) r3 = objects.data();
        r4 = unwrap_object(a4);
    }
    // r1-r4 differ from a1-a4 only in a position the method takes, so the title's frame has that word.
    return static_cast<HRESULT>(static_cast<std::uint32_t>(forward_device<Slot>(self, r1, r2, r3, r4, a5)));
}

HRESULT proxy_create_descriptor_heap(DeviceProxy* self, const D3D12_DESCRIPTOR_HEAP_DESC* desc, const IID* riid,
                                     void** out) {
    note_slot(14);
    if (out) *out = nullptr;  // _COM_Outptr_: NULL on every failure, as the PC device does
    if (!riid) return E_POINTER;
    // Measured (docs/retail_boot_chain.md, stop 13): the title compares the CPU handle the heap hands back with a value
    // it already holds, so both the description it creates the heap from and the handles the heap reports are logged.
    static std::atomic<bool> desc_logged{false};
    if (desc && first_call(desc_logged)) {
        const auto* words = reinterpret_cast<const std::uint32_t*>(desc);
        std::fprintf(stderr,
                     "[rt] d3d12_xs: slot 14 heap desc: type %u descriptors %u flags 0x%x node mask 0x%x (raw %08x %08x "
                     "%08x %08x)\n",
                     static_cast<unsigned>(desc->Type), desc->NumDescriptors, static_cast<unsigned>(desc->Flags),
                     desc->NodeMask, words[0], words[1], words[2], words[3]);
    }
    // No output: a capability check, answered by the device (S_FALSE, as on the PC).
    if (!out) return self->real->CreateDescriptorHeap(desc, *riid, nullptr);

    void* created = nullptr;
    HRESULT hr = self->real->CreateDescriptorHeap(desc, __uuidof(ID3D12DescriptorHeap), &created);
    if (FAILED(hr) || !created) return FAILED(hr) ? hr : E_FAIL;

    ID3D12DescriptorHeap* heap = static_cast<ID3D12DescriptorHeap*>(created);
    SIZE_T custom_cpu_start = 0;
    SIZE_T custom_byte_size = 0;
    SIZE_T real_cpu_start = 0;
    DescriptorHeapBackingAlloc alloc_fn = nullptr;
    DescriptorHeapBackingFree free_fn = nullptr;
    {
        std::lock_guard lock(heap_alloc_mutex());
        alloc_fn = g_heap_alloc;
        free_fn = g_heap_free;
    }
    if (alloc_fn && desc && desc->NumDescriptors > 0) {
        const UINT inc = self->real->GetDescriptorHandleIncrementSize(desc->Type);
        if (inc > 0 && desc->NumDescriptors <= (std::numeric_limits<SIZE_T>::max)() / inc) {
            const SIZE_T byte_size = static_cast<SIZE_T>(desc->NumDescriptors) * static_cast<SIZE_T>(inc);
            void* backing = alloc_fn(byte_size, kXmemDescriptorHeapAttributes);
            if (backing) {
                const auto addr = reinterpret_cast<SIZE_T>(backing);
                if (addr <= (std::numeric_limits<SIZE_T>::max)() - byte_size) {
                    custom_cpu_start = addr;
                    custom_byte_size = byte_size;
                    real_cpu_start = raw_heap_get_cpu_handle(heap, *reinterpret_cast<void***>(heap)).ptr;
                } else if (free_fn) {
                    free_fn(backing, kXmemDescriptorHeapAttributes);
                }
            }
        }
    }
    // The heap's console (GPU) handle, for the unified-memory CPU-handle mapping (LEONIDA_XS_GPU_HANDLE). The layer's
    // shader-visible heap lives in the GPU address arena and reports its arena offset as the GPU handle, so the handle
    // is the heap's host pointer minus the arena's mapping (the slot-10 getter is not called: its return convention
    // through a forwarded vtable slot is not the one this layer uses).
    SIZE_T gpu_start = 0;
    if (custom_cpu_start != 0 && leonida::arena_host_base &&
        real_cpu_start >= reinterpret_cast<SIZE_T>(leonida::arena_host_base))
        gpu_start = real_cpu_start - reinterpret_cast<SIZE_T>(leonida::arena_host_base);

    hr = create_heap_proxy(heap, self, *riid, out, custom_cpu_start, custom_byte_size, real_cpu_start,
                           custom_cpu_start ? free_fn : nullptr, gpu_start);
    if (FAILED(hr)) {
        heap->Release();
        if (custom_cpu_start && free_fn)
            free_fn(reinterpret_cast<void*>(custom_cpu_start), kXmemDescriptorHeapAttributes);
    }
    return hr;
}

// Device slots 17-24 write or copy descriptors using CPU handles. When a title supplies its own descriptor-heap base
// via __XMemAllocationHooks__, those handles fall in [custom_start, custom_end) and must be translated back to the
// underlying PC/Vulkan heap before forwarding.
void proxy_create_cbv(DeviceProxy* self, const D3D12_CONSTANT_BUFFER_VIEW_DESC* desc,
                      D3D12_CPU_DESCRIPTOR_HANDLE dest) {
    note_slot(17);
    self->real->CreateConstantBufferView(desc, translate_cpu_handle(self, dest));
}

void proxy_create_srv(DeviceProxy* self, ID3D12Resource* resource, const D3D12_SHADER_RESOURCE_VIEW_DESC* desc,
                      D3D12_CPU_DESCRIPTOR_HANDLE dest) {
    note_slot(18);
    self->real->CreateShaderResourceView(resource, desc, translate_cpu_handle(self, dest));
}

void proxy_create_uav(DeviceProxy* self, ID3D12Resource* resource, ID3D12Resource* counter,
                      const D3D12_UNORDERED_ACCESS_VIEW_DESC* desc, D3D12_CPU_DESCRIPTOR_HANDLE dest) {
    note_slot(19);
    self->real->CreateUnorderedAccessView(resource, counter, desc, translate_cpu_handle(self, dest));
}

void proxy_create_rtv(DeviceProxy* self, ID3D12Resource* resource, const D3D12_RENDER_TARGET_VIEW_DESC* desc,
                      D3D12_CPU_DESCRIPTOR_HANDLE dest) {
    note_slot(20);
    self->real->CreateRenderTargetView(resource, desc, translate_cpu_handle(self, dest));
}

void proxy_create_dsv(DeviceProxy* self, ID3D12Resource* resource, const D3D12_DEPTH_STENCIL_VIEW_DESC* desc,
                      D3D12_CPU_DESCRIPTOR_HANDLE dest) {
    note_slot(21);
    self->real->CreateDepthStencilView(resource, desc, translate_cpu_handle(self, dest));
}

void proxy_create_sampler(DeviceProxy* self, const D3D12_SAMPLER_DESC* desc, D3D12_CPU_DESCRIPTOR_HANDLE dest) {
    note_slot(22);
    self->real->CreateSampler(desc, translate_cpu_handle(self, dest));
}

void proxy_copy_descriptors(DeviceProxy* self, UINT num_dest, const D3D12_CPU_DESCRIPTOR_HANDLE* dest_starts,
                            const UINT* dest_sizes, UINT num_src, const D3D12_CPU_DESCRIPTOR_HANDLE* src_starts,
                            const UINT* src_sizes, D3D12_DESCRIPTOR_HEAP_TYPE type) {
    note_slot(23);
    if (g_custom_range_count.load(std::memory_order_acquire) == 0) {
        self->real->CopyDescriptors(num_dest, dest_starts, dest_sizes, num_src, src_starts, src_sizes, type);
        return;
    }
    constexpr UINT kInline = 32;
    D3D12_CPU_DESCRIPTOR_HANDLE dest_inline[kInline];
    D3D12_CPU_DESCRIPTOR_HANDLE src_inline[kInline];
    std::vector<D3D12_CPU_DESCRIPTOR_HANDLE> dest_heap;
    std::vector<D3D12_CPU_DESCRIPTOR_HANDLE> src_heap;
    const D3D12_CPU_DESCRIPTOR_HANDLE* dest_out = dest_starts;
    const D3D12_CPU_DESCRIPTOR_HANDLE* src_out = src_starts;
    try {
        if (dest_starts && num_dest > 0) {
            D3D12_CPU_DESCRIPTOR_HANDLE* dst = dest_inline;
            if (num_dest > kInline) {
                dest_heap.resize(num_dest);
                dst = dest_heap.data();
            }
            for (UINT i = 0; i < num_dest; ++i) dst[i] = translate_cpu_handle(self, dest_starts[i]);
            dest_out = dst;
        }
        if (src_starts && num_src > 0) {
            D3D12_CPU_DESCRIPTOR_HANDLE* src = src_inline;
            if (num_src > kInline) {
                src_heap.resize(num_src);
                src = src_heap.data();
            }
            for (UINT i = 0; i < num_src; ++i) src[i] = translate_cpu_handle(self, src_starts[i]);
            src_out = src;
        }
    } catch (const std::bad_alloc&) {
        return;
    }
    self->real->CopyDescriptors(num_dest, dest_out, dest_sizes, num_src, src_out, src_sizes, type);
}

void proxy_copy_descriptors_simple(DeviceProxy* self, UINT num_descriptors, D3D12_CPU_DESCRIPTOR_HANDLE dest_start,
                                   D3D12_CPU_DESCRIPTOR_HANDLE src_start, D3D12_DESCRIPTOR_HEAP_TYPE type) {
    note_slot(24);
    self->real->CopyDescriptorsSimple(num_descriptors, translate_cpu_handle(self, dest_start),
                                      translate_cpu_handle(self, src_start), type);
}

// The Xbox slot 62 takes two more words between the description and the IID: (desc, w2, w3, riid, out). Measured
// on the package's XboxBvhBuilder.dll (its constructor passes w2 = w3 = 0 and IID_ID3D12PipelineState in the fourth
// argument), as the prior art had recorded. Their meaning is unknown; non-zero values are logged once.
HRESULT proxy_create_compute_pipeline_state(DeviceProxy* self, const D3D12_COMPUTE_PIPELINE_STATE_DESC* desc,
                                            std::uint64_t w2, std::uint64_t w3, const IID* riid, void** out) {
    note_slot(62);
    if (w2 || w3) {
        static std::atomic<bool> logged{false};
        if (!logged.exchange(true, std::memory_order_relaxed))
            std::fprintf(stderr, "[rt] d3d12_xs: slot 62 extra words 0x%llx 0x%llx (ignored)\n",
                         static_cast<unsigned long long>(w2), static_cast<unsigned long long>(w3));
    }
    if (!out) return E_POINTER;
    *out = nullptr;
    if (!riid) return E_POINTER;
    return self->real->CreateComputePipelineState(desc, *riid, out);
}

// The Xbox slot 41 is CreateCommandSignature at its PC position, (desc, root signature, riid, out), but a console
// description can hold an argument type the PC does not have. The retail title creates five one-argument signatures
// with no root signature at startup; one has type 105 (0x69) and a 12-byte stride, next to a DISPATCH and a
// DISPATCH_MESH of 12 bytes. Read statically from its call sites (counts and shapes only): its two ExecuteIndirect
// sites pass MaxCommandCount 1 and an argument offset into the same upload buffer as the DISPATCH ones, and set the
// compute state (pipeline, compute root signature, tables, constants, SRV/UAV) right before, as the DISPATCH path
// does; they record on a second command list of the title's context. So type 105 is taken as a dispatch whose
// arguments are a D3D12_DISPATCH_ARGUMENTS. HYPOTHESIS: what the console does differently for it (which command
// stream it feeds) is not known; the PC layer has one compute path. Other descriptions are forwarded unchanged.
// 105 is outside D3D12_INDIRECT_ARGUMENT_TYPE's range, so the field is read as its 32 bits (a constant expression that
// casts 105 to the enum does not compile with clang-cl).
constexpr std::uint32_t kXboxIndirectArgumentDispatch105 = 105;

std::uint32_t argument_type(const D3D12_INDIRECT_ARGUMENT_DESC& arg) {
    std::uint32_t type = 0;
    std::memcpy(&type, &arg.Type, sizeof(type));
    return type;
}

HRESULT proxy_create_command_signature(DeviceProxy* self, const D3D12_COMMAND_SIGNATURE_DESC* desc,
                                       std::uint64_t root_signature, const IID* riid, void** out) {
    note_slot(41);
    using Fn = HRESULT(STDMETHODCALLTYPE*)(ID3D12Device*, const D3D12_COMMAND_SIGNATURE_DESC*, std::uint64_t,
                                           const IID*, void**);
    const auto forward = reinterpret_cast<Fn>(self->real_vtable[41]);
    bool console = false;
    if (desc && desc->pArgumentDescs)
        for (UINT i = 0; i < desc->NumArgumentDescs && !console; ++i) {
            const std::uint32_t type = argument_type(desc->pArgumentDescs[i]);
            console = type == kXboxIndirectArgumentDispatch105 ||
                      type == static_cast<std::uint32_t>(D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_MESH);
        }
    if (!console) {
        const HRESULT hr = forward(self->real, desc, root_signature, riid, out);
        static std::atomic<unsigned> forwarded{0};
        const unsigned n = forwarded.fetch_add(1, std::memory_order_relaxed);
        if (n < 8 || FAILED(hr)) {
            const unsigned type = desc && desc->pArgumentDescs && desc->NumArgumentDescs
                                      ? argument_type(desc->pArgumentDescs[0]) : 0u;
            std::fprintf(stderr,
                         "[rt] d3d12_xs: slot 41 forwarded call %u: %u argument(s), first type %u, stride %u, "
                         "hr=0x%08x\n",
                         n, desc ? desc->NumArgumentDescs : 0u, type, desc ? desc->ByteStride : 0u,
                         static_cast<unsigned>(hr));
        }
        return hr;
    }

    std::unique_ptr<D3D12_INDIRECT_ARGUMENT_DESC[]> args(new (std::nothrow)
                                                             D3D12_INDIRECT_ARGUMENT_DESC[desc->NumArgumentDescs]);
    if (!args) {
        if (out) *out = nullptr;
        return E_OUTOFMEMORY;
    }
    for (UINT i = 0; i < desc->NumArgumentDescs; ++i) {
        args[i] = desc->pArgumentDescs[i];
        const std::uint32_t type = argument_type(args[i]);
        // Type 105 is the console's own dispatch and DISPATCH_MESH is a mesh dispatch; the host backend models neither,
        // and both carry D3D12_DISPATCH_ARGUMENTS (12 bytes), so both are created as a compute dispatch. A mesh
        // pipeline still cannot run without mesh shader support; this keeps the signature and the boot's shape.
        if (type == kXboxIndirectArgumentDispatch105 ||
            type == static_cast<std::uint32_t>(D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_MESH))
            args[i].Type = D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH;
    }
    static std::atomic<bool> logged{false};
    if (!logged.exchange(true, std::memory_order_relaxed))
        std::fprintf(stderr, "[rt] d3d12_xs: slot 41 argument type 105 (stride %u) created as DISPATCH\n",
                     desc->ByteStride);
    D3D12_COMMAND_SIGNATURE_DESC pc = *desc;
    pc.pArgumentDescs = args.get();
    const HRESULT hr = forward(self->real, &pc, root_signature, riid, out);
    static std::atomic<unsigned> calls{0};
    const unsigned n = calls.fetch_add(1, std::memory_order_relaxed);
    if (n < 8 || FAILED(hr))
        std::fprintf(stderr, "[rt] d3d12_xs: slot 41 call %u: %u argument(s), stride %u, hr=0x%08x\n", n,
                     desc->NumArgumentDescs, desc->ByteStride, static_cast<unsigned>(hr));
    return hr;
}

// The Xbox slot 61 is CreateSamplerX(desc, dest): a sampler written to a CPU descriptor handle, with the PC
// CreateSampler's two arguments. Measured on the retail title's boot (stop 20 in #6910's RT-5 measurements): the
// title calls it once per sampler after its descriptor heaps exist, and it goes on when the call is served by the PC
// CreateSampler. The PC slot 61 is CreateMetaCommand, which would take these words as (CommandId, NodeMask, ...).
// HYPOTHESIS: the console description begins with the PC D3D12_SAMPLER_DESC (52 bytes), which is what got the title
// past this call; whatever follows it is not known and not read. The title was not seen testing a result, so a null
// description is skipped rather than reported. The handle is passed as given: a handle in the title's own heap range
// is #7021's translation.
void proxy_create_sampler_x(DeviceProxy* self, const D3D12_SAMPLER_DESC* desc, std::uint64_t dest) {
    note_slot(61);
    if (!desc) {
        static std::atomic<bool> logged{false};
        if (!logged.exchange(true, std::memory_order_relaxed))
            std::fprintf(stderr, "[rt] d3d12_xs: slot 61 CreateSamplerX without a description (skipped)\n");
        return;
    }
    D3D12_CPU_DESCRIPTOR_HANDLE handle{};
    handle.ptr = static_cast<SIZE_T>(dest);
    self->real->CreateSampler(desc, handle);
}

// LEONIDA_DUMP_SHADERS=<dir>: a slot-67 payload the device has no translation for, written as the FSPX file
// tools/fspx_unpack.py reads (a bare payload: the header, the four directory entries and their bytes), named
// cs-<sha256 of the kind-6 program, first 16 hex digits>.fspx so it sits beside the device's own cs-<...>.fssm dump.
// The whole payload matters here and only the device dump's program does not: the translator takes the thread-group
// size, COMPUTE_PGM_RSRC2 and the wave size from kind 0 (docs/fspx.md, "Kind 0: dispatch state"), and a bare kind-6
// program has none of them.
void dump_untranslated_fspx(const std::vector<std::uint8_t>& payload) {
    if (payload.empty()) return;
    char dir[1024] = {};
    size_t length = 0;
    if (getenv_s(&length, dir, sizeof dir, "LEONIDA_DUMP_SHADERS") != 0 || length <= 1) return;
    // The key is the kind-6 program, the same one the device hashes: the payload's own entry for it.
    const leonida::FspxParts parts = leonida::fspx_parts(payload);
    if (parts.code.empty()) return;
    CreateDirectoryA(dir, nullptr);
    const std::string base =
        std::string(dir) + "\\cs-" + leonida::sha256_hex(parts.code).substr(0, 16) + ".fspx";
    std::ofstream out(base, std::ios::binary | std::ios::trunc);
    out.write(reinterpret_cast<const char*>(payload.data()), static_cast<std::streamsize>(payload.size()));
    out.close();
    if (!out) {
        std::fprintf(stderr, "[rt] d3d12_xs: could not write %s\n", base.c_str());
        return;
    }
    std::fprintf(stderr, "[rt] d3d12_xs: wrote the untranslated FSPX payload to %s (%zu B)\n", base.c_str(),
                 payload.size());
}

// The Xbox slot 67 creates a compute pipeline from native GPU code: (desc, riid, out), named
// DeserializeComputePipelineStateX in amd_fidelityfx_upscaler_xbox.dll's messages (call at RVA 0x56d9). The desc
// passes the root signature at +0x00, followed by pointer/size pairs for the shader's FSPX entries (docs/fspx.md):
// kind 0 (dispatch state) at +0x08/+0x10, kind 1 at +0x18/+0x20, kind 6 (the RDNA2 program) at +0x28/+0x30, and
// kind 7 (records) at +0x38/+0x40. Translates the console description to a standard D3D12_COMPUTE_PIPELINE_STATE_DESC
// with CS bytecode pointing at kind 6 and routes it to CreateComputePipelineState on the PC device (slot 11), which
// on the Vulkan device resolves through the translator. The PC slot 67 is CreateProtectedResourceSession1.
HRESULT proxy_create_compute_pipeline_from_native_code(DeviceProxy* self, const void* desc,
                                                       const IID* riid, void** out) {
    note_slot(67);
    if (!out) return E_POINTER;
    *out = nullptr;
    if (!desc) return E_INVALIDARG;
    if (!riid) return E_POINTER;

    // FSPX description layout (docs/fspx.md §How the DLL reads it):
    struct XboxNativeComputePipelineStateDesc {
        ID3D12RootSignature* root_signature;  // +0x00
        const void* kind0;                    // +0x08: dispatch state
        std::uint64_t kind0_size;             // +0x10: 28 bytes
        const void* kind1;                    // +0x18: unknown
        std::uint64_t kind1_size;             // +0x20: 20 bytes
        const void* kind6;                    // +0x28: RDNA2 program code
        std::uint64_t kind6_size;             // +0x30: program size in bytes
        const void* kind7;                    // +0x38: resource/hash records
        std::uint64_t kind7_size;             // +0x40: records size in bytes
    };
    const auto* xbox_desc = static_cast<const XboxNativeComputePipelineStateDesc*>(desc);

    D3D12_COMPUTE_PIPELINE_STATE_DESC pc_desc{};
    pc_desc.pRootSignature = xbox_desc->root_signature;
    pc_desc.CS.pShaderBytecode = xbox_desc->kind6;
    pc_desc.CS.BytecodeLength = static_cast<SIZE_T>(xbox_desc->kind6_size);
    // With kind 0 (the DLL always passes it: its reader refuses a payload without one), the four pairs go on as one
    // FSPX payload, so the device matches the program by its kind-6 and kind-0 keys (docs/fspx.md, "Build and run-time
    // lookup"): two programs with the same code and different dispatch state stay apart. Every pair is checked before
    // it is read (leonida::fspx_payload_from_parts); an inconsistent one refuses the call. Without kind 0 the kind-6
    // bytes go on alone, as before.
    std::vector<std::uint8_t> payload;
    if (xbox_desc->kind0 || xbox_desc->kind0_size) {
        const leonida::FspxPart parts[] = {{0, xbox_desc->kind0, xbox_desc->kind0_size},
                                           {1, xbox_desc->kind1, xbox_desc->kind1_size},
                                           {6, xbox_desc->kind6, xbox_desc->kind6_size},
                                           {7, xbox_desc->kind7, xbox_desc->kind7_size}};
        payload = leonida::fspx_payload_from_parts(parts);
        if (payload.empty()) {
            static std::atomic<bool> logged{false};
            if (!logged.exchange(true, std::memory_order_relaxed))
                std::fprintf(stderr,
                             "[rt] d3d12_xs: slot 67 refused: a pointer/size pair is inconsistent or too large "
                             "(kind0 %llu B, kind1 %llu B, kind6 %llu B, kind7 %llu B)\n",
                             static_cast<unsigned long long>(xbox_desc->kind0_size),
                             static_cast<unsigned long long>(xbox_desc->kind1_size),
                             static_cast<unsigned long long>(xbox_desc->kind6_size),
                             static_cast<unsigned long long>(xbox_desc->kind7_size));
            return E_INVALIDARG;
        }
        pc_desc.CS.pShaderBytecode = payload.data();
        pc_desc.CS.BytecodeLength = payload.size();
    }

    pc_desc.NodeMask = 0;
    pc_desc.CachedPSO = {nullptr, 0};
    pc_desc.Flags = D3D12_PIPELINE_STATE_FLAG_NONE;

    // Every failed call is logged with its number: the DLL creates its pipelines one after the other, and only the
    // first call of a slot is announced, so a miss on a later program would otherwise read as the first call's.
    static std::atomic<std::uint64_t> calls{0};
    const std::uint64_t call = calls.fetch_add(1, std::memory_order_relaxed) + 1;
    const HRESULT hr = self->real->CreateComputePipelineState(&pc_desc, *riid, out);
    if (FAILED(hr)) {
        std::fprintf(stderr,
                     "[rt] d3d12_xs: slot 67 call %llu failed with 0x%08lx (kind 0 %llu B, kind 1 %llu B, kind 6 %llu B, "
                     "kind 7 %llu B)\n",
                     static_cast<unsigned long long>(call), static_cast<unsigned long>(hr),
                     static_cast<unsigned long long>(xbox_desc->kind0_size),
                     static_cast<unsigned long long>(xbox_desc->kind1_size),
                     static_cast<unsigned long long>(xbox_desc->kind6_size),
                     static_cast<unsigned long long>(xbox_desc->kind7_size));
        dump_untranslated_fspx(payload);
    }
    return hr;
}

// ---- Committed resources take their memory through the title's XMem hooks (RT-5 stop 17) ----
// On the console the D3D runtime allocates a committed resource's memory with XMemAlloc, so the allocation goes
// through the title's __XMemAllocationHooks__. Around its first full-screen render targets (slot 127) the retail title
// registers a per-thread matcher that bump-allocates out of a range it reserved, taking any allocation whose XALLOC
// page-size field (bits 14..15) is 2, and afterwards asserts that less than 2 MiB of the range is left. Resources
// created on the PC never allocated through the hooks, so the whole range was left and the assert fired. Each committed
// resource made by slots 76 and 127 therefore allocates its size through the hooks as a lease: the resource holds it
// (SetPrivateDataInterface) and the hooks' free runs when the resource is destroyed. The bytes are not used for the
// resource; the PC device owns its memory. The size is the console's (lease_size): for a render target of a listed
// shape, its whole SW_64KB_D tiles (console_render_target_size), never the PC size. The title's budget is sized for
// exactly those (two 3840 x 2160 RGBA8 targets are 2 x 510 tiles of a 64 MiB range), and a PC driver may answer more:
// AMD's Windows driver gives 513 x 64 KiB for that target, and leasing that overran the range (an int3 in the title's
// hook, @controldebils on #7240). Anything else leases the PC size.
// HYPOTHESIS: the attribute word, allocator ID 0x80 (eXALLOCAllocatorId_D3D) with page-size field 2 and object type 0.
// Of it the measured matcher reads only the page-size field; a title hook that matches on other fields may not take
// these leases for the console's allocations.
inline constexpr std::uint64_t kXmemCommittedResourceAttributes = (0x80ULL << 16) | (2ULL << 14);

// {6c1f3a52-8d47-4e0b-9b1e-2f7a5c3d9e61}: the private-data entry a committed resource keeps its lease under.
constexpr GUID kTitleMemoryLeaseGuid{0x6c1f3a52, 0x8d47, 0x4e0b, {0x9b, 0x1e, 0x2f, 0x7a, 0x5c, 0x3d, 0x9e, 0x61}};

// The console allocation for one clean description: a listed render-target shape's whole tiles, else the PC size.
UINT64 lease_size(ID3D12Device* real, const D3D12_RESOURCE_DESC& clean_desc) {
    if (const UINT64 tiles = console_render_target_size(clean_desc)) return tiles;
    return real->GetResourceAllocationInfo(0, 1, &clean_desc).SizeInBytes;
}

// One reference is held by the resource; the last Release gives the memory back through the hooks' free.
class TitleMemoryLease final : public IUnknown {
public:
    TitleMemoryLease(void* memory, DescriptorHeapBackingFree free_fn) : memory_(memory), free_(free_fn) {}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** out) override {
        if (!out) return E_POINTER;
        if (!IsEqualIID(riid, __uuidof(IUnknown))) {
            *out = nullptr;
            return E_NOINTERFACE;
        }
        AddRef();
        *out = this;
        return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return refs_.fetch_add(1, std::memory_order_relaxed) + 1; }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG left = refs_.fetch_sub(1, std::memory_order_acq_rel) - 1;
        if (left == 0) {
            if (free_) free_(memory_, kXmemCommittedResourceAttributes);
            delete this;
        }
        return left;
    }

private:
    std::atomic<ULONG> refs_{1};
    void* memory_;
    DescriptorHeapBackingFree free_;
};

// `created` is the object a slot 76/127 creation returned (any interface of the resource). Nothing happens without
// installed hooks, for a size the PC device refuses, or when the hooks return null; creation never fails here.
void lease_title_resource_memory(DeviceProxy* self, void* created, const D3D12_RESOURCE_DESC& clean_desc) {
    if (!created) return;
    DescriptorHeapBackingAlloc alloc_fn = nullptr;
    DescriptorHeapBackingFree free_fn = nullptr;
    {
        std::lock_guard lock(heap_alloc_mutex());
        alloc_fn = g_heap_alloc;
        free_fn = g_heap_free;
    }
    if (!alloc_fn) return;
    const UINT64 size = lease_size(self->real, clean_desc);
    if (size == 0 || size == UINT64_MAX || size > (std::numeric_limits<std::size_t>::max)()) return;
    void* const memory = alloc_fn(static_cast<std::size_t>(size), kXmemCommittedResourceAttributes);
    if (!memory) return;
    ID3D12Object* object = nullptr;
    auto* const lease = new (std::nothrow) TitleMemoryLease(memory, free_fn);
    if (!lease || FAILED(static_cast<IUnknown*>(created)->QueryInterface(__uuidof(ID3D12Object),
                                                                         reinterpret_cast<void**>(&object)))) {
        if (lease)
            lease->Release();  // frees the memory
        else if (free_fn)
            free_fn(memory, kXmemCommittedResourceAttributes);
        return;
    }
    object->SetPrivateDataInterface(kTitleMemoryLeaseGuid, lease);  // the resource's reference
    object->Release();
    lease->Release();  // ours; from here the resource's reference is the only one
    static std::atomic<bool> logged{false};
    if (!logged.exchange(true, std::memory_order_relaxed))
        std::fprintf(stderr,
                     "[rt] d3d12_xs: committed resources lease their size through the title's XMem hooks (first: "
                     "%llu B)\n",
                     static_cast<unsigned long long>(size));
}

HRESULT proxy_create_resource(DeviceProxy* self, const D3D12_HEAP_PROPERTIES* heap_properties,
                              D3D12_HEAP_FLAGS heap_flags, const D3D12_RESOURCE_DESC* xbox_desc,
                              D3D12_RESOURCE_STATES initial_state, const D3D12_CLEAR_VALUE* clear_value,
                              const IID* riid, void** out) {
    note_slot(76);
    if (!out) return E_POINTER;
    *out = nullptr;
    if (!xbox_desc) return E_POINTER;
    if (!riid) return E_POINTER;

    // The measured description is 56 bytes plus Xbox extension words; D3D12_RESOURCE_DESC is exactly 56 bytes.
    // The Xbox layout word is 0x101 (tiled) or 0x1 (row-major buffer), and is not a PC layout value. Committed
    // resources take ROW_MAJOR for buffers and UNKNOWN for everything else, so the word is ignored and replaced.
    D3D12_RESOURCE_DESC pc_desc = *xbox_desc;
    const auto xbox_layout = static_cast<std::uint32_t>(pc_desc.Layout);
    pc_desc.Layout = pc_desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER ? D3D12_TEXTURE_LAYOUT_ROW_MAJOR
                                                                         : D3D12_TEXTURE_LAYOUT_UNKNOWN;

    static std::atomic<bool> logged{false};
    if (!logged.exchange(true, std::memory_order_relaxed)) {
        std::fprintf(stderr,
                     "[rt] d3d12_xs: slot 76 resource: dimension=%d xbox layout=0x%x -> PC layout=0x%x\n",
                     static_cast<int>(pc_desc.Dimension), xbox_layout, static_cast<unsigned>(pc_desc.Layout));
    }
    const HRESULT hr = self->real->CreateCommittedResource(heap_properties, heap_flags, &pc_desc, initial_state,
                                                           clear_value, *riid, out);
    if (SUCCEEDED(hr)) lease_title_resource_memory(self, *out, pc_desc);
    return hr;
}

// Xbox device slot 133: CreatePlacedResourceX(va, desc, state, clear, riid, out). The state is 0xFFFFFFFF, which is not a
// PC D3D12_RESOURCE_STATES, and the virtual address belongs to a physical-page range the title just reserved. This
// stand-in creates a committed DEFAULT-heap resource from the description instead of placing it at `va`
// (docs/retail_boot_chain.md §3.4): the title holds a resource and a VA it can write, but the two are not the same
// memory, so what it writes there does not reach the GPU. A real implementation needs the page mapper (#5378) or an
// imported host allocation; this one is what gets the boot past the call.
HRESULT proxy_create_placed_resource_x(DeviceProxy* self, std::uint64_t va, const D3D12_RESOURCE_DESC* xbox_desc,
                                       std::uint64_t state_word, const D3D12_CLEAR_VALUE* clear_value,
                                       const IID* riid, void** out) {
    note_slot(133);
    if (!out) return E_POINTER;
    *out = nullptr;
    if (!xbox_desc) return E_POINTER;

    D3D12_RESOURCE_DESC pc_desc = *xbox_desc;
    const auto xbox_flags = static_cast<std::uint32_t>(pc_desc.Flags);
    constexpr D3D12_RESOURCE_FLAGS kPcFlags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET |
                                              D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL |
                                              D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS |
                                              D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE |
                                              D3D12_RESOURCE_FLAG_ALLOW_CROSS_ADAPTER |
                                              D3D12_RESOURCE_FLAG_ALLOW_SIMULTANEOUS_ACCESS;
    pc_desc.Flags = static_cast<D3D12_RESOURCE_FLAGS>(xbox_flags & kPcFlags);
    pc_desc.Layout = pc_desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER ? D3D12_TEXTURE_LAYOUT_ROW_MAJOR
                                                                         : D3D12_TEXTURE_LAYOUT_UNKNOWN;

    static std::atomic<bool> logged{false};
    if (first_call(logged)) {
        std::fprintf(stderr,
                     "[rt] d3d12_xs: slot 133 placed resource at VA 0x%llx: dimension=%d %llux%u state 0x%llx flags "
                     "0x%x -> committed DEFAULT, PC flags 0x%x, state COMMON (not tied to the VA)\n",
                     static_cast<unsigned long long>(va), static_cast<int>(pc_desc.Dimension),
                     static_cast<unsigned long long>(pc_desc.Width), static_cast<unsigned>(pc_desc.Height),
                     static_cast<unsigned long long>(state_word), xbox_flags,
                     static_cast<unsigned>(pc_desc.Flags));
    }

    D3D12_HEAP_PROPERTIES heap{};
    heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    heap.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
    heap.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
    heap.CreationNodeMask = 1;
    heap.VisibleNodeMask = 1;
    // The clear value is only legal for a render-target or depth-stencil resource, which is what the title passes it for.
    return self->real->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &pc_desc, D3D12_RESOURCE_STATE_COMMON,
                                               clear_value, __uuidof(ID3D12Resource), out);
}

// Xbox device slot 26: GetCustomHeapProperties. MSVC's x64 ABI returns D3D12_HEAP_PROPERTIES by value, so the first
// register holds the caller's buffer and the second the device: the console call `(output, device, node mask, heap
// type)` is that same ABI, not the swapped order docs/retail_boot_chain.md §3.1 assumed, and the generic forward below
// reads the device's `real` pointer out of the title's buffer and faults (measured on 1.0.2.1). This thunk therefore
// takes the first word as the output and the second as the proxy, and recognises the other order as well: the second
// word is only a proxy pointer when the caller did not use the hidden-return form.
std::uint64_t proxy_get_custom_heap_properties(void* first, std::uint64_t second, std::uint64_t third,
                                              std::uint64_t fourth) {
    note_slot(26);
    const bool hidden_return = !is_device_proxy(first) && is_device_proxy(reinterpret_cast<void*>(second));
    auto* proxy = static_cast<DeviceProxy*>(hidden_return ? reinterpret_cast<void*>(second) : first);
    auto* out = static_cast<D3D12_HEAP_PROPERTIES*>(hidden_return ? first : reinterpret_cast<void*>(second));

    static std::atomic<bool> logged{false};
    if (first_call(logged))
        std::fprintf(stderr,
                     "[rt] d3d12_xs: slot 26 GetCustomHeapProperties%s: out %p node mask 0x%llx heap type %llu\n",
                     hidden_return ? " (hidden return buffer)" : "", static_cast<void*>(out),
                     static_cast<unsigned long long>(third), static_cast<unsigned long long>(fourth));
    if (proxy && out) {
        const D3D12_HEAP_PROPERTIES properties =
            proxy->real->GetCustomHeapProperties(static_cast<UINT>(third), static_cast<D3D12_HEAP_TYPE>(fourth));
        *out = properties;
    }
    // The console shape returns the buffer it was handed; the PC method leaves nothing in RAX for the caller to read.
    return reinterpret_cast<std::uint64_t>(out);
}

// The Xbox routes at their configured indices. Returns false when `slot` is none of them. Slot 70 asks too, because an
// override that moves a route onto 70 must win over the built-in 70 handler.
// Device slot 81 (measured on the retail title; docs/2026-11-24_overnight_bringup.md §9.2b): the title's resource
// finalizer calls it as (a ring-buffer token, flags 0x11100, &count, out[]) with *count = 6 on entry, then reads
// out[i] + 0 as a page size (0x10000 or 0x1000), out[i] + 8 as a flag byte and out[i] + 0x18 as a handle, stepping by
// the 0x38 bytes of D3D12_RESOURCE_DESC. A non-zero count sends it on to write page-table entries. The ABI table lists
// 78..81 as frame pacing, GetFrameStatisticsX last, which does not match this shape at all, so the slot's meaning is
// open; what it answers is what decides. LEONIDA_SLOT81 picks the answer while that is being measured:
//
//   unset (or anything else)  refuse as before: E_NOTIMPL
//   zero                      S_OK with no pages to report, which keeps the caller's page-table walk out of the boot
//   page64                    S_OK with one 64 KiB page and a null handle
std::uint64_t proxy_slot_81(std::uint64_t flags, std::uint64_t count_word, std::uint64_t out_word) {
    static const std::string answer = [] {
        char value[32] = {};
        const DWORD n = GetEnvironmentVariableA("LEONIDA_SLOT81", value, sizeof value);
        return n ? std::string(value, n) : std::string();
    }();
    if (answer != "zero" && answer != "page64") return hr_to_u64(E_NOTIMPL);
    auto* const count = reinterpret_cast<std::uint32_t*>(count_word);
    if (!count) return hr_to_u64(E_POINTER);
    const std::uint32_t offered = *count;
    if (answer == "zero") {
        *count = 0;
    } else {
        auto* const out = reinterpret_cast<std::uint8_t*>(out_word);
        if (!out || !offered) {
            *count = 0;
        } else {
            std::memset(out, 0, 0x38);
            *reinterpret_cast<std::uint32_t*>(out) = 0x10000;  // the console's 64 KiB graphics page
            *count = 1;
        }
    }
    static std::atomic<bool> logged{false};
    if (first_call(logged))
        std::fprintf(stderr, "[rt] d3d12_xs: slot 81 answered \"%s\" (flags 0x%llx, offered %u -> %u)\n",
                     answer.c_str(), static_cast<unsigned long long>(flags), offered, *count);
    return hr_to_u64(S_OK);
}

bool route_xbox_device_slot(DeviceProxy* self, unsigned slot, std::uint64_t a1, std::uint64_t a2, std::uint64_t a3,
                            std::uint64_t a4, std::uint64_t a5, std::uint64_t& result) {
    const XboxDeviceSlotRoutes& routes = xbox_device_slot_routes();
    if (slot == 81) {
        log_xbox_route_once(slot, "console page-size query", a1, a2, a3, a4, a5);
        result = proxy_slot_81(a2, a3, a4);
    } else if (slot == routes.frame_interval) {
        log_xbox_route_once(slot, "SetFrameIntervalX", a1, a2, a3, a4, a5);
        result = hr_to_u64(proxy_set_frame_interval_x(self, a1, a2, a3, a4));
    } else if (slot == routes.schedule_event) {
        log_xbox_route_once(slot, "ScheduleFrameEventX", a1, a2, a3, a4, a5);
        result = hr_to_u64(proxy_schedule_frame_event_x(self, a1, a2, a4));
    } else if (slot == routes.wait_event) {
        log_xbox_route_once(slot, "WaitFrameEventX", a1, a2, a3, a4, a5);
        result = hr_to_u64(proxy_wait_frame_event_x(self, a1, a2, a4, a5));
    } else if (slot == routes.queue_x) {
        log_xbox_route_once(slot, "CreateCommandQueueX", a1, a2, a3, a4, a5);
        // The sanitising path: the console description's flag and node words are not PC values (stop 8).
        result = hr_to_u64(proxy_create_command_queue_x(self, a1, reinterpret_cast<const IID*>(a2),
                                                        reinterpret_cast<void**>(a3)));
    } else if (slot == routes.list_x) {
        log_xbox_route_once(slot, "CreateCommandListX", a1, a2, a3, a4, a5);
        result = hr_to_u64(proxy_create_command_list_x(self, a1, a2, a3, reinterpret_cast<const IID*>(a4),
                                                       reinterpret_cast<void**>(a5)));
    } else {
        return false;
    }
    return true;
}
// Xbox device slot 127 (RT-5 stop 15, measured by Pipodi in #6982): the argument list of ID3D12Device10::
// CreateCommittedResource3, (heap properties, heap flags, desc1, initial layout, clear value, protected session,
// castable format count, castable formats, riid, out), with the Xbox layout word in desc1.Layout and Xbox bits above
// the PC flags in desc1.Flags. The slot is past the PC device's vtable, so the generic thunk could only refuse it. The
// PC device creates the resource through its own CreateCommittedResource3 (PC slot 76) with the layout word replaced
// as slot 76 does and the flags limited to the PC bits. The initial layout, clear value, session and castable list go
// on as the title passed them; 0xFFFFFFFF is D3D12_BARRIER_LAYOUT_UNDEFINED, the value the console passes for the
// state of slot 133 as well. The words take the positions the thunk received them in (see the top of the file).
constexpr unsigned kCreateCommittedResource3X = 127;
constexpr unsigned kPcCreateCommittedResource3 = 76;  // ID3D12Device10, the PC device's 77th method
// D3D12_RESOURCE_FLAG_USE_TIGHT_ALIGNMENT: a PC bit the Windows SDK 10.0.26100 header does not name yet; the PC runtime
// accepts it for committed resources (D3D12 tight alignment spec), so a title's request must reach it.
constexpr std::uint32_t kPcResourceFlagUseTightAlignment = 0x400;
constexpr std::uint32_t kPcResourceFlagsMask =
    D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET | D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL |
    D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS | D3D12_RESOURCE_FLAG_DENY_SHADER_RESOURCE |
    D3D12_RESOURCE_FLAG_ALLOW_CROSS_ADAPTER | D3D12_RESOURCE_FLAG_ALLOW_SIMULTANEOUS_ACCESS |
    D3D12_RESOURCE_FLAG_VIDEO_DECODE_REFERENCE_ONLY | D3D12_RESOURCE_FLAG_VIDEO_ENCODE_REFERENCE_ONLY |
    D3D12_RESOURCE_FLAG_RAYTRACING_ACCELERATION_STRUCTURE | kPcResourceFlagUseTightAlignment;

HRESULT proxy_create_committed_resource3_x(DeviceProxy* self, std::uint64_t heap_word, std::uint64_t heap_flags,
                                           std::uint64_t desc_word, std::uint64_t layout_word,
                                           std::uint64_t clear_word, std::uint64_t session_word,
                                           std::uint64_t castable_count, std::uint64_t castable_formats,
                                           std::uint64_t riid_word, std::uint64_t out_word) {
    note_slot(kCreateCommittedResource3X);
    auto* const out = reinterpret_cast<void**>(out_word);
    if (out) *out = nullptr;  // _COM_Outptr_: NULL on every failure, as slots 14/62/76
    const auto* const xbox_desc = reinterpret_cast<const D3D12_RESOURCE_DESC1*>(desc_word);
    const auto* const riid = reinterpret_cast<const IID*>(riid_word);
    if (!out || !heap_word || !xbox_desc || !riid) return E_POINTER;
    if (self->slot_count <= kPcCreateCommittedResource3) {  // the PC device predates ID3D12Device10
        static std::atomic<bool> warned{false};
        if (first_call(warned))
            std::fprintf(stderr,
                         "[rt] d3d12_xs: device slot %u (%s) needs ID3D12Device10 and the PC device has %u slots; "
                         "returning E_NOTIMPL\n",
                         kCreateCommittedResource3X, slot_name(kCreateCommittedResource3X), self->slot_count);
        return E_NOTIMPL;
    }

    D3D12_RESOURCE_DESC1 pc_desc = *xbox_desc;
    const auto xbox_layout = static_cast<std::uint32_t>(pc_desc.Layout);
    const auto xbox_flags = static_cast<std::uint32_t>(pc_desc.Flags);
    pc_desc.Layout = pc_desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER ? D3D12_TEXTURE_LAYOUT_ROW_MAJOR
                                                                         : D3D12_TEXTURE_LAYOUT_UNKNOWN;
    pc_desc.Flags = static_cast<D3D12_RESOURCE_FLAGS>(xbox_flags & kPcResourceFlagsMask);

    static std::atomic<bool> logged{false};
    if (first_call(logged))
        std::fprintf(stderr,
                     "[rt] d3d12_xs: slot 127 resource: dimension=%d xbox layout=0x%x -> PC layout=0x%x, xbox flags "
                     "0x%x -> PC flags 0x%x\n",
                     static_cast<int>(pc_desc.Dimension), xbox_layout, static_cast<unsigned>(pc_desc.Layout),
                     xbox_flags, static_cast<unsigned>(pc_desc.Flags));

    using Fn = HRESULT(WINAPI*)(ID3D12Device*, const D3D12_HEAP_PROPERTIES*, D3D12_HEAP_FLAGS,
                                const D3D12_RESOURCE_DESC1*, std::uint32_t, const D3D12_CLEAR_VALUE*,
                                ID3D12ProtectedResourceSession*, std::uint32_t, const DXGI_FORMAT*, const IID*,
                                void**);
    // The ID3D12Device10 interface pointer, not the base device's: COM only promises that QueryInterface on the
    // original pointer yields that interface, and a different pointer has its own vtable. Released after the call.
    void* device10 = nullptr;
#ifdef __ID3D12Device10_INTERFACE_DEFINED__
    if (FAILED(self->real->QueryInterface(__uuidof(ID3D12Device10), &device10))) device10 = nullptr;
#endif
    if (!device10) return E_NOTIMPL;
    const auto create = reinterpret_cast<Fn>((*reinterpret_cast<void***>(device10))[kPcCreateCommittedResource3]);
    const HRESULT hr =
        create(static_cast<ID3D12Device*>(device10), reinterpret_cast<const D3D12_HEAP_PROPERTIES*>(heap_word),
               static_cast<D3D12_HEAP_FLAGS>(static_cast<std::uint32_t>(heap_flags)), &pc_desc,
               static_cast<std::uint32_t>(layout_word), reinterpret_cast<const D3D12_CLEAR_VALUE*>(clear_word),
               reinterpret_cast<ID3D12ProtectedResourceSession*>(session_word),
               static_cast<std::uint32_t>(castable_count), reinterpret_cast<const DXGI_FORMAT*>(castable_formats), riid,
               out);
    static_cast<IUnknown*>(device10)->Release();
    if (SUCCEEDED(hr)) {
        D3D12_RESOURCE_DESC plain{};  // D3D12_RESOURCE_DESC1 without SamplerFeedbackMipRegion
        plain.Dimension = pc_desc.Dimension;
        plain.Alignment = pc_desc.Alignment;
        plain.Width = pc_desc.Width;
        plain.Height = pc_desc.Height;
        plain.DepthOrArraySize = pc_desc.DepthOrArraySize;
        plain.MipLevels = pc_desc.MipLevels;
        plain.Format = pc_desc.Format;
        plain.SampleDesc = pc_desc.SampleDesc;
        plain.Layout = pc_desc.Layout;
        plain.Flags = pc_desc.Flags;
        lease_title_resource_memory(self, *out, plain);
    }
    return hr;
}



// Xbox slot 86: CreatePipelineState(desc, riid, out), forwarded to PC ID3D12Device2::CreatePipelineState (PC slot 47).
// The stream is not re-encoded: the console's layout is close enough to the PC one that the layer parses it, and where
// the two differ the parse is what reports it (d3d12_vulkan_device.cpp, parse_pipeline_stream, dumps the stream).
HRESULT proxy_create_pipeline_state(DeviceProxy* self, const D3D12_PIPELINE_STATE_STREAM_DESC* desc, const IID* riid,
                                    void** out) {
    note_slot(86);
    if (out) *out = nullptr;
    if (self->slot_count <= 47) return refuse_beyond_device(86, self->slot_count);  // predates ID3D12Device2
    if (!riid) return E_POINTER;
    using Fn = HRESULT(WINAPI*)(ID3D12Device*, const D3D12_PIPELINE_STATE_STREAM_DESC*, const IID*, void**);
    return reinterpret_cast<Fn>(self->real_vtable[47])(self->real, desc, riid, out);
}

// Xbox slot 101: CreateStateObject(desc, riid, out), forwarded to PC ID3D12Device5::CreateStateObject (PC slot 62,
// displaced on Xbox by compute-PSO slot 62).
HRESULT proxy_create_state_object(DeviceProxy* self, const D3D12_STATE_OBJECT_DESC* desc, const IID* riid, void** out) {
    note_slot(101);
    if (out) *out = nullptr;
    if (self->slot_count <= 62) return refuse_beyond_device(101, self->slot_count);  // predates ID3D12Device5
    if (!riid) return E_POINTER;
    using Fn = HRESULT(WINAPI*)(ID3D12Device*, const D3D12_STATE_OBJECT_DESC*, const IID*, void**);
    return reinterpret_cast<Fn>(self->real_vtable[62])(self->real, desc, riid, out);
}

// Xbox slot 108: GetResourceAllocationInfo2, forwarded to PC ID3D12Device8::GetResourceAllocationInfo2 (PC slot 68).
// The retail executable calls struct-returning device methods with the C ABI (return buffer in RCX, device proxy in
// RDX); a PC C++ caller passes the proxy in RCX and the return buffer in RDX. Both orders are accepted. The title's
// descriptor builder populates 56-byte D3D12_RESOURCE_DESC entries, so the input array is indexed with a 56-byte
// stride (sizeof(D3D12_RESOURCE_DESC)), each entry is expanded into a zeroed D3D12_RESOURCE_DESC1 with
// SamplerFeedbackMipRegion cleared, and Xbox layout words (e.g. 0x101, 0x105) and console flag bits above the PC
// D3D12_RESOURCE_FLAGS are normalized on the copy before the PC call.
std::uint64_t proxy_get_resource_allocation_info2(DeviceProxy* self, std::uint64_t a1, std::uint64_t a2,
                                                  std::uint64_t a3, std::uint64_t a4, std::uint64_t a5) {
    note_slot(108);
    DeviceProxy* proxy = nullptr;
    D3D12_RESOURCE_ALLOCATION_INFO* ret = nullptr;
    if (is_device_proxy(self)) {
        proxy = self;
        ret = reinterpret_cast<D3D12_RESOURCE_ALLOCATION_INFO*>(a1);
    } else if (is_device_proxy(reinterpret_cast<const void*>(a1))) {
        proxy = reinterpret_cast<DeviceProxy*>(a1);
        ret = reinterpret_cast<D3D12_RESOURCE_ALLOCATION_INFO*>(self);
    }
    if (!ret) return 0;
    if (!proxy) {
        *ret = {UINT64_MAX, 0};
        return reinterpret_cast<std::uint64_t>(ret);
    }
    if (proxy->slot_count <= 68) {
        refuse_beyond_device(108, proxy->slot_count);
        *ret = {UINT64_MAX, 0};
        return reinterpret_cast<std::uint64_t>(ret);
    }
    const auto visible_mask = static_cast<UINT>(a2);
    const auto count = static_cast<UINT>(a3);
    const auto* raw_descs = reinterpret_cast<const D3D12_RESOURCE_DESC*>(a4);
    auto* info1 = reinterpret_cast<D3D12_RESOURCE_ALLOCATION_INFO1*>(a5);

    std::vector<D3D12_RESOURCE_DESC1> cleaned;
    const D3D12_RESOURCE_DESC1* descs_to_pass = nullptr;
    if (raw_descs && count > 0) {
        try {
            cleaned.resize(count);
        } catch (...) {
            *ret = {UINT64_MAX, 0};
            return reinterpret_cast<std::uint64_t>(ret);
        }
        for (UINT i = 0; i < count; ++i) {
            D3D12_RESOURCE_DESC1 d{};
            std::memcpy(&d, &raw_descs[i], sizeof(D3D12_RESOURCE_DESC));
            d.SamplerFeedbackMipRegion = {};
            if (static_cast<std::uint32_t>(d.Layout) >
                static_cast<std::uint32_t>(D3D12_TEXTURE_LAYOUT_64KB_STANDARD_SWIZZLE)) {
                d.Layout = d.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER ? D3D12_TEXTURE_LAYOUT_ROW_MAJOR
                                                                          : D3D12_TEXTURE_LAYOUT_UNKNOWN;
            }
            constexpr std::uint32_t kPcResourceFlagsMask = 0x1FFu;
            d.Flags = static_cast<D3D12_RESOURCE_FLAGS>(static_cast<std::uint32_t>(d.Flags) & kPcResourceFlagsMask);
            cleaned[i] = d;
        }
        descs_to_pass = cleaned.data();
    }

    using Fn = D3D12_RESOURCE_ALLOCATION_INFO*(WINAPI*)(
        ID3D12Device*, D3D12_RESOURCE_ALLOCATION_INFO*, UINT, UINT, const D3D12_RESOURCE_DESC1*,
        D3D12_RESOURCE_ALLOCATION_INFO1*);
    reinterpret_cast<Fn>(proxy->real_vtable[68])(proxy->real, ret, visible_mask, count, descs_to_pass, info1);
    return reinterpret_cast<std::uint64_t>(ret);
}

template <unsigned Slot>
std::uint64_t WINAPI proxy_slot(DeviceProxy* self, std::uint64_t a1, std::uint64_t a2, std::uint64_t a3,
                                std::uint64_t a4, std::uint64_t a5, std::uint64_t a6, std::uint64_t a7,
                                std::uint64_t a8, std::uint64_t a9, std::uint64_t a10, std::uint64_t a11,
                                std::uint64_t a12, std::uint64_t a13, std::uint64_t a14) {
    if constexpr (Slot == 0) {
        return hr_to_u64(proxy_query_interface(self, reinterpret_cast<const IID*>(a1),
                                               reinterpret_cast<void**>(a2)));
    } else if constexpr (Slot == 1) {
        return proxy_add_ref(self);
    } else if constexpr (Slot == 2) {
        return proxy_release(self);
    } else if constexpr (Slot == 26) {
        return proxy_get_custom_heap_properties(self, a1, a2, a3);
    } else if constexpr (Slot == 8) {
        return hr_to_u64(proxy_create_command_queue(self, reinterpret_cast<const D3D12_COMMAND_QUEUE_DESC*>(a1),
                                                    reinterpret_cast<const IID*>(a2), reinterpret_cast<void**>(a3)));
    } else if constexpr (Slot == 9) {
        return hr_to_u64(proxy_create_command_allocator(self, a1, reinterpret_cast<const IID*>(a2),
                                                        reinterpret_cast<void**>(a3)));
    } else if constexpr (Slot == 12) {
        return hr_to_u64(proxy_create_command_list(self, a1, a2, a3, a4, reinterpret_cast<const IID*>(a5),
                                                   reinterpret_cast<void**>(a6)));
    } else if constexpr (Slot == 36) {
        return hr_to_u64(proxy_create_fence(self, a1, a2, reinterpret_cast<const IID*>(a3),
                                            reinterpret_cast<void**>(a4)));
    } else if constexpr (Slot == 32) {
        return hr_to_u64(proxy_open_shared_handle(self, a1, reinterpret_cast<const IID*>(a2),
                                                  reinterpret_cast<void**>(a3)));
    } else if constexpr (Slot == 31 || Slot == 34 || Slot == 35 || Slot == 45 || Slot == 46 || Slot == 50) {
        return hr_to_u64(proxy_unwrapping_call<Slot>(self, a1, a2, a3, a4, a5));
    } else if constexpr (Slot == 75) {
        return hr_to_u64(proxy_create_command_queue1(self, a1, a2, reinterpret_cast<const IID*>(a3),
                                                     reinterpret_cast<void**>(a4)));
    } else if constexpr (Slot == 14) {
        return hr_to_u64(proxy_create_descriptor_heap(
            self, reinterpret_cast<const D3D12_DESCRIPTOR_HEAP_DESC*>(a1),
            reinterpret_cast<const IID*>(a2), reinterpret_cast<void**>(a3)));
    } else if constexpr (Slot == 17) {
        proxy_create_cbv(self, reinterpret_cast<const D3D12_CONSTANT_BUFFER_VIEW_DESC*>(a1),
                         D3D12_CPU_DESCRIPTOR_HANDLE{static_cast<SIZE_T>(a2)});
        return 0;
    } else if constexpr (Slot == 18) {
        proxy_create_srv(self, reinterpret_cast<ID3D12Resource*>(a1),
                         reinterpret_cast<const D3D12_SHADER_RESOURCE_VIEW_DESC*>(a2),
                         D3D12_CPU_DESCRIPTOR_HANDLE{static_cast<SIZE_T>(a3)});
        return 0;
    } else if constexpr (Slot == 19) {
        proxy_create_uav(self, reinterpret_cast<ID3D12Resource*>(a1), reinterpret_cast<ID3D12Resource*>(a2),
                         reinterpret_cast<const D3D12_UNORDERED_ACCESS_VIEW_DESC*>(a3),
                         D3D12_CPU_DESCRIPTOR_HANDLE{static_cast<SIZE_T>(a4)});
        return 0;
    } else if constexpr (Slot == 20) {
        proxy_create_rtv(self, reinterpret_cast<ID3D12Resource*>(a1),
                         reinterpret_cast<const D3D12_RENDER_TARGET_VIEW_DESC*>(a2),
                         D3D12_CPU_DESCRIPTOR_HANDLE{static_cast<SIZE_T>(a3)});
        return 0;
    } else if constexpr (Slot == 21) {
        proxy_create_dsv(self, reinterpret_cast<ID3D12Resource*>(a1),
                         reinterpret_cast<const D3D12_DEPTH_STENCIL_VIEW_DESC*>(a2),
                         D3D12_CPU_DESCRIPTOR_HANDLE{static_cast<SIZE_T>(a3)});
        return 0;
    } else if constexpr (Slot == 22) {
        proxy_create_sampler(self, reinterpret_cast<const D3D12_SAMPLER_DESC*>(a1),
                             D3D12_CPU_DESCRIPTOR_HANDLE{static_cast<SIZE_T>(a2)});
        return 0;
    } else if constexpr (Slot == 23) {
        proxy_copy_descriptors(self, static_cast<UINT>(a1), reinterpret_cast<const D3D12_CPU_DESCRIPTOR_HANDLE*>(a2),
                               reinterpret_cast<const UINT*>(a3), static_cast<UINT>(a4),
                               reinterpret_cast<const D3D12_CPU_DESCRIPTOR_HANDLE*>(a5),
                               reinterpret_cast<const UINT*>(a6),
                               static_cast<D3D12_DESCRIPTOR_HEAP_TYPE>(static_cast<UINT>(a7)));
        return 0;
    } else if constexpr (Slot == 24) {
        proxy_copy_descriptors_simple(self, static_cast<UINT>(a1),
                                      D3D12_CPU_DESCRIPTOR_HANDLE{static_cast<SIZE_T>(a2)},
                                      D3D12_CPU_DESCRIPTOR_HANDLE{static_cast<SIZE_T>(a3)},
                                      static_cast<D3D12_DESCRIPTOR_HEAP_TYPE>(static_cast<UINT>(a4)));
        return 0;
    } else if constexpr (Slot == 62) {
        return hr_to_u64(proxy_create_compute_pipeline_state(
            self, reinterpret_cast<const D3D12_COMPUTE_PIPELINE_STATE_DESC*>(a1), a2, a3,
            reinterpret_cast<const IID*>(a4), reinterpret_cast<void**>(a5)));
    } else if constexpr (Slot == 41) {
        return hr_to_u64(proxy_create_command_signature(
            self, reinterpret_cast<const D3D12_COMMAND_SIGNATURE_DESC*>(a1), a2, reinterpret_cast<const IID*>(a3),
            reinterpret_cast<void**>(a4)));
    } else if constexpr (Slot == 61) {
        proxy_create_sampler_x(self, reinterpret_cast<const D3D12_SAMPLER_DESC*>(a1), a2);
        return 0;
    } else if constexpr (Slot == 67) {
        return hr_to_u64(proxy_create_compute_pipeline_from_native_code(
            self, reinterpret_cast<const void*>(a1), reinterpret_cast<const IID*>(a2),
            reinterpret_cast<void**>(a3)));
    } else if constexpr (Slot == 70) {
        std::uint64_t routed = 0;
        if (route_xbox_device_slot(self, Slot, a1, a2, a3, a4, a5, routed)) return routed;  // an override moved a route here
        proxy_create_srv_from_raw_x(self, a1, a2, a3);
        return 0;
    } else if constexpr (Slot == 76) {
        return hr_to_u64(proxy_create_resource(
            self, reinterpret_cast<const D3D12_HEAP_PROPERTIES*>(a1),
            static_cast<D3D12_HEAP_FLAGS>(static_cast<std::uint32_t>(a2)),
            reinterpret_cast<const D3D12_RESOURCE_DESC*>(a3),
            static_cast<D3D12_RESOURCE_STATES>(static_cast<std::uint32_t>(a4)),
            reinterpret_cast<const D3D12_CLEAR_VALUE*>(a5), reinterpret_cast<const IID*>(a6),
            reinterpret_cast<void**>(a7)));
    } else if constexpr (Slot == 133) {
        return hr_to_u64(proxy_create_placed_resource_x(
            self, a1, reinterpret_cast<const D3D12_RESOURCE_DESC*>(a2), a3,
            reinterpret_cast<const D3D12_CLEAR_VALUE*>(a4), reinterpret_cast<const IID*>(a5),
            reinterpret_cast<void**>(a6)));
    } else if constexpr (Slot == kCreateCommittedResource3X) {
        return hr_to_u64(proxy_create_committed_resource3_x(self, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10));
    } else if constexpr (Slot == 74) {
        // PR #7323: a console call with no PC counterpart. The PC slot 74 is ShaderCacheControl(kinds, control), which
        // reads the first word as flags and refuses with E_INVALIDARG; nothing is written here and the PC device is not
        // called. The measured boot asks it once and treats a failure as fatal.
        note_slot(74);
        return hr_to_u64(S_OK);
    } else if constexpr (Slot == 86) {
        return hr_to_u64(proxy_create_pipeline_state(
            self, reinterpret_cast<const D3D12_PIPELINE_STATE_STREAM_DESC*>(a1),
            reinterpret_cast<const IID*>(a2), reinterpret_cast<void**>(a3)));
    } else if constexpr (Slot == 101) {
        return hr_to_u64(proxy_create_state_object(
            self, reinterpret_cast<const D3D12_STATE_OBJECT_DESC*>(a1),
            reinterpret_cast<const IID*>(a2), reinterpret_cast<void**>(a3)));
    } else if constexpr (Slot == 108) {
        return proxy_get_resource_allocation_info2(self, a1, a2, a3, a4, a5);
    } else {
        note_slot(Slot);
        // Xbox device slots, before the PC vtable is indexed: the public HLE's 78/79/80 would otherwise reach
        // CreateReservedResource2/CreateSampler2 or fall past an 80-slot table, and 48/73 would reach unrelated PC
        // methods. The check is before the slot-count refusal because slot 80 is outside a PC ID3D12Device11 table.
        std::uint64_t routed = 0;
        if (route_xbox_device_slot(self, Slot, a1, a2, a3, a4, a5, routed)) return routed;
        if (Slot >= self->slot_count) {
            // What the console-only slot was called with, once: a slot past the PC vtable has no known signature, and
            // the arguments are what identify it (a description, two out pointers, a count).
            static std::atomic<bool> logged{false};
            if (!logged.exchange(true, std::memory_order_relaxed))
                std::fprintf(stderr,
                             "[rt] d3d12_xs: device slot %u first call arguments: 0x%llx 0x%llx 0x%llx 0x%llx 0x%llx\n",
                             Slot, static_cast<unsigned long long>(a1), static_cast<unsigned long long>(a2),
                             static_cast<unsigned long long>(a3), static_cast<unsigned long long>(a4),
                             static_cast<unsigned long long>(a5));
            return hr_to_u64(refuse_beyond_device(Slot, self->slot_count, _ReturnAddress()));
        }

        // The PC vtable entry, called with the PC device as `this`. Integer/pointer arguments, the hidden
        // struct-return pointer and any stack arguments keep their positions; XMM registers are untouched.
        using ForwardFn = std::uint64_t(WINAPI*)(void*, std::uint64_t, std::uint64_t, std::uint64_t,
                                                 std::uint64_t, std::uint64_t, std::uint64_t, std::uint64_t,
                                                 std::uint64_t, std::uint64_t, std::uint64_t, std::uint64_t,
                                                 std::uint64_t, std::uint64_t, std::uint64_t);
        auto fn = reinterpret_cast<ForwardFn>(self->real_vtable[Slot]);
        return fn(self->real, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11, a12, a13, a14);
    }
}

template <std::size_t... I>
void fill_vtable(void** vtable, std::index_sequence<I...>) {
    ((vtable[I] = reinterpret_cast<void*>(&proxy_slot<static_cast<unsigned>(I)>) ), ...);
}

// ---- Struct-returning device methods: the console title calls them with the C ABI ----
//
// The PC vtable (MSVC C++) takes `this` in RCX and, for a struct return, the hidden return pointer in RDX. The console
// title does not. Measured with the retail game_scarlett_master.exe 1.0.2.1 under gta6rt --run: its first
// GetCustomHeapProperties call (device slot 26) is `rcx = return buffer on the caller's stack, rdx = the device,
// r8 = node mask 1, r9 = heap type` (DEFAULT, then UPLOAD), and it reads the result from that buffer. That is the C
// convention, where a struct wider than 8 bytes comes back through a hidden *first* argument (an 8-byte one comes back
// in RAX, which is what the heap proxy's slots 9/10 already measured). The generic forwarder took the return buffer
// for the device and crashed on its vtable.
//
// The runtime's own code and tests still call the proxy through d3d12.h, in the PC form, so these thunks accept both,
// as the heap proxy's 9/10 do: whichever of the first two arguments is the device proxy (its first word is the proxy
// vtable) is `this`, and the other is the return buffer. A return buffer never starts with that vtable pointer: the
// C-form caller hands over uninitialised memory to be written, and the PC-form caller is this runtime. The buffer is
// returned in RAX, as both conventions require.
//   slot 25 GetResourceAllocationInfo -> D3D12_RESOURCE_ALLOCATION_INFO (16 bytes): same rule as slot 26 (inferred,
//           no call site measured yet)
//   slot 26 GetCustomHeapProperties   -> D3D12_HEAP_PROPERTIES (20 bytes): measured; it has its own thunk above
//           (proxy_get_custom_heap_properties), not this one
// Not changed: slot 43 GetAdapterLuid (an 8-byte LUID; by the same rule the title would expect it in RAX, but no call
// site is measured yet, so it still forwards in the PC form).
void** get_vtable();

struct StructReturnCall {
    DeviceProxy* self;
    void* ret;
    bool console_abi;
};

StructReturnCall struct_return_call(std::uint64_t a0, std::uint64_t a1) {
    void** const vtable = get_vtable();
    if (a0 && *reinterpret_cast<void***>(a0) == vtable)  // PC form: this, return buffer
        return {reinterpret_cast<DeviceProxy*>(a0), reinterpret_cast<void*>(a1), false};
    return {reinterpret_cast<DeviceProxy*>(a1), reinterpret_cast<void*>(a0), true};  // C form: return buffer, this
}

// The console size of a list of descriptions (d3d12_xs_alloc_size.h): the PC device's answer with every console render
// target laid out at its console size, each at its own alignment. `info` is the PC answer and is replaced only when
// the console layout is larger. `each` is the per-description array of slot 56, or null.
void apply_console_placement(const D3D12_RESOURCE_DESC* descs, UINT count, D3D12_RESOURCE_ALLOCATION_INFO& info,
                             D3D12_RESOURCE_ALLOCATION_INFO1* each,
                             const std::function<D3D12_RESOURCE_ALLOCATION_INFO(UINT)>& pc_one, unsigned slot) {
    if (info.SizeInBytes == UINT64_MAX) return;
    D3D12_RESOURCE_ALLOCATION_INFO console{};
    if (!console_placement(descs, count, pc_one, console, each)) return;
    static std::atomic<bool> logged{false};
    if (!logged.exchange(true, std::memory_order_relaxed))
        std::fprintf(stderr,
                     "[rt] d3d12_xs: slot %u: render targets sized as console tiles, %llu B for %llu B on %u "
                     "description(s)\n",
                     slot, static_cast<unsigned long long>(console.SizeInBytes),
                     static_cast<unsigned long long>(info.SizeInBytes), static_cast<unsigned>(count));
    if (console.SizeInBytes >= info.SizeInBytes) info = console;
}

void* WINAPI proxy_get_resource_allocation_info(std::uint64_t a0, std::uint64_t a1, std::uint64_t visible_mask,
                                                std::uint64_t count, const D3D12_RESOURCE_DESC* descs) {
    note_slot(25);
    const StructReturnCall call = struct_return_call(a0, a1);
    ID3D12Device* const real = call.self->real;
    // Normalize console descriptors in a copy. PC calls can also pass the Xbox tiled layout explicitly. Otherwise
    // their flags and layout reach the host unchanged so the host can validate them.
    const UINT desc_count = static_cast<UINT>(count);
    std::unique_ptr<D3D12_RESOURCE_DESC[]> clean;
    if (descs && desc_count) {
        clean.reset(new (std::nothrow) D3D12_RESOURCE_DESC[desc_count]);
        if (!clean) {
            *static_cast<D3D12_RESOURCE_ALLOCATION_INFO*>(call.ret) = {UINT64_MAX, D3D12_DEFAULT_RESOURCE_PLACEMENT_ALIGNMENT};
            return call.ret;
        }
        for (UINT i = 0; i < desc_count; ++i) {
            const auto& desc = descs[i];
            clean[i] = call.console_abi || desc.Layout == kXboxTiledResourceLayout ? pc_resource_desc(desc) : desc;
        }
        descs = clean.get();
    }
    const UINT mask = static_cast<UINT>(visible_mask);
    D3D12_RESOURCE_ALLOCATION_INFO info = real->GetResourceAllocationInfo(mask, desc_count, descs);
    apply_console_placement(descs, desc_count, info, nullptr,
                            [&](UINT i) { return real->GetResourceAllocationInfo(mask, 1, &descs[i]); }, 25);
    *static_cast<D3D12_RESOURCE_ALLOCATION_INFO*>(call.ret) = info;
    return call.ret;
}

// Slot 56, ID3D12Device4::GetResourceAllocationInfo1: the same answer, and the per-description offsets follow the
// padded sizes, so a title that places its resources at those offsets does not overlap them. The call is forwarded
// as it came; the console layout then replaces the total and the offsets.
void* WINAPI proxy_get_resource_allocation_info1(std::uint64_t a0, std::uint64_t a1, std::uint64_t visible_mask,
                                                 std::uint64_t count, const D3D12_RESOURCE_DESC* descs,
                                                 D3D12_RESOURCE_ALLOCATION_INFO1* each) {
    const StructReturnCall call = struct_return_call(a0, a1);
    DeviceProxy* const self = call.self;
    note_slot(56);
    if (56 >= self->slot_count) {
        return reinterpret_cast<void*>(hr_to_u64(refuse_beyond_device(56, self->slot_count)));
    }
    using Fn = void*(WINAPI*)(void*, void*, UINT, UINT, const D3D12_RESOURCE_DESC*, D3D12_RESOURCE_ALLOCATION_INFO1*);
    const UINT n = static_cast<UINT>(count);
    // The per-resource array is optional (_Out_writes_opt_). The console total must not depend on whether the
    // caller asked for it, so without one the host's per-resource answers go to a local array (found by Alpha325).
    std::vector<D3D12_RESOURCE_ALLOCATION_INFO1> local;
    if (!each && descs && n) {
        try {
            local.resize(n);
            each = local.data();
        } catch (const std::bad_alloc&) {
            each = nullptr;  // the host total stands
        }
    }
    void* const returned = reinterpret_cast<Fn>(self->real_vtable[56])(
        self->real, call.ret, static_cast<UINT>(visible_mask), n, descs, each);
    if (each) {
        auto& info = *static_cast<D3D12_RESOURCE_ALLOCATION_INFO*>(call.ret);
        apply_console_placement(descs, n, info, each, [&](UINT i) {
            return D3D12_RESOURCE_ALLOCATION_INFO{each[i].SizeInBytes, each[i].Alignment};
        }, 56);
    }
    return returned;
}

void** get_vtable() {
    static void** vtable = [] {
        static void* storage[kMaxSlots];
        fill_vtable(storage, std::make_index_sequence<kMaxSlots>{});
        storage[25] = reinterpret_cast<void*>(&proxy_get_resource_allocation_info);
        storage[56] = reinterpret_cast<void*>(&proxy_get_resource_allocation_info1);
        return storage;
    }();
    return vtable;
}

bool supports(ID3D12Device* real, REFIID iid) {
    void* found = nullptr;
    const HRESULT hr = real->QueryInterface(iid, &found);
    if (FAILED(hr) || !found) return false;
    static_cast<IUnknown*>(found)->Release();
    return true;
}

// Highest device interface the PC runtime exposes; its cumulative slot count (from d3d12.h) bounds forwarding.
// Windows SDK 10.0.26100 defines ID3D12Device through ID3D12Device14 only; there is no Device15/Device16 header.
unsigned detect_slot_count(ID3D12Device* real) {
#ifdef __ID3D12Device14_INTERFACE_DEFINED__
    if (supports(real, __uuidof(ID3D12Device14))) return 83;
#endif
#ifdef __ID3D12Device13_INTERFACE_DEFINED__
    if (supports(real, __uuidof(ID3D12Device13))) return 82;
#endif
#ifdef __ID3D12Device12_INTERFACE_DEFINED__
    if (supports(real, __uuidof(ID3D12Device12))) return 81;
#endif
#ifdef __ID3D12Device11_INTERFACE_DEFINED__
    if (supports(real, __uuidof(ID3D12Device11))) return 80;
#endif
#ifdef __ID3D12Device10_INTERFACE_DEFINED__
    if (supports(real, __uuidof(ID3D12Device10))) return 79;
#endif
#ifdef __ID3D12Device9_INTERFACE_DEFINED__
    if (supports(real, __uuidof(ID3D12Device9))) return 76;
#endif
    if (supports(real, __uuidof(ID3D12Device8))) return 73;
    if (supports(real, __uuidof(ID3D12Device7))) return 68;
    if (supports(real, __uuidof(ID3D12Device6))) return 66;
    if (supports(real, __uuidof(ID3D12Device5))) return 65;
    if (supports(real, __uuidof(ID3D12Device4))) return 57;
    if (supports(real, __uuidof(ID3D12Device3))) return 51;
    if (supports(real, __uuidof(ID3D12Device2))) return 48;
    if (supports(real, __uuidof(ID3D12Device1))) return 47;
    return 44;
}

}  // namespace

HRESULT create_device_proxy(ID3D12Device* real, REFIID riid, void** out, bool console_lists) {
    if (!real) return E_POINTER;
    if (!out) return E_POINTER;
    *out = nullptr;

    DeviceProxy* proxy = new (std::nothrow) DeviceProxy;
    if (!proxy) return E_OUTOFMEMORY;
    proxy->vtable = get_vtable();
    proxy->real = real;
    proxy->real_vtable = *reinterpret_cast<void***>(real);
    proxy->slot_count = detect_slot_count(real);
    proxy->console_lists = console_lists;
    proxy->refs.store(1, std::memory_order_relaxed);
    proxy->dxgi_vtable = get_dxgi_vtable();
    proxy->adapter_provider = nullptr;
    proxy->resource_provider = nullptr;
    proxy->gpu_thread_priority.store(0, std::memory_order_relaxed);
    proxy->max_frame_latency.store(3, std::memory_order_relaxed);

    const unsigned slot_count = proxy->slot_count;
    HRESULT hr = proxy_query_interface(proxy, &riid, out);
    if (FAILED(hr)) {
        // No reference escaped, and the caller still owns its reference to `real`.
        delete proxy;
        return hr;
    }
    if (!d3d12_trace::enabled())
        std::fprintf(stderr, "[rt] d3d12_xs: device proxy %p wraps PC device %p (%u PC vtable slots)\n",
                     static_cast<void*>(proxy), static_cast<void*>(real), slot_count);
    proxy_release(proxy);  // drop the creation reference; `out` holds the caller's reference
    return S_OK;
}

bool is_device_proxy(const void* device) {
    return device && *reinterpret_cast<void* const*>(device) == reinterpret_cast<void*>(get_vtable());
}

void* device_proxy_from_dxgi_device(void* dxgi_device) {
    if (!dxgi_device || *static_cast<void***>(dxgi_device) != get_dxgi_vtable()) return nullptr;
    return dxgi_owner(dxgi_device);
}

void* device_proxy_primary(void* device) {
    if (is_device_proxy(device)) return device;
    return device_proxy_from_dxgi_device(device);
}

bool set_device_adapter_provider(void* device, DxgiAdapterProvider provider) {
    auto* proxy = static_cast<DeviceProxy*>(device_proxy_primary(device));
    if (!proxy) {
        // Not installing one silently would leave the host's adapter in its place.
        std::fprintf(stderr, "[rt] d3d12_xs: set_device_adapter_provider: %p is not a device proxy; no provider set\n",
                     device);
        return false;
    }
    proxy->adapter_provider = provider;
    return true;
}

bool set_device_resource_provider(void* device, ResourceByVaProvider provider) {
    auto* proxy = static_cast<DeviceProxy*>(device_proxy_primary(device));
    if (!proxy) {
        std::fprintf(stderr, "[rt] d3d12_xs: set_device_resource_provider: %p is not a device proxy; no provider set\n",
                     device);
        return false;
    }
    proxy->resource_provider = provider;
    return true;
}

bool is_heap_proxy(const void* heap) {
    return heap && *reinterpret_cast<void* const*>(heap) == reinterpret_cast<void*>(get_heap_vtable());
}

ID3D12DescriptorHeap* heap_proxy_real_heap(const void* heap) {
    if (!is_heap_proxy(heap)) return nullptr;
    return static_cast<const HeapProxy*>(heap)->real;
}

void set_descriptor_heap_allocator(DescriptorHeapBackingAlloc alloc, DescriptorHeapBackingFree free) {
    std::lock_guard lock(heap_alloc_mutex());
    g_heap_alloc = alloc;
    g_heap_free = free;
    g_heap_alloc_explicit = (alloc != nullptr || free != nullptr);
}

void set_default_descriptor_heap_allocator(DescriptorHeapBackingAlloc alloc, DescriptorHeapBackingFree free) {
    std::lock_guard lock(heap_alloc_mutex());
    if (g_heap_alloc_explicit) return;
    g_heap_alloc = alloc;
    g_heap_free = free;
}

D3D12_CPU_DESCRIPTOR_HANDLE translate_cpu_descriptor_handle(const void* device, D3D12_CPU_DESCRIPTOR_HANDLE handle) {
    const auto* proxy = is_device_proxy(device) ? static_cast<const DeviceProxy*>(device) : nullptr;
    return translate_cpu_handle(proxy, handle);
}

unsigned device_proxy_slot_count(const void* device) {
    if (!is_device_proxy(device)) return 0;
    return static_cast<const DeviceProxy*>(device)->slot_count;
}

ID3D12Device* device_proxy_real_device(const void* device) {
    if (!is_device_proxy(device)) return nullptr;
    return static_cast<const DeviceProxy*>(device)->real;
}

std::shared_ptr<leonida::FrameTokenSource> device_frame_tokens(const void* device) {
    if (!is_device_proxy(device)) return nullptr;
    return static_cast<const DeviceProxy*>(device)->frame_tokens;
}

bool device_slot_overridden(unsigned slot) {
    if (slot <= 2 || slot == 8 || slot == 9 || slot == 12 || slot == 14 || (slot >= 17 && slot <= 24) ||
        slot == 25 || slot == 26 || slot == 31 || slot == 32 || slot == 34 || slot == 35 || slot == 36 ||
        slot == 41 || slot == 45 || slot == 46 || slot == 50 || slot == 56 || slot == 61 || slot == 62 ||
        slot == 67 || slot == 70 || slot == 74 || slot == 75 || slot == 76 || slot == 81 || slot == 86 || slot == 101 ||
        slot == 108 || slot == 133 || slot == kCreateCommittedResource3X)
        return true;
    const XboxDeviceSlotRoutes& routes = xbox_device_slot_routes();
    return slot == routes.frame_interval || slot == routes.schedule_event || slot == routes.wait_event ||
           slot == routes.queue_x || slot == routes.list_x;
}

bool is_queue_proxy(const void* queue) { return is_child_proxy<ChildKind::queue>(queue); }

bool is_list_proxy(const void* list) { return is_list_proxy_impl(list); }

ID3D12CommandList* list_proxy_real_list(const void* list) {
    if (!is_list_proxy_impl(list)) return nullptr;
    return static_cast<const ListProxy*>(list)->real;
}

const char* list_tail_slot_name(unsigned slot, unsigned char console_layout) {
    return list_tail_name(slot, console_layout);
}
std::uint64_t list_slot_hits(unsigned slot) {
    return slot < kListSlots ? ListStats::hits[slot].load(std::memory_order_relaxed) : 0;
}
bool is_fence_proxy(const void* fence) { return is_child_proxy<ChildKind::fence>(fence); }
bool is_allocator_proxy(const void* allocator) { return is_child_proxy<ChildKind::allocator>(allocator); }

ID3D12CommandAllocator* allocator_proxy_real_allocator(const void* allocator) {
    if (!is_allocator_proxy(allocator)) return nullptr;
    return reinterpret_cast<ID3D12CommandAllocator*>(static_cast<const ChildProxy*>(allocator)->real);
}

ID3D12CommandQueue* queue_proxy_real_queue(const void* queue) {
    if (!is_queue_proxy(queue)) return nullptr;
    return reinterpret_cast<ID3D12CommandQueue*>(static_cast<const ChildProxy*>(queue)->real);
}

ID3D12Fence* fence_proxy_real_fence(const void* fence) {
    if (!is_fence_proxy(fence)) return nullptr;
    return reinterpret_cast<ID3D12Fence*>(static_cast<const ChildProxy*>(fence)->real);
}

unsigned queue_proxy_slot_count(const void* queue) {
    return is_queue_proxy(queue) ? static_cast<const ChildProxy*>(queue)->slot_count : 0;
}

unsigned fence_proxy_slot_count(const void* fence) {
    return is_fence_proxy(fence) ? static_cast<const ChildProxy*>(fence)->slot_count : 0;
}

unsigned queue_proxy_table_size() { return kQueueSlots; }
unsigned fence_proxy_table_size() { return kFenceSlots; }
unsigned allocator_proxy_table_size() { return kAllocatorSlots; }
unsigned allocator_proxy_slot_count(const void* allocator) {
    return is_allocator_proxy(allocator) ? static_cast<const ChildProxy*>(allocator)->slot_count : 0;
}

unsigned long command_object_proxy_ref_count(const void* object) {
    if (!is_queue_proxy(object) && !is_fence_proxy(object) && !is_allocator_proxy(object)) return 0;
    return static_cast<const ChildProxy*>(object)->refs.load(std::memory_order_relaxed);
}

std::uint64_t queue_slot_hits(unsigned slot) {
    using S = ChildStats<ChildKind::queue>;
    start_hit_counts();
    return slot < S::kSlots ? S::hits[slot].load(std::memory_order_relaxed) : 0;
}

bool queue_slot_first_call_seen(unsigned slot) {
    using S = ChildStats<ChildKind::queue>;
    return slot < S::kSlots && S::first[slot].load(std::memory_order_relaxed);
}

std::uint64_t fence_slot_hits(unsigned slot) {
    using S = ChildStats<ChildKind::fence>;
    start_hit_counts();
    return slot < S::kSlots ? S::hits[slot].load(std::memory_order_relaxed) : 0;
}

bool fence_slot_first_call_seen(unsigned slot) {
    using S = ChildStats<ChildKind::fence>;
    return slot < S::kSlots && S::first[slot].load(std::memory_order_relaxed);
}

std::uint64_t queue_table_overruns() {
    return ChildStats<ChildKind::queue>::overruns.load(std::memory_order_relaxed);
}

std::uint64_t fence_table_overruns() {
    return ChildStats<ChildKind::fence>::overruns.load(std::memory_order_relaxed);
}

bool command_object_tables_guarded() {
    get_child_vtable<ChildKind::queue>();
    get_child_vtable<ChildKind::fence>();
    get_child_vtable<ChildKind::allocator>();
    return ChildStats<ChildKind::queue>::table_end.load(std::memory_order_acquire) &&
           ChildStats<ChildKind::fence>::table_end.load(std::memory_order_acquire) &&
           ChildStats<ChildKind::allocator>::table_end.load(std::memory_order_acquire);
}

std::uint64_t allocator_slot_hits(unsigned slot) {
    using S = ChildStats<ChildKind::allocator>;
    start_hit_counts();
    return slot < S::kSlots ? S::hits[slot].load(std::memory_order_relaxed) : 0;
}

bool allocator_slot_first_call_seen(unsigned slot) {
    using S = ChildStats<ChildKind::allocator>;
    return slot < S::kSlots && S::first[slot].load(std::memory_order_relaxed);
}

std::uint64_t allocator_table_overruns() {
    return ChildStats<ChildKind::allocator>::overruns.load(std::memory_order_relaxed);
}

bool heap_slot_overridden(unsigned slot) {
    return slot <= 2 || slot == 7 || slot == 9 || slot == 10;
}

std::uint64_t device_slot_hits(unsigned slot) {
    start_hit_counts();
    return slot < kMaxSlots ? g_hits[slot].load(std::memory_order_relaxed) : 0;
}

bool device_slot_first_call_seen(unsigned slot) {
    return slot < kMaxSlots && g_first[slot].load(std::memory_order_relaxed);
}

unsigned long device_proxy_ref_count(const void* device) {
    if (!is_device_proxy(device)) return 0;
    return static_cast<const DeviceProxy*>(device)->refs.load(std::memory_order_relaxed);
}

std::uint64_t heap_slot_hits(unsigned slot) {
    start_hit_counts();
    return slot < kHeapSlots ? g_heap_hits[slot].load(std::memory_order_relaxed) : 0;
}

bool heap_slot_first_call_seen(unsigned slot) {
    return slot < kHeapSlots && g_heap_first[slot].load(std::memory_order_relaxed);
}

void reset_device_proxy_stats() {
    start_hit_counts();
    for (unsigned slot = 0; slot < kMaxSlots; ++slot) {
        g_hits[slot].store(0, std::memory_order_relaxed);
        g_first[slot].store(false, std::memory_order_relaxed);
        g_beyond_warned[slot].store(false, std::memory_order_relaxed);
    }
    for (unsigned slot = 0; slot < kHeapSlots; ++slot) {
        g_heap_hits[slot].store(0, std::memory_order_relaxed);
        g_heap_first[slot].store(false, std::memory_order_relaxed);
    }
    reset_child_stats<ChildKind::queue>();
    reset_child_stats<ChildKind::fence>();
    reset_child_stats<ChildKind::allocator>();
}

}  // namespace rt::d3d12_xs

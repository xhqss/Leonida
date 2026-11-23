// Checks the registered xmem.dll shim against the measured Xbox ABI.
//
// Every function is resolved through rt::shims::find, so the test exercises the same shim table and calling
// convention the runtime installs for xmem.dll imports instead of a private copy of the implementation. The checks
// cover what the measured call sites use: 64-byte-aligned allocations, XMemFree, working-set statistics with the
// size at +8, and XMemVirtualQuery's MEMORY_BASIC_INFORMATION prefix inside a zeroed 64-byte output.
#include <windows.h>
#include <psapi.h>

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>

#include "shims.h"
#include "startup_trace.h"

namespace {

using XMemAllocFn = void*(__stdcall*)(size_t, uint64_t);
using XMemFreeFn = void(__stdcall*)(void*, uint64_t);
using XMemVirtualQueryFn = SIZE_T(__stdcall*)(HANDLE, const void*, void*);
// BOOL XMemGetWorkingSetStatistics(UINT32 kind, stats*): the FSR DLL passes kind 0x20000001 at every call site.
using XMemGetWorkingSetStatisticsFn = BOOL(__stdcall*)(uint32_t, void*);
using XMemVirtualAllocFn = void*(WINAPI*)(void*, SIZE_T, DWORD, ULONGLONG, DWORD);
using XMemGetWriteWatchFn = UINT(WINAPI*)(DWORD, void*, SIZE_T, void**, ULONG_PTR*, ULONG*);
using XMemResetWriteWatchFn = UINT(WINAPI*)(void*, SIZE_T);
constexpr uint32_t kMeasuredKind = 0x20000001;

int g_checks = 0;
int g_failures = 0;

void check(bool condition, const char* what) {
    ++g_checks;
    std::printf("%s %s\n", condition ? "PASS" : "FAIL", what);
    if (!condition) ++g_failures;
}

bool aligned_64(const void* p) { return p && (reinterpret_cast<uintptr_t>(p) & 63u) == 0; }

void test_alloc(XMemAllocFn alloc, XMemFreeFn free_mem) {
    void* const zero = alloc(0, 0);
    check(zero != nullptr, "XMemAlloc(0, 0) returns non-null");
    check(aligned_64(zero), "XMemAlloc(0, 0) returns a 64-byte aligned block");

    bool zero_readable = false;
    if (zero) {
        auto* bytes = static_cast<uint8_t*>(zero);
        bytes[0] = 0xa5;
        zero_readable = bytes[0] == 0xa5;
    }
    check(zero_readable, "XMemAlloc(0, 0) block is writable and readable");

    void* const big = alloc(4096, 0x5e50000);
    check(big != nullptr, "XMemAlloc(4096, 0x5e50000) returns non-null");
    check(aligned_64(big), "XMemAlloc(4096, 0x5e50000) returns a 64-byte aligned block");

    bool pattern_ok = big != nullptr;
    if (big) {
        auto* bytes = static_cast<uint8_t*>(big);
        for (size_t i = 0; i < 4096; ++i) bytes[i] = static_cast<uint8_t>((i * 37u + 11u) & 0xffu);
        for (size_t i = 0; i < 4096; ++i) {
            if (bytes[i] != static_cast<uint8_t>((i * 37u + 11u) & 0xffu)) {
                pattern_ok = false;
                break;
            }
        }
    }
    check(pattern_ok, "XMemAlloc(4096, 0x5e50000) block holds a write/read pattern");

    free_mem(zero, 0);
    free_mem(big, 0x5e50000);
    check(true, "XMemFree accepts both blocks (no crash)");
}

void test_working_set(XMemGetWorkingSetStatisticsFn get_stats) {
    alignas(64) uint8_t stats[64];
    std::memset(stats, 0xa5, sizeof(stats));
    // The pointer is the second argument: the first (0x20000001 here, as the FSR DLL passes it) must never be written
    // through, which is what the one-argument form did.
    const BOOL ok = get_stats(kMeasuredKind, stats);
    check(ok != FALSE, "XMemGetWorkingSetStatistics(kind, 64-byte out) returns TRUE");

    uint64_t total_memory = 0;
    std::memcpy(&total_memory, stats + 0, sizeof(total_memory));
    check(ok != FALSE && total_memory != 0, "XMemGetWorkingSetStatistics writes a non-zero total memory at +0");

    MEMORYSTATUSEX ms{sizeof(ms)};
    if (GlobalMemoryStatusEx(&ms) && ms.ullTotalPhys != 0) {
        check(ok != FALSE && total_memory == ms.ullTotalPhys,
              "XMemGetWorkingSetStatistics total memory (+0) matches GlobalMemoryStatusEx ullTotalPhys");
    } else {
        check(ok != FALSE && total_memory == 12ULL * 1024 * 1024 * 1024,
              "XMemGetWorkingSetStatistics total memory (+0) falls back to 12 GiB title limit");
    }

    uint64_t working_set = 0;
    std::memcpy(&working_set, stats + 8, sizeof(working_set));
    check(ok != FALSE && working_set != 0, "XMemGetWorkingSetStatistics writes a non-zero size at +8");

    check(ok != FALSE && total_memory >= working_set,
          "XMemGetWorkingSetStatistics total memory (+0) >= working set (+8)");

    bool tail_untouched = true;
    for (size_t i = 16; i < sizeof(stats); ++i) {
        if (stats[i] != 0xa5) {
            tail_untouched = false;
            break;
        }
    }
    check(ok != FALSE && tail_untouched, "XMemGetWorkingSetStatistics leaves bytes 16..63 untouched");

    check(get_stats(kMeasuredKind, nullptr) == FALSE,
          "XMemGetWorkingSetStatistics(kind, nullptr) returns FALSE without crashing");
}

void test_virtual_query(XMemVirtualQueryFn query) {
    uint8_t local = 0;
    const void* const local_address = &local;

    alignas(64) uint8_t out[64];
    std::memset(out, 0xa5, sizeof(out));
    const SIZE_T written = query(GetCurrentProcess(), local_address, out);
    check(written != 0, "XMemVirtualQuery(current process, stack local) returns non-zero");

    MEMORY_BASIC_INFORMATION info{};
    std::memcpy(&info, out, sizeof(info));
    const uintptr_t base = reinterpret_cast<uintptr_t>(info.BaseAddress);
    const uintptr_t address = reinterpret_cast<uintptr_t>(local_address);
    const bool contains_local =
        base != 0 && info.RegionSize != 0 && address >= base && address - base < info.RegionSize;
    check(written != 0 && contains_local,
          "XMemVirtualQuery fills MEMORY_BASIC_INFORMATION with a region containing the local");

    bool tail_zero = true;
    for (size_t i = sizeof(info); i < sizeof(out); ++i) {
        if (out[i] != 0) {
            tail_zero = false;
            break;
        }
    }
    check(written != 0 && tail_zero, "XMemVirtualQuery leaves the 64-byte output tail zero");

    alignas(64) uint8_t bad_out[64];
    std::memset(bad_out, 0xa5, sizeof(bad_out));
    // Address 1 is queryable (it falls in the free region starting at 0), so use a non-canonical address that
    // VirtualQueryEx rejects.
    const void* const unmappable = reinterpret_cast<const void*>(~static_cast<uintptr_t>(0));
    const SIZE_T bad = query(GetCurrentProcess(), unmappable, bad_out);
    check(bad == 0, "XMemVirtualQuery on an invalid address returns 0 without crashing");
}

void test_virtual_alloc(XMemVirtualAllocFn virtual_alloc, XMemGetWriteWatchFn get_write_watch,
                       XMemResetWriteWatchFn reset_write_watch) {
    SYSTEM_INFO system_info{};
    GetSystemInfo(&system_info);
    const SIZE_T page_size = system_info.dwPageSize;
    check(page_size != 0, "GetSystemInfo reports a non-zero page size");
    if (page_size == 0) return;

    const SIZE_T region_size = page_size * 3;
    void* const base = virtual_alloc(nullptr, region_size, MEM_RESERVE | MEM_COMMIT | MEM_WRITE_WATCH, 0,
                                     PAGE_READWRITE);
    check(base != nullptr, "XMemVirtualAlloc creates a synthetic write-watch region");
    if (!base) return;

    void* watched[4]{};
    ULONG_PTR count = 4;
    ULONG granularity = 0;
    UINT status = get_write_watch(0, base, region_size, watched, &count, &granularity);
    check(status == NO_ERROR && count == 0, "new write-watch region starts with no dirty pages");
    check(status == NO_ERROR && granularity == page_size, "write-watch reports the host page granularity");

    auto* const bytes = static_cast<volatile uint8_t*>(base);
    bytes[0] = 0xa1;
    bytes[page_size * 2] = 0xb2;

    count = 4;
    status = get_write_watch(0, base, region_size, watched, &count, &granularity);
    bool saw_first_page = false;
    bool saw_third_page = false;
    bool all_addresses_are_page_aligned = count <= 4;
    for (ULONG_PTR i = 0; i < count && i < 4; ++i) {
        const uintptr_t address = reinterpret_cast<uintptr_t>(watched[i]);
        const uintptr_t start = reinterpret_cast<uintptr_t>(base);
        all_addresses_are_page_aligned &= address >= start && (address - start) % page_size == 0;
        saw_first_page |= address == start;
        saw_third_page |= address == start + page_size * 2;
    }
    check(status == NO_ERROR && count == 2 && saw_first_page && saw_third_page,
          "write-watch reports each written page by its relative address");
    check(all_addresses_are_page_aligned, "write-watch addresses are page aligned and within the region");

    count = 4;
    status = get_write_watch(0, base, region_size, watched, &count, &granularity);
    check(status == NO_ERROR && count == 2, "query-only write-watch leaves dirty pages recorded");

    status = reset_write_watch(base, region_size);
    check(status == NO_ERROR, "XMemResetWriteWatch clears the synthetic region");
    count = 4;
    status = get_write_watch(0, base, region_size, watched, &count, &granularity);
    check(status == NO_ERROR && count == 0, "reset write-watch query returns no dirty pages");

    bytes[page_size] = 0xc3;
    count = 4;
    status = get_write_watch(0, base, region_size, watched, &count, &granularity);
    check(status == NO_ERROR && count == 1 &&
              reinterpret_cast<uintptr_t>(watched[0]) == reinterpret_cast<uintptr_t>(base) + page_size,
          "write-watch records writes again after reset");

    check(VirtualFree(base, 0, MEM_RELEASE), "release the synthetic write-watch region");

    SetLastError(ERROR_SUCCESS);
    void* const large_page = virtual_alloc(nullptr, page_size, MEM_RESERVE | MEM_COMMIT | 0x20000000, 0,
                                           PAGE_READWRITE);
    check(large_page == nullptr && GetLastError() == ERROR_NOT_SUPPORTED,
          "unsupported large-page allocation is rejected before host allocation");

    SetLastError(ERROR_SUCCESS);
    void* const attributed = virtual_alloc(nullptr, page_size, MEM_RESERVE | MEM_COMMIT, 1, PAGE_READWRITE);
    check(attributed == nullptr && GetLastError() == ERROR_NOT_SUPPORTED,
          "nonzero XMem allocation attributes are rejected before host allocation");
}

// gta6rt --trace: one "xmem_attributes" event per distinct raw attribute word and API, none with the trace off.
void test_attribute_trace(XMemAllocFn alloc, XMemFreeFn free_mem) {
    wchar_t temp[MAX_PATH];
    GetTempPathW(MAX_PATH, temp);
    const std::wstring path =
        std::wstring(temp) + L"xmem_trace_test_" + std::to_wstring(GetCurrentProcessId()) + L".jsonl";
    DeleteFileW(path.c_str());

    void* untraced = alloc(64, 0x1111);  // trace not open yet: nothing to write, nothing to crash
    check(untraced != nullptr, "XMemAlloc with the trace off still allocates");
    free_mem(untraced, 0x1111);

    check(rt::startup_trace::open(path.c_str()), "open the startup trace file");
    void* a = alloc(4096, 0x5e50000);
    void* b = alloc(8192, 0x5e50000);  // same word again: no second event
    void* c = alloc(16, 0x99);
    free_mem(a, 0x5e50000);
    free_mem(b, 0x5e50000);  // same word again: no second event
    free_mem(c, 0x99);
    free_mem(nullptr, 0x77);  // a free-only word is recorded too
    rt::startup_trace::close();

    std::ifstream in(path);
    std::stringstream text;
    text << in.rdbuf();
    const std::string log = text.str();
    auto count = [&](const std::string& needle) {
        int n = 0;
        for (size_t at = log.find(needle); at != std::string::npos; at = log.find(needle, at + 1)) ++n;
        return n;
    };
    check(count("\"kind\":\"xmem_attributes\"") == 5, "five events: two XMemAlloc words and three XMemFree words");
    check(count("\"api\":\"XMemAlloc\",\"attributes\":\"0x5e50000\",\"size\":4096") == 1,
          "XMemAlloc 0x5e50000 is recorded once, with the size of the first call");
    check(count("\"api\":\"XMemAlloc\",\"attributes\":\"0x99\",\"size\":16") == 1, "XMemAlloc 0x99 is recorded once");
    check(count("\"api\":\"XMemFree\",\"attributes\":\"0x5e50000\"") == 1, "XMemFree 0x5e50000 is recorded once");
    check(count("\"api\":\"XMemFree\",\"attributes\":\"0x77\"") == 1, "XMemFree 0x77 is recorded");
    check(count("\"attributes\":\"0x1111\"") == 0, "nothing was recorded while the trace was off");
    check(count("\"caller\":\"") == 5, "every event names its caller");
    DeleteFileW(path.c_str());
}

using HostVirtualAllocFn = void*(WINAPI*)(void*, SIZE_T, DWORD, DWORD);

// RT-217: a title's kernel32!VirtualAlloc carries the console's MEM_GRAPHICS / MEM_TITLE type bits, which the host
// rejects. The registered shim has to drop them and still allocate.
void test_title_virtual_alloc() {
    constexpr DWORD kMemGraphics = 0x10000000;
    constexpr DWORD kMemTitle = 0x40000000;
    const char* const dlls[] = {"kernel32.dll", "kernelbase.dll", "api-ms-win-core-memory-l1-1-0.dll"};
    for (const char* dll : dlls) {
        const auto alloc = reinterpret_cast<HostVirtualAllocFn>(rt::shims::find(dll, "VirtualAlloc"));
        char what[160];
        std::snprintf(what, sizeof what, "registry exposes %s!VirtualAlloc", dll);
        check(alloc != nullptr, what);
        if (!alloc) continue;

        for (const DWORD bits : {kMemGraphics, kMemTitle, kMemGraphics | kMemTitle, DWORD{0}}) {
            void* const p = alloc(nullptr, 0x10000, MEM_RESERVE | MEM_COMMIT | bits, PAGE_READWRITE);
            std::snprintf(what, sizeof what, "%s!VirtualAlloc with type bits %#lx returns memory", dll,
                          static_cast<unsigned long>(bits));
            check(p != nullptr, what);
            if (!p) continue;
            static_cast<volatile uint8_t*>(p)[0] = 0x5a;
            check(static_cast<volatile uint8_t*>(p)[0] == 0x5a, "the allocation is committed and writable");
            check(VirtualFree(p, 0, MEM_RELEASE), "release the allocation");
        }
    }
}

void print_result() {
    if (g_failures == 0) {
        std::printf("xmem_test: OK (%d checks)\n", g_checks);
    } else {
        std::printf("xmem_test: FAILED (%d checks, %d failure%s)\n", g_checks, g_failures,
                    g_failures == 1 ? "" : "s");
    }
}

}  // namespace

int main() {
    std::printf("xmem_test: registered xmem.dll shim ABI\n");

    const auto alloc = reinterpret_cast<XMemAllocFn>(rt::shims::find("xmem.dll", "XMemAlloc"));
    const auto free_mem = reinterpret_cast<XMemFreeFn>(rt::shims::find("xmem.dll", "XMemFree"));
    const auto query = reinterpret_cast<XMemVirtualQueryFn>(rt::shims::find("xmem.dll", "XMemVirtualQuery"));
    const auto get_stats =
        reinterpret_cast<XMemGetWorkingSetStatisticsFn>(rt::shims::find("xmem.dll", "XMemGetWorkingSetStatistics"));
    const auto virtual_alloc =
        reinterpret_cast<XMemVirtualAllocFn>(rt::shims::find("xmem.dll", "XMemVirtualAlloc"));
    const auto get_write_watch =
        reinterpret_cast<XMemGetWriteWatchFn>(rt::shims::find("xmem.dll", "XMemGetWriteWatch"));
    const auto reset_write_watch =
        reinterpret_cast<XMemResetWriteWatchFn>(rt::shims::find("xmem.dll", "XMemResetWriteWatch"));

    check(alloc != nullptr, "registry exposes xmem.dll!XMemAlloc");
    check(free_mem != nullptr, "registry exposes xmem.dll!XMemFree");
    check(query != nullptr, "registry exposes xmem.dll!XMemVirtualQuery");
    check(get_stats != nullptr, "registry exposes xmem.dll!XMemGetWorkingSetStatistics");
    check(virtual_alloc != nullptr, "registry exposes xmem.dll!XMemVirtualAlloc");
    check(get_write_watch != nullptr, "registry exposes xmem.dll!XMemGetWriteWatch");
    check(reset_write_watch != nullptr, "registry exposes xmem.dll!XMemResetWriteWatch");

    if (!alloc || !free_mem || !query || !get_stats) {
        print_result();
        return 1;
    }

    test_alloc(alloc, free_mem);
    test_working_set(get_stats);
    test_virtual_query(query);
    if (virtual_alloc && get_write_watch && reset_write_watch) {
        test_virtual_alloc(virtual_alloc, get_write_watch, reset_write_watch);
    }
    test_attribute_trace(alloc, free_mem);

    test_title_virtual_alloc();

    print_result();
    return g_failures ? 1 : 0;
}

#include "thread_sampler.h"

#include <windows.h>
#include <dbghelp.h>
#include <psapi.h>
#include <tlhelp32.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#pragma comment(lib, "dbghelp.lib")
#pragma comment(lib, "psapi.lib")

namespace rt::thread_sampler {
namespace {

struct Image {
    std::uint64_t base = 0;
    std::uint64_t size = 0;
    std::string name;
};

struct Row {
    DWORD tid = 0;
    std::uint64_t cpu_100ns = 0;
    double cpu = 0;  // percent of one core over the last interval
    std::vector<std::uint64_t> frames;
};

ImageResolver g_resolver = nullptr;

// dbghelp names this runtime's own frames -- which is what says which layer call the driver is spinning under. The
// symbols load deferred, on first use, and only the address of this executable is ever asked for.
bool g_symbols_ready = false;
std::once_flag g_symbols_once;

void init_symbols() {
    std::call_once(g_symbols_once, [] {
        SymSetOptions(SYMOPT_UNDNAME | SYMOPT_DEFERRED_LOADS | SYMOPT_NO_PROMPTS);
        g_symbols_ready = SymInitialize(GetCurrentProcess(), nullptr, TRUE) != FALSE;
    });
}

bool in_own_image(std::uint64_t address) {
    const auto base = reinterpret_cast<std::uint64_t>(GetModuleHandleA(nullptr));
    if (!address || address < base) return false;
    const auto* dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
    const auto* nt = reinterpret_cast<const IMAGE_NT_HEADERS*>(base + dos->e_lfanew);
    return address < base + nt->OptionalHeader.SizeOfImage;
}

std::string symbolize(std::uint64_t address) {
    if (!in_own_image(address)) return {};
    init_symbols();
    if (!g_symbols_ready) return {};
    union {
        SYMBOL_INFO info;
        char storage[sizeof(SYMBOL_INFO) + 512];
    } buffer{};
    buffer.info.SizeOfStruct = sizeof(SYMBOL_INFO);
    buffer.info.MaxNameLen = 500;
    DWORD64 displacement = 0;
    if (!SymFromAddr(GetCurrentProcess(), address, &displacement, &buffer.info)) return {};
    char text[560] = {};
    std::snprintf(text, sizeof text, "%s+0x%llx", buffer.info.Name,
                  static_cast<unsigned long long>(displacement));
    return text;
}

std::string hex(std::uint64_t value) {
    char text[24] = {};
    std::snprintf(text, sizeof text, "0x%llx", static_cast<unsigned long long>(value));
    return text;
}

// The host's own modules, for the frames that are not console images (ntdll, kernel32, amdvlk64.dll).
std::vector<Image> host_images() {
    std::vector<Image> out;
    HMODULE handles[512] = {};
    DWORD needed = 0;
    if (!EnumProcessModules(GetCurrentProcess(), handles, sizeof handles, &needed)) return out;
    const std::size_t count = std::min<std::size_t>(needed / sizeof handles[0], 512);
    for (std::size_t i = 0; i < count; ++i) {
        MODULEINFO info{};
        if (!GetModuleInformation(GetCurrentProcess(), handles[i], &info, sizeof info)) continue;
        char path[MAX_PATH] = {};
        if (!GetModuleBaseNameA(GetCurrentProcess(), handles[i], path, sizeof path)) continue;
        out.push_back({reinterpret_cast<std::uint64_t>(info.lpBaseOfDll), std::uint64_t(info.SizeOfImage),
                       std::string(path)});
    }
    return out;
}

// "module+0xRVA" for an address: the mapped images first (the loader's own table, which the host's module list does not
// hold), then the host's. `host` is built once per sample and passed in.
std::string where(std::uint64_t address, const std::vector<Image>& host) {
    if (!address) return "(none)";
    if (g_resolver) {
        std::string module;
        std::uint32_t rva = 0;
        if (g_resolver(address, module, rva)) return module + "+0x" + hex(rva).substr(2);
    }
    if (const std::string name = symbolize(address); !name.empty()) return name;
    for (const Image& image : host) {
        if (address >= image.base && address < image.base + image.size)
            return image.name + "+0x" + hex(address - image.base).substr(2);
    }
    return hex(address);
}

bool maps_a_console_image(std::uint64_t address) {
    if (!g_resolver) return false;
    std::string module;
    std::uint32_t rva = 0;
    return g_resolver(address, module, rva);
}

std::uint64_t thread_cpu_100ns(HANDLE thread) {
    FILETIME created{}, exited{}, kernel{}, user{};
    if (!GetThreadTimes(thread, &created, &exited, &kernel, &user)) return 0;
    ULARGE_INTEGER k{};
    k.LowPart = kernel.dwLowDateTime;
    k.HighPart = kernel.dwHighDateTime;
    ULARGE_INTEGER u{};
    u.LowPart = user.dwLowDateTime;
    u.HighPart = user.dwHighDateTime;
    return k.QuadPart + u.QuadPart;
}

// A real stack walk over a stopped thread, from the CONTEXT of the sample. The loader registers every mapped image's
// .pdata with RtlAddFunctionTable, so RtlLookupFunctionEntry / RtlVirtualUnwind work for console code here even though
// the host's loader never saw those images. A raw scan of the stack was tried first and found nothing: the frames are
// deeper than any window, and every stale word in the window belongs to ntdll.
constexpr int kMaxFrames = 48;

std::vector<std::uint64_t> walk(HANDLE thread, const CONTEXT& start) {
    std::vector<std::uint64_t> frames;
    CONTEXT context = start;
    for (int frame = 0; frame < kMaxFrames; ++frame) {
        frames.push_back(context.Rip);
        DWORD64 image_base = 0;
        PRUNTIME_FUNCTION entry = RtlLookupFunctionEntry(context.Rip, &image_base, nullptr);
        if (entry) {
            void* handler_data = nullptr;
            DWORD64 establisher = 0;
            RtlVirtualUnwind(UNW_FLAG_NHANDLER, image_base, context.Rip, entry, &context, &handler_data, &establisher,
                             nullptr);
        } else {
            // A leaf function without unwind data: the return address is the word at RSP.
            std::uint64_t next = 0;
            SIZE_T got = 0;
            if (!ReadProcessMemory(GetCurrentProcess(), reinterpret_cast<const void*>(context.Rsp), &next,
                                   sizeof next, &got) ||
                got != sizeof next || !next)
                break;
            context.Rip = next;
            context.Rsp += 8;
        }
        if (!context.Rip || context.Rip == start.Rip) break;
    }
    (void)thread;
    return frames;
}

void sample(double interval_seconds) {
    const DWORD self = GetCurrentThreadId();
    const DWORD pid = GetCurrentProcessId();
    const std::vector<Image> host = host_images();
    static std::map<DWORD, std::uint64_t> last_cpu;
    std::vector<Row> rows;

    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return;
    THREADENTRY32 entry{};
    entry.dwSize = sizeof entry;
    if (Thread32First(snapshot, &entry)) {
        do {
            if (entry.th32OwnerProcessID != pid || entry.th32ThreadID == self) continue;
            const DWORD tid = entry.th32ThreadID;
            HANDLE thread = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT | THREAD_QUERY_INFORMATION, FALSE,
                                       tid);
            if (!thread) continue;
            Row row;
            row.tid = tid;
            if (SuspendThread(thread) != static_cast<DWORD>(-1)) {
                CONTEXT context{};
                context.ContextFlags = CONTEXT_CONTROL;
                if (GetThreadContext(thread, &context)) row.frames = walk(thread, context);
                ResumeThread(thread);
            }
            row.cpu_100ns = thread_cpu_100ns(thread);
            CloseHandle(thread);
            const auto it = last_cpu.find(tid);
            const std::uint64_t previous = it == last_cpu.end() ? row.cpu_100ns : it->second;
            if (row.cpu_100ns > previous && interval_seconds > 0)
                row.cpu = 100.0 * double(row.cpu_100ns - previous) / (interval_seconds * 1e7);
            last_cpu[tid] = row.cpu_100ns;
            rows.push_back(std::move(row));
        } while (Thread32Next(snapshot, &entry));
    }
    CloseHandle(snapshot);

    std::sort(rows.begin(), rows.end(), [](const Row& a, const Row& b) { return a.cpu > b.cpu; });
    // Per leaf address: how many threads stop there, and how much of a core they add up to.
    std::map<std::string, std::pair<int, double>> by_leaf;
    double total = 0;
    for (const Row& row : rows) {
        total += row.cpu;
        auto& group = by_leaf[where(row.frames.empty() ? 0 : row.frames[0], host)];
        group.first += 1;
        group.second += row.cpu;
    }
    std::vector<std::pair<std::string, std::pair<int, double>>> sorted(by_leaf.begin(), by_leaf.end());
    std::sort(sorted.begin(), sorted.end(),
              [](const auto& a, const auto& b) { return a.second.second > b.second.second; });

    std::fprintf(stderr, "[rt] threads: %d sampled, %.0f%% of a core in the last %.1f s\n", int(rows.size()), total,
                 interval_seconds);
    int shown = 0;
    for (const auto& group : sorted) {
        if (group.second.second < 1.0 && shown >= 6) break;
        std::fprintf(stderr, "[rt] threads: %2d x %-44s %6.0f%%\n", group.second.first, group.first.c_str(),
                     group.second.second);
        if (++shown >= 12) break;
    }
    // The threads that are not burning CPU are the interesting ones when the title stalls: they are parked. Print the
    // console-image frames of each, which is where it waits.
    int parked = 0;
    int busy_shown = 0;
    for (const Row& row : rows) {
        if (row.cpu >= 5.0 || parked >= 12) continue;
        std::string line;
        int console_frames = 0;
        for (std::uint64_t address : row.frames) {
            if (!maps_a_console_image(address)) continue;
            line += " " + where(address, host);
            if (++console_frames >= 8) break;
        }
        if (console_frames < 3) continue;
        std::fprintf(stderr, "[rt] parked %5lu %4.0f%% leaf %s:%s\n", static_cast<unsigned long>(row.tid), row.cpu,
                     where(row.frames[0], host).c_str(), line.c_str());
        ++parked;
    }
    // And the fastest one's whole walk, so a spin can be followed.
    for (const Row& row : rows) {
        if (row.cpu < 50.0 || busy_shown >= 3) break;
        std::string line;
        // Only the frames that are not the driver itself: which layer call the driver spins under is the question, and
        // the driver's own 30 frames push it off a printed line.
        for (std::uint64_t address : row.frames) {
            const std::string name = where(address, host);
            if (name.rfind("amdvlk64.dll", 0) == 0) continue;
            line += " " + name;
        }
        std::fprintf(stderr, "[rt] busy %5lu %4.0f%% (deepest first):%s\n", static_cast<unsigned long>(row.tid),
                     row.cpu, line.c_str());
        ++busy_shown;
    }
    std::fflush(stderr);
}

}  // namespace

void install() {
    char text[32] = {};
    const DWORD n = GetEnvironmentVariableA("LEONIDA_THREAD_MAP", text, sizeof text);
    if (n == 0 || n >= sizeof text) return;
    const double seconds = std::strtod(text, nullptr);
    if (!(seconds > 0)) return;
    std::thread([seconds] {
        for (;;) {
            Sleep(DWORD(seconds * 1000));
            sample(seconds);
        }
    }).detach();
    std::fprintf(stderr, "[rt] thread sampler: every %.1f s\n", seconds);
}

void set_image_resolver(ImageResolver resolver) { g_resolver = resolver; }

}  // namespace rt::thread_sampler

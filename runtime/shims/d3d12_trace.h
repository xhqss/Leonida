#pragma once

#include <windows.h>

#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <mutex>

namespace rt::d3d12_trace {

inline std::atomic<bool> tracing{false};

inline void set_enabled(bool enable) noexcept {
    tracing.store(enable, std::memory_order_relaxed);
}

inline bool enabled() noexcept {
    return tracing.load(std::memory_order_relaxed);
}

// The method a refusal happens in, for a caller that reports one without a trace log: a command list records this on
// entry (also when tracing is off) and names it when its first recorded command is refused.
inline thread_local const char* last_method = nullptr;

// Callers supply fixed method names and outcomes only. Keep one ordered record per call across threads.
inline void record(const char* method, const char* outcome = "called") noexcept {
    last_method = method;
    if (!enabled()) return;
    static std::mutex mutex;
    static unsigned long long sequence = 0;
    const std::lock_guard lock(mutex);
    std::fprintf(stderr, "[d3d12-trace] #%llu %s %s\n", ++sequence, method, outcome);
    std::fflush(stderr);
}

// LEONIDA_LOG_FAILED_CALLS: which calls the shim refuses, counted per method. A failure here is silent otherwise --
// the title gets an HRESULT it may log nowhere and then works around, and the run just looks slow or blank (the depth
// description whose ALLOW_UNORDERED_ACCESS made GetResourceAllocationInfo answer UINT64_MAX was exactly that). Off
// unless the variable is set; one small table, printed every two seconds.
inline void note_outcome(const char* method, HRESULT code) noexcept {
    static const bool on = [] {
        const char* value = std::getenv("LEONIDA_LOG_FAILED_CALLS");
        return value && value[0] && value[0] != '0';
    }();
    if (!on) return;
    struct Entry {
        const char* method;
        unsigned long long ok;
        unsigned long long failed;
        unsigned long long unsupported;
        HRESULT last;
    };
    static std::mutex mutex;
    static Entry entries[64] = {};
    static unsigned used = 0;
    static unsigned long long next_dump = 0;
    const std::lock_guard lock(mutex);
    Entry* entry = nullptr;
    for (unsigned i = 0; i < used; ++i)
        if (entries[i].method == method) {
            entry = &entries[i];
            break;
        }
    if (!entry && used < 64) {
        entry = &entries[used++];
        entry->method = method;
    }
    if (entry) {
        if (code == E_NOTIMPL) {
            ++entry->unsupported;
            entry->last = code;
        } else if (FAILED(code)) {
            ++entry->failed;
            entry->last = code;
        } else {
            ++entry->ok;
        }
    }
    const unsigned long long at = GetTickCount64();
    if (at < next_dump) return;
    next_dump = at + 2000;
    for (unsigned i = 0; i < used; ++i) {
        if (!entries[i].failed && !entries[i].unsupported) continue;
        std::fprintf(stderr, "[rt] d3d12_xs: failed calls: %s ok %llu, failed %llu (last 0x%08lx), unsupported %llu\n",
                     entries[i].method, entries[i].ok, entries[i].failed,
                     static_cast<unsigned long>(entries[i].last), entries[i].unsupported);
        entries[i].ok = entries[i].failed = entries[i].unsupported = 0;
    }
}

inline HRESULT result(const char* method, HRESULT code) noexcept {
    record(method, code == E_NOTIMPL ? "unsupported" : FAILED(code) ? "failed" : "ok");
    note_outcome(method, code);
    return code;
}

}  // namespace rt::d3d12_trace

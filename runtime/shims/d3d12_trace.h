#pragma once

#include <windows.h>

#include <atomic>
#include <cstdio>
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

inline HRESULT result(const char* method, HRESULT code) noexcept {
    record(method, code == E_NOTIMPL ? "unsupported" : FAILED(code) ? "failed" : "ok");
    return code;
}

}  // namespace rt::d3d12_trace

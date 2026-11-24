// Where the layer's per-frame wall time goes, for LEONIDA_PROFILE_LAYER=1.
//
// The interesting question when a title presents at 200 Hz with four tiny draws is not "which API is called most"
// (LEONIDA_SLOT_PROFILE answers that) but "which call takes the time": a run can make five thousand cheap calls and
// still burn ten cores. The scopes here are the few operations that touch the driver, and the report prints count,
// average and worst case in milliseconds every two seconds.
//
// Off unless the variable is set: a Scope does nothing but one relaxed load when profiling is off.
#pragma once

#include <windows.h>

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>

namespace leonida::layer_profile {

enum Slot {
    submit_slot = 0,     // VulkanQueue::execute_now: replaying and submitting one batch
    present_slot = 1,    // Present::submit: composing and presenting a frame
    allocate_slot = 2,   // vkAllocateMemory through the memory-type policy
    pipeline_slot = 3,   // vkCreateGraphicsPipelines
    exchange_slot = 4,   // swapchain acquire/present of one image
    slot_count = 5,
};

inline constexpr const char* kSlotNames[slot_count] = {"submit", "present", "allocate", "pipeline", "exchange"};

struct Counters {
    std::atomic<std::uint64_t> count[slot_count];
    std::atomic<std::uint64_t> micros[slot_count];
    std::atomic<std::uint64_t> worst[slot_count];
    std::atomic<std::uint64_t> next_dump{0};

    Counters() {
        for (int i = 0; i < slot_count; ++i) {
            count[i].store(0, std::memory_order_relaxed);
            micros[i].store(0, std::memory_order_relaxed);
            worst[i].store(0, std::memory_order_relaxed);
        }
    }
};

inline Counters& counters() {
    static auto* c = new Counters;
    return *c;
}

inline bool enabled() {
    static const bool on = [] {
        const char* value = std::getenv("LEONIDA_PROFILE_LAYER");
        return value && value[0] && value[0] != '0';
    }();
    return on;
}

inline std::uint64_t now_micros() {
    static const std::uint64_t frequency = [] {
        LARGE_INTEGER f{};
        QueryPerformanceFrequency(&f);
        return static_cast<std::uint64_t>(f.QuadPart ? f.QuadPart : 1);
    }();
    LARGE_INTEGER now{};
    QueryPerformanceCounter(&now);
    return static_cast<std::uint64_t>(now.QuadPart) * 1000000ull / frequency;
}

inline void report_if_due() {
    Counters& c = counters();
    const std::uint64_t at = GetTickCount64();
    std::uint64_t expected = c.next_dump.load(std::memory_order_relaxed);
    if (at < expected || !c.next_dump.compare_exchange_strong(expected, at + 2000, std::memory_order_relaxed)) return;
    for (int i = 0; i < slot_count; ++i) {
        const std::uint64_t count = c.count[i].exchange(0, std::memory_order_relaxed);
        const std::uint64_t micros = c.micros[i].exchange(0, std::memory_order_relaxed);
        const std::uint64_t worst = c.worst[i].exchange(0, std::memory_order_relaxed);
        if (!count) continue;
        std::fprintf(stderr, "[rt] profile: %-9s %6llu calls, %7.2f ms each, worst %7.2f ms\n", kSlotNames[i],
                     static_cast<unsigned long long>(count), double(micros) / 1000.0 / double(count),
                     double(worst) / 1000.0);
    }
}

struct Scope {
    Slot slot;
    std::uint64_t started = 0;
    bool on = false;

    explicit Scope(Slot s) : slot(s), on(enabled()) {
        if (on) started = now_micros();
    }
    ~Scope() {
        if (!on) return;
        const std::uint64_t took = now_micros() - started;
        Counters& c = counters();
        c.count[slot].fetch_add(1, std::memory_order_relaxed);
        c.micros[slot].fetch_add(took, std::memory_order_relaxed);
        std::uint64_t worst = c.worst[slot].load(std::memory_order_relaxed);
        while (took > worst && !c.worst[slot].compare_exchange_weak(worst, took, std::memory_order_relaxed)) {
        }
        report_if_due();
    }
    Scope(const Scope&) = delete;
    Scope& operator=(const Scope&) = delete;
};

}  // namespace leonida::layer_profile

#pragma once

// LEONIDA_WATCH_WRITE: trap the writes into the pages holding a title address and log the instruction that makes them,
// with the page's bytes before and after. This is how "who writes this memory, and with what" gets answered without a
// kernel debugger: a PAGE_GUARD raises EXCEPTION_GUARD_PAGE before the first access to the page, and the trap flag
// re-arms it after the instruction, so every write to the page is caught once.
//
// The pages are armed by the caller (arm_page) and the addresses are plain title VAs: the title's allocator hands out
// host memory at the console VA, and the driver copies a root descriptor table out of it, so the writes that matter are
// ordinary stores the title makes. Off unless LEONIDA_WATCH_WRITE is set.

#include <windows.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace rt::write_watch {

inline constexpr unsigned kPages = 8;
inline constexpr int kEvents = 64;

struct State {
    std::uint64_t pages[kPages] = {};
    unsigned count = 0;
    int events = 0;
    bool stepping = false;
    std::uint64_t trap_address = 0;   // the guard page that raised
    std::uint64_t fault_address = 0;  // what the instruction touched
    std::uint8_t before[8][64] = {};  // the first 64 bytes of each page, before the write
};

inline State& state() {
    static State s;
    return s;
}

inline bool enabled() {
    const char* value = std::getenv("LEONIDA_WATCH_WRITE");
    return value && value[0] && value[0] != '0';
}

// Where an instruction is: its module and the offset inside it, which is what a disassembler or a map file needs.
inline void describe_instruction(const void* address, char* out, std::size_t bytes) {
    HMODULE module = nullptr;
    if (GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                           static_cast<LPCWSTR>(address), &module) &&
        module) {
        wchar_t path[MAX_PATH] = {};
        const DWORD length = GetModuleFileNameW(module, path, MAX_PATH);
        const char* name = length ? "?" : "?";
        char narrow[MAX_PATH] = {};
        if (length) {
            const int written = WideCharToMultiByte(CP_UTF8, 0, path, int(length), narrow, MAX_PATH, nullptr, nullptr);
            if (written > 0) narrow[written] = '\0';
            name = std::strrchr(narrow, '\\') ? std::strrchr(narrow, '\\') + 1 : narrow;
        }
        std::snprintf(out, bytes, "%s+0x%llx", name,
                      static_cast<unsigned long long>(reinterpret_cast<std::uint64_t>(address) -
                                                      reinterpret_cast<std::uint64_t>(module)));
        return;
    }
    std::snprintf(out, bytes, "%p", address);
}

inline void dump_page(const char* when, std::uint64_t page, const std::uint8_t* bytes) {
    std::fprintf(stderr, "[rt] watch %s 0x%llx:", when, static_cast<unsigned long long>(page));
    for (int i = 0; i < 16; ++i) std::fprintf(stderr, " %02x", bytes[i]);
    std::fprintf(stderr, "\n");
}

inline void arm_page(std::uint64_t page) {
    State& s = state();
    for (unsigned i = 0; i < s.count; ++i)
        if (s.pages[i] == page) return;
    if (s.count >= kPages) return;
    // Read the page before the guard goes on: reading it afterwards would trip the guard from this thread.
    if (s.count < kPages) std::memcpy(s.before[s.count], reinterpret_cast<const void*>(page), 64);
    DWORD previous = 0;
    if (!VirtualProtect(reinterpret_cast<void*>(page), 0x1000, PAGE_READWRITE | PAGE_GUARD, &previous)) return;
    s.pages[s.count] = page;
    ++s.count;
    if (s.count <= 2)
        std::fprintf(stderr, "[rt] watch: page 0x%llx armed\n", static_cast<unsigned long long>(page));
}

inline std::uint8_t* before_of(std::uint64_t page) {
    State& s = state();
    for (unsigned i = 0; i < s.count; ++i)
        if (s.pages[i] == page) return s.before[i];
    return nullptr;
}

inline LONG CALLBACK on_exception(EXCEPTION_POINTERS* info) {
    State& s = state();
    if (!info || !info->ExceptionRecord) return EXCEPTION_CONTINUE_SEARCH;
    const DWORD code = info->ExceptionRecord->ExceptionCode;
    if (code == EXCEPTION_GUARD_PAGE) {
        if (!s.count) return EXCEPTION_CONTINUE_SEARCH;
        const std::uint64_t fault = static_cast<std::uint64_t>(info->ExceptionRecord->ExceptionInformation[1]);
        const std::uint64_t page = fault & ~std::uint64_t{0xfff};
        std::uint8_t* before = before_of(page);
        if (!before) return EXCEPTION_CONTINUE_SEARCH;
        char what[280];
        describe_instruction(info->ExceptionRecord->ExceptionAddress, what, sizeof(what));
        if (s.events < kEvents)
            std::fprintf(stderr, "[rt] watch: write to page 0x%llx at 0x%llx by %s\n",
                         static_cast<unsigned long long>(page), static_cast<unsigned long long>(fault), what);
        ++s.events;
        dump_page("before", page, before);
        s.stepping = true;
        s.trap_address = page;
        s.fault_address = fault;
        // Trap after the instruction so the page can be re-armed only once the write has landed: the guard bit is
        // cleared by the access that trips it, so without this only the first write would ever be seen.
        info->ContextRecord->EFlags |= 0x100;
        return EXCEPTION_CONTINUE_EXECUTION;
    }
    if (code == EXCEPTION_SINGLE_STEP && s.stepping) {
        s.stepping = false;
        info->ContextRecord->EFlags &= ~std::uint32_t{0x100};
        const std::uint64_t page = s.trap_address;
        dump_page("after ", page, reinterpret_cast<const std::uint8_t*>(page));
        std::memcpy(before_of(page), reinterpret_cast<const void*>(page), 64);
        if (s.events < kEvents) {
            DWORD previous = 0;
            VirtualProtect(reinterpret_cast<void*>(page), 0x1000, PAGE_READWRITE | PAGE_GUARD, &previous);
        }
        return EXCEPTION_CONTINUE_EXECUTION;
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

// Starts watching; `pages` are page-aligned title addresses.
inline void start(const std::uint64_t* pages, unsigned count) {
    if (!enabled() || !count) return;
    static bool handler = false;
    if (!handler) {
        AddVectoredExceptionHandler(1, on_exception);
        handler = true;
    }
    for (unsigned i = 0; i < count; ++i) arm_page(pages[i]);
    // LEONIDA_WATCH_SELFTEST: store one byte back into the first armed page, which must be reported by the handler. A
    // guard the title's allocator or the driver clears, or a handler that never ran, would otherwise look exactly like
    // "nothing writes this page".
    if (std::getenv("LEONIDA_WATCH_SELFTEST") && state().count) {
        volatile std::uint8_t* byte = reinterpret_cast<volatile std::uint8_t*>(pages[0]);
        const std::uint8_t was = *byte;
        *byte = was;
        std::fprintf(stderr, "[rt] watch: selftest wrote to page 0x%llx (events now %d)\n",
                     static_cast<unsigned long long>(pages[0]), state().events);
    }
}

}  // namespace rt::write_watch

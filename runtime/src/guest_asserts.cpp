#include "guest_asserts.h"

#include <cstdio>
#include <cstring>

#include "../shims/startup_trace.h"

namespace rt::guest_asserts {

namespace {
std::atomic<long> g_remaining{0};

// The byte at `p` can be read: committed, not guarded or inaccessible, and a readable protection.
bool readable(const void* p) {
    MEMORY_BASIC_INFORMATION mbi{};
    if (!p || VirtualQuery(p, &mbi, sizeof mbi) != sizeof mbi) return false;
    if (mbi.State != MEM_COMMIT || (mbi.Protect & (PAGE_GUARD | PAGE_NOACCESS))) return false;
    return (mbi.Protect & (PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READ |
                           PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)) != 0;
}
}

LONG CALLBACK handler(EXCEPTION_POINTERS* info) {
    if (!info || !info->ExceptionRecord || info->ExceptionRecord->ExceptionCode != EXCEPTION_BREAKPOINT)
        return EXCEPTION_CONTINUE_SEARCH;
    const auto* at = static_cast<const unsigned char*>(info->ExceptionRecord->ExceptionAddress);
    HMODULE host = nullptr;
    const bool in_host = GetModuleHandleExW(
        GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        reinterpret_cast<LPCWSTR>(at), &host) != 0;
    if (in_host) return EXCEPTION_CONTINUE_SEARCH;  // a host module's own breakpoint
    if (!info->ContextRecord || info->ContextRecord->Rip != reinterpret_cast<DWORD64>(at) || !readable(at))
        return EXCEPTION_CONTINUE_SEARCH;
    if (!should_skip(g_remaining, in_host, *at)) return EXCEPTION_CONTINUE_SEARCH;
    std::fprintf(stderr, "[rt] assert skipped: int3 in guest code at %s (LEONIDA_SKIP_ASSERTS)\n",
                 rt::startup_trace::where(reinterpret_cast<uint64_t>(at)).c_str());
    // What the assert said: a console title's assert reports through a call, so the registers on entry to the `int3`
    // hold its arguments (a message, a file name, a condition, an id). Printing them, and any string they point at,
    // is what turns "the title asserted" into something actionable. The return address names the caller, which is
    // where the failing check actually lives.
    if (info->ContextRecord) {
        const CONTEXT& c = *info->ContextRecord;
        const DWORD64 words[6] = {c.Rcx, c.Rdx, c.R8, c.R9, c.Rax, c.Rbx};
        std::fprintf(stderr, "[rt] assert registers: rcx %llx rdx %llx r8 %llx r9 %llx rax %llx rbx %llx\n",
                     static_cast<unsigned long long>(words[0]), static_cast<unsigned long long>(words[1]),
                     static_cast<unsigned long long>(words[2]), static_cast<unsigned long long>(words[3]),
                     static_cast<unsigned long long>(words[4]), static_cast<unsigned long long>(words[5]));
        std::uint64_t back[4] = {};
        if (readable(reinterpret_cast<const void*>(c.Rsp))) {
            std::memcpy(back, reinterpret_cast<const void*>(c.Rsp), sizeof(back));
            std::fprintf(stderr, "[rt] assert call from %s (rsp %llx: %llx %llx %llx %llx)\n",
                         rt::startup_trace::where(back[0]).c_str(), static_cast<unsigned long long>(c.Rsp),
                         static_cast<unsigned long long>(back[0]), static_cast<unsigned long long>(back[1]),
                         static_cast<unsigned long long>(back[2]), static_cast<unsigned long long>(back[3]));
        }
        // Any argument that points at printable ASCII is a message; print a bounded slice of each.
        for (int i = 0; i < 4; ++i) {
            const auto* p = reinterpret_cast<const char*>(words[i]);
            if (!readable(p)) continue;
            char text[160] = {};
            std::size_t n = 0;
            for (; n < sizeof(text) - 1; ++n) {
                const char ch = p[n];
                if (!ch) break;
                if (ch < 0x20 || ch > 0x7e) break;
                text[n] = ch;
            }
            text[n] = '\0';
            if (n >= 4) std::fprintf(stderr, "[rt] assert arg%d: \"%s\"\n", i, text);
        }
    }
    info->ContextRecord->Rip = reinterpret_cast<DWORD64>(at) + 1;
    return EXCEPTION_CONTINUE_EXECUTION;
}

bool install(long count) {
    if (count <= 0) return false;
    g_remaining = count;
    // Last in the chain, after a debugger and the other handlers.
    return AddVectoredExceptionHandler(0, &handler) != nullptr;
}

}  // namespace rt::guest_asserts

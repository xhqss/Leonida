#include "guest_asserts.h"

#include <cstdio>

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

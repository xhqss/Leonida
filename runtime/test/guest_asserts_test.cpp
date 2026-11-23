// Tests for LEONIDA_SKIP_ASSERTS (src/guest_asserts.h). Synthetic only: the decision function on made-up inputs,
// then a real int3 round trip on executable memory from VirtualAlloc, which lies outside every module and so counts
// as guest code. No game data, no GPU.
#include <windows.h>

#include <atomic>
#include <cstdio>
#include <cstring>
#include <string>

#include "guest_asserts.h"

namespace {

int g_checks = 0;
int g_failed = 0;

void check(bool ok, const std::string& what) {
    ++g_checks;
    std::printf("%s %s\n", ok ? "ok   " : "FAIL ", what.c_str());
    if (!ok) ++g_failed;
}

using rt::guest_asserts::should_skip;

void decision_tests() {
    std::atomic<long> n{2};
    check(should_skip(n, false, 0xCC) && n == 1, "guest int3 is skipped and the counter decremented");
    check(!should_skip(n, true, 0xCC) && n == 1, "an address in a host module is not skipped and costs nothing");
    check(!should_skip(n, false, 0x90) && n == 1, "a byte other than 0xCC is not skipped and costs nothing");
    check(should_skip(n, false, 0xCC) && n == 0, "the last allowed skip uses the counter up");
    check(!should_skip(n, false, 0xCC), "an exhausted counter does not skip");
    check(!should_skip(n, false, 0xCC), "and stays exhausted");

    check(rt::guest_asserts::parse_count(nullptr) == 0, "default (variable unset) is off");
    check(rt::guest_asserts::parse_count("") == 0, "an empty value is off");
    check(rt::guest_asserts::parse_count("0") == 0, "0 is off");
    check(rt::guest_asserts::parse_count("-3") == 0, "a negative value is off");
    check(rt::guest_asserts::parse_count("x") == 0, "a non-number is off");
    check(rt::guest_asserts::parse_count("2") == 2, "2 allows two skips");
    check(!rt::guest_asserts::install(0), "a count of 0 installs no handler");
}

typedef int (*Fn)();

// Runs `code` as a function in executable memory under __try, so a not-skipped int3 reports instead of crashing.
// Returns 1 when it returned normally, 0 when the breakpoint reached the __except filter.
int call_guarded(Fn fn, int* result) {
    __try {
        *result = fn();
        return 1;
    } __except (GetExceptionCode() == EXCEPTION_BREAKPOINT ? EXCEPTION_EXECUTE_HANDLER : EXCEPTION_CONTINUE_SEARCH) {
        return 0;
    }
}

void round_trip_tests() {
    // mov eax, 42 ; int3 ; int3 ; int3 ; ret
    const unsigned char code[] = {0xB8, 42, 0, 0, 0, 0xCC, 0xCC, 0xCC, 0xC3};
    void* mem = VirtualAlloc(nullptr, 4096, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    check(mem != nullptr, "executable memory allocated");
    if (!mem) return;
    std::memcpy(mem, code, sizeof code);
    HMODULE host = nullptr;
    check(!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                              static_cast<LPCWSTR>(mem), &host),
          "the allocation belongs to no module (guest code)");

    int r = 0;
    check(call_guarded(reinterpret_cast<Fn>(mem), &r) == 0, "without the handler the int3 is not stepped over");

    check(rt::guest_asserts::install(3), "install(3) adds the handler");
    r = 0;
    check(call_guarded(reinterpret_cast<Fn>(mem), &r) == 1 && r == 42, "three guest int3 are stepped over, code resumes");
    r = 0;
    check(call_guarded(reinterpret_cast<Fn>(mem), &r) == 0, "the budget is spent: the next int3 is not skipped");
    VirtualFree(mem, 0, MEM_RELEASE);
}

}  // namespace

int main() {
    decision_tests();
    round_trip_tests();
    std::printf("%d checks, %d failed\n", g_checks, g_failed);
    return g_failed ? 1 : 0;
}

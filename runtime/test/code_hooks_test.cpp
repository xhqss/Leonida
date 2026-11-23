// code_hooks.h: a remapping thunk and direct-call redirection, on small x64 functions written into memory. No game
// files: the archetype lookup they stand in for takes its model hash in ecx (docs/modding.md).
#include "../src/code_hooks.h"
#include "../src/mods.h"

#include <cstdio>
#include <cstring>

namespace {

int g_checks = 0, g_failures = 0;

void check(bool ok, const char* what) {
    ++g_checks;
    if (!ok) {
        ++g_failures;
        std::printf("FAIL %s\n", what);
    }
}

uint32_t remap_lucia(uint32_t hash) {
    return hash == leonida::mods::joaat("player_story000_00") ? leonida::mods::joaat("cs_mandy") : hash;
}

void put(uint8_t* at, std::initializer_list<uint8_t> bytes) {
    for (uint8_t b : bytes) *at++ = b;
}

// `call rel32` (or `jmp` with e9) at `at` towards `to`.
void branch(uint8_t* at, uint8_t opcode, const uint8_t* to) {
    at[0] = opcode;
    const int32_t rel = static_cast<int32_t>(to - (at + 5));
    std::memcpy(at + 1, &rel, 4);
}

// sub rsp,0x28 / call to / add rsp,0x28 / ret
void caller(uint8_t* at, const uint8_t* to) {
    put(at, {0x48, 0x83, 0xEC, 0x28});
    branch(at + 4, 0xE8, to);
    put(at + 9, {0x48, 0x83, 0xC4, 0x28, 0xC3});
}

}  // namespace

int main() {
    using namespace leonida::hooks;
    uint8_t* mem = alloc_near(reinterpret_cast<const void*>(&remap_lucia), 4096);
    check(mem != nullptr, "executable memory near the module");
    if (!mem) return 1;
    std::memset(mem, 0xCC, 4096);

    uint8_t* target0 = mem;          // returns its first argument:  mov eax, ecx ; ret
    put(target0, {0x89, 0xC8, 0xC3});
    uint8_t* target1 = mem + 16;     // returns its second argument: mov eax, edx ; ret
    put(target1, {0x89, 0xD0, 0xC3});
    uint8_t* target2 = mem + 32;     // returns its third argument:  mov eax, r8d ; ret
    put(target2, {0x44, 0x89, 0xC0, 0xC3});

    uint8_t* call0 = mem + 64;
    caller(call0, target0);
    uint8_t* tail0 = mem + 96;       // jmp target0 (a tail call)
    branch(tail0, 0xE9, target0);
    uint8_t* decoy = mem + 112;      // a call to target0 + 1: not the hooked function, must stay as it is
    caller(decoy, target0 + 1);
    uint8_t* call1 = mem + 144;
    caller(call1, target1);
    uint8_t* call2 = mem + 176;
    caller(call2, target2);
    uint8_t decoy_before[16];
    std::memcpy(decoy_before, decoy, 16);

    uint8_t* thunk0 = make_remap_thunk(target0, 0, &remap_lucia, target0);
    uint8_t* thunk1 = make_remap_thunk(target1, 1, &remap_lucia, target1);
    uint8_t* thunk2 = make_remap_thunk(target2, 0, &remap_lucia, target2);
    check(thunk0 && thunk1 && thunk2, "thunks are allocated near their targets");
    if (!thunk0 || !thunk1 || !thunk2) return 1;

    const std::size_t n0 = redirect_direct_branches(mem + 64, 112, target0, thunk0);
    const std::size_t n1 = redirect_direct_branches(call1, 16, target1, thunk1);
    const std::size_t n2 = redirect_direct_branches(call2, 16, target2, thunk2);
    check(n0 == 2, "the call and the tail jump to the function are redirected, the decoy is not");
    check(n1 == 1 && n2 == 1, "one call each in the other callers");
    check(std::memcmp(decoy, decoy_before, 16) == 0, "a branch to another address is left byte for byte");
    FlushInstructionCache(GetCurrentProcess(), mem, 4096);

    using Fn1 = uint32_t (*)(uint32_t);
    using Fn2 = uint32_t (*)(uint64_t, uint32_t);
    using Fn3 = uint32_t (*)(uint32_t, uint32_t, uint32_t);
    const uint32_t lucia = 0xE472B687u, mandy = 0xD7C0CDFFu;
    check(reinterpret_cast<Fn1>(call0)(lucia) == mandy, "through the call, Lucia's model id becomes cs_mandy's");
    check(reinterpret_cast<Fn1>(call0)(1234) == 1234, "any other id passes through unchanged");
    check(reinterpret_cast<Fn1>(tail0)(lucia) == mandy, "a tail jump is remapped as well");
    check(reinterpret_cast<Fn2>(call1)(0x1111, lucia) == mandy, "argument 1 (edx) is remapped when asked");
    check(reinterpret_cast<Fn3>(call2)(lucia, 2, 777) == 777, "r8 reaches the original unchanged");

    // hook_image_calls refuses what is not a mapped image or not code
    check(!hook_image_calls(mem, 0, &remap_lucia, 0).ok, "memory without a PE header is refused");
    auto* self = reinterpret_cast<uint8_t*>(GetModuleHandleW(nullptr));
    check(!hook_image_calls(self, 0x7FFFFFF0u, &remap_lucia, 0).ok, "an RVA outside the image is refused");
    check(!hook_image_calls(self, 0, &remap_lucia, 0).ok, "an RVA in the headers (not code) is refused");
    check(!make_remap_thunk(target0, 2, &remap_lucia, target0), "only argument 0 or 1 can be remapped");

    // The result-override thunk (a view-mode getter): the original returns 2 ("third person, far") for every
    // context; the override answers 4 ("first person") for context 0 only and leaves the others as they were.
    uint8_t* getter = mem + 512;
    put(getter, {0xB8, 0x02, 0x00, 0x00, 0x00, 0xC3});   // mov eax, 2 ; ret
    uint8_t* call_getter = mem + 544;
    caller(call_getter, getter);
    static int original_calls = 0;
    auto first_person_on_foot = [](uint32_t result, uint32_t context) -> uint32_t {
        ++original_calls;
        return context == 0 ? 4u : result;
    };
    uint8_t* rthunk = make_result_thunk(getter, first_person_on_foot, getter);
    check(rthunk != nullptr, "a result thunk is allocated");
    if (rthunk) {
        check(redirect_direct_branches(call_getter, 16, getter, rthunk) == 1, "the getter's caller is redirected");
        FlushInstructionCache(GetCurrentProcess(), mem, 4096);
        using Ctx = uint32_t (*)(uint32_t);
        check(reinterpret_cast<Ctx>(call_getter)(0) == 4, "context 0 gets the overridden result");
        check(reinterpret_cast<Ctx>(call_getter)(3) == 2, "another context keeps the original's result");
        check(original_calls == 2, "the override sees every call, after the original ran");
    }
    check(!hook_image_results(mem, 0, first_person_on_foot).ok, "hook_image_results refuses memory without a PE");

    std::printf("code_hooks_test: %d checks, %d failures\n", g_checks, g_failures);
    return g_failures == 0 ? 0 : 1;
}

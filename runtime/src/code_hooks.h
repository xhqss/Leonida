// Hooks on a function of the loaded game image, by redirecting its direct callers (docs/modding.md, "Day one").
//
// Patching inside a function needs an instruction decoder to move its prologue. Redirecting the callers does not:
// every `call rel32` (E8) and `jmp rel32` (E9) in the image's executable sections whose target is the function is
// pointed at a thunk instead. The thunk saves the argument registers (rcx, rdx, r8, r9, xmm0-3), passes one 32-bit
// argument through a C++ function, restores the rest and jumps to the original. A call through a pointer (vtable,
// import slot) is not redirected, which suits RAGE's archetype lookup: a static function called directly.
//
// A byte pattern can look like E8/E9 + rel32 inside another instruction; it is only rewritten when its target is
// exactly the hooked function, so a false match must also point there. x64 Windows only.
#pragma once

#include <windows.h>

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <initializer_list>

namespace leonida::hooks {

using RemapFn = uint32_t (*)(uint32_t);

// Executable memory within +-2 GiB of `close_to`, so a rel32 branch from the image reaches it. nullptr if none is free.
inline uint8_t* alloc_near(const void* close_to, std::size_t size) {
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    const uintptr_t gran = si.dwAllocationGranularity;
    const uintptr_t center = reinterpret_cast<uintptr_t>(close_to) & ~(gran - 1);
    const uintptr_t span = 0x70000000;  // stay well inside the rel32 range from anywhere in a large image
    for (uintptr_t delta = gran; delta < span; delta += gran) {
        for (int side = 0; side < 2; ++side) {
            const uintptr_t at = side ? center + delta : center - delta;
            if (side == 0 && center < delta) continue;
            if (void* p = VirtualAlloc(reinterpret_cast<void*>(at), size, MEM_RESERVE | MEM_COMMIT,
                                       PAGE_EXECUTE_READWRITE))
                return static_cast<uint8_t*>(p);
        }
    }
    return nullptr;
}

// The thunk: argument `arg` (0 = rcx, 1 = rdx; the low 32 bits) goes through `fn`, then the original runs with every
// other argument register as the caller left it. Stack: the caller's call leaves rsp = 8 mod 16; four pushes and
// 0x68 bytes make it 16-aligned for the call to `fn`, with its 32-byte shadow space and room for xmm0-3.
inline uint8_t* make_remap_thunk(const void* close_to, int arg, RemapFn fn, const void* original) {
    if (arg != 0 && arg != 1) return nullptr;
    uint8_t* t = alloc_near(close_to, 4096);
    if (!t) return nullptr;
    uint8_t* p = t;
    auto put = [&](std::initializer_list<uint8_t> bytes) { for (uint8_t b : bytes) *p++ = b; };
    auto put64 = [&](uint64_t v) { std::memcpy(p, &v, 8); p += 8; };
    put({0x51, 0x52, 0x41, 0x50, 0x41, 0x51});        // push rcx, rdx, r8, r9
    put({0x48, 0x83, 0xEC, 0x68});                    // sub rsp, 0x68
    put({0xF3, 0x0F, 0x7F, 0x44, 0x24, 0x20});        // movdqu [rsp+0x20], xmm0
    put({0xF3, 0x0F, 0x7F, 0x4C, 0x24, 0x30});        // movdqu [rsp+0x30], xmm1
    put({0xF3, 0x0F, 0x7F, 0x54, 0x24, 0x40});        // movdqu [rsp+0x40], xmm2
    put({0xF3, 0x0F, 0x7F, 0x5C, 0x24, 0x50});        // movdqu [rsp+0x50], xmm3
    if (arg == 1) put({0x89, 0xD1});                   // mov ecx, edx
    put({0x48, 0xB8}); put64(reinterpret_cast<uint64_t>(fn));         // mov rax, fn
    put({0xFF, 0xD0});                                 // call rax
    put({0xF3, 0x0F, 0x6F, 0x44, 0x24, 0x20});        // movdqu xmm0, [rsp+0x20]
    put({0xF3, 0x0F, 0x6F, 0x4C, 0x24, 0x30});        // movdqu xmm1, [rsp+0x30]
    put({0xF3, 0x0F, 0x6F, 0x54, 0x24, 0x40});        // movdqu xmm2, [rsp+0x40]
    put({0xF3, 0x0F, 0x6F, 0x5C, 0x24, 0x50});        // movdqu xmm3, [rsp+0x50]
    put({0x48, 0x83, 0xC4, 0x68});                    // add rsp, 0x68
    put({0x41, 0x59, 0x41, 0x58, 0x5A, 0x59});        // pop r9, r8, rdx, rcx
    put({0x89, static_cast<uint8_t>(arg == 0 ? 0xC1 : 0xC2)});         // mov ecx|edx, eax (the remapped value)
    put({0x48, 0xB8}); put64(reinterpret_cast<uint64_t>(original));   // mov rax, original
    put({0xFF, 0xE0});                                 // jmp rax
    DWORD old = 0;
    VirtualProtect(t, 4096, PAGE_EXECUTE_READ, &old);
    FlushInstructionCache(GetCurrentProcess(), t, p - t);
    return t;
}

using ResultFn = uint32_t (*)(uint32_t result, uint32_t arg0);

// A thunk that runs the original and then passes (its 32-bit result, its first argument) through `fn`, returning
// what `fn` returns: for a getter such as "the view mode of context N". Only for functions with at most four
// register arguments (a stack argument would be one slot off) and an integer result.
// Stack: rsp = 8 mod 16 at entry; one push and 0x20 bytes of shadow space make it 16-aligned for both calls.
inline uint8_t* make_result_thunk(const void* close_to, ResultFn fn, const void* original) {
    uint8_t* t = alloc_near(close_to, 4096);
    if (!t) return nullptr;
    uint8_t* p = t;
    auto put = [&](std::initializer_list<uint8_t> bytes) { for (uint8_t b : bytes) *p++ = b; };
    auto put64 = [&](uint64_t v) { std::memcpy(p, &v, 8); p += 8; };
    put({0x51});                                       // push rcx (the first argument, for fn)
    put({0x48, 0x83, 0xEC, 0x20});                     // sub rsp, 0x20
    put({0x48, 0xB8}); put64(reinterpret_cast<uint64_t>(original));   // mov rax, original
    put({0xFF, 0xD0});                                 // call rax (rcx, rdx, r8, r9 still the caller's)
    put({0x48, 0x8B, 0x54, 0x24, 0x20});               // mov rdx, [rsp+0x20] (the saved first argument)
    put({0x89, 0xC1});                                 // mov ecx, eax (the original's result)
    put({0x48, 0xB8}); put64(reinterpret_cast<uint64_t>(fn));         // mov rax, fn
    put({0xFF, 0xD0});                                 // call rax
    put({0x48, 0x83, 0xC4, 0x20});                     // add rsp, 0x20
    put({0x59, 0xC3});                                 // pop rcx ; ret
    DWORD old = 0;
    VirtualProtect(t, 4096, PAGE_EXECUTE_READ, &old);
    FlushInstructionCache(GetCurrentProcess(), t, p - t);
    return t;
}

// Points every E8/E9 rel32 in [begin, begin + size) whose target is `target` at `thunk`. The range must be writable.
// Returns how many were rewritten.
inline std::size_t redirect_direct_branches(uint8_t* begin, std::size_t size, const uint8_t* target,
                                            const uint8_t* thunk) {
    std::size_t n = 0;
    for (std::size_t i = 0; i + 5 <= size; ++i) {
        if (begin[i] != 0xE8 && begin[i] != 0xE9) continue;
        int32_t rel;
        std::memcpy(&rel, begin + i + 1, 4);
        const uint8_t* next = begin + i + 5;
        if (next + rel != target) continue;
        const intptr_t to = thunk - next;
        if (to < INT32_MIN || to > INT32_MAX) continue;
        const int32_t rel2 = static_cast<int32_t>(to);
        std::memcpy(begin + i + 1, &rel2, 4);
        ++n;
        i += 4;
    }
    return n;
}

struct HookResult {
    bool ok = false;
    std::size_t redirected = 0;
    const char* error = "";
};

// Hooks the function at `target_rva` of the mapped image at `base`: one thunk, then every direct call or jump to it
// in the image's executable sections. Runs before any image code (main.cpp), so no thread is executing the bytes.
// Validates `target_rva` as code of the mapped image at `base`, builds the thunk with `make(target)` and points the
// image's direct calls and jumps to the target at it.
template <class MakeThunk>
inline HookResult redirect_image_calls(uint8_t* base, uint32_t target_rva, MakeThunk make) {
    HookResult r;
    const auto* dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return r.error = "not a mapped PE image", r;
    const auto* nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || target_rva >= nt->OptionalHeader.SizeOfImage)
        return r.error = "the RVA is outside the image", r;
    const IMAGE_SECTION_HEADER* sec = IMAGE_FIRST_SECTION(nt);
    bool in_code = false;
    for (unsigned i = 0; i < nt->FileHeader.NumberOfSections; ++i)
        if ((sec[i].Characteristics & IMAGE_SCN_MEM_EXECUTE) && target_rva >= sec[i].VirtualAddress &&
            target_rva < sec[i].VirtualAddress + sec[i].Misc.VirtualSize)
            in_code = true;
    if (!in_code) return r.error = "the RVA is not in an executable section", r;
    uint8_t* target = base + target_rva;
    uint8_t* thunk = make(target);
    if (!thunk) return r.error = "no memory within 2 GiB of the image for the thunk", r;
    for (unsigned i = 0; i < nt->FileHeader.NumberOfSections; ++i) {
        if (!(sec[i].Characteristics & IMAGE_SCN_MEM_EXECUTE)) continue;
        uint8_t* begin = base + sec[i].VirtualAddress;
        const std::size_t size = sec[i].Misc.VirtualSize;
        DWORD old = 0;
        if (!VirtualProtect(begin, size, PAGE_EXECUTE_READWRITE, &old)) return r.error = "VirtualProtect failed", r;
        r.redirected += redirect_direct_branches(begin, size, target, thunk);
        DWORD ignored = 0;
        VirtualProtect(begin, size, old, &ignored);
        FlushInstructionCache(GetCurrentProcess(), begin, size);
    }
    r.ok = true;
    return r;
}

// The argument-remapping hook (archetype lookup): argument `arg` of every direct call goes through `fn`.
inline HookResult hook_image_calls(uint8_t* base, uint32_t target_rva, RemapFn fn, int arg) {
    if (arg != 0 && arg != 1) return HookResult{false, 0, "only argument 0 or 1 can be remapped"};
    return redirect_image_calls(base, target_rva,
                                [&](uint8_t* target) { return make_remap_thunk(target, arg, fn, target); });
}

// The result-override hook (view-mode getter): every direct call's result goes through `fn` with the first argument.
inline HookResult hook_image_results(uint8_t* base, uint32_t target_rva, ResultFn fn) {
    return redirect_image_calls(base, target_rva,
                                [&](uint8_t* target) { return make_result_thunk(target, fn, target); });
}

}  // namespace leonida::hooks

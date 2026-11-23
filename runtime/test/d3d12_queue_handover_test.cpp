// Cross-queue Wait-before-Signal with a texture and a buffer handed over (docs/d3d12_vulkan.md section 4.4).
//
// d3d12_queue_wait_test covers Wait-before-Signal with nothing handed over. Here the COPY, COMPUTE and DIRECT queues
// pass a texture and a buffer around, and the consumer is submitted BEFORE the producer it waits for, in every order:
//   produce(queue a)  upload buffer -> texture + buffer      (CopyTextureRegion / CopyBufferRegion, COMMON -> COPY_DEST)
//   relay(queue b)    texture + buffer -> second texture + buffer
//   consume(queue c)  texture + buffer -> readback buffer
// linked by fences (a signals f1, b waits f1 and signals f2, c waits f2 and signals f3, the CPU waits f3), for every
// assignment of queue types to a, b and c (27, repeats on separate queues) and every one of the 6 orders the three
// submissions can reach the queues in. Then: a consumer waiting for a texture from one queue and a buffer from another
// (two waits), a chain released by a CPU Signal (the producer waits for a fence the CPU sets after everything is
// submitted), a CPU Signal before the Wait, and two layer-only cases that must not hang: a fence nobody signals (the
// other queues keep working, the held batch never runs, the queue is destroyed and the layer reports it), and a fence
// released while a batch waits for it.
//
// The same cases run on the Vulkan layer and on the host D3D12 runtime (the adapter is named in the output). Results are
// compared byte for byte with the data written by the CPU: a consumer replayed against the layout from before its
// producer ran reads a zero-filled texture and fails here (and with LEONIDA_VK_VALIDATION=1 under sync validation).
// No game data.
#include "../shims/d3d12_com.h"

#include <dxgi1_4.h>
#include <io.h>
#include <share.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <wrl/client.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <string>
#include <thread>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace {

constexpr UINT dim = 16;                            // 16 x 16 RGBA8
constexpr UINT pitch = D3D12_TEXTURE_DATA_PITCH_ALIGNMENT;
constexpr UINT64 buffer_bytes = 1024;
constexpr UINT64 texture_offset = 1024;             // in the upload and readback buffers; a multiple of 512
constexpr UINT64 staging_bytes = texture_offset + UINT64(dim) * pitch;

// The watchdog: a stuck case ends the process with a message instead of a ctest timeout with none.
std::atomic<ULONGLONG> deadline{0};
std::atomic<const char*> current_case{"start"};
void arm(const char* name, ULONGLONG ms = 20000) {
    current_case = name;
    deadline = GetTickCount64() + ms;
}

std::uint32_t pattern(std::uint32_t seed, std::uint32_t i) {
    std::uint32_t x = seed * 0x9e3779b1u + i * 0x85ebca6bu + 0x1234567u;
    x ^= x >> 15; x *= 0x2c1b3c6du; x ^= x >> 12; x *= 0x297a2d39u; x ^= x >> 15;
    return x | 0x01000000u;  // never zero, so a zero-filled or stale resource cannot match
}

enum Type { copy_q = 0, compute_q = 1, direct_q = 2 };
constexpr const char* type_name[] = {"COPY", "COMPUTE", "DIRECT"};
constexpr D3D12_COMMAND_LIST_TYPE list_type[] = {D3D12_COMMAND_LIST_TYPE_COPY, D3D12_COMMAND_LIST_TYPE_COMPUTE,
                                                 D3D12_COMMAND_LIST_TYPE_DIRECT};
constexpr int pool_size = 3;  // queues per type: distinct roles of one type need distinct queues

struct Rig {
    ID3D12Device* device = nullptr;
    ComPtr<ID3D12CommandQueue> queues[3][pool_size];
    int available[3] = {0, 0, 0};
};

D3D12_RESOURCE_DESC buffer_desc(UINT64 bytes) {
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = bytes;
    desc.Height = desc.DepthOrArraySize = desc.MipLevels = 1;
    desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    return desc;
}

D3D12_RESOURCE_DESC texture_desc() {
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = desc.Height = dim;
    desc.DepthOrArraySize = desc.MipLevels = 1;
    desc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.SampleDesc.Count = 1;
    return desc;
}

D3D12_RESOURCE_BARRIER transition(ID3D12Resource* resource, D3D12_RESOURCE_STATES before,
                                  D3D12_RESOURCE_STATES after) {
    D3D12_RESOURCE_BARRIER barrier{};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition = {resource, D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES, before, after};
    return barrier;
}

struct Set {
    ComPtr<ID3D12Resource> texture, buffer;
};

struct Case {
    Rig& rig;
    int seed;
    ComPtr<ID3D12Resource> upload, readback;
    Set stage[2];  // [0] written by the producer, [1] by the relay
    std::vector<ComPtr<ID3D12CommandAllocator>> allocators;
    std::vector<ComPtr<ID3D12GraphicsCommandList>> lists;
    std::vector<ComPtr<ID3D12Fence>> fences;
    bool ok = true;

    explicit Case(Rig& r, int s) : rig(r), seed(s) {}

    bool fail(const char* what, int line) {
        std::fprintf(stderr, "case \"%s\" (seed %d): line %d: %s\n", current_case.load(), seed, line, what);
        ok = false;
        return false;
    }
#define CHECK(c) do { if (!(c)) return fail(#c, __LINE__); } while (0)

    bool make_resources() {
        ID3D12Device* d = rig.device;
        D3D12_HEAP_PROPERTIES def{D3D12_HEAP_TYPE_DEFAULT}, up{D3D12_HEAP_TYPE_UPLOAD}, rb{D3D12_HEAP_TYPE_READBACK};
        const auto staging = buffer_desc(staging_bytes);
        const auto buf = buffer_desc(buffer_bytes);
        const auto tex = texture_desc();
        CHECK(SUCCEEDED(d->CreateCommittedResource(&up, D3D12_HEAP_FLAG_NONE, &staging,
                                                   D3D12_RESOURCE_STATE_GENERIC_READ, nullptr, IID_PPV_ARGS(&upload))));
        CHECK(SUCCEEDED(d->CreateCommittedResource(&rb, D3D12_HEAP_FLAG_NONE, &staging, D3D12_RESOURCE_STATE_COPY_DEST,
                                                   nullptr, IID_PPV_ARGS(&readback))));
        for (auto& s : stage) {
            CHECK(SUCCEEDED(d->CreateCommittedResource(&def, D3D12_HEAP_FLAG_NONE, &tex, D3D12_RESOURCE_STATE_COMMON,
                                                       nullptr, IID_PPV_ARGS(&s.texture))));
            CHECK(SUCCEEDED(d->CreateCommittedResource(&def, D3D12_HEAP_FLAG_NONE, &buf, D3D12_RESOURCE_STATE_COMMON,
                                                       nullptr, IID_PPV_ARGS(&s.buffer))));
        }
        std::uint8_t* mapped = nullptr;
        const D3D12_RANGE none{0, 0};
        CHECK(SUCCEEDED(upload->Map(0, &none, reinterpret_cast<void**>(&mapped))));
        for (UINT64 i = 0; i < buffer_bytes / 4; ++i)
            reinterpret_cast<std::uint32_t*>(mapped)[i] = pattern(seed, std::uint32_t(i));
        for (UINT y = 0; y < dim; ++y)
            for (UINT x = 0; x < dim; ++x)
                reinterpret_cast<std::uint32_t*>(mapped + texture_offset + y * pitch)[x] =
                    pattern(seed + 7919, y * dim + x);
        upload->Unmap(0, nullptr);
        return true;
    }

    ID3D12Fence* fence(int i) {
        while (int(fences.size()) <= i) {
            ComPtr<ID3D12Fence> f;
            if (FAILED(rig.device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&f)))) return nullptr;
            fences.push_back(f);
        }
        return fences[i].Get();
    }

    static D3D12_TEXTURE_COPY_LOCATION footprint(ID3D12Resource* resource) {
        D3D12_TEXTURE_COPY_LOCATION loc{};
        loc.pResource = resource;
        loc.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        loc.PlacedFootprint = {texture_offset, {DXGI_FORMAT_R8G8B8A8_UNORM, dim, dim, 1, pitch}};
        return loc;
    }
    static D3D12_TEXTURE_COPY_LOCATION subresource(ID3D12Resource* resource) {
        D3D12_TEXTURE_COPY_LOCATION loc{};
        loc.pResource = resource;
        loc.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        return loc;
    }

    // A closed list of `type`: moves `src` (null: the upload buffer) to `dst` (null: the readback buffer), the parts in
    // `mask` (1 texture, 2 buffer), with COMMON -> COPY_SOURCE / COPY_DEST before and COMMON again after.
    ID3D12GraphicsCommandList* make_list(Type type, const Set* src, const Set* dst, int mask) {
        ComPtr<ID3D12CommandAllocator> alloc;
        ComPtr<ID3D12GraphicsCommandList> list;
        if (FAILED(rig.device->CreateCommandAllocator(list_type[type], IID_PPV_ARGS(&alloc))) ||
            FAILED(rig.device->CreateCommandList(0, list_type[type], alloc.Get(), nullptr, IID_PPV_ARGS(&list))))
            return nullptr;
        std::vector<D3D12_RESOURCE_BARRIER> before, after;
        const auto track = [&](ID3D12Resource* r, D3D12_RESOURCE_STATES state) {
            before.push_back(transition(r, D3D12_RESOURCE_STATE_COMMON, state));
            after.push_back(transition(r, state, D3D12_RESOURCE_STATE_COMMON));
        };
        if (src) {
            if (mask & 1) track(src->texture.Get(), D3D12_RESOURCE_STATE_COPY_SOURCE);
            if (mask & 2) track(src->buffer.Get(), D3D12_RESOURCE_STATE_COPY_SOURCE);
        }
        if (dst) {
            if (mask & 1) track(dst->texture.Get(), D3D12_RESOURCE_STATE_COPY_DEST);
            if (mask & 2) track(dst->buffer.Get(), D3D12_RESOURCE_STATE_COPY_DEST);
        }
        if (!before.empty()) list->ResourceBarrier(UINT(before.size()), before.data());
        if (mask & 2)
            list->CopyBufferRegion(dst ? dst->buffer.Get() : readback.Get(), 0,
                                   src ? src->buffer.Get() : upload.Get(), 0, buffer_bytes);
        if (mask & 1) {
            const auto to = dst ? subresource(dst->texture.Get()) : footprint(readback.Get());
            const auto from = src ? subresource(src->texture.Get()) : footprint(upload.Get());
            list->CopyTextureRegion(&to, 0, 0, 0, &from, nullptr);
        }
        if (!after.empty()) list->ResourceBarrier(UINT(after.size()), after.data());
        if (FAILED(list->Close())) return nullptr;
        allocators.push_back(alloc);
        lists.push_back(list);
        return list.Get();
    }

    // One submission, in the order the call reaches the queue: Wait(s), the list, Signal.
    struct Job {
        Type type;
        int slot;  // which queue of the type
        const Set* src;
        const Set* dst;
        int mask;
        std::vector<std::pair<int, UINT64>> waits;  // (fence, value)
        int signal = -1;                            // fence signaled with 1; -1: none
    };

    bool submit(const Job& job) {
        ID3D12CommandQueue* queue = rig.queues[job.type][job.slot].Get();
        ID3D12GraphicsCommandList* list = make_list(job.type, job.src, job.dst, job.mask);
        CHECK(queue && list);
        for (const auto& [f, v] : job.waits) {
            ID3D12Fence* fe = fence(f);
            CHECK(fe && SUCCEEDED(queue->Wait(fe, v)));
        }
        ID3D12CommandList* batch[] = {list};
        queue->ExecuteCommandLists(1, batch);
        if (job.signal >= 0) {
            ID3D12Fence* fe = fence(job.signal);
            CHECK(fe && SUCCEEDED(queue->Signal(fe, 1)));
        }
        return true;
    }

    bool wait_fence(int f, UINT64 value) {
        HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        ID3D12Fence* fe = fence(f);
        CHECK(event && fe && SUCCEEDED(fe->SetEventOnCompletion(value, event)));
        if (WaitForSingleObject(event, 15000) != WAIT_OBJECT_0) {
            // A stuck queue can keep the destructors blocked: end the process without cleaning up.
            std::fprintf(stderr, "case \"%s\" (seed %d): fence %d never reached %llu (completed %llu)\n",
                         current_case.load(), seed, f, static_cast<unsigned long long>(value),
                         static_cast<unsigned long long>(fe->GetCompletedValue()));
            std::fflush(stderr);
            std::_Exit(1);
        }
        CloseHandle(event);
        return true;
    }

    // Compares the readback buffer with what the upload buffer held.
    bool verify() {
        std::uint8_t* mapped = nullptr;
        CHECK(SUCCEEDED(readback->Map(0, nullptr, reinterpret_cast<void**>(&mapped))));
        unsigned bad = 0;
        for (UINT64 i = 0; i < buffer_bytes / 4; ++i) {
            const auto got = reinterpret_cast<const std::uint32_t*>(mapped)[i];
            if (got != pattern(seed, std::uint32_t(i)) && bad++ < 3)
                std::fprintf(stderr, "buffer word %llu: got %08x want %08x\n", static_cast<unsigned long long>(i), got,
                             pattern(seed, std::uint32_t(i)));
        }
        for (UINT y = 0; y < dim; ++y)
            for (UINT x = 0; x < dim; ++x) {
                const auto got = reinterpret_cast<const std::uint32_t*>(mapped + texture_offset + y * pitch)[x];
                if (got != pattern(seed + 7919, y * dim + x) && bad++ < 3)
                    std::fprintf(stderr, "texel (%u,%u): got %08x want %08x\n", x, y, got,
                                 pattern(seed + 7919, y * dim + x));
            }
        readback->Unmap(0, nullptr);
        if (bad) std::fprintf(stderr, "%u words differ\n", bad);
        return bad == 0 ? true : fail("result differs from the data the CPU wrote", __LINE__);
    }
};

// A queue slot per role: the n-th role of a type uses the n-th queue of the type.
struct Roles {
    Type type[3];
    int slot[3];
    bool fits(const Rig& rig) const {
        for (int r = 0; r < 3; ++r)
            if (slot[r] >= rig.available[type[r]]) return false;
        return true;
    }
    static Roles make(Type a, Type b, Type c) {
        Roles roles{{a, b, c}, {0, 0, 0}};
        for (int r = 0; r < 3; ++r)
            for (int p = 0; p < r; ++p)
                if (roles.type[p] == roles.type[r]) ++roles.slot[r];
        return roles;
    }
};

int cases_run = 0;

// Chain a -> b -> c, with the three submissions in `order` (a permutation of 0 = produce, 1 = relay, 2 = consume).
bool chain(Rig& rig, const Roles& roles, const std::array<int, 3>& order, int seed) {
    std::string name = std::string("chain ") + type_name[roles.type[0]] + "->" + type_name[roles.type[1]] + "->" +
                       type_name[roles.type[2]] + " submitted " + char('A' + order[0]) + char('A' + order[1]) +
                       char('A' + order[2]);
    arm(name.c_str());
    Case c(rig, seed);
    if (!c.make_resources()) return false;
    const Case::Job jobs[3] = {
        {roles.type[0], roles.slot[0], nullptr, &c.stage[0], 3, {}, 0},
        {roles.type[1], roles.slot[1], &c.stage[0], &c.stage[1], 3, {{0, 1}}, 1},
        {roles.type[2], roles.slot[2], &c.stage[1], nullptr, 3, {{1, 1}}, 2},
    };
    for (int i : order)
        if (!c.submit(jobs[i])) return false;
    ++cases_run;
    return c.wait_fence(2, 1) && c.verify();
}

// The consumer waits for the texture from one queue and the buffer from another.
bool two_producers(Rig& rig, Type a, Type b, Type consumer, const std::array<int, 3>& order, int seed) {
    const int sa = 0, sb = a == b ? 1 : 0, sc = (consumer == a) + (consumer == b);
    if (sa >= rig.available[a] || sb >= rig.available[b] || sc >= rig.available[consumer]) return true;
    std::string name = std::string("two producers ") + type_name[a] + "(texture)+" + type_name[b] + "(buffer)->" +
                       type_name[consumer] + " submitted " + char('A' + order[0]) + char('A' + order[1]) +
                       char('A' + order[2]);
    arm(name.c_str());
    Case c(rig, seed);
    if (!c.make_resources()) return false;
    const Case::Job jobs[3] = {
        {a, sa, nullptr, &c.stage[0], 1, {}, 0},
        {b, sb, nullptr, &c.stage[0], 2, {}, 1},
        {consumer, sc, &c.stage[0], nullptr, 3, {{0, 1}, {1, 1}}, 2},
    };
    for (int i : order)
        if (!c.submit(jobs[i])) return false;
    ++cases_run;
    return c.wait_fence(2, 1) && c.verify();
}

// The whole chain is submitted consumer first and the producer waits for a fence the CPU signals last.
bool cpu_released_chain(Rig& rig, const Roles& roles, int seed) {
    std::string name = std::string("CPU Signal releases chain ") + type_name[roles.type[0]] + "->" +
                       type_name[roles.type[1]] + "->" + type_name[roles.type[2]];
    arm(name.c_str());
    Case c(rig, seed);
    if (!c.make_resources()) return false;
    const Case::Job jobs[3] = {
        {roles.type[0], roles.slot[0], nullptr, &c.stage[0], 3, {{3, 1}}, 0},
        {roles.type[1], roles.slot[1], &c.stage[0], &c.stage[1], 3, {{0, 1}}, 1},
        {roles.type[2], roles.slot[2], &c.stage[1], nullptr, 3, {{1, 1}}, 2},
    };
    if (!c.submit(jobs[2]) || !c.submit(jobs[1]) || !c.submit(jobs[0])) return false;
    Sleep(20);
    if (c.fence(2)->GetCompletedValue() != 0) return c.fail("the chain ran before the CPU signaled its gate", __LINE__);
    if (FAILED(c.fence(3)->Signal(1))) return c.fail("CPU Signal failed", __LINE__);
    ++cases_run;
    return c.wait_fence(2, 1) && c.verify();
}

// A CPU Signal that comes before the Wait (nothing is held), and one that comes after the batch was submitted.
bool cpu_gate(Rig& rig, Type producer, Type consumer, bool signal_first, int seed) {
    if (producer == consumer && rig.available[producer] < 2) return true;
    const int cslot = producer == consumer ? 1 : 0;
    std::string name = std::string("CPU Signal ") + (signal_first ? "before" : "after") + " the consumer's Wait, " +
                       type_name[producer] + "->" + type_name[consumer];
    arm(name.c_str());
    Case c(rig, seed);
    if (!c.make_resources()) return false;
    if (!c.submit({producer, 0, nullptr, &c.stage[0], 3, {}, 0}) || !c.wait_fence(0, 1)) return false;
    if (signal_first && FAILED(c.fence(1)->Signal(1))) return c.fail("CPU Signal failed", __LINE__);
    if (!c.submit({consumer, cslot, &c.stage[0], nullptr, 3, {{1, 1}}, 2})) return false;
    if (!signal_first) {
        Sleep(20);
        if (c.fence(2)->GetCompletedValue() != 0) return c.fail("the consumer ran before the CPU Signal", __LINE__);
        if (FAILED(c.fence(1)->Signal(1))) return c.fail("CPU Signal failed", __LINE__);
    }
    ++cases_run;
    return c.wait_fence(2, 1) && c.verify();
}

// Runs `body` with stderr redirected to a file and returns what it wrote.
std::string capture_stderr(const std::function<void()>& body) {
    char dir[MAX_PATH], path[MAX_PATH];
    GetTempPathA(MAX_PATH, dir);
    GetTempFileNameA(dir, "qho", 0, path);
    std::fflush(stderr);
    const int saved = _dup(2);
    int file = -1;
    _sopen_s(&file, path, _O_RDWR | _O_CREAT | _O_TRUNC | _O_BINARY, _SH_DENYNO, _S_IREAD | _S_IWRITE);
    _dup2(file, 2);
    body();
    std::fflush(stderr);
    _dup2(saved, 2);
    _close(saved);
    _close(file);
    std::string text;
    FILE* f = nullptr;
    if (fopen_s(&f, path, "rb") == 0 && f) {
        char buffer[512];
        std::size_t n;
        while ((n = std::fread(buffer, 1, sizeof(buffer), f)) > 0) text.append(buffer, n);
        std::fclose(f);
    }
    std::remove(path);
    return text;
}

// A batch waits for a fence nobody signals. The other queues keep working, the batch never runs, destroying the queue
// does not hang and the layer says why. Layer only: the host runtime has nothing to report.
bool never_signaled(Rig& rig, int seed) {
    arm("fence nobody signals");
    Case c(rig, seed);
    if (!c.make_resources()) return false;
    ComPtr<ID3D12CommandQueue> victim;
    const D3D12_COMMAND_QUEUE_DESC desc{D3D12_COMMAND_LIST_TYPE_DIRECT};
    if (FAILED(rig.device->CreateCommandQueue(&desc, IID_PPV_ARGS(&victim)))) {
        std::puts("never-signaled case: no spare queue, skipped");
        return true;
    }
    // The victim's batch would overwrite stage[1]; the working chain below uses stage[0] and the readback buffer.
    ID3D12Fence* gate = c.fence(5);
    ID3D12GraphicsCommandList* held = c.make_list(direct_q, nullptr, &c.stage[1], 3);
    if (!gate || !held) return c.fail("setup", __LINE__);
    ID3D12CommandList* batch[] = {held};
    if (FAILED(victim->Wait(gate, 5))) return c.fail("Wait", __LINE__);
    victim->ExecuteCommandLists(1, batch);
    if (FAILED(victim->Signal(c.fence(6), 1))) return c.fail("Signal", __LINE__);
    // Other queues are not held up behind it.
    const Case::Job work[2] = {{copy_q, 0, nullptr, &c.stage[0], 3, {}, 0},
                               {direct_q, 0, &c.stage[0], nullptr, 3, {{0, 1}}, 2}};
    if (!c.submit(work[1]) || !c.submit(work[0]) || !c.wait_fence(2, 1) || !c.verify()) return false;
    Sleep(100);
    if (c.fence(6)->GetCompletedValue() != 0) return c.fail("the held batch's Signal was reached", __LINE__);
    arm("destroy a queue with a held batch");
    const std::string log = capture_stderr([&] { victim.Reset(); });
    if (log.find("queue destroyed with") == std::string::npos || log.find("never signaled") == std::string::npos) {
        std::fprintf(stderr, "the layer did not report the held batch; stderr was: \"%s\"\n", log.c_str());
        return c.fail("no report for the held batch", __LINE__);
    }
    std::printf("never-signaled fence: held, others unaffected, destroyed without a hang; layer says: %s",
                log.c_str());
    ++cases_run;
    return true;
}

// The fence a batch waits for is released: the batch is dropped and reported, nothing hangs.
bool fence_released(Rig& rig, int seed) {
    arm("fence released while a batch waits");
    Case c(rig, seed);
    if (!c.make_resources()) return false;
    ComPtr<ID3D12CommandQueue> victim;
    const D3D12_COMMAND_QUEUE_DESC desc{D3D12_COMMAND_LIST_TYPE_DIRECT};
    if (FAILED(rig.device->CreateCommandQueue(&desc, IID_PPV_ARGS(&victim)))) return true;
    ComPtr<ID3D12Fence> doomed;
    if (FAILED(rig.device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&doomed)))) return false;
    ID3D12GraphicsCommandList* held = c.make_list(direct_q, nullptr, &c.stage[1], 3);
    if (!held) return c.fail("setup", __LINE__);
    ID3D12CommandList* batch[] = {held};
    if (FAILED(victim->Wait(doomed.Get(), 9))) return c.fail("Wait", __LINE__);
    victim->ExecuteCommandLists(1, batch);
    const std::string log = capture_stderr([&] { doomed.Reset(); victim.Reset(); });
    if (log.find("fence was released while a queue still held work") == std::string::npos) {
        std::fprintf(stderr, "stderr was: \"%s\"\n", log.c_str());
        return c.fail("no report for the dropped batch", __LINE__);
    }
    ++cases_run;
    return true;
}

bool run_all(ID3D12Device* device, bool layer) {
    Rig rig;
    rig.device = device;
    for (int t = 0; t < 3; ++t)
        for (int i = 0; i < pool_size; ++i) {
            D3D12_COMMAND_QUEUE_DESC desc{};
            desc.Type = list_type[t];
            if (FAILED(device->CreateCommandQueue(&desc, IID_PPV_ARGS(&rig.queues[t][i])))) break;
            ++rig.available[t];
        }
    std::printf("queues available per type (COPY/COMPUTE/DIRECT): %d/%d/%d\n", rig.available[0], rig.available[1],
                rig.available[2]);
    if (!rig.available[0] || !rig.available[1] || !rig.available[2]) return true;  // nothing to hand over between

    bool ok = true;
    int seed = 100, skipped = 0;
    std::array<int, 3> order{0, 1, 2};
    for (int a = 0; a < 3; ++a)
        for (int b = 0; b < 3; ++b)
            for (int cc = 0; cc < 3; ++cc) {
                const Roles roles = Roles::make(Type(a), Type(b), Type(cc));
                if (!roles.fits(rig)) { ++skipped; continue; }
                order = {0, 1, 2};
                do {
                    ok &= chain(rig, roles, order, ++seed);
                } while (std::next_permutation(order.begin(), order.end()));
                ok &= cpu_released_chain(rig, roles, ++seed);
            }
    for (int a = 0; a < 3; ++a)
        for (int b = 0; b < 3; ++b)
            for (int cc = 0; cc < 3; ++cc) {
                order = {0, 1, 2};
                do {
                    ok &= two_producers(rig, Type(a), Type(b), Type(cc), order, ++seed);
                } while (std::next_permutation(order.begin(), order.end()));
            }
    for (int p = 0; p < 3; ++p)
        for (int q = 0; q < 3; ++q)
            for (bool first : {true, false}) ok &= cpu_gate(rig, Type(p), Type(q), first, ++seed);
    if (layer) {
        // The layer hands out a limited number of VkQueues: give the spare ones back for the two cases below.
        for (auto& type_queues : rig.queues)
            for (int i = 1; i < pool_size; ++i) type_queues[i].Reset();
        ok &= never_signaled(rig, ++seed);
        ok &= fence_released(rig, ++seed);
    }
    if (skipped) std::printf("(%d type assignments need more queues than the device gave)\n", skipped);
    return ok;
}

ComPtr<ID3D12Device> native_device(std::wstring& name) {
    ComPtr<IDXGIFactory4> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) return {};
    for (UINT i = 0;; ++i) {
        ComPtr<IDXGIAdapter1> adapter;
        if (factory->EnumAdapters1(i, &adapter) != S_OK) return {};
        DXGI_ADAPTER_DESC1 desc{};
        if (FAILED(adapter->GetDesc1(&desc)) || (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)) continue;
        ComPtr<ID3D12Device> device;
        if (FAILED(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&device)))) continue;
        name = desc.Description;
        return device;
    }
}

}  // namespace

int main() {
    std::thread([] {
        for (;;) {
            Sleep(100);
            const ULONGLONG limit = deadline.load();
            if (limit && GetTickCount64() > limit) {
                std::fprintf(stderr, "HANG in case \"%s\"\n", current_case.load());
                std::fflush(stderr);
                std::_Exit(1);
            }
        }
    }).detach();

    ComPtr<ID3D12Device> layer;
    if (FAILED(rt::d3d12::create_vulkan_device_standalone(IID_PPV_ARGS(&layer)))) {
        std::puts("Vulkan standalone device unavailable");
        return 77;
    }
    arm("Vulkan layer", 300000);
    cases_run = 0;
    if (!run_all(layer.Get(), true)) return 1;
    const int layer_cases = cases_run;
    std::printf("Vulkan layer: %d cases passed, every one exact\n", layer_cases);

    std::wstring name;
    if (auto host = native_device(name)) {
        arm("host runtime", 300000);
        cases_run = 0;
        if (!run_all(host.Get(), false)) return 1;
        std::printf("Host D3D12 (%ls): %d cases passed, every one exact\n", name.c_str(), cases_run);
    } else {
        std::puts("no host D3D12 adapter: the host comparison was skipped");
    }
    deadline = 0;
    return 0;
}

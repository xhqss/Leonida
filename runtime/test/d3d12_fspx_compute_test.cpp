// Compute PSOs from the FidelityFX DLL's FSPX programs (docs/fspx.md, "Run-time lookup"), with synthetic inputs:
//   - device slot 62 (the Xbox shape) with a DXBC container whose ACCC part is an FSPX payload, what the DLL passes
//     when slot 67 fails;
//   - CreateComputePipelineState with a bare kind-6 program, what the slot-67 description carries at +0x28 and what
//     a proxy mapping of slot 67 forwards;
//   - both found through make_game.py's <shaders>/fspx/index.json by fspx_code_sha256, the SHA-256 of the kind-6
//     bytes; each PSO is dispatched once and its shader reads its code base, which must be the kind-6 program;
//   - a malformed directory, a program without its translation and a container without a usable ACCC part are
//     refused with E_INVALIDARG, as bytecode without a program is today.
// The shader (test/glsl/cs_user_data.comp) stands in for a translated one. No game data. Set LEONIDA_VK_VALIDATION=1
// to run under VK_LAYER_KHRONOS_validation.
#include "shader_fixture_path.h"
#include "test_temp_dir.h"
#include "vk_test_device.h"
#include "../shims/d3d12_com.h"
#include "../src/shader_library.h"
#include "../shims/d3d12_xs_device.h"

#include <array>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <vector>

#ifndef LEONIDA_TEST_CS_SPV
#error "LEONIDA_TEST_CS_SPV must name the compiled test shader"
#endif

#define EXPECT(condition) do { \
    if (!(condition)) { std::fprintf(stderr, "line %d: %s\n", __LINE__, #condition); return 1; } \
} while (0)

namespace {

using XboxHeapHandleFn = std::uint64_t(WINAPI*)(void* heap);  // heap slots 9/10: handle in RAX
using XboxComputePipelineFn = HRESULT(WINAPI*)(void* device, const D3D12_COMPUTE_PIPELINE_STATE_DESC*, std::uint64_t,
                                               std::uint64_t, const IID*, void**);
using SerializeFn = HRESULT(WINAPI*)(const D3D12_VERSIONED_ROOT_SIGNATURE_DESC*, ID3DBlob**, ID3DBlob**);

template<class T>
struct Ref {
    T* p = nullptr;
    ~Ref() { reset(); }
    void reset() {
        if (p) p->Release();
        p = nullptr;
    }
    T* operator->() const { return p; }
};

void* slot(void* object, unsigned index) { return (*reinterpret_cast<void***>(object))[index]; }

void put32(std::vector<std::uint8_t>& out, std::uint32_t v) {
    for (int i = 0; i < 4; ++i) out.push_back(std::uint8_t(v >> (8 * i)));
}

// A kind-6 program: the 36-byte prologue (three VOP3 words with opcode 0x180, bits 31..16 = 0xD580, and literals),
// a filler word and s_endpgm. Dword 1, which the test shader reads through its code base, is `marker`.
std::vector<std::uint8_t> kind6(std::uint32_t marker) {
    std::vector<std::uint8_t> out;
    for (std::uint32_t i = 0; i < 3; ++i) {
        put32(out, 0xd5800000u | i);
        put32(out, i == 0 ? marker : 0xffu);
        put32(out, 0x1000u + i);
    }
    put32(out, 0x7e000280u);
    put32(out, 0xbf810000u);
    return out;
}

// An FSPX payload with kinds 0, 1, 6 and 7 after a four-entry directory; `code_offset` and `code_size` override the
// kind-6 entry to make a malformed directory.
std::vector<std::uint8_t> payload(const std::vector<std::uint8_t>& code, std::uint32_t code_offset = 128,
                                  std::int64_t code_size = -1, std::uint8_t state_fill = 0x11) {
    std::vector<std::uint8_t> out = {'F', 'S', 'P', 'X'};
    const auto n = std::uint32_t(code.size());
    put32(out, 1);
    put32(out, 4);
    put32(out, 128 + n + 8);
    const std::uint32_t size6 = code_size < 0 ? n : std::uint32_t(code_size);
    const std::array<std::array<std::uint32_t, 3>, 4> directory = {
        {{0, 80, 28}, {1, 108, 20}, {6, code_offset, size6}, {7, 128 + n, 8}}};
    for (const auto& entry : directory) {
        put32(out, entry[0]);
        put32(out, entry[1]);
        put32(out, entry[2]);
        put32(out, 0);
    }
    out.insert(out.end(), 28, state_fill);  // kind 0
    out.insert(out.end(), 20, 0x22);
    out.insert(out.end(), code.begin(), code.end());
    out.insert(out.end(), 8, 0x77);
    return out;
}

// DXBC { HASH, ACCC = payload }, the parts that matter of the DLL's shader containers.
std::vector<std::uint8_t> container(const std::vector<std::uint8_t>& accc) {
    std::vector<std::uint8_t> out = {'D', 'X', 'B', 'C'};
    out.resize(28, 0);
    put32(out, 2);
    put32(out, 40);
    put32(out, 40 + 8 + 20);
    out.insert(out.end(), {'H', 'A', 'S', 'H'});
    put32(out, 20);
    out.insert(out.end(), 20, 0x5a);
    out.insert(out.end(), {'A', 'C', 'C', 'C'});
    put32(out, std::uint32_t(accc.size()));
    out.insert(out.end(), accc.begin(), accc.end());
    return out;
}

std::vector<std::uint8_t> serialize(const D3D12_VERSIONED_ROOT_SIGNATURE_DESC& desc) {
    const HMODULE d3d12 = LoadLibraryExA("d3d12.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    const auto fn = d3d12 ? reinterpret_cast<SerializeFn>(
                                reinterpret_cast<void*>(GetProcAddress(d3d12, "D3D12SerializeVersionedRootSignature")))
                          : nullptr;
    std::vector<std::uint8_t> out;
    ID3DBlob* blob = nullptr;
    if (fn && SUCCEEDED(fn(&desc, &blob, nullptr)) && blob) {
        const auto* p = static_cast<const std::uint8_t*>(blob->GetBufferPointer());
        out.assign(p, p + blob->GetBufferSize());
        blob->Release();
    }
    return out;
}

D3D12_RESOURCE_DESC buffer_desc(UINT64 bytes, D3D12_RESOURCE_FLAGS flags) {
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = bytes;
    desc.Height = 1;
    desc.DepthOrArraySize = 1;
    desc.MipLevels = 1;
    desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    desc.Flags = flags;
    return desc;
}

D3D12_RESOURCE_BARRIER transition(ID3D12Resource* resource) {
    D3D12_RESOURCE_BARRIER barrier{};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    barrier.Transition.pResource = resource;
    barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
    barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
    return barrier;
}

}  // namespace

int main() {
    vktest::Device d;
    const auto device_status = d.initialize();
    if (device_status == vktest::DeviceStatus::unavailable) {
        std::fprintf(stderr, "SKIP: no usable Vulkan 1.3 device\n");
        return 77;
    }
    EXPECT(device_status == vktest::DeviceStatus::ready);

    // Two programs, one translated entry each in <library>/fspx/index.json; the main index holds only an FSSM entry.
    constexpr std::uint32_t marker_a = 5000, marker_b = 7000;
    const auto code_a = kind6(marker_a), code_b = kind6(marker_b);
    const auto container_a = container(payload(code_a));
    const testfiles::ScratchDir scratch("leonida_fspx_compute_test");
    const auto& library = scratch.path();
    std::filesystem::create_directories(library / "fspx");
    EXPECT(testfiles::copy_file(vktest::shader_fixture_path(LEONIDA_TEST_CS_SPV),
                                library / "fspx" / "cs_user_data.spv"));
    EXPECT(testfiles::copy_file(vktest::shader_fixture_path(LEONIDA_TEST_CS_SPV),
                                library / "fspx" / "cs_user_data_b.spv"));
    // The review case on #1911: program C in two containers whose kind-0 entries differ, translated to two
    // different modules (the second has another generator word in its header, still a valid module).
    const auto code_c = kind6(9100);
    EXPECT(testfiles::copy_file(vktest::shader_fixture_path(LEONIDA_TEST_CS_SPV),
                                library / "fspx" / "cs_c_state1.spv"));
    {
        std::ifstream in(LEONIDA_TEST_CS_SPV, std::ios::binary);
        std::vector<char> words((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
        EXPECT(words.size() > 12);
        words[8] ^= 0x5a;  // word 2: the generator
        std::ofstream out(library / "fspx" / "cs_c_state2.spv", std::ios::binary);
        out.write(words.data(), std::streamsize(words.size()));
    }
    {
        std::ofstream index(library / "index.json", std::ios::binary);
        index << "{\"cs_game\": {\"stage\": \"cs\", \"fssm_sha256\": \"" << std::string(64, '0') << "\"}}";
        std::ofstream sub(library / "fspx" / "index.json", std::ios::binary);
        const auto entry = [](const std::vector<std::uint8_t>& code, std::uint8_t state_fill) {
            return "{\"stage\": \"cs\", \"fspx_code_sha256\": \"" + leonida::sha256_hex(code) +
                   "\", \"fspx_state_sha256\": \"" + leonida::sha256_hex(std::vector<std::uint8_t>(28, state_fill)) +
                   "\", \"bindings\": [], \"push_constant_bytes\": 144, \"local_size\": [64, 1, 1], "
                   "\"uses_code_base\": true, \"wave64\": false}";
        };
        sub << "{\"cs_user_data\": " << entry(code_a, 0x11) << ",\n \"cs_user_data_b\": " << entry(code_b, 0x11)
            << ",\n \"cs_c_state1\": " << entry(code_c, 0x31) << ",\n \"cs_c_state2\": " << entry(code_c, 0x32) << "}";
    }

    constexpr UINT elements = 64, bytes = elements * 4;
    const std::uint32_t table_values[4] = {1000, 2000, 3000, 4000};
    std::vector<std::uint32_t> first(elements), second(elements);
    {
        rt::d3d12::VulkanDeviceInfo info{};
        info.get_instance_proc = d.instance_proc;
        info.instance = d.instance;
        info.physical = d.physical;
        info.device = d.device;
        info.queue = d.queue;
        info.queue_family = d.family;
        info.buffer_device_address = true;
        info.shader_dir = library.string();
        ID3D12Device* inner = nullptr;
        EXPECT(SUCCEEDED(rt::d3d12::create_vulkan_device(info, IID_PPV_ARGS(&inner))));
        Ref<ID3D12Device> device;
        EXPECT(SUCCEEDED(rt::d3d12_xs::create_device_proxy(inner, __uuidof(ID3D12Device),
                                                           reinterpret_cast<void**>(&device.p))));
        rt::d3d12::set_device_outer(inner, device.p);
        if (rt::d3d12::device_push_constants_need_fallback(inner)) {
            std::printf("d3d12_fspx_compute_test: skipped, the PSOs need 144 push-constant bytes and this GPU has %u\n",
                        rt::d3d12::device_max_push_constants_size(inner));
            return 77;
        }

        // Root signature: [0] two constants (s0, s1), [1] a root UAV (s2:s3), [2] a one-CBV table (s4:s5).
        D3D12_DESCRIPTOR_RANGE range{D3D12_DESCRIPTOR_RANGE_TYPE_CBV, 1, 1, 0, 0};
        D3D12_ROOT_PARAMETER params[3] = {};
        params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
        params[0].Constants = {0, 0, 2};
        params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
        params[1].Descriptor = {0, 0};
        params[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        params[2].DescriptorTable = {1, &range};
        D3D12_VERSIONED_ROOT_SIGNATURE_DESC root_desc{};
        root_desc.Version = D3D_ROOT_SIGNATURE_VERSION_1_0;
        root_desc.Desc_1_0 = {3, params, 0, nullptr, D3D12_ROOT_SIGNATURE_FLAG_NONE};
        const auto root_blob = serialize(root_desc);
        EXPECT(!root_blob.empty());
        Ref<ID3D12RootSignature> root;
        EXPECT(SUCCEEDED(device->CreateRootSignature(0, root_blob.data(), root_blob.size(), IID_PPV_ARGS(&root.p))));

        // Program A through slot 62 in its container; program B bare, as the slot-67 description carries it.
        D3D12_COMPUTE_PIPELINE_STATE_DESC pso_desc{};
        pso_desc.pRootSignature = root.p;
        pso_desc.CS = {container_a.data(), container_a.size()};
        const IID pso_iid = __uuidof(ID3D12PipelineState);
        Ref<ID3D12PipelineState> pso_a, pso_b;
        EXPECT(SUCCEEDED(reinterpret_cast<XboxComputePipelineFn>(slot(device.p, 62))(
            device.p, &pso_desc, 0, 0, &pso_iid, reinterpret_cast<void**>(&pso_a.p))));
        pso_desc.CS = {code_b.data(), code_b.size()};
        EXPECT(SUCCEEDED(device->CreateComputePipelineState(&pso_desc, IID_PPV_ARGS(&pso_b.p))));
        {
            // Program A bare and program B in a container resolve too: the key is the kind-6 bytes either way.
            const auto container_b = container(payload(code_b));
            Ref<ID3D12PipelineState> bare_a, wrapped_b;
            pso_desc.CS = {code_a.data(), code_a.size()};
            EXPECT(SUCCEEDED(device->CreateComputePipelineState(&pso_desc, IID_PPV_ARGS(&bare_a.p))));
            pso_desc.CS = {container_b.data(), container_b.size()};
            EXPECT(SUCCEEDED(device->CreateComputePipelineState(&pso_desc, IID_PPV_ARGS(&wrapped_b.p))));
        }

        // Program C: each container finds its own translation by its kind-0 state; one with a kind 0 nothing was
        // translated for is refused; the bare program is refused as ambiguous, since its two translations differ.
        {
            const auto c1 = container(payload(code_c, 128, -1, 0x31)), c2 = container(payload(code_c, 128, -1, 0x32));
            const auto c_other = container(payload(code_c, 128, -1, 0x11));
            Ref<ID3D12PipelineState> pso_c1, pso_c2, none_other, none_bare;
            pso_desc.CS = {c1.data(), c1.size()};
            EXPECT(SUCCEEDED(reinterpret_cast<XboxComputePipelineFn>(slot(device.p, 62))(
                device.p, &pso_desc, 0, 0, &pso_iid, reinterpret_cast<void**>(&pso_c1.p))));
            pso_desc.CS = {c2.data(), c2.size()};
            EXPECT(SUCCEEDED(device->CreateComputePipelineState(&pso_desc, IID_PPV_ARGS(&pso_c2.p))));
            EXPECT(pso_c1.p != pso_c2.p);
            pso_desc.CS = {c_other.data(), c_other.size()};
            EXPECT(device->CreateComputePipelineState(&pso_desc, IID_PPV_ARGS(&none_other.p)) == E_INVALIDARG &&
                   !none_other.p);
            pso_desc.CS = {code_c.data(), code_c.size()};
            EXPECT(device->CreateComputePipelineState(&pso_desc, IID_PPV_ARGS(&none_bare.p)) == E_INVALIDARG &&
                   !none_bare.p);
        }

        // Refused with E_INVALIDARG, and no object: an untranslated program, bare and in a container; directories
        // whose kind-6 entry is out of bounds or missing; a container without ACCC; bytes without the prologue.
        {
            const auto untranslated = kind6(9000);
            auto no_prologue = code_a;
            no_prologue[2] ^= 0x01;
            const auto padded = [&] {  // one more byte than the program: a different key, not a translated one
                auto out = code_a;
                out.insert(out.end(), {0, 0, 0, 0});
                return out;
            }();
            std::vector<std::uint8_t> hash_only = container(payload(code_a));
            std::memcpy(hash_only.data() + 40 + 8 + 20, "ACCX", 4);
            const std::vector<std::vector<std::uint8_t>> refused = {
                untranslated,
                container(payload(untranslated)),
                container(payload(code_a, 140)),                   // kind 6 ends past the total
                container(payload(code_a, 16)),                    // kind 6 inside the directory
                container(payload(code_a, 0xffffff00u, 0x200)),    // offset + size wraps 32 bits
                container(payload(code_a, 128, 0xffffffffu)),      // size past the payload
                container(payload(code_a, 128, 0)),                // kind 6 empty: entry ignored, kind 6 missing
                hash_only,
                no_prologue,
                padded,
            };
            for (std::size_t i = 0; i < refused.size(); ++i) {
                Ref<ID3D12PipelineState> none;
                pso_desc.CS = {refused[i].data(), refused[i].size()};
                const HRESULT hr = device->CreateComputePipelineState(&pso_desc, IID_PPV_ARGS(&none.p));
                if (hr != E_INVALIDARG || none.p) std::fprintf(stderr, "refused input %zu: 0x%08x\n", i, unsigned(hr));
                EXPECT(hr == E_INVALIDARG && !none.p);
            }
            Ref<ID3D12PipelineState> none;
            pso_desc.CS = {refused[2].data(), refused[2].size()};
            EXPECT(reinterpret_cast<XboxComputePipelineFn>(slot(device.p, 62))(
                       device.p, &pso_desc, 0, 0, &pso_iid, reinterpret_cast<void**>(&none.p)) == E_INVALIDARG &&
                   !none.p);
        }

        // One dispatch per PSO; each shader adds dword 1 of its code base, which is its program's marker.
        const D3D12_HEAP_PROPERTIES default_heap{D3D12_HEAP_TYPE_DEFAULT};
        const D3D12_HEAP_PROPERTIES upload_heap{D3D12_HEAP_TYPE_UPLOAD};
        const D3D12_HEAP_PROPERTIES readback_heap{D3D12_HEAP_TYPE_READBACK};
        Ref<ID3D12Resource> out_a, out_b, constants, readback;
        const auto uav = buffer_desc(bytes, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
        for (auto* out : {&out_a, &out_b})
            EXPECT(SUCCEEDED(device->CreateCommittedResource(&default_heap, D3D12_HEAP_FLAG_NONE, &uav,
                                                             D3D12_RESOURCE_STATE_UNORDERED_ACCESS, nullptr,
                                                             IID_PPV_ARGS(&out->p))));
        const auto constants_desc = buffer_desc(256, D3D12_RESOURCE_FLAG_NONE);
        EXPECT(SUCCEEDED(device->CreateCommittedResource(&upload_heap, D3D12_HEAP_FLAG_NONE, &constants_desc,
                                                         D3D12_RESOURCE_STATE_GENERIC_READ, nullptr,
                                                         IID_PPV_ARGS(&constants.p))));
        const auto readback_desc = buffer_desc(2 * bytes, D3D12_RESOURCE_FLAG_NONE);
        EXPECT(SUCCEEDED(device->CreateCommittedResource(&readback_heap, D3D12_HEAP_FLAG_NONE, &readback_desc,
                                                         D3D12_RESOURCE_STATE_COPY_DEST, nullptr,
                                                         IID_PPV_ARGS(&readback.p))));
        void* mapped = nullptr;
        EXPECT(SUCCEEDED(constants->Map(0, nullptr, &mapped)));
        std::memcpy(mapped, table_values, sizeof(table_values));

        const D3D12_DESCRIPTOR_HEAP_DESC heap_desc{D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 4,
                                                   D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE, 0};
        Ref<ID3D12DescriptorHeap> heap;
        EXPECT(SUCCEEDED(device->CreateDescriptorHeap(&heap_desc, IID_PPV_ARGS(&heap.p))));
        const std::uint64_t cpu = reinterpret_cast<XboxHeapHandleFn>(slot(heap.p, 9))(heap.p);
        const std::uint64_t gpu = reinterpret_cast<XboxHeapHandleFn>(slot(heap.p, 10))(heap.p);
        EXPECT(cpu && gpu);
        const std::uint64_t target = constants->GetGPUVirtualAddress();
        std::memcpy(reinterpret_cast<void*>(cpu), &target, sizeof(target));

        D3D12_COMMAND_QUEUE_DESC queue_desc{};
        Ref<ID3D12CommandQueue> queue;
        Ref<ID3D12CommandAllocator> allocator;
        Ref<ID3D12GraphicsCommandList> list;
        Ref<ID3D12Fence> fence;
        EXPECT(SUCCEEDED(device->CreateCommandQueue(&queue_desc, IID_PPV_ARGS(&queue.p))));
        EXPECT(SUCCEEDED(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&allocator.p))));
        EXPECT(SUCCEEDED(device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, allocator.p, nullptr,
                                                   IID_PPV_ARGS(&list.p))));
        EXPECT(SUCCEEDED(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence.p))));
        ID3D12DescriptorHeap* heaps[] = {heap.p};
        list->SetDescriptorHeaps(1, heaps);
        list->SetComputeRootSignature(root.p);
        const std::uint32_t base_and_step[2] = {7, 3};
        list->SetComputeRoot32BitConstants(0, 2, base_and_step, 0);
        list->SetComputeRootDescriptorTable(2, D3D12_GPU_DESCRIPTOR_HANDLE{gpu});
        list->SetPipelineState(pso_a.p);
        list->SetComputeRootUnorderedAccessView(1, out_a->GetGPUVirtualAddress());
        list->Dispatch(1, 1, 1);
        list->SetPipelineState(pso_b.p);
        list->SetComputeRootUnorderedAccessView(1, out_b->GetGPUVirtualAddress());
        list->Dispatch(1, 1, 1);
        D3D12_RESOURCE_BARRIER barriers[2] = {transition(out_a.p), transition(out_b.p)};
        list->ResourceBarrier(2, barriers);
        list->CopyBufferRegion(readback.p, 0, out_a.p, 0, bytes);
        list->CopyBufferRegion(readback.p, bytes, out_b.p, 0, bytes);
        EXPECT(list->Close() == S_OK);
        ID3D12CommandList* lists[] = {list.p};
        queue->ExecuteCommandLists(1, lists);
        EXPECT(rt::d3d12::last_execute_result(queue.p) == leonida::ReplayResult::success);
        EXPECT(SUCCEEDED(queue->Signal(fence.p, 1)));
        EXPECT(SUCCEEDED(fence->SetEventOnCompletion(1, nullptr)));
        void* results = nullptr;
        EXPECT(SUCCEEDED(readback->Map(0, nullptr, &results)));
        std::memcpy(first.data(), results, bytes);
        std::memcpy(second.data(), static_cast<const std::uint8_t*>(results) + bytes, bytes);

        for (auto* r : {&out_a, &out_b, &constants, &readback}) r->reset();
        pso_a.reset();
        pso_b.reset();
        root.reset();
        heap.reset();
        list.reset();
        allocator.reset();
        queue.reset();
        fence.reset();
        EXPECT(rt::d3d12_xs::device_proxy_ref_count(device.p) == 1);
    }

    unsigned mismatches = 0;
    for (std::uint32_t i = 0; i < elements; ++i) {
        const std::uint32_t a = 7 + 3 * i + table_values[i % 4] + marker_a;
        const std::uint32_t b = 7 + 3 * i + table_values[i % 4] + marker_b;
        if ((first[i] != a || second[i] != b) && mismatches++ < 4)
            std::fprintf(stderr, "element %u: got %u and %u, want %u and %u\n", i, first[i], second[i], a, b);
    }
    EXPECT(!mismatches);
    if (d.validation_errors()) {
        std::fprintf(stderr, "%u validation errors\n", d.validation_errors());
        return 1;
    }
    std::printf("fspx compute: PSOs from an ACCC container (slot 62) and a bare kind-6 program found their translated "
                "modules by fspx_code_sha256; each dispatch read its kind-6 program as its code base; malformed and "
                "untranslated inputs refused\n");
    return 0;
}
